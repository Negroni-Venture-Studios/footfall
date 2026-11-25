"""Tiny Tasks, the demo app: it signs in, lists, searches, and keeps its planted bugs.

Each test starts `demo-app/serve.py` on 127.0.0.1 in a temporary folder with its own
SQLite file, and talks to it over HTTP. Two tests pin planted bugs on purpose, so that
nobody fixes the demo by accident; `demo-app/BUGS.md` lists all twelve.
"""

import http.client
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from footfall.manifest import load_brief, load_manifest
from footfall.personas import load_devices, load_personas

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo-app"
USERNAME = "tester-ada"
PASSWORD = "fake-pass-for-tests-only"  # gitleaks:allow
BUGS = DEMO / "BUGS.md"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: str


@dataclass
class Client:
    port: int
    cookie: str = ""
    cookies_seen: list[str] = field(default_factory=list)

    def request(self, method: str, path: str, form: dict[str, str] | None = None) -> Response:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {}
            body = None
            if form is not None:
                body = urllib.parse.urlencode(form)
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            if self.cookie:
                headers["Cookie"] = self.cookie
            connection.request(method, path, body=body, headers=headers)
            reply = connection.getresponse()
            data = reply.read().decode()
            for name, value in reply.getheaders():
                if name.lower() == "set-cookie":
                    self.cookies_seen.append(value)
                    self.cookie = value.split(";", 1)[0]
            return Response(reply.status, {k.lower(): v for k, v in reply.getheaders()}, data)
        finally:
            connection.close()

    def get(self, path: str) -> Response:
        return self.request("GET", path)

    def post(self, path: str, form: dict[str, str] | None = None) -> Response:
        return self.request("POST", path, form or {})

    def sign_in(self) -> Response:
        return self.post("/login", {"username": USERNAME, "password": PASSWORD})


@dataclass
class Demo:
    port: int
    db: Path
    folder: Path
    process: "subprocess.Popen[bytes] | None" = None

    def seed(self, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(DEMO / "seed.py")],
            env=self.env(env_extra),
            cwd=self.folder,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "PORT": str(self.port),
            "TINY_TASKS_DB": str(self.db),
            "FOOTFALL_USERNAME": USERNAME,
            "FOOTFALL_PASSWORD": PASSWORD,
        }
        env.update(extra or {})
        return env


