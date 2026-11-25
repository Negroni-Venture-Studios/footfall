"""A probe that runs inside a container the runner started, as an app copy does.

The isolation tests copy this folder into a run's workspace and ask the runner to start it
with `start_app`, so it runs with exactly the settings an app gets from `runner/spec.py`. It
uses Python's standard library alone, since the app image has nothing else. Each check
prints one line, `FOOTFALL-PROBE <json>`, which the test reads back through the runner's
logs; the servers print `FOOTFALL-READY <json>` once they listen, and a line for each thing
they see.

    python3 probes/probe.py <check> [arguments...]

It never prints an environment variable's value, and is never given a secret.
"""

import asyncio
import base64
import contextlib
import ctypes
import ctypes.util
import errno
import http.server
import json
import os
import resource
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

MARK: Final = "FOOTFALL-PROBE"
READY: Final = "FOOTFALL-READY"
HELLO: Final = "FOOTFALL-HELLO"
HIT: Final = "FOOTFALL-HIT"
WORKSPACE: Final = Path("/workspace")
CONNECT_TIMEOUT: Final = 3.0
MIB: Final = 1024 * 1024


def say(tag: str, data: object) -> None:
    print(f"{tag} {json.dumps(data, sort_keys=True)}", flush=True)


def errno_name(error: OSError) -> str:
    if isinstance(error, TimeoutError) or error.errno is None:
        return "timeout" if isinstance(error, TimeoutError) else type(error).__name__
    return errno.errorcode.get(error.errno, str(error.errno))


# ---------------------------------------------------------------- the sandbox


def _status_fields() -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        name, _, value = line.partition(":")
        if name.startswith("Cap") or name in ("NoNewPrivs", "Seccomp", "Uid", "Gid", "Groups"):
            fields[name] = value.strip()
    return fields


def _kernel_log() -> dict[str, str]:
    """The kernel's log, as `dmesg` and the syslog call give it, with any failure."""
    found: dict[str, str] = {}
    try:
        done = subprocess.run(
            ["dmesg"],  # noqa: S607  # whichever the image has
            capture_output=True,
            timeout=10,
            check=False,
            text=True,
            errors="replace",
        )
        found["dmesg"] = done.stdout[:4000]
        found["dmesg_error"] = done.stderr[:400]
    except (OSError, subprocess.TimeoutExpired) as error:
        found["dmesg_error"] = type(error).__name__
    name = ctypes.util.find_library("c") or "libc.so.6"
    try:
        libc = ctypes.CDLL(name, use_errno=True)
        buffer = ctypes.create_string_buffer(65536)
        size = libc.klogctl(3, buffer, len(buffer))  # SYSLOG_ACTION_READ_ALL
        if size < 0:
            found["klogctl_error"] = errno.errorcode.get(ctypes.get_errno(), "unknown")
        else:
            found["klogctl"] = buffer.raw[:size].decode("utf-8", "replace")[:4000]
    except (OSError, AttributeError) as error:
        found["klogctl_error"] = type(error).__name__
    return found


def _try_create(folder: str) -> str:
    target = Path(folder) / f"footfall-probe-{secrets.token_hex(4)}"
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        return errno_name(error)
    os.close(descriptor)
    target.unlink()
    return "created"


def _ipv6() -> dict[str, Any]:
    """What IPv6 the container has: /proc's list of addresses, whether a socket on ::1 works
    (the control), and the source address each other interface would use for its own link."""
    found: dict[str, Any] = {}
    try:
        found["if_inet6"] = Path("/proc/net/if_inet6").read_text().splitlines()
    except OSError as error:
        found["if_inet6"] = errno_name(error)
    try:
        with socket.create_server(("::1", 0), family=socket.AF_INET6) as server:
            port = server.getsockname()[1]
            with socket.create_connection(("::1", port), timeout=CONNECT_TIMEOUT):
                found["loopback"] = "connected"
    except OSError as error:
        found["loopback"] = errno_name(error)
    found["links"] = {}
    try:
        interfaces = socket.if_nameindex()
    except OSError as error:
        found["links_error"] = errno_name(error)
        interfaces = []
    for index, name in interfaces:
        if name == "lo":
            continue
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as link:
            try:
                link.connect(("ff02::1", 9, 0, index))  # a datagram socket sends nothing
                found["links"][name] = str(link.getsockname()[0])
            except OSError as error:
                found["links"][name] = errno_name(error)
    return found


