"""The manifest and briefs: strict validation with every problem reported at once."""

import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from footfall.manifest import (
    Brief,
    Manifest,
    ManifestError,
    load_brief,
    load_manifest,
    parse_manifest,
    require_listed,
)

FIXTURE = Path(__file__).parent / "fixtures" / "apps" / "minimal"

MINIMAL = """
name = "Tiny"
[app]
runtime = "python3.13"
start = "python3 serve.py 8080"
port = 8080
"""

WITH_LOGIN = (
    MINIMAL
    + """
[login]
page = "/login"
username_label = "Email"
password_label = "Password"
submit = "Sign in"
"""
)


def problems_of(text: str) -> list[str]:
    with pytest.raises(ManifestError) as caught:
        parse_manifest(text)
    return caught.value.problems


def test_minimal_manifest_defaults() -> None:
    manifest = load_manifest(FIXTURE)
    assert manifest.name == "Minimal"
    assert manifest.app.ready == "/"
    assert manifest.app.ready_timeout == 60
    assert manifest.app.egress == ()
    assert manifest.app.install is None
    assert manifest.browser.assets == ()
    assert manifest.login is None
    assert manifest.test.pages == ("/",)
    assert manifest.test.briefs == "footfall/briefs"


def test_full_manifest_is_read_and_hosts_are_normalised() -> None:
    manifest = parse_manifest(
        WITH_LOGIN
        + """
[browser]
assets = ["Tile.OpenStreetMap.org"]
[test]
pages = ["/", "/settings"]
personas = ["sven", "grace"]
"""
    )
    assert manifest.browser.assets == ("tile.openstreetmap.org",)
    assert manifest.login is not None
    assert manifest.login.submit == "Sign in"
    assert manifest.test.personas == ("sven", "grace")


def test_every_problem_is_reported_at_once() -> None:
    text = MINIMAL.replace("python3.13", "ruby3").replace("port = 8080", "port = 80")
    text += 'egress = ["10.0.0.1"]\n'
    assert problems_of(text) == [
        "app.runtime: must be one of python3.13, node22, not 'ruby3'",
        "app.port: must be between 1024 and 65535, not 80",
        "app.egress: '10.0.0.1' is not a valid host name",
    ]


def test_bad_browser_asset_names_field_and_value() -> None:
    assert problems_of(MINIMAL + '[browser]\nassets = ["localhost"]\n') == [
        "browser.assets: 'localhost' is not a valid host name"
    ]


@pytest.mark.parametrize(
    ("port", "ok"), [(1023, False), (1024, True), (65535, True), (65536, False)]
)
def test_port_range(port: int, ok: bool) -> None:
    text = MINIMAL.replace("port = 8080", f"port = {port}")
    if ok:
        assert parse_manifest(text).app.port == port
    else:
        assert problems_of(text) == [f"app.port: must be between 1024 and 65535, not {port}"]


def test_port_must_be_an_integer() -> None:
    assert problems_of(MINIMAL.replace("port = 8080", "port = true")) == [
        "app.port: must be a whole number, not true"
    ]


@pytest.mark.parametrize(
    "path",
    [
        "login",
        "//evil.example/x",
        "/a\\b",
        "/a b",
        "/a\tb",
        "/a\nb",
        "",
        "http://x/",
        "/a\0b",
        "/a\x7fb",
        "/a\x85b",
        "/a\u200bb",
        "/a\u202eb",
        "/a\ufeffb",
        "/a#b",
        "/#",
        "/..",
        "/a/../b",
        "/a/./b",
        "/a/%2e%2E/b",
        "/a/.%2e",
        "/a/%2E",
        "/../?x=1",
    ],
)
def test_paths_must_be_app_paths(path: str) -> None:
    escaped = "".join(
        f"\\u{ord(ch):04x}" if ch == "\\" or not ch.isprintable() else ch for ch in path
    )
    ready = MINIMAL.replace("port = 8080", f'port = 8080\nready = "{escaped}"')
    assert len(problems_of(ready)) == 1
    assert problems_of(ready)[0].startswith("app.ready: ")
    login = WITH_LOGIN.replace('page = "/login"', f'page = "{escaped}"')
    assert problems_of(login)[0].startswith("login.page: ")
    pages = MINIMAL + f'[test]\npages = ["/", "{escaped}"]\n'
    assert problems_of(pages)[0].startswith("test.pages[1]: ")


