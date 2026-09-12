"""The local HTTP server behind the dashboard.

Stdlib only. Binds 127.0.0.1, serves ``static/`` and the JSON API from
``api.py``, and guards everything with a per-run token:

* ``GET /?token=X`` — the link the menu bar opens — sets an ``HttpOnly``
  cookie and redirects to ``/``. Browsers keep the URL fragment across a
  redirect whose Location has none, so ``/?token=X#stats`` lands on the
  stats tab.
* Every other request needs that cookie or ``Authorization: Bearer X``,
  except ``/api/ping``.
* ``Host`` must be loopback and ``Origin`` (when present) must be this
  server, so a web page in another tab cannot drive the API. No CORS headers
  are ever emitted.

The token and URL are also written to a discovery file (mode 0600) so the
CLI can find a running instance.
"""

from __future__ import annotations

import datetime as _dt
import errno
import hmac
import http.cookies
import importlib.resources
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

if TYPE_CHECKING:  # pragma: no cover
    from whisperlocal.web.api import AppContext, Request, Response

COOKIE_NAME = "wl_session"
MAX_BODY_BYTES = 1024 * 1024
DEFAULT_PORT = 47311

_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PARAM_VALUE = r"[A-Za-z0-9_.:-]+"

_MIME_OVERRIDES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}


# ─── Config and discovery ────────────────────────────────────────────────────────


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))


def default_discovery_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "WhisperLocal" / "web.json"


def read_discovery(path: Path | None = None) -> dict | None:
    """The running server's ``{url, token, pid, started_at}``, or None."""
    path = path or default_discovery_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "url" not in data or "token" not in data:
        return None
    return data


def ping(url: str, timeout: float = 1.0) -> bool:
    """True if a WhisperLocal server answers at ``url`` (no token needed)."""
    import urllib.request

    if not url:
        return False
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/ping", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("app") == "whisperlocal"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


# ─── Router ──────────────────────────────────────────────────────────────────────

Handler = Callable[["Request"], Any]


@dataclass
class _Route:
    method: str
    pattern: str
    regex: re.Pattern[str]
    handler: Handler
    literals: int  # literal segments; more specific routes are tried first


class Router:
    """Method + path pattern -> handler. ``{name}`` captures one segment."""

    def __init__(self) -> None:
        self._routes: list[_Route] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        segments = [s for s in pattern.split("/") if s]
        literals = sum(1 for s in segments if not _PARAM_RE.fullmatch(s))
        pieces = []
        for seg in segments:
            m = _PARAM_RE.fullmatch(seg)
            pieces.append(f"(?P<{m.group(1)}>{_PARAM_VALUE})" if m else re.escape(seg))
        regex = "^/" + "/".join(pieces) + "$"
        self._routes.append(_Route(method.upper(), pattern, re.compile(regex), handler, literals))
        # Literal routes before parameterised ones, registration order otherwise.
        self._routes.sort(key=lambda r: -r.literals)

    def match(self, method: str, path: str) -> tuple[Handler, dict[str, str]] | None:
        method = method.upper()
        for route in self._routes:
            m = route.regex.match(path)
            if m and route.method == method:
                return route.handler, {k: unquote(v) for k, v in m.groupdict().items()}
        return None

    def allowed_methods(self, path: str) -> list[str]:
        return sorted({r.method for r in self._routes if r.regex.match(path)})

    def method_not_allowed(self, path: str) -> bool:
        """True when the path exists but not for the requested method."""
        return bool(self.allowed_methods(path))

    def routes(self) -> list[tuple[str, str]]:
        return [(r.method, r.pattern) for r in self._routes]


# ─── Server ──────────────────────────────────────────────────────────────────────


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], handler: type, *, ctx: AppContext, router: Router, token: str) -> None:
        self.ctx = ctx
        self.router = router
        self.token = token
        super().__init__(addr, handler)


