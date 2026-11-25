"""The runner's side of its Unix-socket protocol.

Each connection carries one request and one reply, each a JSON object on one line. A request is
at most 64 KiB and names an op and exactly that op's fields, each of the right type; anything
else is refused before the service sees it. The service's methods block, so each request runs
in a thread of its own. The socket's mode is 0660, so only the runner's user and group can use
it.
At most `MAX_CONNECTIONS` connections are served at once, every request alike, and `stop` and
`status` run in a small pool of threads of their own, so that once read they never wait for a
thread behind long preparations.

    {"op": "prepare", "run_id": "...", ...}
    {"ok": true, "result": ...}
    {"ok": false, "error": "...", "log_tail": "..."}
"""

import asyncio
import contextlib
import functools
import json
import logging
import os
import resource
import socket
import stat
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final

from footfall.runner.service import RunnerError, RunnerService

logger = logging.getLogger(__name__)

MAX_REQUEST: Final = 64 * 1024
MAX_CONNECTIONS = 64
_CONTROL_OPS: Final = frozenset({"stop", "status"})
_CONTROL_THREADS: Final = 4
_READ_TIMEOUT: Final = 30.0
_DISCARD_LIMIT: Final = 1024 * 1024
_MAX_ERROR: Final = 4000
_SOCKET_MODE: Final = 0o660


def _is_text(value: object) -> bool:
    return isinstance(value, str)