def test_good_paths_are_accepted() -> None:
    text = MINIMAL.replace("port = 8080", 'port = 8080\nready = "/healthz?x=1"')
    assert parse_manifest(text).app.ready == "/healthz?x=1"


def test_unknown_keys_are_refused() -> None:
    assert problems_of(MINIMAL.replace("port = 8080", 'port = 8080\nstrat = "x"')) == [
        "app.strat: unknown field"
    ]
    assert problems_of("colour = 1\n" + MINIMAL) == ["colour: unknown field"]
    assert problems_of(MINIMAL + "[extra]\nx = 1\n") == ["extra: unknown field"]
    assert problems_of(WITH_LOGIN + '[test]\nbriefs = "b"\nbriefz = "x"\n') == [
        "test.briefz: unknown field"
    ]


FORGED_KEY = "\x1b[2J\x1b]0;title\x07key-marker\nfootfall test: forged"
# The same key in TOML, where a quoted key spells a control character with an escape.
FORGED_TOML = '"\\U0000001b[2J\\U0000001b]0;title\\U00000007key-marker\\nfootfall test: forged"'


def test_an_unknown_key_with_control_characters_is_shown_escaped() -> None:
    shown = repr(FORGED_KEY)
    assert "\x1b" not in shown
    assert "\n" not in shown
    assert problems_of(f"{FORGED_TOML} = 1\n" + MINIMAL) == [f"{shown}: unknown field"]
    in_app = MINIMAL.replace("port = 8080", f"port = 8080\n{FORGED_TOML} = 1")
    assert problems_of(in_app) == [f"app.{shown}: unknown field"]
    assert problems_of(MINIMAL + f"[{FORGED_TOML}]\nx = 1\n") == [f"{shown}: unknown field"]


def test_an_unknown_key_with_control_characters_in_a_brief_is_shown_escaped(
    tmp_path: Path,
) -> None:
    repo, manifest = make_repo(tmp_path, f"+++\n{FORGED_TOML} = 1\n+++\nBody\n")
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert caught.value.problems == [f"default.md: {FORGED_KEY!r}: unknown field"]


def test_a_long_unknown_key_is_cut_short() -> None:
    (problem,) = problems_of('"' + "q" * 5000 + '" = 1\n' + MINIMAL)
    assert problem == repr("q" * 63 + "\N{HORIZONTAL ELLIPSIS}") + ": unknown field"
    (problem,) = problems_of("w" * 5000 + " = 1\n" + MINIMAL)  # plain, but too long
    assert problem == repr("w" * 63 + "\N{HORIZONTAL ELLIPSIS}") + ": unknown field"


def test_missing_required_fields() -> None:
    assert problems_of("") == ["name: is required", "app: is required"]
    assert problems_of('name = "x"\n[app]\n') == [
        "app.runtime: is required",
        "app.start: is required",
        "app.port: is required",
    ]
    assert problems_of(MINIMAL + "[login]\npage = '/l'\n") == [
        "login.username_label: is required",
        "login.password_label: is required",
        "login.submit: is required",
    ]


def test_wrong_types() -> None:
    assert problems_of(MINIMAL.replace('name = "Tiny"', "name = 3")) == [
        "name: must be text, not 3"
    ]
    assert problems_of(MINIMAL + '[browser]\nassets = "a.example"\n') == [
        "browser.assets: must be a list of text"
    ]
    assert problems_of('name = "x"\napp = 1\n') == ["app: must be a table"]


