"""The egress proxy's control socket, through which the runner gives and takes allow lists, and
tells the proxy where to listen.

Each connection carries one request and one reply, each a JSON object on one line. A request
is at most 64 KiB and names an op and exactly that op's fields, each of the right type; anything
else is refused before the policy sees it.

Only the runner, which runs as root, may use the socket. Its mode is 0600, but the proxy and the
orchestrator run as the same user, so the mode alone does not keep the orchestrator out. The
proxy therefore asks the kernel which user is at the other end of each connection (SO_PEERCRED
on Linux, LOCAL_PEERCRED on macOS) and answers only root, refusing everyone else, and everyone
on a platform that cannot say, before it reads the request.

    {"op": "allow", "ip": "10.89.0.10", "run_id": "r_...", "hosts": ["api.example.com"]}
    {"op": "revoke", "ip": "10.89.0.10"}
    {"op": "revoke_run", "run_id": "r_..."}
    {"op": "reached", "run_id": "r_..."}
    {"op": "listen", "ip": "10.89.0.2", "run_id": "r_..."}
    {"ok": true, "result": ...}
    {"ok": false, "error": "..."}

`listen` names the proxy's own address on a run's network, once the runner has connected the
proxy to it, and the proxy accepts connections there on its port until `revoke_run` closes
every listener the run holds, after revoking its allow lists. `revoke` and `revoke_run` also
close every connection from the addresses they revoke, open tunnels included.

`ProxyControlClient` is the runner's side. It is synchronous, as the runner's `ProxyControl` is.
It raises OSError when the proxy cannot be reached or does not answer properly, and
`ProxyRefused`, a ValueError, when the proxy refuses a request.
"""

import asyncio
import contextlib
import functools
import json
import logging
import os
import socket
import stat
import struct
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from footfall.proxy.server import ProxyServer

__all__ = ["MAX_REQUEST", "ProxyControlClient", "ProxyRefused", "serve_control"]

logger = logging.getLogger(__name__)

MAX_REQUEST: Final = 64 * 1024
MAX_CONNECTIONS: Final = 16
_READ_TIMEOUT: Final = 10.0
_DISCARD_LIMIT: Final = 1024 * 1024
_REPLY_LIMIT: Final = 1024 * 1024
_MAX_ERROR: Final = 4000
_SOCKET_MODE: Final = 0o600
_ROOT: Final = 0
# What the kernel reports of a Unix socket's peer: struct ucred on Linux (pid, uid, gid), and
# struct xucred on macOS and the BSDs (a version, which is 0, the uid, then the groups).
_UCRED: Final = struct.Struct("iII")
_XUCRED_HEAD: Final = struct.Struct("=II")
_XUCRED_SIZE: Final = 76
_SOL_LOCAL: Final = 0


class ProxyRefused(ValueError):
    """The proxy refused a control request, as malformed or as naming something invalid."""


# ---------------------------------------------------------------- the server


def _is_text(value: object) -> bool:
    return isinstance(value, str)


def _is_text_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


_TEXT = (_is_text, "must be a string")
_TEXT_LIST = (_is_text_list, "must be a list of strings")

_OPS: Final[Mapping[str, Mapping[str, tuple[Callable[[object], bool], str]]]] = {
    "allow": {"ip": _TEXT, "run_id": _TEXT, "hosts": _TEXT_LIST},
    "revoke": {"ip": _TEXT},
    "revoke_run": {"run_id": _TEXT},
    "reached": {"run_id": _TEXT},
    "listen": {"ip": _TEXT, "run_id": _TEXT},
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


async def _call(proxy: ProxyServer, op: str, fields: dict[str, Any]) -> object:
    """Run one op against the proxy and its policy; ValueError means it was refused."""
    policy = proxy.policy
    match op:
        case "allow":
            policy.allow(fields["ip"], fields["run_id"], fields["hosts"])
            logger.info(
                "allowed %s for run %s: %d hosts",
                fields["ip"],
                fields["run_id"],
                len(set(fields["hosts"])),
            )
            return None
        case "revoke":
            policy.revoke(fields["ip"])
            dropped = proxy.drop(fields["ip"])
            logger.info("revoked %s and closed %d connections", fields["ip"], dropped)
            return None
        case "revoke_run":
            revoked = policy.revoke_run(fields["run_id"])
            closed = await proxy.close_run(fields["run_id"])
            dropped = sum(proxy.drop(ip) for ip in revoked)
            logger.info(
                "revoked every allow list of run %s and closed %d listeners and %d connections",
                fields["run_id"],
                closed,
                dropped,
            )
            return None
        case "listen":
            await proxy.listen(fields["ip"], fields["run_id"])
            logger.info("listening on %s for run %s", fields["ip"], fields["run_id"])
            return None
        case _:
            return policy.reached(fields["run_id"])


def _failure(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error[:_MAX_ERROR]}


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


async def _reply(proxy: ProxyServer, reader: asyncio.StreamReader) -> dict[str, Any]:
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
        result = await _call(proxy, op, fields)
    except _Refused as refused:
        return _failure(str(refused))
    except ValueError as error:
        return _failure(str(error))
    return {"ok": True, "result": result}


def _peer_uid(sock: socket.socket) -> int | None:
    """The user id of the process at the other end of a Unix socket, as the kernel reports it,
    or None if this platform, or the socket, cannot say."""
    try:
        peercred = getattr(socket, "SO_PEERCRED", None)
        if peercred is not None:
            _, uid, _ = _UCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, peercred, _UCRED.size))
            return int(uid)
        local_peercred = getattr(socket, "LOCAL_PEERCRED", None)
        if local_peercred is not None:
            raw = sock.getsockopt(_SOL_LOCAL, local_peercred, _XUCRED_SIZE)
            version, uid = _XUCRED_HEAD.unpack_from(raw)
            return int(uid) if version == 0 else None
    except (OSError, struct.error):
        return None
    return None