def _is_optional_text(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_text_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_text_map(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_TEXT = (_is_text, "must be a string")
_OPTIONAL_TEXT = (_is_optional_text, "must be a string or null")
_TEXT_LIST = (_is_text_list, "must be a list of strings")
_TEXT_MAP = (_is_text_map, "must map strings to strings")
_INTEGER = (_is_integer, "must be an integer")

_OPS: Final[Mapping[str, Mapping[str, tuple[Callable[[object], bool], str]]]] = {
    "prepare": {
        "run_id": _TEXT,
        "workspace": _TEXT,
        "runtime": _TEXT,
        "install": _OPTIONAL_TEXT,
        "seed": _OPTIONAL_TEXT,
        "egress": _TEXT_LIST,
        "env": _TEXT_MAP,
    },
    "start_app": {
        "run_id": _TEXT,
        "copy": _TEXT,
        "start": _TEXT,
        "port": _INTEGER,
        "egress": _TEXT_LIST,
    },
    "start_browsers": {"run_id": _TEXT, "assets": _TEXT_LIST},
    "logs": {"run_id": _TEXT, "copy": _TEXT},
    "stop": {"run_id": _TEXT},
    "status": {"run_id": _TEXT},
}


class _Refused(Exception):
    """A request that does not follow the protocol."""


def _show(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else f"{text[:37]}..."


def _parse(line: bytes) -> tuple[str, dict[str, Any]]:
    """Return a request's op and fields, or raise _Refused saying what is wrong with it."""
    try:
        request = json.loads(line)
    except (ValueError, RecursionError) as error:
        raise _Refused("request is not valid JSON") from error
    if not isinstance(request, dict):
        raise _Refused("request must be a JSON object")
    op = request.pop("op", None)
    if not isinstance(op, str) or op not in _OPS:
        raise _Refused(f"unknown op: {_show(op)}")
    fields = _OPS[op]
    for name in request:
        if name not in fields:
            raise _Refused(f"unexpected field: {name[:40]}")
    for name, (check, rule) in fields.items():
        if name not in request:
            raise _Refused(f"missing field: {name}")
        if not check(request[name]):
            raise _Refused(f"{name} {rule}")
    return op, request


def _call(service: RunnerService, op: str, fields: dict[str, Any]) -> object:
    """Run one op; blocking, so it runs in a thread."""
    run_id = fields["run_id"]
    match op:
        case "prepare":
            service.prepare(
                run_id,
                Path(fields["workspace"]),
                fields["runtime"],
                fields["install"],
                fields["seed"],
                fields["egress"],
                fields["env"],
            )
            return None
        case "start_app":
            return service.start_app(
                run_id, fields["copy"], fields["start"], fields["port"], fields["egress"]
            )
        case "start_browsers":
            return service.start_browsers(run_id, fields["assets"])
        case "logs":
            return service.logs(run_id, fields["copy"])
        case "stop":
            service.stop(run_id)
            return None
        case _:
            return service.status(run_id)


def _failure(error: str, log_tail: str = "") -> dict[str, Any]:
    return {"ok": False, "error": error[:_MAX_ERROR], "log_tail": log_tail}


async def _discard_line(reader: asyncio.StreamReader) -> None:
    """Read and drop the rest of an over-long request, so that the client can read the reply."""
    remaining = _DISCARD_LIMIT
    with contextlib.suppress(TimeoutError, OSError):
        async with asyncio.timeout(_READ_TIMEOUT):
            while remaining > 0:
                chunk = await reader.read(min(remaining, MAX_REQUEST))
                if not chunk or b"\n" in chunk:
                    return
                remaining -= len(chunk)


async def _reply(
    service: RunnerService, control: Executor, reader: asyncio.StreamReader
) -> dict[str, Any]:
    try:
        async with asyncio.timeout(_READ_TIMEOUT):
            line = await reader.readuntil(b"\n")
    except asyncio.LimitOverrunError:
        await _discard_line(reader)
        return _failure("request is larger than 64 KiB")
    except asyncio.IncompleteReadError:
        return _failure("request must end with a newline")
    except TimeoutError:
        return _failure("request did not arrive in time")
    try:
        op, fields = _parse(line)
    except _Refused as refused:
        return _failure(str(refused))
    try:
        if op in _CONTROL_OPS:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(control, _call, service, op, fields)
        else:
            result = await asyncio.to_thread(_call, service, op, fields)
    except RunnerError as error:
        return _failure(str(error), error.log_tail)
    except Exception:
        # Fields are not logged: a prepare request carries the run's throwaway password.
        logger.exception("the runner failed unexpectedly during %s", op)
        return _failure("the runner failed unexpectedly")
    return {"ok": True, "result": result}


async def _handle(
    service: RunnerService,
    control: Executor,
    gate: asyncio.Semaphore,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        async with gate:
            reply = await _reply(service, control, reader)
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
    except ConnectionError:
        pass  # the client went away; there is nobody to tell
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


def _bind(path: Path, group: int | None = None) -> socket.socket:
    """Bind a Unix socket at `path` with mode 0660, replacing a stale socket but nothing else.

    With `group`, the socket is given to that group before the group may use it. Otherwise
    its group is whatever the system gives a new file, which can be the folder's.
    """
    with contextlib.suppress(FileNotFoundError):
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise FileExistsError(f"{path} exists and is not a socket")
        path.unlink()  # left by a runner that did not shut down cleanly
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # The mask sets the socket's mode from the moment it exists, with no window in between:
        # 0600 until its group is set, when there is one to set.
        first = _SOCKET_MODE if group is None else _SOCKET_MODE & 0o700
        previous = os.umask(0o777 & ~first)
        try:
            sock.bind(str(path))
        finally:
            os.umask(previous)
        if group is not None:
            os.chown(path, -1, group, follow_symlinks=False)
        path.chmod(_SOCKET_MODE)
    except BaseException:
        sock.close()
        raise
    return sock


def _raise_file_limit() -> None:
    """Let the runner hold as many open files as it may, since walking a deep tree takes one
    for each level."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == hard:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    except (ValueError, OSError):
        logger.warning("could not raise the open-file limit from %s", soft)


async def serve(service: RunnerService, socket_path: Path, *, group: int | None = None) -> None:
    """Answer requests on `socket_path` until cancelled, and watch the runs meanwhile.

    `group`, when given, is the group the socket belongs to: the group of the orchestrator's
    user, which may then connect.
    """
    _raise_file_limit()
    sock = _bind(socket_path, group)
    inode = socket_path.lstat().st_ino
    stop = threading.Event()
    watcher = threading.Thread(
        target=service.watch, args=(stop,), name="footfall-runner-watch", daemon=True
    )
    control = ThreadPoolExecutor(_CONTROL_THREADS, thread_name_prefix="footfall-runner-control")
    gate = asyncio.Semaphore(MAX_CONNECTIONS)
    try:
        server = await asyncio.start_unix_server(
            functools.partial(_handle, service, control, gate), sock=sock, limit=MAX_REQUEST
        )
        watcher.start()
        async with server:
            await server.serve_forever()
    finally:
        stop.set()
        control.shutdown(wait=False, cancel_futures=True)
        sock.close()
        with contextlib.suppress(FileNotFoundError):
            if socket_path.lstat().st_ino == inode:
                socket_path.unlink()