def test_invalid_toml_is_a_problem() -> None:
    problems = problems_of("name = ")
    assert len(problems) == 1
    assert problems[0].startswith("footfall.toml: not valid TOML")


def test_command_length_limit() -> None:
    ok = MINIMAL.replace("python3 serve.py 8080", "x" * 2000)
    assert parse_manifest(ok).app.start == "x" * 2000
    bad = MINIMAL.replace("python3 serve.py 8080", "x" * 2001)
    assert problems_of(bad) == ["app.start: must be at most 2000 characters, not 2001"]


def test_empty_command_is_refused() -> None:
    assert problems_of(MINIMAL.replace("python3 serve.py 8080", "  ")) == [
        "app.start: must not be empty"
    ]


def test_ready_timeout_range() -> None:
    text = MINIMAL.replace("port = 8080", "port = 8080\nready_timeout = 0")
    assert problems_of(text) == ["app.ready_timeout: must be between 1 and 600, not 0"]


@pytest.mark.parametrize("briefs", ["/etc", "../x", "a/../../x", "", "a\\b"])
def test_briefs_folder_must_stay_inside_the_repo(briefs: str) -> None:
    problems = problems_of(MINIMAL + f'[test]\nbriefs = "{briefs.replace(chr(92), chr(92) * 2)}"\n')
    assert len(problems) == 1
    assert problems[0].startswith("test.briefs: ")


def test_load_manifest_missing_oversized_and_undecodable(tmp_path: Path) -> None:
    with pytest.raises(ManifestError) as missing:
        load_manifest(tmp_path)
    assert missing.value.problems == ["footfall.toml: not found at the root of the repository"]

    (tmp_path / "footfall.toml").write_text("#" * (64 * 1024 + 1))
    with pytest.raises(ManifestError) as big:
        load_manifest(tmp_path)
    assert big.value.problems == ["footfall.toml: larger than the 65536 byte limit"]

    (tmp_path / "footfall.toml").write_bytes(b"name = '\xff\xfe'")
    with pytest.raises(ManifestError) as bad:
        load_manifest(tmp_path)
    assert bad.value.problems == ["footfall.toml: is not valid UTF-8"]


def test_manifest_symlink_out_of_repo_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.toml"
    outside.write_text(MINIMAL)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "footfall.toml").symlink_to(outside)
    with pytest.raises(ManifestError, match="is a link"):
        load_manifest(repo)


def test_manifest_symlink_inside_repo_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "real.toml").write_text(MINIMAL)
    (repo / "footfall.toml").symlink_to("config/real.toml")
    with pytest.raises(ManifestError) as caught:
        load_manifest(repo)
    assert caught.value.problems == [
        "footfall.toml: is a link, and Footfall reads a manifest or brief only as a plain "
        "file, never through a link"
    ]


# ------------------------------------------------------------------ briefs


def make_repo(
    tmp_path: Path, brief: str | None = None, *, login: bool = False
) -> tuple[Path, Manifest]:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    if brief is not None:
        (repo / "footfall" / "briefs" / "default.md").write_text(brief)
    manifest = parse_manifest(WITH_LOGIN if login else MINIMAL)
    return repo, manifest


def test_brief_without_front_matter() -> None:
    manifest = load_manifest(FIXTURE)
    brief = load_brief(FIXTURE, manifest)
    assert brief.name == "default"
    assert brief.start == "signed-out"
    assert brief.max_actions is None
    assert brief.text.startswith("Look around the app")


def test_brief_start_defaults_to_signed_in_with_a_login(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path, login=True)
    assert load_brief(repo, manifest).start == "signed-in"


def test_brief_front_matter(tmp_path: Path) -> None:
    repo, manifest = make_repo(
        tmp_path, '+++\nstart = "signed-out"\nmax_actions = 60\n+++\n\n\nTry it.\n', login=True
    )
    brief = load_brief(repo, manifest)
    assert brief.start == "signed-out"
    assert brief.max_actions == 60
    assert brief.text == "Try it.\n"


