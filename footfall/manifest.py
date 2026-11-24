"""The manifest (`footfall.toml`) and the briefs that sit beside it.

Both come from repositories Footfall does not control, and app authors and their agents
write them, so reading is strict and fails closed. Every problem is collected and
reported at once in a `ManifestError`, so an author can fix a manifest in one go. Files
are read as UTF-8 with a size limit, and a brief is never read from outside the
repository, even through a symlink.
"""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from footfall.hosts import HostError, validate_host_name

__all__ = [
    "RUNTIMES",
    "AppSection",
    "Brief",
    "BrowserSection",
    "LoginSection",
    "Manifest",
    "ManifestError",
    "TestSection",
    "load_brief",
    "load_manifest",
    "parse_manifest",
]

RUNTIMES = ("python3.13", "node22")

MANIFEST_FILE = "footfall.toml"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_BRIEF_BYTES = 256 * 1024
MAX_COMMAND_LENGTH = 2000
MAX_LABEL_LENGTH = 200
MAX_NAME_LENGTH = 200
MIN_PORT, MAX_PORT = 1024, 65535
MAX_READY_TIMEOUT = 600
MAX_ACTIONS_CEILING = 120

_BRIEF_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_PERSONA_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_BAD_PATH_CHARACTER = re.compile(r"[\s\\]")
_LEADING_BLANK_LINES = re.compile(r"\A(?:[ \t]*\r?\n)+")


class ManifestError(ValueError):
    """A manifest or brief is not acceptable. `problems` lists every one found."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        lines = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(f"The manifest or brief has problems:\n{lines}")


@dataclass(frozen=True)
class AppSection:
    runtime: str
    start: str
    port: int
    ready: str = "/"
    ready_timeout: int = 60
    install: str | None = None
    seed: str | None = None
    egress: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrowserSection:
    assets: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoginSection:
    page: str
    username_label: str
    password_label: str
    submit: str


@dataclass(frozen=True)
class TestSection:
    __test__ = False  # not a pytest class, whatever its name suggests

    pages: tuple[str, ...] = ("/",)
    briefs: str = "footfall/briefs"
    personas: tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    name: str
    app: AppSection
    browser: BrowserSection
    login: LoginSection | None
    test: TestSection


@dataclass(frozen=True)
class Brief:
    name: str
    text: str
    start: Literal["signed-in", "signed-out"]
    max_actions: int | None


def _show(value: object) -> str:
    """How a value appears in a message: TOML's spelling for booleans, else its repr."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


class _Reader:
    """Reads one table's fields, noting each problem against its dotted name."""

    def __init__(self, table: Mapping[str, Any], prefix: str, problems: list[str]) -> None:
        self.table = table
        self.prefix = prefix
        self.problems = problems
        self.seen: set[str] = set()

    def field(self, key: str) -> str:
        return f"{self.prefix}.{key}" if self.prefix else key

    def problem(self, key: str, message: str) -> None:
        self.problems.append(f"{self.field(key)}: {message}")

    def _get(self, key: str, *, required: bool) -> Any | None:
        self.seen.add(key)
        if key in self.table:
            return self.table[key]
        if required:
            self.problem(key, "is required")
        return None

    def text(self, key: str, *, required: bool = False, limit: int | None = None) -> str | None:
        value = self._get(key, required=required)
        if value is None:
            return None
        if not isinstance(value, str):
            self.problem(key, f"must be text, not {_show(value)}")
            return None
        if not value.strip():
            self.problem(key, "must not be empty")
            return None
        if limit is not None and len(value) > limit:
            self.problem(key, f"must be at most {limit} characters, not {len(value)}")
            return None
        return value

    def integer(self, key: str, low: int, high: int, *, required: bool = False) -> int | None:
        value = self._get(key, required=required)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self.problem(key, f"must be a whole number, not {_show(value)}")
            return None
        if not low <= value <= high:
            self.problem(key, f"must be between {low} and {high}, not {value}")
            return None
        return value

    def path(self, key: str, *, required: bool = False) -> str | None:
        value = self.text(key, required=required)
        if value is None:
            return None
        message = _path_problem(value)
        if message:
            self.problem(key, message)
            return None
        return value

    def text_list(self, key: str) -> list[tuple[str, str]] | None:
        """Entries as (field name, value) pairs, or None if absent or not a list of text."""
        value = self._get(key, required=False)
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self.problem(key, "must be a list of text")
            return None
        return [(f"{self.field(key)}[{index}]", item) for index, item in enumerate(value)]

    def hosts(self, key: str) -> tuple[str, ...]:
        names: list[str] = []
        for _, item in self.text_list(key) or []:
            try:
                names.append(validate_host_name(item))
            except HostError:
                self.problem(key, f"{item!r} is not a valid host name")
        return tuple(names)

    def finish(self) -> None:
        for key in self.table:
            if key not in self.seen:
                self.problem(key, "unknown field")


