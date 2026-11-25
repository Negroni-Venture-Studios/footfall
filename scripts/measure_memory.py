"""Measure each container role's peak memory while a run goes.

The isolation workflow starts this in the background before the end-to-end run and stops it
with SIGTERM afterwards. Twice a second it lists the running containers and reads each one's
cgroup: `memory.current`, summed by role at that moment, and `memory.peak`, the kernel's own
high-water mark, which no sample can miss, kept for each container while it lives. Where it
cannot read a cgroup it samples `docker stats` instead. It then writes, for each role (proxy,
runner, orchestrator, and each run's install, seed, app and browsers containers), the most
one container used and the most the role used at one moment, sampled, and the kernel's peak
of one and the sum of every container's peak, an upper bound for the role at once. They go to
a JSON file and, as a table, to the job summary, from which a deployer sizes a droplet.

A run's own containers start and end within the run, so their kernel peaks are the run's. The
proxy, the runner and the orchestrator have been up since the stack started, so theirs cover all
they did since then, the tests before the run included: an upper bound for the run alone.

Run: python3 scripts/measure_memory.py --out memory.json [--summary FILE] [--interval 0.5]
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
# What the services' kernel peaks cover: they have been up since the stack started.
_WHOLE_LIFE: Final = "the stack's whole life"
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
    """The largest memory use seen for each role, one container and all together, sampled,
    and the kernel's own peak for each container."""

    def __init__(self) -> None:
        self.samples = 0
        self._one: dict[str, int] = {}
        self._all: dict[str, int] = {}
        self._seen: dict[str, set[str]] = {}
        self._total = 0
        self._kernel: dict[str, int] = {}  # container name -> memory.peak

    def note_peak(self, name: str, peak: int) -> None:
        """Keep a container's cgroup memory.peak, which only ever rises while it lives."""
        role = role_of(name)
        if role is None:
            return
        self._seen.setdefault(role, set()).add(name)
        self._kernel[name] = max(self._kernel.get(name, 0), peak)

    def _kernel_peaks(self, role: str) -> list[int]:
        return [peak for name, peak in self._kernel.items() if role_of(name) == role]

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
        roles: dict[str, Any] = {}
        for role in _ROLES:
            if role not in self._seen:
                continue
            kernel = self._kernel_peaks(role)
            roles[role] = {
                "containers": len(self._seen[role]),
                "peak_one_mib": _mib(self._one.get(role, 0)),
                "peak_all_mib": _mib(self._all.get(role, 0)),
                "kernel_peak_one_mib": _mib(max(kernel)) if kernel else None,
                "kernel_peak_sum_mib": _mib(sum(kernel)) if kernel else None,
                "kernel_peak_covers": _covers([role]) if kernel else None,
            }
        peaked = {found for name in self._kernel if (found := role_of(name)) is not None}
        return {
            "samples": self.samples,
            "roles": roles,
            "peak_total_mib": _mib(self._total),
            "kernel_peak_sum_mib": _mib(sum(self._kernel.values())),
            # The sum takes in every container's peak, the services' too when they were read.
            "kernel_peak_sum_covers": _covers(peaked) if peaked else None,
        }

    def markdown(self) -> str:
        report = self.to_json()
        if not report["roles"]:
            return "### Peak memory\n\nNo container was sampled.\n"

        def shown(value: float | None) -> str:
            return "not read" if value is None else str(value)

        rows = [
            "### Peak memory during the end-to-end run",
            "",
            f"From {report['samples']} samples, and each container's own cgroup peak.",
            "",
            "| Role | Containers | Sampled, one (MiB) | Sampled, all at once (MiB) "
            "| Kernel peak, one (MiB) | Kernel peaks, summed (MiB) |",
            "|---|---|---|---|---|---|",
        ]
        for role, peak in report["roles"].items():
            rows.append(
                f"| {role} | {peak['containers']} | {peak['peak_one_mib']} | "
                f"{peak['peak_all_mib']} | {shown(peak['kernel_peak_one_mib'])} | "
                f"{shown(peak['kernel_peak_sum_mib'])} |"
            )
        rows += [
            "",
            f"The whole stack, sampled at its peak: {report['peak_total_mib']} MiB. Every "
            f"container's kernel peak, summed, an upper bound: "
            f"{report['kernel_peak_sum_mib']} MiB.",
            "",
        ]
        if report["kernel_peak_sum_covers"] == _WHOLE_LIFE:
            rows += [
                "The proxy, runner and orchestrator have been up since the stack started, so "
                "their kernel peaks cover all they did since then, not only this run: an upper "
                "bound for it.",
                "",
            ]
        return "\n".join(rows)


def _covers(roles: Iterable[str]) -> str:
    """What the kernel peaks of these roles cover: the services have been up since the stack
    started, and a run's own containers only for the run."""
    return _WHOLE_LIFE if any(role in _SERVICES.values() for role in roles) else "the run"


CGROUP_ROOTS: Final = (Path("/sys/fs/cgroup/system.slice"), Path("/sys/fs/cgroup/docker"))
_DOCKER_TIMEOUT: Final = 20


def _read_number(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def cgroup_memory(
    container_id: str, roots: Sequence[Path] = CGROUP_ROOTS
) -> tuple[int, int | None] | None:
    """A container's memory now and its peak, from its cgroup as Docker's systemd or
    cgroupfs driver places it; None if neither is there."""
    for root in roots:
        for folder in (root / f"docker-{container_id}.scope", root / container_id):
            current = _read_number(folder / "memory.current")
            if current is not None:
                return current, _read_number(folder / "memory.peak")
    return None


def containers() -> list[tuple[str, str]]:
    """The id and name of every running container."""
    try:
        done = subprocess.run(
            ["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return []
    found = [line.split("\t", 1) for line in done.stdout.splitlines() if "\t" in line]
    return [(identity, name) for identity, name in found]


def sample() -> list[tuple[str, int]]:
    """One reading of every running container's memory."""
    try:
        done = subprocess.run(
            ["docker", "stats", "--no-stream", "--no-trunc", "--format", "{{json .}}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return []
    found = [parse_line(line) for line in done.stdout.splitlines()]
    return [item for item in found if item is not None]


def measure(peaks: Peaks) -> None:
    """One reading: each container's cgroup if it can be read, else `docker stats`."""
    now: list[tuple[str, int]] = []
    unread = False
    for identity, name in containers():
        memory = cgroup_memory(identity)
        if memory is None:
            unread = True
            continue
        current, peak = memory
        now.append((name, current))
        if peak is not None:
            peaks.note_peak(name, peak)
    peaks.add(sample() if unread or not now else now)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args(argv)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    peaks = Peaks()
    try:
        while not stop.is_set():
            measure(peaks)
            stop.wait(args.interval)
    finally:
        # Whatever stopped the loop, what was measured is kept.
        args.out.write_text(json.dumps(peaks.to_json(), indent=2) + "\n")
        if args.summary is not None:
            with args.summary.open("a") as summary:
                summary.write(peaks.markdown())
        print(peaks.markdown())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