def test_front_matter_must_be_at_the_very_top(tmp_path: Path) -> None:
    text = "\n+++\nmax_actions = 5\n+++\nBody\n"
    repo, manifest = make_repo(tmp_path, text)
    brief = load_brief(repo, manifest)
    assert brief.max_actions is None
    assert "+++" in brief.text


def test_brief_unknown_front_matter_key(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path, "+++\nmax_action = 5\n+++\nBody\n")
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert caught.value.problems == ["default.md: max_action: unknown field"]


def test_brief_front_matter_never_closed(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path, "+++\nmax_actions = 5\nBody\n")
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert caught.value.problems == ["default.md: front matter is not closed by a +++ line"]


def test_brief_front_matter_invalid_toml(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path, "+++\nmax_actions = \n+++\nBody\n")
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert caught.value.problems[0].startswith("default.md: front matter is not valid TOML")


def test_brief_bad_values_are_all_reported(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path, '+++\nstart = "later"\nmax_actions = 0\n+++\nBody\n')
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert caught.value.problems == [
        "default.md: start: must be signed-in or signed-out, not 'later'",
        "default.md: max_actions: must be between 1 and 120, not 0",
    ]


def test_brief_cannot_start_signed_in_without_a_login(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path, '+++\nstart = "signed-in"\n+++\nBody\n')
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert caught.value.problems == [
        "default.md: start: 'signed-in' needs a [login] section in footfall.toml"
    ]


@pytest.mark.parametrize("max_actions", [1, 120])
def test_brief_max_actions_bounds_accepted(tmp_path: Path, max_actions: int) -> None:
    repo, manifest = make_repo(tmp_path, f"+++\nmax_actions = {max_actions}\n+++\nBody\n")
    assert load_brief(repo, manifest).max_actions == max_actions


def test_brief_max_actions_ceiling(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path, "+++\nmax_actions = 121\n+++\nBody\n")
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert caught.value.problems == ["default.md: max_actions: must be between 1 and 120, not 121"]


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", "", "A", "-x", "x" * 65, "a.md"])
def test_brief_name_cannot_escape_the_repo(tmp_path: Path, name: str) -> None:
    repo, manifest = make_repo(tmp_path)
    with pytest.raises(ManifestError, match="brief name"):
        load_brief(repo, manifest, name)


def test_brief_name_limits_accepted(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path)
    for name in ("a", "x" * 64):
        (repo / "footfall" / "briefs" / f"{name}.md").write_text("Hi\n")
        assert load_brief(repo, manifest, name).name == name


def test_brief_symlink_out_of_repo_is_refused(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path)
    secret = tmp_path / "secret.md"
    secret.write_text("not for the tester\n")
    (repo / "footfall" / "briefs" / "leak.md").symlink_to(secret)
    with pytest.raises(ManifestError, match="is a link"):
        load_brief(repo, manifest, "leak")


def test_brief_symlinked_folder_out_of_repo_is_refused(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "default.md").write_text("outside\n")
    shutil.rmtree(repo / "footfall" / "briefs")
    (repo / "footfall" / "briefs").symlink_to(elsewhere)
    with pytest.raises(ManifestError, match="is a link or a file, not a folder"):
        load_brief(repo, manifest)


def test_brief_symlinked_folder_inside_repo_is_refused(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path)
    (repo / "footfall" / "briefs").rename(repo / "footfall" / "real-briefs")
    (repo / "footfall" / "briefs").symlink_to("real-briefs")
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert caught.value.problems == [
        "default.md: 'footfall/briefs' is a link or a file, not a folder, and Footfall reads "
        "nothing through a link"
    ]