def _path_problem(value: str) -> str | None:
    if not value.startswith("/") or value.startswith("//"):
        return f"must be a path on the app that starts with a single /, not {value!r}"
    if _BAD_PATH_CHARACTER.search(value):
        return f"must not contain a backslash or whitespace, not {value!r}"
    return None


def _table(
    data: Mapping[str, Any], key: str, problems: list[str], *, required: bool
) -> Mapping[str, Any] | None:
    if key not in data:
        if required:
            problems.append(f"{key}: is required")
        return None
    value = data[key]
    if not isinstance(value, dict):
        problems.append(f"{key}: must be a table")
        return None
    return value


def _app_section(table: Mapping[str, Any], problems: list[str]) -> AppSection | None:
    before = len(problems)
    reader = _Reader(table, "app", problems)
    runtime = reader.text("runtime", required=True)
    if runtime is not None and runtime not in RUNTIMES:
        reader.problem("runtime", f"must be one of {', '.join(RUNTIMES)}, not {runtime!r}")
    start = reader.text("start", required=True, limit=MAX_COMMAND_LENGTH)
    port = reader.integer("port", MIN_PORT, MAX_PORT, required=True)
    ready = reader.path("ready")
    ready_timeout = reader.integer("ready_timeout", 1, MAX_READY_TIMEOUT)
    install = reader.text("install", limit=MAX_COMMAND_LENGTH)
    seed = reader.text("seed", limit=MAX_COMMAND_LENGTH)
    egress = reader.hosts("egress")
    reader.finish()
    if len(problems) > before or runtime is None or start is None or port is None:
        return None
    return AppSection(
        runtime=runtime,
        start=start,
        port=port,
        ready=ready if ready is not None else "/",
        ready_timeout=ready_timeout if ready_timeout is not None else 60,
        install=install,
        seed=seed,
        egress=egress,
    )


def _browser_section(table: Mapping[str, Any], problems: list[str]) -> BrowserSection:
    reader = _Reader(table, "browser", problems)
    assets = reader.hosts("assets")
    reader.finish()
    return BrowserSection(assets=assets)


def _login_section(table: Mapping[str, Any], problems: list[str]) -> LoginSection | None:
    before = len(problems)
    reader = _Reader(table, "login", problems)
    page = reader.path("page", required=True)
    username = reader.text("username_label", required=True, limit=MAX_LABEL_LENGTH)
    password = reader.text("password_label", required=True, limit=MAX_LABEL_LENGTH)
    submit = reader.text("submit", required=True, limit=MAX_LABEL_LENGTH)
    reader.finish()
    if len(problems) > before or page is None or username is None:
        return None
    if password is None or submit is None:
        return None
    return LoginSection(page, username, password, submit)


def _briefs_folder_problem(value: str) -> str | None:
    parts = value.split("/")
    if value.startswith("/") or "\\" in value or "\0" in value or ".." in parts:
        return f"must be a folder inside the repository, not {value!r}"
    return None


def _test_section(table: Mapping[str, Any], problems: list[str]) -> TestSection:
    defaults = TestSection()
    reader = _Reader(table, "test", problems)
    pages = defaults.pages
    entries = reader.text_list("pages")
    if entries is not None:
        good: list[str] = []
        for field, item in entries:
            message = _path_problem(item)
            if message:
                problems.append(f"{field}: {message}")
            else:
                good.append(item)
        pages = tuple(good)
    briefs = reader.text("briefs")
    if briefs is not None:
        message = _briefs_folder_problem(briefs)
        if message:
            reader.problem("briefs", message)
            briefs = None
    personas: tuple[str, ...] = ()
    entries = reader.text_list("personas")
    if entries is not None:
        for field, item in entries:
            if not _PERSONA_NAME.fullmatch(item):
                problems.append(
                    f"{field}: must be lower-case letters, digits and hyphens, not {item!r}"
                )
        personas = tuple(item for _, item in entries)
    reader.finish()
    return TestSection(
        pages=pages, briefs=briefs if briefs is not None else defaults.briefs, personas=personas
    )


