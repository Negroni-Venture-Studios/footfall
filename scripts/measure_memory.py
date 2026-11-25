"""Sample `docker stats` while a run goes, and report the peak memory of each container role.

The isolation workflow starts this in the background before the end-to-end run and stops it
with SIGTERM afterwards. It then writes, for each role (proxy, runner, orchestrator, and each
run's install, seed, app and browsers containers), the most any one container of that role
used, the most all of them used together at one moment, and the peak of the whole stack, to
a JSON file and, as a table, to the job summary. Task 18 sizes the droplet from them.

Run: python3 scripts/measure_memory.py --out memory.json [--summary FILE] [--interval 1]
"""

import argparse
import json
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Final

MIB: Final = 1024**2
_UNITS: Final = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
}
_SIZE: Final = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)")
_SERVICES: Final = {
    "footfall-proxy": "proxy",
    "footfall-runner": "runner",
    "footfall-orchestrator": "orchestrator",
}
_RUN_CONTAINER: Final = re.compile(
    r"footfall-r_[a-z2-7]{26}-(install|seed|app|browsers)-[a-z][a-z0-9-]{0,31}"
)
_ROLES: Final = ("proxy", "runner", "orchestrator", "install", "seed", "app", "browsers")


def parse_size(text: str) -> int:
    """Bytes from a size as `docker stats` prints one, such as "123.4MiB" or "2kB"."""
    found = _SIZE.fullmatch(text.strip())
    if found is None or found.group(2).upper() not in _UNITS:
        raise ValueError(f"not a size: {text!r}")
    return round(float(found.group(1)) * _UNITS[found.group(2).upper()])


def role_of(name: str) -> str | None:
    """The role of a container from its name, or None for one that is not Footfall's."""
    if name in _SERVICES:
        return _SERVICES[name]
    found = _RUN_CONTAINER.fullmatch(name)
    return found.group(1) if found else None


def parse_line(line: str) -> tuple[str, int] | None:
    """A container's name and memory in use from one line of `docker stats` JSON."""
    try:
        data = json.loads(line)
        name = str(data["Name"])
        used = parse_size(str(data["MemUsage"]).split("/")[0])
    except (ValueError, KeyError, TypeError):
        return None
    return name, used


def _mib(size: int) -> float:
    return round(size / MIB, 1)


class Peaks:
    """The largest memory use seen for each role, one container and all together."""

    def __init__(self) -> None:
        self.samples = 0
        self._one: dict[str, int] = {}
        self._all: dict[str, int] = {}
        self._seen: dict[str, set[str]] = {}
        self._total = 0

    def add(self, sample: Iterable[tuple[str, int]]) -> None:
        self.samples += 1
        sums: dict[str, int] = {}
        for name, used in sample:
            role = role_of(name)
            if role is None:
                continue
            self._seen.setdefault(role, set()).add(name)
            self._one[role] = max(self._one.get(role, 0), used)
            sums[role] = sums.get(role, 0) + used
        for role, used in sums.items():
            self._all[role] = max(self._all.get(role, 0), used)
        self._total = max(self._total, sum(sums.values()))

    def to_json(self) -> dict[str, Any]:
        roles = {
            role: {
                "containers": len(self._seen[role]),
                "peak_one_mib": _mib(self._one[role]),
                "peak_all_mib": _mib(self._all[role]),
            }
            for role in _ROLES
            if role in self._seen
        }
        return {"samples": self.samples, "roles": roles, "peak_total_mib": _mib(self._total)}

    def markdown(self) -> str:
        report = self.to_json()
        if not report["roles"]:
            return "### Peak memory\n\nNo container was sampled.\n"
        rows = [
            "### Peak memory during the end-to-end run",
            "",
            f"From {report['samples']} samples of `docker stats`.",
            "",
            "| Role | Containers | Peak of one (MiB) | Peak of all at once (MiB) |",
            "|---|---|---|---|",
        ]
        for role, peak in report["roles"].items():
            rows.append(
                f"| {role} | {peak['containers']} | {peak['peak_one_mib']} | "
                f"{peak['peak_all_mib']} |"
            )
        rows += ["", f"The whole stack at its peak: {report['peak_total_mib']} MiB.", ""]
        return "\n".join(rows)


def sample() -> list[tuple[str, int]]:
    """One reading of every running container's memory."""
    done = subprocess.run(
        ["docker", "stats", "--no-stream", "--no-trunc", "--format", "{{json .}}"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    found = [parse_line(line) for line in done.stdout.splitlines()]
    return [item for item in found if item is not None]


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    peaks = Peaks()
    while not stop.is_set():
        peaks.add(sample())
        stop.wait(args.interval)
    args.out.write_text(json.dumps(peaks.to_json(), indent=2) + "\n")
    if args.summary is not None:
        with args.summary.open("a") as summary:
            summary.write(peaks.markdown())
    print(peaks.markdown())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