class WebServer:
    def __init__(self, ctx: AppContext, config: WebConfig, *, discovery_path: Path | None = None) -> None:
        from whisperlocal.web.api import build_router

        self.ctx = ctx
        self.config = config
        self.discovery_path = discovery_path or default_discovery_path()
        self.router = build_router(ctx)
        self._httpd: _HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> str:
        if self._httpd is not None:
            return self.base_url
        try:
            httpd = self._bind(self.config.port)
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES) or self.config.port == 0:
                raise
            print(
                f"Port {self.config.port} is in use; picking a free one instead.",
                file=sys.stderr,
            )
            httpd = self._bind(0)
        self._httpd = httpd
        self._write_discovery()
        # A short poll interval keeps stop() snappy; it costs nothing while idle.
        self._thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, name="whisperlocal-web", daemon=True
        )
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            finally:
                httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            self.discovery_path.unlink()
        except OSError:
            pass

    def _bind(self, port: int) -> _HTTPServer:
        return _HTTPServer(
            (self.config.host, port),
            _RequestHandler,
            ctx=self.ctx,
            router=self.router,
            token=self.config.token,
        )

    def _write_discovery(self) -> None:
        payload = {
            "url": self.base_url,
            "token": self.config.token,
            "pid": os.getpid(),
            "started_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        try:
            _write_private(self.discovery_path, json.dumps(payload, indent=2) + "\n")
        except OSError as exc:
            print(f"Warning: could not write {self.discovery_path}: {exc}", file=sys.stderr)

    # ── addressing ───────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self.config.port
        return int(self._httpd.server_address[1])

    @property
    def token(self) -> str:
        return self.config.token

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, fragment: str = "") -> str:
        """The bootstrap link to open in a browser, e.g. ``url("stats")``."""
        out = f"{self.base_url}/?token={self.config.token}"
        fragment = fragment.lstrip("#")
        if fragment:
            out += f"#{fragment}"
        return out


# ─── Request handler ─────────────────────────────────────────────────────────────