GIT = shutil.which("git") or "git"
CONFIG_MARKER = "f07-config-marker-token"
IGNORED_MARKER = "F07_IGNORED_MARKER_5c1e"
TRACKED_MARKER = "F07_TRACKED_MARKER_9a3d"


def git(folder: Path, *arguments: str) -> None:
    subprocess.run([GIT, "-C", str(folder), *arguments], check=True, capture_output=True)


def repo_with_secrets(tmp_path: Path) -> tuple[Path, Manifest]:
    """A repository whose git configuration, ignored file and tracked file each hold a marker
    that nothing else in the repository, or any default, holds."""
    repo, manifest = make_repo(tmp_path)
    git(repo, "init", "-q")
    git(repo, "config", "remote.origin.url", f"https://{CONFIG_MARKER}@example.invalid/a.git")
    (repo / ".gitignore").write_text("f07-secret.env\n")
    (repo / "f07-secret.env").write_text(f"API_KEY={IGNORED_MARKER}\n")
    (repo / "f07-notes.md").write_text(f"{TRACKED_MARKER}\n")
    git(repo, "add", ".gitignore", "f07-notes.md", "footfall.toml", "footfall")
    return repo, manifest


@pytest.mark.parametrize(
    ("target", "marker"),
    [
        (".git/config", CONFIG_MARKER),
        ("f07-secret.env", IGNORED_MARKER),
        ("f07-notes.md", TRACKED_MARKER),
    ],
)
def test_a_brief_that_is_a_link_inside_the_repo_is_refused(
    tmp_path: Path, target: str, marker: str
) -> None:
    repo, manifest = repo_with_secrets(tmp_path)
    briefs = repo / "footfall" / "briefs"
    (briefs / "f07-linked.md").symlink_to(Path("../..") / target)
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest, "f07-linked")
    assert caught.value.problems == [
        "f07-linked.md: is a link, and Footfall reads a manifest or brief only as a plain "
        "file, never through a link"
    ]
    assert marker not in str(caught.value)
    # The same file, copied in as a plain file, would have been read, marker and all.
    shutil.copyfile(repo / target, briefs / "f07-copied.md")
    assert marker in load_brief(repo, manifest, "f07-copied").text


@pytest.mark.parametrize("folder", [".git", ".GIT/briefs", "x/.g\u200cit"])
def test_a_brief_in_a_git_folder_is_refused(tmp_path: Path, folder: str) -> None:
    repo, _ = make_repo(tmp_path)
    (repo / folder).mkdir(parents=True, exist_ok=True)
    (repo / folder / "f07-brief.md").write_text("Look in the git folder.\n")
    manifest = parse_manifest(MINIMAL + f'[test]\nbriefs = "{folder}"\n')
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest, "f07-brief")
    assert caught.value.problems == ["f07-brief.md: is in the repository's git folder"]


def test_a_brief_that_is_a_folder_is_refused(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path)
    (repo / "footfall" / "briefs" / "f07-folder.md").mkdir()
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest, "f07-folder")
    assert caught.value.problems == ["f07-folder.md: is not a plain file"]


def test_a_manifest_or_brief_must_be_listed() -> None:
    manifest = parse_manifest(MINIMAL + '[test]\nbriefs = "./f07/briefs/"\n')
    brief = Brief(name="f07-listed", text="Hi\n", start="signed-out", max_actions=None)
    listed = {PurePosixPath("footfall.toml"), PurePosixPath("f07/briefs/f07-listed.md")}
    require_listed(manifest, brief, listed)  # the folder is spelt differently, but the same
    with pytest.raises(ManifestError) as caught:
        require_listed(manifest, brief, {PurePosixPath("f07/briefs/f07-listed.md")})
    assert caught.value.problems == [
        "footfall.toml: is not among the files git would commit, the only files a run copies"
    ]
    with pytest.raises(ManifestError) as caught:
        require_listed(manifest, brief, {PurePosixPath("footfall.toml")})
    assert caught.value.problems == [
        "f07-listed.md: is not among the files git would commit, the only files a run copies"
    ]