def start(demo: Demo) -> Iterator[Demo]:
    log = (demo.folder / "server.log").open("w")
    process = subprocess.Popen(
        [sys.executable, str(DEMO / "serve.py")],
        env=demo.env(),
        cwd=demo.folder,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    demo.process = process
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if Client(demo.port).get("/healthz").status == 200:
                    break
            except OSError:
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                log.flush()
                raise RuntimeError(
                    f"serve.py did not start: {(demo.folder / 'server.log').read_text()}"
                )
            time.sleep(0.05)
        yield demo
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
            process.wait()
        log.close()


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[Demo]:
    app = Demo(free_port(), tmp_path / "tiny.sqlite3", tmp_path)
    seeded = app.seed()
    assert seeded.returncode == 0, seeded.stderr
    yield from start(app)


@pytest.fixture
def client(demo: Demo) -> Client:
    signed_in = Client(demo.port)
    assert signed_in.sign_in().status == 303
    return signed_in


def task_texts(page: str) -> list[str]:
    return re.findall(r'<span class="task-text">(.*?)</span>', page)


def test_seed_creates_account_from_env(tmp_path: Path) -> None:
    app = Demo(free_port(), tmp_path / "tiny.sqlite3", tmp_path)
    done = app.seed({"FOOTFALL_USERNAME": "grace-hopper"})
    assert done.returncode == 0, done.stderr
    assert PASSWORD not in done.stdout + done.stderr
    with sqlite3.connect(app.db) as db:
        users = db.execute("select username from users").fetchall()
        tasks = db.execute("select text, due from tasks").fetchall()
        stored = db.execute("select * from users").fetchone()
    db.close()
    assert users == [("grace-hopper",)]
    assert len(tasks) == 12
    assert any(due is not None and due.endswith("23:58") for _, due in tasks)
    assert PASSWORD not in repr(stored)


def test_seed_needs_both_variables(tmp_path: Path) -> None:
    app = Demo(free_port(), tmp_path / "tiny.sqlite3", tmp_path)
    done = app.seed({"FOOTFALL_PASSWORD": ""})
    assert done.returncode == 2
    assert "FOOTFALL_PASSWORD" in done.stderr
    assert not app.db.exists()


def test_seed_twice_keeps_one_account_and_twelve_tasks(demo: Demo) -> None:
    assert demo.seed().returncode == 0
    with sqlite3.connect(demo.db) as db:
        assert db.execute("select count(*) from users").fetchone() == (1,)
        assert db.execute("select count(*) from tasks").fetchone() == (12,)
    db.close()


def test_healthz(demo: Demo) -> None:
    reply = Client(demo.port).get("/healthz")
    assert (reply.status, reply.body.strip()) == (200, "ok")


def test_pages_need_a_sign_in(demo: Demo) -> None:
    for path in ("/", "/settings"):
        reply = Client(demo.port).get(path)
        assert reply.status == 303
        assert reply.headers["location"] == "/login"


def test_sign_in_and_list(demo: Demo) -> None:
    visitor = Client(demo.port)
    page = visitor.get("/login")
    assert page.status == 200
    assert 'for="username"' in page.body
    assert "Password" in page.body
    reply = visitor.sign_in()
    assert (reply.status, reply.headers["location"]) == (303, "/")
    cookie = visitor.cookies_seen[0]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    listing = visitor.get("/")
    assert listing.status == 200
    texts = task_texts(listing.body)
    assert len(texts) == 12
    assert "Buy oat milk" in texts


def test_sign_out_ends_the_session(client: Client) -> None:
    assert client.post("/logout").status == 303
    assert client.get("/").status == 303
    assert client.get("/logout").status == 405


def test_every_sign_in_error_says_the_same_thing(demo: Demo) -> None:
    """Bug 1: the message never says what was wrong."""
    wrong_password = Client(demo.port).post("/login", {"username": USERNAME, "password": "x"})
    unknown_user = Client(demo.port).post("/login", {"username": "nobody", "password": "x"})
    blank = Client(demo.port).post("/login", {"username": "", "password": ""})
    for reply in (wrong_password, unknown_user, blank):
        assert reply.status == 401
        assert "Something went wrong" in reply.body
    assert wrong_password.body == unknown_user.body == blank.body


def test_search_is_case_sensitive(client: Client) -> None:
    """Bug 2: pinned so that nobody fixes the demo."""
    assert task_texts(client.get("/?q=milk").body) == ["Buy oat milk"]
    assert task_texts(client.get("/?q=Milk").body) == []
    assert task_texts(client.get("/?q=Oat").body) == []


def test_empty_task_accepted(client: Client) -> None:
    """Bug 3: pinned so that nobody fixes the demo."""
    before = len(task_texts(client.get("/").body))
    assert client.post("/tasks", {"text": "", "due": ""}).status == 303
    assert client.post("/tasks", {"text": "   ", "due": ""}).status == 303
    assert len(task_texts(client.get("/").body)) == before + 2


def test_new_task_appears_with_its_due_date(client: Client) -> None:
    client.post("/tasks", {"text": "Ring the plumber", "due": "2027-03-25"})
    page = client.get("/?q=plumber").body
    assert task_texts(page) == ["Ring the plumber"]
    assert "03/25/2027" in page  # bug 11: always month first


def test_one_task_left_reads_one_tasks(client: Client) -> None:
    """Bug 5."""
    for number in range(1, 13):
        client.post(f"/tasks/{number}/delete")
    client.post("/tasks", {"text": "Only one", "due": ""})
    assert "1 tasks left" in client.get("/").body


def test_empty_list_has_no_empty_state(client: Client) -> None:
    """Bug 6: nothing says the list or the search is empty."""
    page = client.get("/?q=zzzz").body
    assert task_texts(page) == []
    assert "No tasks" not in page
    # Nothing at all sits between the count and the end of the page's content.
    bare = r'<p class="count">\d+ tasks left</p>\s*<ul class="tasks">\s*</ul>\s*</main>'
    assert re.search(bare, page)
    for number in range(1, 13):
        client.post(f"/tasks/{number}/delete")
    emptied = client.get("/").body
    assert re.search(bare, emptied)
    assert "0 tasks left" in emptied


def test_toggle_and_delete(client: Client) -> None:
    page = client.get("/?q=milk").body
    match = re.search(r"/tasks/(\d+)/toggle", page)
    assert match
    number = match.group(1)
    assert "task done" not in page
    client.post(f"/tasks/{number}/toggle")
    assert "task done" in client.get("/?q=milk").body
    client.post(f"/tasks/{number}/delete")
    assert task_texts(client.get("/?q=milk").body) == []


def test_state_changes_are_post_only(client: Client) -> None:
    for path in ("/tasks/1/delete", "/tasks/1/toggle", "/tasks"):
        assert client.get(path).status == 405
    assert len(task_texts(client.get("/").body)) == 12


def test_user_text_is_escaped(client: Client) -> None:
    client.post("/tasks", {"text": '<script>alert("x")</script> & <b>', "due": ""})
    page = client.get("/").body
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &lt;b&gt;" in page
    client.post("/tasks", {"text": '"><img src=x onerror=alert(1)>', "due": ""})
    assert "<img src=x" not in client.get("/").body


def test_search_text_is_escaped_and_not_sql(client: Client) -> None:
    page = client.get("/?q=%22%3E%3Cscript%3Ex%3C%2Fscript%3E").body
    assert "<script>x" not in page
    reply = client.get("/?q=%27%20OR%201%3D1%20--")
    assert task_texts(reply.body) == []


def test_tasks_belong_to_their_owner(demo: Demo, client: Client) -> None:
    with sqlite3.connect(demo.db) as db:
        db.execute("insert into users (username, salt, hash) values ('other', x'00', x'00')")
        user = db.execute("select id from users where username = 'other'").fetchone()[0]
        db.execute("insert into tasks (user_id, text, done) values (?, 'Private', 0)", (user,))
        number = db.execute("select id from tasks where text = 'Private'").fetchone()[0]
    db.close()
    assert "Private" not in client.get("/").body
    client.post(f"/tasks/{number}/delete")
    with sqlite3.connect(demo.db) as db:
        assert db.execute("select count(*) from tasks where text = 'Private'").fetchone() == (1,)
    db.close()


def test_a_cross_site_post_is_refused(client: Client, demo: Demo) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", demo.port, timeout=10)
    try:
        connection.request(
            "POST",
            "/tasks/1/delete",
            body="",
            headers={"Cookie": client.cookie, "Origin": "http://elsewhere.example"},
        )
        reply = connection.getresponse()
        reply.read()
        assert reply.status == 403
    finally:
        connection.close()


def test_pages_send_hardening_headers(client: Client) -> None:
    headers = client.get("/").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"


def test_static_files_and_unknown_paths(client: Client) -> None:
    css = client.get("/static/app.css")
    assert css.status == 200
    assert css.headers["content-type"].startswith("text/css")
    assert client.get("/static/settings.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/static/../serve.py").status == 404
    assert client.get("/static/%2e%2e/serve.py").status == 404
    assert client.get("/nowhere").status == 404


def test_planted_markup_bugs(client: Client) -> None:
    """Bugs 4, 7, 8, 9, 10 and 12 are in the markup and styles, so pin their sources."""
    page = client.get("/").body
    assert 'class="icon-button"' in page  # 9: a link holding only a picture
    assert not re.search(r'class="icon-button"[^>]*aria-label', page)
    assert re.search(r'class="delete"[^>]*aria-label="Delete task', page)  # 8
    css = client.get("/static/app.css").body
    assert re.search(r"\.app-header\s*{[^}]*height:\s*40px", css)  # 4
    assert re.search(r"\.toolbar\s*{[^}]*width:\s*420px", css)  # 10
    assert re.search(r"\.task-text\s*{[^}]*color:\s*#333", css)  # 7
    assert re.search(r"\.delete\s*{[^}]*width:\s*16px[^}]*height:\s*16px", css)  # 8
    settings = client.get("/settings")
    assert settings.status == 200
    assert "Signed in as" in settings.body  # 12: the page still reads
    assert "/static/settings.js" in settings.body


def test_manifest_parses() -> None:
    manifest = load_manifest(DEMO)
    assert manifest.name == "Tiny Tasks"
    assert manifest.app.runtime == "python3.13"
    assert manifest.app.install is None
    assert manifest.app.seed == "python3 seed.py"
    assert manifest.app.start == "python3 serve.py --host 0.0.0.0"
    assert manifest.app.ready == "/healthz"
    assert manifest.login is not None
    assert manifest.login.page == "/login"
    assert manifest.test.pages == ("/", "/settings")
    devices = load_devices()
    known = load_personas(devices)
    assert set(manifest.test.personas) <= set(known)
    brief = load_brief(DEMO, manifest)
    assert brief.start == "signed-in"
    assert brief.text.strip()


def test_manifest_labels_match_the_sign_in_page(demo: Demo) -> None:
    manifest = load_manifest(DEMO)
    assert manifest.login is not None
    page = Client(demo.port).get(manifest.login.page).body
    assert f">{manifest.login.username_label}</label>" in page
    assert f">{manifest.login.password_label}</label>" in page
    assert f">{manifest.login.submit}</button>" in page


def test_bugs_file_names_real_personas() -> None:
    text = BUGS.read_text()
    known = load_personas(load_devices())
    named = set(re.findall(r"`([a-z]+)`", text)) & set(known)
    assert {"malik", "mehmet", "sven", "grace", "clara", "layla", "olga", "jonas", "anna"} <= named
    assert len(re.findall(r"^## Bug \d+\b", text, re.MULTILINE)) == 12


def test_bugs_file_marks_every_persona_the_demo_does_not_run() -> None:
    """A persona outside the manifest's list is named "when run", never silently dropped."""
    defaults = set(load_manifest(DEMO).test.personas)
    others = set(load_personas(load_devices())) - defaults
    sections = BUGS.read_text().split("\n## ")[1:]
    for section in sections:
        for sentence in re.split(r"(?<=\.) ", section):
            named = set(re.findall(r"`([a-z]+)`", sentence)) & others
            assert not named or "when run" in sentence, sentence


def test_bug_1_names_the_personas_who_try_to_break_things() -> None:
    """Mistyping on purpose is how a tester meets bug 1, so those personas are named there."""
    breakers = {
        p.id
        for p in load_personas(load_devices()).values()
        if re.search(r"\bbreak\b|odd input", p.profile)
    }
    bug_1 = BUGS.read_text().split("\n## ")[1]
    assert bug_1.startswith("Bug 1:")
    assert breakers == {"jonas", "mehmet"}
    assert breakers <= set(re.findall(r"`([a-z]+)`", bug_1))


def test_a_second_session_does_not_log_the_first_out(demo: Demo) -> None:
    first, second = Client(demo.port), Client(demo.port)
    first.sign_in()
    second.sign_in()
    assert first.get("/").status == 200
    assert second.get("/").status == 200
    assert first.cookie != second.cookie


def rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*{([^}]*)}", css)
    assert match, selector
    return match.group(1)