async def _handle(
    proxy: ProxyServer,
    allowed_uid: int,
    gate: asyncio.Semaphore,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        async with gate:
            uid = _peer_uid(writer.get_extra_info("socket"))
            if uid is None or uid != allowed_uid:
                logger.warning(
                    "refused a control connection from user %s",
                    "unknown" if uid is None else uid,
                )
                reply = _failure(f"this control socket answers only user {allowed_uid}")
            else:
                reply = await _reply(proxy, reader)
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
    except ConnectionError:
        pass  # the client went away; there is nobody to tell
    except Exception:
        logger.exception("the proxy's control socket failed unexpectedly")
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


def _bind(path: Path) -> socket.socket:
    """Bind a Unix socket at `path` with mode 0600, replacing a stale socket but nothing else."""
    with contextlib.suppress(FileNotFoundError):
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise FileExistsError(f"{path} exists and is not a socket")
        path.unlink()  # left by a proxy that did not shut down cleanly
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # The mask makes the socket 0600 from the moment it exists, with no window in between.
        previous = os.umask(0o777 & ~_SOCKET_MODE)
        try:
            sock.bind(str(path))
        finally:
            os.umask(previous)
        path.chmod(_SOCKET_MODE)
    except BaseException:
        sock.close()
        raise
    return sock


async def serve_control(proxy: ProxyServer, socket_path: Path, *, allowed_uid: int = _ROOT) -> None:
    """Answer control requests for `proxy` and its policy on `socket_path` until cancelled.

    Only a peer running as `allowed_uid` is answered: root, the runner, unless a test says
    otherwise.
    """
    sock = _bind(socket_path)
    inode = socket_path.lstat().st_ino
    gate = asyncio.Semaphore(MAX_CONNECTIONS)
    try:
        server = await asyncio.start_unix_server(
            functools.partial(_handle, proxy, allowed_uid, gate), sock=sock, limit=MAX_REQUEST
        )
        async with server:
            await server.serve_forever()
    finally:
        sock.close()
        with contextlib.suppress(FileNotFoundError):
            if socket_path.lstat().st_ino == inode:
                socket_path.unlink()


# ---------------------------------------------------------------- the client


def _decode(reply: bytes) -> object:
    try:
        message = json.loads(reply)
    except (ValueError, RecursionError) as error:
        raise ConnectionError("the proxy sent a reply that is not valid JSON") from error
    if not isinstance(message, dict) or not isinstance(message.get("ok"), bool):
        raise ConnectionError("the proxy sent a reply this client does not understand")
    if not message["ok"]:
        raise ProxyRefused(str(message.get("error")))
    return message.get("result")


class ProxyControlClient:
    """Asks the egress proxy, over its control socket, to allow and revoke client addresses,
    and where to listen.

    It implements the runner's `ProxyControl`. Each call opens one connection, and waits at
    most `timeout` seconds for each step of it.
    """

    def __init__(self, socket_path: Path, *, timeout: float = 10.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._socket_path = socket_path
        self._timeout = timeout

    def _call(self, op: str, **fields: Any) -> object:
        request = json.dumps({"op": op, **fields}).encode() + b"\n"
        reply = bytearray()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self._timeout)
            sock.connect(str(self._socket_path))
            sock.sendall(request)
            while not reply.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("the proxy closed the connection without a reply")
                reply += chunk
                if len(reply) > _REPLY_LIMIT:
                    raise ConnectionError("the proxy sent a reply that is too long")
        return _decode(bytes(reply))

    def allow(self, ip: str, run_id: str, hosts: Sequence[str]) -> None:
        self._call("allow", ip=ip, run_id=run_id, hosts=list(hosts))

    def revoke(self, ip: str) -> None:
        self._call("revoke", ip=ip)

    def revoke_run(self, run_id: str) -> None:
        self._call("revoke_run", run_id=run_id)

    def listen(self, ip: str, run_id: str) -> None:
        self._call("listen", ip=ip, run_id=run_id)

    def reached(self, run_id: str) -> list[str]:
        result = self._call("reached", run_id=run_id)
        if not isinstance(result, list) or not all(isinstance(host, str) for host in result):
            raise ConnectionError("the proxy sent a result that is not a list of host names")
        return result
