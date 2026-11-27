"""The manifest (`footfall.toml`) and the briefs that sit beside it.

Both come from repositories Footfall does not control, and app authors and their agents
write them, so reading is strict and fails closed. Every problem is collected and
reported at once in a `ManifestError`, so an author can fix a manifest in one go. Files
are read as UTF-8 with a size limit, and only as plain files: each is opened one name at a
time from an open folder, as a run copies files, never through a link and never from the
repository's git folder, so that neither can bring in a file the run would not copy.
"""

import errno
import os
import re
import stat
import tomllib
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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
    "is_dot_git",
    "load_brief",
    "load_manifest",
    "parse_manifest",
    "require_listed",
]

RUNTIMES = ("python3.13", "node26")

MANIFEST_FILE = "footfall.toml"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_BRIEF_BYTES = 256 * 1024
# The most characters a brief may hold, front matter apart. It goes into every persona's
# system prompt, so the tested repository may not fill that prompt with it.
MAX_BRIEF_CHARACTERS = 8_000
MAX_COMMAND_LENGTH = 2000
MAX_LABEL_LENGTH = 200
MAX_FOLDER_LENGTH = 200
MAX_NAME_LENGTH = 200
MIN_PORT, MAX_PORT = 1024, 65535
MAX_READY_TIMEOUT = 600
MAX_ACTIONS_CEILING = 120
MAX_LIST_LENGTH = 64

_BRIEF_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_PERSONA_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")  # becomes a copy name later
_BAD_PATH_CHARACTER = re.compile(r"[\s\\#]")
# Characters a path may not hold besides those: controls, surrogates, invisible format
# characters (such as U+200B, U+202E and U+FEFF), private-use and unassigned code points.
_NOT_IN_PATHS = frozenset({"Cc", "Cs", "Cf", "Co", "Cn"})
# Path segments a browser reads as "this folder" or "the one above", spelt out or encoded.
_DOT_SEGMENTS = frozenset({".", "..", "%2e", ".%2e", "%2e.", "%2e%2e"})
_LARGE = 10**12
_LEADING_BLANK_LINES = re.compile(r"\A(?:[ \t]*\r?\n)+")
_KEY_CAP = 64  # the most characters of a key a message shows, before any is escaped
# A key TOML can write without quotes, which shows nothing but itself.
_PLAIN_KEY = re.compile(rf"[A-Za-z0-9_-]{{1,{_KEY_CAP}}}")
# The code points HFS+ ignores in a name, which git's is_hfs_dotgit skips.
_HFS_IGNORED = frozenset(
    [*range(0x200C, 0x2010), *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF]
)
_FOLDER_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Never through a link, and never waiting on a FIFO, which could block or never end.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# The errors that mean a name is a link, or, where a folder should be, a file.
_NOT_FOLLOWED = frozenset({errno.ELOOP, errno.ENOTDIR, errno.EMLINK})
_LINK_PROBLEM = (
    "is a link, and Footfall reads a manifest or brief only as a plain file, never through a link"
)


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
    """How a value appears in a message: TOML's spelling for booleans, else its repr.

    An integer too long to read is named without its digits; past 4300 digits, which
    a hexadecimal or octal number in TOML can reach, Python cannot even print it, nor a
    list or table that holds one.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not -_LARGE < value < _LARGE:
        return "a very large number"
    try:
        return repr(value)
    except ValueError:
        return "a value holding a very large number"


def _show_key(key: str) -> str:
    """How a key appears in a message: as it is when TOML could write it without quotes, and
    otherwise cut short and shown with repr, which escapes every control, invisible and line
    breaking character. A quoted key can hold any character, and the file is the tested
    repository's, so a key shown raw could write to the terminal or forge a line."""
    if _PLAIN_KEY.fullmatch(key):
        return key
    if len(key) > _KEY_CAP:
        key = key[: _KEY_CAP - 1] + "…"
    return repr(key)