def test_header_title_is_clearly_clipped_at_200_percent() -> None:
    """Bug 4, by arithmetic: the CSS is read, not rendered.

    A root font size of 16px is scaled by the persona's text scale. At 1.0 the title's
    line box must fit the header. At 2.0 one line of it must overflow by at least half
    the header's height, which no screen width can lessen. The arithmetic cannot say
    which part of the letters is lost; a render at 200% shows the lower part cut off.
    """
    css = (DEMO / "static" / "app.css").read_text()
    header = rule(css, ".app-header")
    title = rule(css, ".app-title")
    assert re.search(r"overflow:\s*hidden", header)
    height = float(re.search(r"(?<![-\w])height:\s*([\d.]+)px", header).group(1))  # type: ignore[union-attr]
    size = float(re.search(r"font-size:\s*([\d.]+)rem", title).group(1))  # type: ignore[union-attr]
    line = float(re.search(r"line-height:\s*([\d.]+)\s*;", title).group(1))  # type: ignore[union-attr]

    def line_box(scale: float) -> float:
        return size * 16 * scale * line

    assert line_box(1.0) <= height
    assert line_box(2.0) - height >= height / 2


def test_sigterm_stops_the_server_cleanly(demo: Demo) -> None:
    """As PID 1 in a container, Python ignores SIGTERM unless a handler is installed."""
    assert demo.process is not None
    started = time.monotonic()
    demo.process.send_signal(signal.SIGTERM)
    assert demo.process.wait(timeout=5) == 0
    assert time.monotonic() - started < 5