def test_brief_missing_oversized_and_undecodable(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path)
    with pytest.raises(ManifestError) as missing:
        load_brief(repo, manifest, "nope")
    assert missing.value.problems == ["nope.md: not found in footfall/briefs"]

    briefs = repo / "footfall" / "briefs"
    (briefs / "big.md").write_text("x" * (256 * 1024 + 1))
    with pytest.raises(ManifestError) as big:
        load_brief(repo, manifest, "big")
    assert big.value.problems == ["big.md: larger than the 262144 byte limit"]

    (briefs / "bad.md").write_bytes(b"\xff\xfe")
    with pytest.raises(ManifestError) as bad:
        load_brief(repo, manifest, "bad")
    assert bad.value.problems == ["bad.md: is not valid UTF-8"]


# ------------------------------------------------- hostile input never crashes


def test_huge_integer_is_a_problem() -> None:
    problems = problems_of(MINIMAL.replace("port = 8080", "port = " + "9" * 5000))
    assert problems == ["footfall.toml: not valid TOML (a number in it has too many digits)"]


@pytest.mark.parametrize("number", ["0x" + "f" * 5000, "0o" + "7" * 20000, "1" * 4000])
def test_huge_integer_out_of_range_is_named_without_its_digits(number: str) -> None:
    """Python cannot print an integer of over 4300 digits, which hex and octal can give."""
    problems = problems_of(MINIMAL.replace("port = 8080", f"port = {number}"))
    assert problems == ["app.port: must be between 1024 and 65535, not a very large number"]


def test_huge_integer_in_front_matter_is_a_problem(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path, "+++\nmax_actions = " + "9" * 5000 + "\n+++\nBody\n")
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert caught.value.problems == [
        "default.md: front matter is not valid TOML (a number in it has too many digits)"
    ]


@pytest.mark.parametrize(
    "nested", ["[" * 30000 + "]" * 30000, "{a=" * 5000 + "1" + "}" * 5000], ids=["array", "table"]
)
def test_deep_nesting_is_a_problem(tmp_path: Path, nested: str) -> None:
    problems = problems_of(MINIMAL + f"[test]\npages = {nested}\n")
    assert len(problems) == 1
    assert problems[0].startswith("footfall.toml: not valid TOML")
    repo, manifest = make_repo(tmp_path, f"+++\nmax_actions = {nested}\n+++\nBody\n")
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert caught.value.problems[0].startswith("default.md: front matter is not valid TOML")


@pytest.mark.parametrize("briefs", ["a" * 300, "/".join(["a" * 50] * 5)])
def test_briefs_folder_length_limit(briefs: str) -> None:
    problems = problems_of(MINIMAL + f'[test]\nbriefs = "{briefs}"\n')
    assert len(problems) == 1
    assert problems[0].startswith("test.briefs: must be at most 200 characters")