class _Reader:
    """Reads one table's fields, noting each problem against its dotted name."""

    def __init__(self, table: Mapping[str, Any], prefix: str, problems: list[str]) -> None:
        self.table = table
        self.prefix = prefix
        self.problems = problems
        self.seen: set[str] = set()

    def field(self, key: str) -> str:
        shown = _show_key(key)
        return f"{self.prefix}.{shown}" if self.prefix else shown

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
            self.problem(key, f"must be between {low} and {high}, not {_show(value)}")
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
        if len(value) > MAX_LIST_LENGTH:
            self.problem(key, f"must list at most {MAX_LIST_LENGTH} entries, not {len(value)}")
            return None
        return [(f"{self.field(key)}[{index}]", item) for index, item in enumerate(value)]

    def hosts(self, key: str) -> tuple[str, ...]:
        names: list[str] = []
        for field, item in self.text_list(key) or []:
            try:
                name = validate_host_name(item)
            except HostError:
                self.problem(key, f"{item!r} is not a valid host name")
                continue
            if name in names:
                self.problems.append(f"{field}: {item!r} is listed more than once")
            else:
                names.append(name)
        return tuple(names)

    def finish(self) -> None:
        for key in self.table:
            if key not in self.seen:
                self.problem(key, "unknown field")


def _path_problem(value: str) -> str | None:
    if not value.startswith("/") or value.startswith("//"):
        return f"must be a path on the app that starts with a single /, not {value!r}"
    if _BAD_PATH_CHARACTER.search(value) or any(
        unicodedata.category(ch) in _NOT_IN_PATHS for ch in value
    ):
        return (
            "must not contain a backslash, whitespace, a #, or a control or invisible "
            f"character, not {value!r}"
        )
    path = value.split("?", 1)[0]
    if any(segment.lower() in _DOT_SEGMENTS for segment in path.split("/")):
        return f"must not contain a . or .. segment, not {value!r}"
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
    if len(value) > MAX_FOLDER_LENGTH:
        return f"must be at most {MAX_FOLDER_LENGTH} characters, not {len(value)}"
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
                    f"{field}: must start with a lower-case letter and then use lower-case "
                    f"letters, digits, and hyphens, at most 32 characters in all, not {item!r}"
                )
        seen_ids: set[str] = set()
        for field, item in entries:
            if item in seen_ids:
                problems.append(f"{field}: {item!r} is listed more than once")
            seen_ids.add(item)
        personas = tuple(item for _, item in entries)
    reader.finish()
    return TestSection(
        pages=pages, briefs=briefs if briefs is not None else defaults.briefs, personas=personas
    )


def _parse_toml(text: str, subject: str) -> dict[str, Any]:
    """Parse TOML from an untrusted file, turning every parser failure into a problem.

    Beyond `TOMLDecodeError`, the parser raises `ValueError` for integers over Python's
    digit limit, whose message advises changing a Python setting, and `RecursionError`
    for very deep nesting.
    """
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ManifestError([f"{subject} not valid TOML ({error})"]) from error
    except ValueError as error:
        detail = "a number in it has too many digits"
        raise ManifestError([f"{subject} not valid TOML ({detail})"]) from error
    except RecursionError as error:
        raise ManifestError([f"{subject} not valid TOML (too deeply nested)"]) from error


def parse_manifest(text: str) -> Manifest:
    """Check a manifest's text, raising `ManifestError` with every problem found."""
    data = _parse_toml(text, f"{MANIFEST_FILE}:")

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


def _why(error: Exception) -> str:
    return error.strerror or "error" if isinstance(error, OSError) else str(error)


def is_dot_git(part: str) -> bool:
    """Whether a name is `.git` as a case-insensitive file system, or HFS+, would read it."""
    kept = "".join(ch for ch in part if ord(ch) not in _HFS_IGNORED)
    return kept.casefold() == ".git"


def _open_in(repo: Path, parts: Sequence[str], label: str, missing: str) -> int:
    """Open the file `parts` names below the folder `repo`, one name at a time from an open
    folder and never through a link, and return its descriptor.

    Raises ManifestError for a name that is missing, a link, or a file where a folder should
    be, or that cannot be opened. `missing` is the problem to report if the file does not
    exist.
    """
    try:
        current = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError as error:
        raise ManifestError([f"{label}: {missing}"]) from error
    except OSError as error:
        raise ManifestError([f"{label}: cannot be read ({_why(error)})"]) from error
    try:
        for index, part in enumerate(parts):
            leaf = index == len(parts) - 1
            try:
                inner = os.open(part, _FILE_FLAGS if leaf else _FOLDER_FLAGS, dir_fd=current)
            except FileNotFoundError as error:
                raise ManifestError([f"{label}: {missing}"]) from error
            except OSError as error:
                if error.errno not in _NOT_FOLLOWED:
                    raise ManifestError([f"{label}: cannot be read ({_why(error)})"]) from error
                if leaf:
                    raise ManifestError([f"{label}: {_LINK_PROBLEM}"]) from error
                where = "/".join(parts[: index + 1])
                raise ManifestError(
                    [
                        f"{label}: {where!r} is a link or a file, not a folder, and Footfall "
                        "reads nothing through a link"
                    ]
                ) from error
            os.close(current)
            current = inner
    except BaseException:
        os.close(current)
        raise
    return current