def check_sandbox(_: Sequence[str]) -> dict[str, Any]:
    root = os.statvfs("/")
    mounts = Path("/proc/self/mounts").read_text().splitlines()
    workspace = WORKSPACE.stat()
    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "euid": os.geteuid(),
        "groups": os.getgroups(),
        "status": _status_fields(),
        "proc_version": Path("/proc/version").read_text().strip(),
        "kernel_log": _kernel_log(),
        "root_read_only": bool(root.f_flag & os.ST_RDONLY),
        "root_mount": [line for line in mounts if line.split()[1:2] == ["/"]],
        "create": {
            folder: _try_create(folder)
            for folder in ("/", "/etc", "/usr/local/bin", "/var/tmp", "/tmp", str(WORKSPACE))  # noqa: S108
        },
        "workspace": {"uid": workspace.st_uid, "gid": workspace.st_gid},
        "workspace_files": len(list(WORKSPACE.rglob("*"))),
        "not_the_apps": sorted(
            str(path.relative_to(WORKSPACE))
            for path in WORKSPACE.rglob("*")
            if (path.lstat().st_uid, path.lstat().st_gid) != (10010, 10010)
        ),
        "paths": {
            path: os.path.lexists(path)  # a dangling link counts as there
            for path in (
                "/var/run/docker.sock",
                "/run/docker.sock",
                "/run/footfall/runner.sock",
                "/run/footfall-proxy/proxy.sock",
                "/var/lib/footfall",
                "/repo",
            )
        },
        "environment": sorted(os.environ),
        "proxy": os.environ.get("HTTPS_PROXY", ""),
        "hostname": socket.gethostname(),
        "nofile": resource.getrlimit(resource.RLIMIT_NOFILE),
        "ipv6": _ipv6(),
    }


# ---------------------------------------------------------------- memory and processes


def check_memory(arguments: Sequence[str]) -> dict[str, Any]:
    """Allocate and touch each size in MiB in a child of its own, and say how each ended."""
    results: dict[str, Any] = {}
    for text in arguments:
        size = int(text)
        script = (
            "import sys\n"
            f"block = bytearray({size} * {MIB})\n"
            f"for i in range(0, len(block), 4096): block[i] = 1\n"
            f"print('held {size}', flush=True)\n"
        )
        say(MARK + "-STEP", {"allocating": size})
        try:
            done = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                timeout=120,
                check=False,
                text=True,
            )
        except subprocess.TimeoutExpired:
            results[text] = {"outcome": "timeout"}
            continue
        held = f"held {size}" in done.stdout
        outcome: dict[str, Any] = {"returncode": done.returncode, "held": held}
        if done.returncode < 0:
            outcome["signal"] = signal.Signals(-done.returncode).name
        if "MemoryError" in done.stderr:
            outcome["memory_error"] = True
        results[text] = outcome
        say(MARK + "-STEP", {"allocated": size, **outcome})
    return results


def check_processes(arguments: Sequence[str]) -> dict[str, Any]:
    """Start processes until the kernel refuses one, or `most` of them, then end them all."""
    most = int(arguments[0])
    children: list[subprocess.Popen[bytes]] = []
    refused: str | None = None
    try:
        while len(children) < most:
            try:
                children.append(subprocess.Popen(["sleep", "300"]))  # noqa: S607
            except OSError as error:
                refused = errno_name(error)
                break
    finally:
        for child in children:
            with contextlib.suppress(OSError):
                child.kill()
        for child in children:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                child.wait(timeout=10)
    return {"started": len(children), "refused": refused, "most": most}


# ---------------------------------------------------------------- the network