class _RequestHandler(BaseHTTPRequestHandler):
    server: _HTTPServer
    server_version = "WhisperLocal"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    # ── entry points ─────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_OPTIONS(self) -> None:
        # No CORS preflight is ever answered: the API is same-origin only.
        self._send_json(405, {"error": "method not allowed"})

    # ── logging ──────────────────────────────────────────────────────────────

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Quiet by default; _log() prints the failures.
        pass

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        try:
            status = int(code)
        except (TypeError, ValueError):
            return
        if status >= 400:
            print(f"web: {status} {self.command} {self.path}", file=sys.stderr)

    # ── dispatch ─────────────────────────────────────────────────────────────

    def _handle(self, method: str) -> None:
        try:
            self._dispatch(method)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = unquote(parts.path) or "/"
        query = parse_qs(parts.query, keep_blank_values=True)

        if not self._host_ok():
            self._send_json(403, {"error": "forbidden host"})
            return
        if not self._origin_ok():
            self._send_json(403, {"error": "forbidden origin"})
            return

        if path == "/api/ping":
            self._route(method, path, query)
            return

        if path in ("/", "/index.html"):
            self._bootstrap(method, query)
            return

        if path.startswith("/api/"):
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._route(method, path, query)
            return

        if path.startswith("/static/"):
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            if method not in ("GET", "HEAD"):
                self._send_json(405, {"error": "method not allowed"})
                return
            self._static(path[len("/static/"):])
            return

        self._send_json(404, {"error": "not found"})

    def _bootstrap(self, method: str, query: dict[str, list[str]]) -> None:
        if method not in ("GET", "HEAD"):
            self._send_json(405, {"error": "method not allowed"})
            return
        tokens = query.get("token")
        if tokens:
            if self._token_ok(tokens[0]):
                cookie = f"{COOKIE_NAME}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
                self._send(302, b"", "text/plain; charset=utf-8", {"Location": "/", "Set-Cookie": cookie})
            else:
                self._send(403, b"Forbidden: bad token.\n", "text/plain; charset=utf-8")
            return
        if self._authorized():
            self._static("index.html")
            return
        body = (
            "<!doctype html><meta charset='utf-8'><title>WhisperLocal</title>"
            "<p style='font: 15px -apple-system, system-ui, sans-serif; margin: 3em'>"
            "Open the dashboard from the WhisperLocal menu bar icon.</p>\n"
        ).encode("utf-8")
        self._send(401, body, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _route(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        from whisperlocal.web import api

        router = self.server.router
        matched = router.match("GET" if method == "HEAD" else method, path)
        if matched is None:
            if router.method_not_allowed(path):
                self._send_json(405, {"error": "method not allowed"}, {"Allow": ", ".join(router.allowed_methods(path))})
            else:
                self._send_json(404, {"error": "not found"})
            return
        handler, params = matched

        try:
            body = self._read_json_body()
        except _BodyTooLarge:
            self.close_connection = True
            self._send_json(413, {"error": f"request body larger than {MAX_BODY_BYTES} bytes"})
            return
        except ValueError as exc:
            self._send_json(400, {"error": f"invalid JSON body: {exc}"})
            return

        request = api.Request(method=method, path=path, query=query, params=params, body=body, headers=self.headers)
        try:
            result = handler(request)
        except api.NotSupported as exc:
            self._send_json(503, {"error": str(exc) or "not supported"})
            return
        except api.HTTPError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        except (KeyError, ValueError) as exc:
            self._send_json(400, {"error": _message(exc)})
            return
        except Exception as exc:  # noqa: BLE001 — anything else is a 500
            traceback.print_exc()
            self._send_json(500, {"error": str(exc) or exc.__class__.__name__})
            return

        if isinstance(result, api.Response):
            self._send_response(result)
        else:
            self._send_json(200, result)

    # ── auth ─────────────────────────────────────────────────────────────────

    def _token_ok(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(candidate.encode("utf-8"), self.server.token.encode("utf-8"))

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            if self._token_ok(auth[7:].strip()):
                return True
        raw = self.headers.get("Cookie")
        if raw:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(raw)
            except http.cookies.CookieError:
                return False
            morsel = jar.get(COOKIE_NAME)
            if morsel is not None and self._token_ok(morsel.value):
                return True
        return False

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        port = str(self.server.server_address[1])
        allowed = {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
            "127.0.0.1",
            "localhost",
        }
        return host in allowed

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        origin = origin.strip().lower()
        port = str(self.server.server_address[1])
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    # ── bodies ───────────────────────────────────────────────────────────────

    def _read_json_body(self) -> dict | list | None:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return None
        try:
            length = int(raw_len)
        except ValueError:
            raise ValueError("bad Content-Length") from None
        if length <= 0:
            return None
        if length > MAX_BODY_BYTES:
            raise _BodyTooLarge()
        data = self.rfile.read(length)
        if not data.strip():
            return None
        return json.loads(data.decode("utf-8"))

    # ── static files ─────────────────────────────────────────────────────────

    def _static(self, rel: str) -> None:
        parts = rel.split("/")
        if not parts or any(p in ("", ".", "..") or "\\" in p or "\0" in p for p in parts):
            self._send_json(404, {"error": "not found"})
            return
        try:
            resource = importlib.resources.files("whisperlocal.web").joinpath("static").joinpath(*parts)
            if not resource.is_file():
                raise FileNotFoundError(rel)
            data = resource.read_bytes()
        except (OSError, TypeError, ValueError):
            self._send_json(404, {"error": "not found"})
            return
        ext = os.path.splitext(parts[-1])[1].lower()
        ctype = _MIME_OVERRIDES.get(ext) or mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype, {"Cache-Control": "no-cache"})

    # ── output ───────────────────────────────────────────────────────────────

    def _send_json(self, status: int, payload: object, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        extra = {"Cache-Control": "no-store"}
        if headers:
            extra.update(headers)
        self._send(status, data, "application/json; charset=utf-8", extra)

    def _send_response(self, response: Response) -> None:
        body = response.body
        ctype = response.content_type
        if body is None:
            data = b""
        elif isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode("utf-8")
            if ctype.startswith("text/") and "charset" not in ctype:
                ctype += "; charset=utf-8"
        else:
            data = json.dumps(body, ensure_ascii=False, default=_json_default).encode("utf-8")
            if ctype == "application/json":
                ctype = "application/json; charset=utf-8"
        headers = {"Cache-Control": "no-store"}
        headers.update(response.headers)
        self._send(response.status, data, ctype, headers)

    def _send(self, status: int, data: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and data:
            self.wfile.write(data)


class _BodyTooLarge(Exception):
    pass


def _message(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return f"missing {exc.args[0]!r}"
    return str(exc) or exc.__class__.__name__


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    raise TypeError(f"{value.__class__.__name__} is not JSON serialisable")


__all__ = [
    "COOKIE_NAME",
    "DEFAULT_PORT",
    "MAX_BODY_BYTES",
    "Router",
    "WebConfig",
    "WebServer",
    "default_discovery_path",
    "ping",
    "read_discovery",
]