def _read_repo_file(repo: Path, name: PurePosixPath, label: str, limit: int, missing: str) -> str:
    """Read a UTF-8 file within a size limit: `name`, a plain file inside the repository,
    reached through no link and lying in no git folder, however `.git` is spelt.

    `missing` is the problem to report if the file does not exist.
    """
    if not name.parts or name.is_absolute() or ".." in name.parts:
        raise ManifestError([f"{label}: is outside the repository"])
    if any(is_dot_git(part) for part in name.parts):
        raise ManifestError([f"{label}: is in the repository's git folder"])
    handle = _open_in(repo, name.parts, label, missing)
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise ManifestError([f"{label}: is not a plain file"])
        data = b""
        while len(data) <= limit and (chunk := os.read(handle, limit + 1 - len(data))):
            data += chunk
    except OSError as error:
        raise ManifestError([f"{label}: cannot be read ({_why(error)})"]) from error
    finally:
        os.close(handle)
    if len(data) > limit:
        raise ManifestError([f"{label}: larger than the {limit} byte limit"])
    try:
        return data.decode("utf-8-sig")  # without a byte order mark, which editors may add
    except UnicodeDecodeError as error:
        raise ManifestError([f"{label}: is not valid UTF-8"]) from error


def load_manifest(repo: Path) -> Manifest:
    """Read `footfall.toml` from the root of a repository."""
    path = PurePosixPath(MANIFEST_FILE)
    missing = "not found at the root of the repository"
    return parse_manifest(_read_repo_file(repo, path, MANIFEST_FILE, MAX_MANIFEST_BYTES, missing))


def _split_front_matter(text: str, label: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "+++":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "+++":
            front = _parse_toml("".join(lines[1:index]), f"{label}: front matter is")
            return front, "".join(lines[index + 1 :])
    raise ManifestError([f"{label}: front matter is not closed by a +++ line"])


def _brief_path(manifest: Manifest, name: str) -> PurePosixPath:
    """Where the brief `name` lies in the repository, as git would list it."""
    return PurePosixPath(manifest.test.briefs) / f"{name}.md"


def load_brief(repo: Path, manifest: Manifest, name: str = "default") -> Brief:
    """Read one brief from the manifest's briefs folder."""
    if not _BRIEF_NAME.fullmatch(name):
        raise ManifestError(
            [
                f"brief name {name!r}: must be lower-case letters, digits, and hyphens, "
                "starting with a letter or digit, at most 64 characters"
            ]
        )
    label = f"{name}.md"
    folder = manifest.test.briefs
    path = _brief_path(manifest, name)
    raw = _read_repo_file(repo, path, label, MAX_BRIEF_BYTES, f"not found in {folder}")
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
    text = _LEADING_BLANK_LINES.sub("", body)
    if len(text) > MAX_BRIEF_CHARACTERS:
        problems.append(
            f"is {len(text):,} characters long, and a brief may be at most {MAX_BRIEF_CHARACTERS:,}"
        )
    if problems:
        raise ManifestError([f"{label}: {problem}" for problem in problems])

    return Brief(name=name, text=text, start=start, max_actions=max_actions)


def require_listed(manifest: Manifest, brief: Brief, listed: Collection[PurePosixPath]) -> None:
    """Refuse a manifest or brief that is not in `listed`, git's listing of the folder.

    `footfall test` copies only the files git tracks, and with --untracked the untracked
    files it does not ignore, unless it is told to copy every file. A manifest or brief
    outside that listing, such as an untracked file or one git ignores, is not one of the
    files the run copies, and is refused before its text is used.
    """
    wanted = {
        MANIFEST_FILE: PurePosixPath(MANIFEST_FILE),
        f"{brief.name}.md": _brief_path(manifest, brief.name),
    }
    problems = [
        f"{label}: is not among the files the run copies, which git tracks or, with "
        "--untracked, does not ignore"
        for label, path in wanted.items()
        if path not in listed
    ]
    if problems:
        raise ManifestError(problems)