def test_overlong_path_component_is_a_problem(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    manifest = parse_manifest(MINIMAL + f'[test]\nbriefs = "{"a" * 200}"\n')
    with pytest.raises(ManifestError) as caught:
        load_brief(repo, manifest)
    assert len(caught.value.problems) == 1
    assert caught.value.problems[0].startswith("default.md: ")


def test_symlink_loops_are_problems(tmp_path: Path) -> None:
    repo, manifest = make_repo(tmp_path)
    (repo / "footfall" / "briefs" / "loop.md").symlink_to("loop.md")
    with pytest.raises(ManifestError) as brief:
        load_brief(repo, manifest, "loop")
    assert brief.value.problems[0].startswith("loop.md: is a link")
    (repo / "footfall.toml").unlink()
    (repo / "footfall.toml").symlink_to("footfall.toml")
    with pytest.raises(ManifestError) as caught:
        load_manifest(repo)
    assert caught.value.problems[0].startswith("footfall.toml: is a link")


@pytest.mark.parametrize("persona", ["1", "-a", "Sven", "a" * 33, "a_b"])
def test_persona_ids_must_be_copy_names(persona: str) -> None:
    problems = problems_of(MINIMAL + f'[test]\npersonas = ["{persona}"]\n')
    assert len(problems) == 1
    assert problems[0].startswith("test.personas[0]: ")


def test_persona_copy_name_limits_accepted() -> None:
    ids = ("a", "a" + "b" * 31)
    manifest = parse_manifest(MINIMAL + f'[test]\npersonas = ["{ids[0]}", "{ids[1]}"]\n')
    assert manifest.test.personas == ids


def test_duplicate_personas_are_refused() -> None:
    assert problems_of(MINIMAL + '[test]\npersonas = ["sven", "grace", "sven"]\n') == [
        "test.personas[2]: 'sven' is listed more than once"
    ]


@pytest.mark.parametrize("path", ["/a..b", "/.well-known/x", "/a/...", "/search?q=../x", "/a.b/"])
def test_paths_may_hold_dots_that_are_no_dot_segment(path: str) -> None:
    manifest = parse_manifest(MINIMAL.replace("port = 8080", f'port = 8080\nready = "{path}"'))
    assert manifest.app.ready == path


@pytest.mark.parametrize(
    ("table", "key", "item", "field"),
    [
        ("app", "egress", "h{}.example.com", "app.egress"),
        ("browser", "assets", "h{}.example.com", "browser.assets"),
        ("test", "pages", "/p{}", "test.pages"),
        ("test", "personas", "p{}", "test.personas"),
    ],
)
def test_lists_are_capped(table: str, key: str, item: str, field: str) -> None:
    def manifest_with(count: int) -> str:
        entries = ", ".join(f'"{item.format(index)}"' for index in range(count))
        if table == "app":
            return MINIMAL + f"{key} = [{entries}]\n"
        return MINIMAL + f"[{table}]\n{key} = [{entries}]\n"

    parse_manifest(manifest_with(64))
    assert problems_of(manifest_with(65)) == [f"{field}: must list at most 64 entries, not 65"]


@pytest.mark.parametrize(("table", "key"), [("app", "egress"), ("browser", "assets")])
def test_duplicate_hosts_are_refused(table: str, key: str) -> None:
    entries = '["example.com", "tiles.example.org", "Example.COM"]'
    text = MINIMAL + (
        f"{key} = {entries}\n" if table == "app" else f"[{table}]\n{key} = {entries}\n"
    )
    assert problems_of(text) == [f"{table}.{key}[2]: 'Example.COM' is listed more than once"]


def test_a_byte_order_mark_is_ignored(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, '\ufeff+++\nstart = "signed-out"\nmax_actions = 7\n+++\nBody\n')
    text = (repo / "footfall.toml").read_text()
    (repo / "footfall.toml").write_text("\ufeff" + text)
    manifest = load_manifest(repo)
    brief = load_brief(repo, manifest)
    assert brief.max_actions == 7
    assert brief.text == "Body\n"


@pytest.mark.parametrize("shape", ["[{n}]", "{{ a = {n} }}", "[[{n}]]"])
@pytest.mark.parametrize(
    ("before", "field", "message"),
    [
        ('name = "Tiny"', "name", "must be text"),
        ("port = 8080", "app.port", "must be a whole number"),
    ],
)
def test_a_very_large_number_inside_a_list_or_table_is_named_without_its_digits(
    shape: str, before: str, field: str, message: str
) -> None:
    """Python cannot print an integer of over 4300 digits, even inside a list or a table."""
    value = shape.format(n="0x" + "f" * 5000)
    key = before.split(" = ")[0]
    problems = problems_of(MINIMAL.replace(before, f"{key} = {value}"))
    assert problems == [f"{field}: {message}, not a value holding a very large number"]