def parse_manifest(text: str) -> Manifest:
    """Check a manifest's text, raising `ManifestError` with every problem found."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ManifestError([f"{MANIFEST_FILE}: not valid TOML ({error})"]) from error

    problems: list[str] = []
    root = _Reader(data, "", problems)
    name = root.text("name", required=True, limit=MAX_NAME_LENGTH)
    app_table = _table(data, "app", problems, required=True)
    app = _app_section(app_table, problems) if app_table is not None else None
    browser_table = _table(data, "browser", problems, required=False)
    browser = _browser_section(browser_table, problems) if browser_table is not None else None
    login_table = _table(data, "login", problems, required=False)
    login = _login_section(login_table, problems) if login_table is not None else None
    test_table = _table(data, "test", problems, required=False)
    test = _test_section(test_table, problems) if test_table is not None else None
    root.seen.update(("app", "browser", "login", "test"))
    root.finish()

    if problems or name is None or app is None:
        raise ManifestError(problems)
    return Manifest(
        name=name,
        app=app,
        browser=browser or BrowserSection(),
        login=login,
        test=test or TestSection(),
    )


def _read_repo_file(repo: Path, path: Path, label: str, limit: int) -> str:
    """Read a UTF-8 file that must lie inside the repository, within a size limit."""
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ManifestError([f"{label}: not found"]) from error
    if not resolved.is_relative_to(repo.resolve()):
        raise ManifestError([f"{label}: is outside the repository"])
    try:
        with resolved.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as error:
        raise ManifestError([f"{label}: cannot be read ({error.strerror})"]) from error
    if len(data) > limit:
        raise ManifestError([f"{label}: larger than the {limit} byte limit"])
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManifestError([f"{label}: is not valid UTF-8"]) from error


def load_manifest(repo: Path) -> Manifest:
    """Read `footfall.toml` from the root of a repository."""
    path = repo / MANIFEST_FILE
    if not path.exists() and not path.is_symlink():
        raise ManifestError([f"{MANIFEST_FILE}: not found at the root of the repository"])
    return parse_manifest(_read_repo_file(repo, path, MANIFEST_FILE, MAX_MANIFEST_BYTES))


def _split_front_matter(text: str, label: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "+++":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "+++":
            try:
                front = tomllib.loads("".join(lines[1:index]))
            except tomllib.TOMLDecodeError as error:
                raise ManifestError(
                    [f"{label}: front matter is not valid TOML ({error})"]
                ) from error
            return front, "".join(lines[index + 1 :])
    raise ManifestError([f"{label}: front matter is not closed by a +++ line"])


def load_brief(repo: Path, manifest: Manifest, name: str = "default") -> Brief:
    """Read one brief from the manifest's briefs folder."""
    if not _BRIEF_NAME.fullmatch(name):
        raise ManifestError(
            [
                f"brief name {name!r}: must be lower-case letters, digits and hyphens, "
                "starting with a letter or digit, at most 64 characters"
            ]
        )
    label = f"{name}.md"
    folder = manifest.test.briefs
    path = repo / folder / label
    if not path.exists() and not path.is_symlink():
        raise ManifestError([f"{label}: not found in {folder}"])
    raw = _read_repo_file(repo, path, label, MAX_BRIEF_BYTES)
    front, body = _split_front_matter(raw, label)

    problems: list[str] = []
    reader = _Reader(front, "", problems)
    start: Literal["signed-in", "signed-out"] = (
        "signed-in" if manifest.login is not None else "signed-out"
    )
    raw_start = reader.text("start")
    if raw_start == "signed-out":
        start = "signed-out"
    elif raw_start == "signed-in":
        start = "signed-in"
        if manifest.login is None:
            reader.problem("start", f"'signed-in' needs a [login] section in {MANIFEST_FILE}")
    elif raw_start is not None:
        reader.problem("start", f"must be signed-in or signed-out, not {raw_start!r}")
    max_actions = reader.integer("max_actions", 1, MAX_ACTIONS_CEILING)
    reader.finish()
    if problems:
        raise ManifestError([f"{label}: {problem}" for problem in problems])

    return Brief(
        name=name,
        text=_LEADING_BLANK_LINES.sub("", body),
        start=start,
        max_actions=max_actions,
    )
