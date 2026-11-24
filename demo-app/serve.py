#!/usr/bin/env python3
"""Tiny Tasks: a small to-do list with a dozen planted usability and accessibility bugs.

Standard library only. The bugs are in what people see and do. Everything underneath is
built the way it should be, because people copy from this file: SQL is parameterised,
all user text is escaped on output, passwords are hashed with scrypt, session tokens
are random and kept in an HttpOnly cookie, and state changes need a POST.

Run `python3 serve.py`. It listens on 127.0.0.1 and takes its port from `PORT`
(8080 by default). `--host 0.0.0.0` listens on every interface, which is what the
Footfall container needs and what a local run should not use.
"""

import argparse
import hashlib
import hmac
import html
import os
import re
import secrets
import sqlite3
import string
import sys
import time
from collections.abc import Mapping
from contextlib import closing
from datetime import date
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "settings.js": "text/javascript; charset=utf-8",
}
SESSION_COOKIE = "session"
SESSION_SECONDS = 7 * 24 * 3600
MAX_BODY = 16 * 1024
MAX_TASK_TEXT = 500
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 64}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; frame-ancestors 'none'; "
        "form-action 'self'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
TASK_ROUTE = re.compile(r"/tasks/(\d+)/(toggle|delete)")

SCHEMA = """
create table if not exists users (
    id integer primary key,
    username text not null unique,
    salt blob not null,
    hash blob not null
);
create table if not exists tasks (
    id integer primary key,
    user_id integer not null references users (id) on delete cascade,
    text text not null,
    due text,
    done integer not null default 0
);
create table if not exists sessions (
    token_hash text primary key,
    user_id integer not null references users (id) on delete cascade,
    expires integer not null
);
"""


# ----------------------------------------------------------------------- storage


def db_path() -> Path:
    return Path(os.environ.get("TINY_TASKS_DB") or HERE / "tiny-tasks.sqlite3")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(db_path(), timeout=10)
    connection.execute("pragma foreign_keys = on")
    connection.executescript(SCHEMA)
    return connection


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)


def new_salt() -> bytes:
    return secrets.token_bytes(16)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_login(db: sqlite3.Connection, username: str, password: str) -> int | None:
    """The user's id if the password is right. Unknown names cost the same time."""
    row = db.execute("select id, salt, hash from users where username = ?", (username,)).fetchone()
    salt = row[1] if row else b"\0" * 16
    candidate = hash_password(password, salt)
    if row and hmac.compare_digest(candidate, row[2]):
        user_id: int = row[0]
        return user_id
    return None


# ----------------------------------------------------------------------- rendering


class Safe(str):
    """Text that is already HTML. Everything else is escaped on its way into a template."""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render(template_name: str, /, **values: object) -> str:
    template = string.Template((TEMPLATES / template_name).read_text(encoding="utf-8"))
    return template.substitute({k: v if isinstance(v, Safe) else esc(v) for k, v in values.items()})


def page(title: str, content: str, *, user: str | None = None, scripts: str = "") -> str:
    actions = render("settings-link.html") if user is not None else ""
    return render(
        "base.html",
        title=title,
        header_actions=Safe(actions),
        content=Safe(content),
        scripts=Safe(scripts),
    )


def show_due(due: str | None) -> str:
    """Planted bug 11: always month first, whoever is reading."""
    if not due:
        return ""
    day = date.fromisoformat(due[:10])
    shown = f"{day.month:02d}/{day.day:02d}/{day.year}"
    return f"{shown} {due[11:]}" if len(due) > 10 else shown


def task_row(number: int, text: str, due: str | None, done: bool) -> str:
    named = f"Delete task: {text}" if text.strip() else "Delete task"
    marked = f"Mark {'not done' if done else 'done'}"
    return render(
        "task.html",
        id=number,
        css=" done" if done else "",
        text=text,
        due=Safe(render("due.html", due=show_due(due)) if due else ""),
        toggle_label=marked,
        toggle_name=f"{marked}: {text}" if text.strip() else marked,
        delete_name=named,
    )