def _connect(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "connected"
    except OSError as error:
        return errno_name(error)


def check_connect(arguments: Sequence[str]) -> dict[str, Any]:
    """Connect directly, with no proxy, to each host:port, and resolve each host name."""
    results: dict[str, Any] = {}
    for target in arguments:
        host, _, port = target.rpartition(":")
        results[target] = _connect(host, int(port))
    return results


def check_resolve(arguments: Sequence[str]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name in arguments:
        try:
            found = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
            results[name] = sorted({str(item[4][0]) for item in found})
        except OSError as error:
            results[name] = errno_name(error)
    return results


async def _scan_one(host: str, port: int, timeout: float, limit: asyncio.Semaphore) -> str:
    async with limit:
        try:
            async with asyncio.timeout(timeout):
                _, writer = await asyncio.open_connection(host, port)
        except TimeoutError:
            return "timeout"
        except OSError as error:
            return errno_name(error)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return "open"


async def _scan(host: str, first: int, last: int, timeout: float) -> dict[str, Any]:
    limit = asyncio.Semaphore(256)
    ports = range(first, last + 1)
    outcomes = await asyncio.gather(*(_scan_one(host, port, timeout, limit) for port in ports))
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    open_ports = [port for port, outcome in zip(ports, outcomes, strict=True) if outcome == "open"]
    return {"open": open_ports, "counts": counts}


def check_scan(arguments: Sequence[str]) -> dict[str, Any]:
    """Try every port in each host:first-last range, with a short timeout for each."""
    results: dict[str, Any] = {}
    for target in arguments:
        host, _, ports = target.rpartition(":")
        first, _, last = ports.partition("-")
        started = time.monotonic()
        results[target] = asyncio.run(_scan(host, int(first), int(last or first), 1.0))
        results[target]["seconds"] = round(time.monotonic() - started, 1)
    return results


def _proxy() -> tuple[str, int]:
    parts = urllib.parse.urlsplit(os.environ["HTTPS_PROXY"])
    return parts.hostname or "", parts.port or 3128


def _status(line: bytes) -> int | None:
    parts = line.split()
    if len(parts) >= 2 and parts[0].startswith(b"HTTP/") and parts[1].isdigit():
        return int(parts[1])
    return None


def _read_head(stream: socket.socket) -> bytes:
    head = b""
    while b"\r\n\r\n" not in head and len(head) < 65536:
        chunk = stream.recv(4096)
        if not chunk:
            break
        head += chunk
    return head


def fetch(url: str) -> dict[str, Any]:
    """Fetch a URL through the container's egress proxy, as an app's HTTP client would.

    Returns the proxy's own status for a CONNECT, and the server's status for the request.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    proxy_host, proxy_port = _proxy()
    result: dict[str, Any] = {"url": url, "proxy": f"{proxy_host}:{proxy_port}"}
    try:
        with socket.create_connection((proxy_host, proxy_port), timeout=20) as stream:
            stream.settimeout(20)
            if parts.scheme == "https":
                port = parts.port or 443
                stream.sendall(
                    f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
                )
                head = _read_head(stream)
                result["proxy_status"] = _status(head.split(b"\r\n", 1)[0])
                result["proxy_reason"] = head.split(b"\r\n\r\n", 1)[-1][:200].decode(
                    "utf-8", "replace"
                )
                if result["proxy_status"] != 200:
                    return result
                context = ssl.create_default_context()
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                with context.wrap_socket(stream, server_hostname=host) as secure:
                    path = parts.path or "/"
                    secure.sendall(
                        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: footfall-probe\r\n"
                        "Connection: close\r\n\r\n".encode()
                    )
                    result["status"] = _status(_read_head(secure).split(b"\r\n", 1)[0])
            else:
                stream.sendall(
                    f"GET {url} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: footfall-probe\r\n"
                    "Connection: close\r\n\r\n".encode()
                )
                head = _read_head(stream)
                result["status"] = _status(head.split(b"\r\n", 1)[0])
                result["proxy_reason"] = head.split(b"\r\n\r\n", 1)[-1][:200].decode(
                    "utf-8", "replace"
                )
    except (OSError, ssl.SSLError) as error:
        result["error"] = errno_name(error) if isinstance(error, OSError) else str(error)
    return result


def check_fetch(arguments: Sequence[str]) -> dict[str, Any]:
    return {url: fetch(url) for url in arguments}


def check_fetch_to_file(arguments: Sequence[str]) -> dict[str, Any]:
    """Fetch through the proxy, as install does, and keep the result in the workspace."""
    name, urls = arguments[0], arguments[1:]
    result = check_fetch(urls)
    (WORKSPACE / name).write_text(json.dumps(result))
    return result


def check_raw(arguments: Sequence[str]) -> dict[str, Any]:
    """Send each named request to host:port and read whatever comes back, until it closes."""
    host, _, port = arguments[0].rpartition(":")
    key = base64.b64encode(secrets.token_bytes(16))
    upgrade = (
        b"Connection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
        b"Sec-WebSocket-Key: " + key + b"\r\n\r\n"
    )
    wrong = b"/" + secrets.token_urlsafe(24).encode()
    requests = {
        "json": b"GET /json HTTP/1.1\r\nHost: browsers\r\n\r\n",
        "root": b"GET / HTTP/1.1\r\nHost: browsers\r\n\r\n",
        "json-upgrade": b"GET /json HTTP/1.1\r\nHost: browsers\r\n" + upgrade,
        "wrong-path-upgrade": b"GET " + wrong + b" HTTP/1.1\r\nHost: browsers\r\n" + upgrade,
        "root-upgrade": b"GET / HTTP/1.1\r\nHost: browsers\r\n" + upgrade,
        "http10": b"GET /json HTTP/1.0\r\n\r\n",
    }
    results: dict[str, Any] = {}
    for name, request in requests.items():
        try:
            with socket.create_connection((host, int(port)), timeout=10) as stream:
                stream.settimeout(10)
                stream.sendall(request)
                answer = b""
                while len(answer) < 65536:
                    chunk = stream.recv(4096)
                    if not chunk:
                        break
                    answer += chunk
        except OSError as error:
            results[name] = {"error": errno_name(error)}
            continue
        head, _, body = answer.partition(b"\r\n\r\n")
        results[name] = {
            "status_line": head.split(b"\r\n", 1)[0].decode("latin-1"),
            "head": head.decode("latin-1")[:1000],
            "body_bytes": len(body),
            "body": body[:200].decode("latin-1"),
            "bytes": len(answer),
        }
    return results


# ---------------------------------------------------------------- files


def check_write(arguments: Sequence[str]) -> dict[str, Any]:
    """Write a value to a file in the workspace, as a seed step does."""
    name, value = arguments
    (WORKSPACE / name).write_text(value)
    return {"wrote": name}


def check_read(arguments: Sequence[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name in arguments:
        try:
            found[name] = (WORKSPACE / name).read_text()
        except OSError as error:
            found[name] = {"error": errno_name(error)}
    return found


def check_fill(arguments: Sequence[str]) -> dict[str, Any]:
    """Write up to `most` MiB of real data to the workspace, a MiB at a time, then wait."""
    most = int(arguments[0])
    block = os.urandom(MIB)
    with (WORKSPACE / "fill.bin").open("wb") as out:
        for written in range(1, most + 1):
            out.write(block)
            if written % 16 == 0:
                out.flush()
                os.fsync(out.fileno())
                say(MARK + "-STEP", {"written_mib": written})
    time.sleep(600)
    return {"written_mib": most}


def check_sparse(arguments: Sequence[str]) -> dict[str, Any]:
    """Make a sparse file of `size` MiB, which writes almost nothing to the disk."""
    size = int(arguments[0])
    with (WORKSPACE / "sparse.bin").open("wb") as out:
        out.truncate(size * MIB)
    return {"sparse_mib": size}


# ---------------------------------------------------------------- servers


# Where `/redirect?to=<name>` sends the browser: a host on the browser's list, one off it,
# and Playwright's run-server on the browser container's own loopback.
REDIRECTS: Final = {
    "listed": "https://example.com/",
    "unlisted": "https://pypi.org/simple/",
    "run-server": "http://127.0.0.1:3001/json",
}
_PAGES: Final[dict[str, str]] = {
    "/": "<!doctype html><title>Probe</title><h1>Probe</h1>",
    # Each `beacon` gets a sendBeacon and each `keepalive` a keepalive fetch, both sent as
    # the page goes: Chromium's route handlers never see them, while WebKit routes them.
    "/leaving": (
        "<!doctype html><title>Leaving</title><h1>Leaving</h1><script>\n"
        "const params = new URLSearchParams(location.search);\n"
        "addEventListener('pagehide', () => {\n"
        "  for (const url of params.getAll('beacon')) navigator.sendBeacon(url, 'gone');\n"
        "  for (const url of params.getAll('keepalive')) fetch(url, {method: 'POST',"
        " body: 'gone', keepalive: true, mode: 'no-cors'});\n"
        "});\n</script>"
    ),
    "/asset": (
        "<!doctype html><title>Asset</title><h1>Asset</h1><script>\n"
        "const s = document.createElement('script');\n"
        "s.src = new URLSearchParams(location.search).get('src');\n"
        "document.head.append(s);\n</script>"
    ),
    "/frame": (
        "<!doctype html><title>Frame</title><h1>Frame</h1><script>\n"
        "const f = document.createElement('iframe');\n"
        "f.src = new URLSearchParams(location.search).get('src');\n"
        "document.body.append(f);\n</script>"
    ),
}


class _Pages(http.server.BaseHTTPRequestHandler):
    """The browser tests' app: a few pages, a redirect to anywhere, and a line per request."""

    def _answer(self) -> None:
        say(HIT, {"method": self.command, "path": self.path, "host": self.headers.get("Host")})
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/redirect":
            # A fixed set of places, so that nothing in a request reaches a header.
            target = REDIRECTS.get(urllib.parse.parse_qs(parts.query).get("to", [""])[0], "/")
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        page = _PAGES.get(parts.path)
        body = (page or "not found").encode()
        self.send_response(200 if page else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_HEAD = _answer

    def log_message(self, format: str, *args: object) -> None:
        return  # each request is reported as a FOOTFALL-HIT line instead


def serve_pages(arguments: Sequence[str]) -> None:
    port = int(arguments[0])
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Pages)  # noqa: S104
    say(READY, {"port": port, "hostname": socket.gethostname()})
    server.serve_forever()


def _extensions(record: bytes) -> tuple[list[int], str | None]:
    """The extension types of the ClientHello in one TLS record, and its server name."""
    body = record[5:]
    if record[:1] != b"\x16" or body[:1] != b"\x01":
        raise ValueError("not a ClientHello")
    at = 4 + 2 + 32  # handshake header, version, random
    at += 1 + body[at]  # session id
    at += 2 + int.from_bytes(body[at : at + 2], "big")  # cipher suites
    at += 1 + body[at]  # compression methods
    end = at + 2 + int.from_bytes(body[at : at + 2], "big")
    at += 2
    types: list[int] = []
    name: str | None = None
    while at + 4 <= end:
        kind = int.from_bytes(body[at : at + 2], "big")
        size = int.from_bytes(body[at + 2 : at + 4], "big")
        data = body[at + 4 : at + 4 + size]
        types.append(kind)
        if kind == 0 and len(data) > 5:
            length = int.from_bytes(data[3:5], "big")
            name = data[5 : 5 + length].decode("ascii", "replace")
        at += 4 + size
    return types, name


def _hello(connection: socket.socket) -> None:
    with connection:
        connection.settimeout(10)
        try:
            header = connection.recv(5, socket.MSG_WAITALL)
            length = int.from_bytes(header[3:5], "big")
            record = header + connection.recv(length, socket.MSG_WAITALL)
            types, name = _extensions(record)
        except (OSError, ValueError, IndexError) as error:
            say(HELLO, {"error": type(error).__name__})
            return
        say(HELLO, {"extensions": types, "server_name": name})


def serve_hello(arguments: Sequence[str]) -> None:
    """Accept TLS connections, report each ClientHello's extensions, and close them."""
    port = int(arguments[0])
    listener = socket.create_server(("0.0.0.0", port))  # noqa: S104
    say(READY, {"port": port, "hostname": socket.gethostname()})
    while True:
        connection, peer = listener.accept()
        say(HIT, {"accepted": peer[0]})
        threading.Thread(target=_hello, args=(connection,), daemon=True).start()


CHECKS: Final[dict[str, Callable[[Sequence[str]], dict[str, Any]]]] = {
    "sandbox": check_sandbox,
    "memory": check_memory,
    "processes": check_processes,
    "connect": check_connect,
    "resolve": check_resolve,
    "scan": check_scan,
    "fetch": check_fetch,
    "fetch-to-file": check_fetch_to_file,
    "raw": check_raw,
    "write": check_write,
    "read": check_read,
    "fill": check_fill,
    "sparse": check_sparse,
}
SERVERS: Final[dict[str, Callable[[Sequence[str]], None]]] = {
    "pages": serve_pages,
    "hello": serve_hello,
}


def main(argv: Sequence[str]) -> int:
    name, arguments = argv[0], argv[1:]
    if name in SERVERS:
        SERVERS[name](arguments)
        return 0
    say(MARK, {"check": name, "result": CHECKS[name](arguments)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
