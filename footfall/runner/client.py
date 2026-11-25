"""The orchestrator's side of the runner's protocol: one request per connection, each awaited.

Every failure, whether the runner refused the request or could not be reached, is raised as
RunnerError, so a caller handles one kind of exception.
"""

import asyncio
import contextlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from footfall.actions import mask
from footfall.runner.service import RunnerError

# Replies carry at most a capped log tail, which JSON escaping can make several times longer.
_REPLY_LIMIT: Final = 1024 * 1024


def _decode(line: bytes) -> object:
    try:
        reply = json.loads(line)
    except (ValueError, RecursionError) as error:
        raise RunnerError("the runner sent a reply that is not valid JSON") from error
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        raise RunnerError("the runner sent a reply this client does not understand")
    if not reply["ok"]:
        tail = reply.get("log_tail")
        raise RunnerError(str(reply.get("error")), tail if isinstance(tail, str) else "")
    return reply.get("result")


def _text(result: object) -> str:
    if not isinstance(result, str):
        raise RunnerError("the runner sent a result that is not text")
    return result


class RunnerClient:
    """Asks the runner, over its Unix socket, to prepare, start, inspect and stop runs."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

    async def _call(self, op: str, **fields: Any) -> object:
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self._socket_path), limit=_REPLY_LIMIT
            )
        except OSError as error:
            raise RunnerError(f"cannot reach the runner at {self._socket_path}: {error}") from error
        try:
            writer.write(json.dumps({"op": op, **fields}).encode() + b"\n")
            await writer.drain()
            line = await reader.readuntil(b"\n")
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
            raise RunnerError("the runner closed the connection without a reply") from error
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        return _decode(line)

    async def prepare(
        self,
        run_id: str,
        workspace: Path,
        runtime: str,
        install: str | None,
        seed: str | None,
        egress: Sequence[str],
        env: Mapping[str, str],
    ) -> None:
        await self._call(
            "prepare",
            run_id=run_id,
            workspace=str(workspace),
            runtime=runtime,
            install=install,
            seed=seed,
            egress=list(egress),
            env=dict(env),
        )

    async def start_app(
        self, run_id: str, copy: str, start: str, port: int, egress: Sequence[str]
    ) -> str:
        result = await self._call(
            "start_app", run_id=run_id, copy=copy, start=start, port=port, egress=list(egress)
        )
        return _text(result)

    async def start_browsers(self, run_id: str, assets: Sequence[str]) -> str:
        """Start the run's browsers and return their endpoint, whose path is a secret.

        The runner makes the path in its own container, whose output no GitHub Actions job
        reads as it goes; the orchestrator's output is, so the path is masked here, before
        anything can print it.
        """
        endpoint = _text(await self._call("start_browsers", run_id=run_id, assets=list(assets)))
        path = urlsplit(endpoint).path.lstrip("/")
        if path:
            mask(path)
        return endpoint

    async def logs(self, run_id: str, copy: str) -> str:
        return _text(await self._call("logs", run_id=run_id, copy=copy))

    async def stop(self, run_id: str) -> None:
        await self._call("stop", run_id=run_id)

    async def status(self, run_id: str) -> str | None:
        result = await self._call("status", run_id=run_id)
        return None if result is None else _text(result)