# ----------------------------------------------------------------------- server


def _clean_host(netloc: str) -> str:
    return netloc.strip().lower()


class Handler(BaseHTTPRequestHandler):
    server_version = "TinyTasks"
    sys_version = ""

    # -- plumbing

    def _send(
        self,
        status: int,
        body: str | bytes,
        content_type: str = "text/html; charset=utf-8",
        extra: Mapping[str, str] | None = None,
    ) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, where: str, cookie: str | None = None) -> None:
        extra = {"Location": where}
        if cookie:
            extra["Set-Cookie"] = cookie
        self._send(HTTPStatus.SEE_OTHER, "", extra=extra)

    def _text(self, status: int, message: str, extra: Mapping[str, str] | None = None) -> None:
        self._send(status, message + "\n", "text/plain; charset=utf-8", extra)

    def _form(self) -> dict[str, str] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self._text(HTTPStatus.BAD_REQUEST, "Bad request")
            return None
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return _clean_host(urlsplit(origin).netloc) == _clean_host(self.headers.get("Host", ""))

    def _session_user(self, db: sqlite3.Connection) -> tuple[int, str] | None:
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001  http.cookies raises its own error for bad input
            return None
        morsel = jar.get(SESSION_COOKIE)
        if morsel is None:
            return None
        row = db.execute(
            "select users.id, users.username from sessions join users on users.id = user_id "
            "where token_hash = ? and expires > ?",
            (token_hash(morsel.value), int(time.time())),
        ).fetchone()
        return (row[0], row[1]) if row else None

    # -- routes

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        if path == "/healthz":
            return self._text(HTTPStatus.OK, "ok")
        if path.startswith("/static/"):
            return self._static(path.removeprefix("/static/"))
        if path in ("/login", "/", "/settings"):
            with closing(connect()) as db:
                who = self._session_user(db)
                if path == "/login":
                    return self._send(
                        HTTPStatus.OK, page("Sign in", render("login.html", error=Safe("")))
                    )
                if who is None:
                    return self._redirect("/login")
                if path == "/settings":
                    return self._settings(who[1])
                return self._index(db, who, parse_qs(url.query).get("q", [""])[0])
        if path in ("/logout", "/tasks") or TASK_ROUTE.fullmatch(path):
            return self._text(HTTPStatus.METHOD_NOT_ALLOWED, "Use POST", {"Allow": "POST"})
        return self._text(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        change = TASK_ROUTE.fullmatch(path)
        known = path in ("/login", "/logout", "/tasks") or change
        if not known:
            if path in ("/", "/settings", "/healthz"):
                return self._text(HTTPStatus.METHOD_NOT_ALLOWED, "Use GET", {"Allow": "GET"})
            return self._text(HTTPStatus.NOT_FOUND, "Not found")
        if not self._same_origin():
            return self._text(HTTPStatus.FORBIDDEN, "Cross-site request refused")
        form = self._form()
        if form is None:
            return None
        with closing(connect()) as db:
            if path == "/login":
                return self._login(db, form)
            who = self._session_user(db)
            if who is None:
                return self._redirect("/login")
            if path == "/logout":
                return self._logout(db)
            if path == "/tasks":
                return self._add(db, who[0], form)
            if change is None:  # unreachable: the route was checked above
                return self._text(HTTPStatus.NOT_FOUND, "Not found")
            return self._change(db, who[0], int(change.group(1)), change.group(2))

    def _static(self, name: str) -> None:
        kind = STATIC.get(name)
        if kind is None:
            return self._text(HTTPStatus.NOT_FOUND, "Not found")
        body = (HERE / "static" / name).read_bytes()
        return self._send(HTTPStatus.OK, body, kind, {"Cache-Control": "no-cache"})

    def _login(self, db: sqlite3.Connection, form: dict[str, str]) -> None:
        user_id = check_login(db, form.get("username", ""), form.get("password", ""))
        if user_id is None:
            # Planted bug 1: every failure says the same unhelpful thing.
            body = page("Sign in", render("login.html", error=Safe(render("error.html"))))
            return self._send(HTTPStatus.UNAUTHORIZED, body)
        token = secrets.token_urlsafe(32)
        with db:
            db.execute("delete from sessions where expires <= ?", (int(time.time()),))
            db.execute(
                "insert into sessions (token_hash, user_id, expires) values (?, ?, ?)",
                (token_hash(token), user_id, int(time.time()) + SESSION_SECONDS),
            )
        cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_SECONDS}"
        )
        return self._redirect("/", cookie)

    def _logout(self, db: sqlite3.Connection) -> None:
        jar: SimpleCookie = SimpleCookie()
        jar.load(self.headers.get("Cookie", ""))
        morsel = jar.get(SESSION_COOKIE)
        if morsel is not None:
            with db:
                db.execute("delete from sessions where token_hash = ?", (token_hash(morsel.value),))
        cleared = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        return self._redirect("/login", cleared)

    def _index(self, db: sqlite3.Connection, who: tuple[int, str], query: str) -> None:
        user_id, name = who
        if query:
            # Planted bug 2: instr() compares exactly, so "milk" does not find "Milk".
            rows = db.execute(
                "select id, text, due, done from tasks where user_id = ? and instr(text, ?) > 0 "
                "order by id",
                (user_id, query),
            ).fetchall()
        else:
            rows = db.execute(
                "select id, text, due, done from tasks where user_id = ? order by id", (user_id,)
            ).fetchall()
        (left,) = db.execute(
            "select count(*) from tasks where user_id = ? and done = 0", (user_id,)
        ).fetchone()
        # Planted bug 5: no singular. Planted bug 6: no message when `rows` is empty.
        items = "".join(task_row(r[0], r[1], r[2], bool(r[3])) for r in rows)
        content = render(
            "index.html",
            query=query,
            left=f"{left} tasks left",
            items=Safe(items),
            clear=Safe(render("clear.html") if query else ""),
        )
        return self._send(HTTPStatus.OK, page("Tasks", content, user=name))

    def _settings(self, name: str) -> None:
        # Planted bug 12: settings.js throws as soon as it loads.
        content = render("settings.html", name=name)
        scripts = '<script src="/static/settings.js"></script>'
        return self._send(HTTPStatus.OK, page("Settings", content, user=name, scripts=scripts))

    def _add(self, db: sqlite3.Connection, user_id: int, form: dict[str, str]) -> None:
        # Planted bug 3: an empty or blank task is accepted as it is.
        text = form.get("text", "")
        due = form.get("due", "") or None
        if len(text) > MAX_TASK_TEXT:
            return self._text(HTTPStatus.BAD_REQUEST, "That task is too long")
        if due is not None:
            try:
                date.fromisoformat(due)
            except ValueError:
                return self._text(HTTPStatus.BAD_REQUEST, "That date is not valid")
        with db:
            db.execute(
                "insert into tasks (user_id, text, due, done) values (?, ?, ?, 0)",
                (user_id, text, due),
            )
        return self._redirect("/")

    def _change(self, db: sqlite3.Connection, user_id: int, number: int, action: str) -> None:
        with db:
            if action == "toggle":
                db.execute(
                    "update tasks set done = 1 - done where id = ? and user_id = ?",
                    (number, user_id),
                )
            else:
                # Planted bug 8: deleted at once, with no confirmation and no undo.
                db.execute("delete from tasks where id = ? and user_id = ?", (number, user_id))
        return self._redirect("/")

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} {format % args}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Tiny Tasks, the Footfall demo app.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to listen on; use 0.0.0.0 only inside a container (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT") or 8080),
        help="port to listen on (default: the PORT variable, else 8080)",
    )
    args = parser.parse_args()
    with closing(connect()):
        pass  # create the tables so that /healthz is honest about the database
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Tiny Tasks listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
