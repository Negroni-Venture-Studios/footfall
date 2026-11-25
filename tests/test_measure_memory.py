"""scripts/measure_memory.py: reading `docker stats` and keeping each role's peak."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "measure_memory", ROOT / "scripts" / "measure_memory.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


measure = _load()
RUN = "r_" + "a" * 26


@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("0B", 0),
        ("512B", 512),
        ("1.5KiB", 1536),
        ("123.4MiB", round(123.4 * 1024**2)),
        ("1.25GiB", round(1.25 * 1024**3)),
        ("2kB", 2000),
        ("3MB", 3_000_000),
        ("1GB", 1_000_000_000),
    ],
)
def test_sizes_are_read_as_docker_prints_them(text: str, size: int) -> None:
    assert measure.parse_size(text) == size


@pytest.mark.parametrize("text", ["", "MiB", "12 parsecs", "--", "1.2.3MiB"])
def test_a_size_it_cannot_read_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="size"):
        measure.parse_size(text)


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("footfall-proxy", "proxy"),
        ("footfall-runner", "runner"),
        ("footfall-orchestrator", "orchestrator"),
        (f"footfall-{RUN}-app-sven", "app"),
        (f"footfall-{RUN}-app-grace-2", "app"),
        (f"footfall-{RUN}-browsers-browsers", "browsers"),
        (f"footfall-{RUN}-seed-prepare", "seed"),
        (f"footfall-{RUN}-install-prepare", "install"),
        ("something-else", None),
        (f"footfall-{RUN}-wizard-x", None),
    ],
)
def test_each_container_is_known_by_its_role(name: str, role: str | None) -> None:
    assert measure.role_of(name) == role


def test_a_stats_line_gives_the_name_and_the_memory_in_use() -> None:
    line = json.dumps({"Name": "footfall-proxy", "MemUsage": "41.5MiB / 7.75GiB", "ID": "abc"})
    assert measure.parse_line(line) == ("footfall-proxy", round(41.5 * 1024**2))
    assert measure.parse_line(json.dumps({"Name": "x", "MemUsage": "--"})) is None
    assert measure.parse_line("not json") is None


def test_peaks_keep_the_largest_container_and_the_largest_total_of_each_role() -> None:
    peaks = measure.Peaks()
    mib = 1024**2
    peaks.add([(f"footfall-{RUN}-app-sven", 100 * mib), (f"footfall-{RUN}-app-grace", 50 * mib)])
    peaks.add([(f"footfall-{RUN}-app-sven", 80 * mib), (f"footfall-{RUN}-app-grace", 90 * mib)])
    peaks.add([("footfall-proxy", 40 * mib), ("unrelated", 999 * mib)])
    report = peaks.to_json()
    assert report["samples"] == 3
    assert report["roles"]["app"] == {
        "containers": 2,
        "peak_one_mib": 100.0,
        "peak_all_mib": 170.0,
        "kernel_peak_one_mib": None,
        "kernel_peak_sum_mib": None,
    }
    assert report["roles"]["proxy"]["peak_one_mib"] == 40.0
    assert "unrelated" not in json.dumps(report)
    assert report["peak_total_mib"] == 170.0
    table = peaks.markdown()
    assert "| app | 2 | 100.0 | 170.0 | not read | not read |" in table


def test_the_kernels_own_peak_outranks_any_sample() -> None:
    # A sample can fall between a container's highest moments, and a short-lived container
    # between two samples; the cgroup's memory.peak is the kernel's own high-water mark.
    peaks = measure.Peaks()
    mib = 1024**2
    sven, grace = f"footfall-{RUN}-app-sven", f"footfall-{RUN}-app-grace"
    peaks.add([(sven, 30 * mib), (grace, 20 * mib)])
    peaks.note_peak(sven, 120 * mib)
    peaks.note_peak(sven, 110 * mib)  # read later; the peak never falls
    peaks.note_peak(grace, 60 * mib)
    peaks.note_peak(f"footfall-{RUN}-seed-prepare", 45 * mib)  # never sampled
    peaks.note_peak("unrelated", 999 * mib)
    report = peaks.to_json()
    assert report["roles"]["app"]["kernel_peak_one_mib"] == 120.0
    assert report["roles"]["app"]["kernel_peak_sum_mib"] == 180.0
    assert report["roles"]["seed"]["containers"] == 1
    assert report["roles"]["seed"]["kernel_peak_one_mib"] == 45.0
    assert report["kernel_peak_sum_mib"] == 225.0
    assert "| seed | 1 | 0.0 | 0.0 | 45.0 | 45.0 |" in peaks.markdown()


def test_cgroup_files_are_read_where_docker_puts_them(tmp_path: Path) -> None:
    full = "a" * 64
    scope = tmp_path / "system.slice" / f"docker-{full}.scope"
    scope.mkdir(parents=True)
    (scope / "memory.peak").write_text("52428800\n")
    (scope / "memory.current").write_text("1048576\n")
    assert measure.cgroup_memory(full, roots=(tmp_path / "system.slice",)) == (1048576, 52428800)
    assert measure.cgroup_memory("b" * 64, roots=(tmp_path / "system.slice",)) is None
    plain = tmp_path / "docker" / full
    plain.mkdir(parents=True)
    (plain / "memory.current").write_text("2048\n")
    (plain / "memory.peak").write_text("max\n")  # unreadable as a number: no peak
    assert measure.cgroup_memory(full, roots=(tmp_path / "docker",)) == (2048, None)


def test_a_docker_command_that_hangs_gives_no_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(*args: object, **options: object) -> None:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    monkeypatch.setattr(measure.subprocess, "run", hang)
    assert measure.sample() == []
    assert measure.containers() == []


def test_no_samples_say_so() -> None:
    peaks = measure.Peaks()
    assert peaks.to_json() == {
        "samples": 0,
        "roles": {},
        "peak_total_mib": 0.0,
        "kernel_peak_sum_mib": 0.0,
    }
    assert "No container was sampled" in peaks.markdown()
