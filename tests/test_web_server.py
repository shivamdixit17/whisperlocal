"""The local web server: auth, origin checks, routing and the API shapes.

Runs against a stub AppContext so nothing here needs the menu bar app, the
settings manager, or the analytics module. Requests go through http.client so
the tests can send exact headers and odd paths without urllib normalising them.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from whisperlocal import __version__
from whisperlocal import config as cfg
from whisperlocal.web import api as web_api
from whisperlocal.web.server import COOKIE_NAME, Router, WebConfig, WebServer, ping, read_discovery

# ─── Stubs ───────────────────────────────────────────────────────────────────────

try:  # the real thing, if the neighbouring engineer has landed it
    from whisperlocal.settings_manager import EnvOverrideError
except ImportError:  # pragma: no cover - depends on checkout state

    class EnvOverrideError(cfg.ConfigError):
        pass


class FakeSettingsManager:
    def __init__(self, settings: cfg.Settings | None = None) -> None:
        self.current = settings or cfg.Settings()
        self.applied: list[dict] = []

    def preview(self, changes):
        return self.current, {k: "live" for k in changes}

    def apply(self, changes, persist=True):
        if "bad" in changes:
            raise cfg.ConfigError("bad: unknown setting")
        if "model" in changes and changes["model"] == "env-locked":
            raise EnvOverrideError("model: set by WHISPERLOCAL_MODEL; edit the environment instead")
        self.applied.append(dict(changes))
        return SimpleNamespace(
            settings=self.current,
            changed=sorted(changes),
            applied={"live": sorted(changes), "listeners": [], "restart": []},
            restart_required=False,
            warnings=["a warning"],
            persisted=persist,
        )


class FakeMeetings:
    def __init__(self) -> None:
        self.items = {"m1": {"id": "m1", "title": "Standup", "transcript": []}}
        self.deleted: list[str] = []

    def list(self, limit=50, offset=0, days=None, query=None):
        items = list(self.items.values())
        return {"total": len(items), "items": items[offset : offset + limit], "days": days, "query": query}

    def get(self, meeting_id):
        return self.items.get(meeting_id)

    def search(self, query, limit=50):
        return []

    def export(self, meeting_id, fmt):
        return f"standup.{fmt}", b"# Standup\n", {"md": "text/markdown", "txt": "text/plain", "json": "application/json"}[fmt]

    def delete(self, meeting_id):
        self.deleted.append(meeting_id)
        return self.items.pop(meeting_id, None) is not None

    def stats(self, days=None):
        return {"count": len(self.items), "days": days}


class FakeRecorder:
    def __init__(self) -> None:
        self.state = {"recording": False}

    def start(self, trigger="manual", title=None):
        self.state = {"recording": True, "trigger": trigger, "title": title}
        return dict(self.state)

    def stop(self):
        self.state = {"recording": False}
        return dict(self.state)

    def status(self):
        return dict(self.state)


class StubContext:
    def __init__(self, history_path: Path, *, meetings=None, recorder=None, running=True) -> None:
        self.settings = FakeSettingsManager()
        self.version = __version__
        self.running = running
        self.history_path = history_path
        self.meetings = meetings
        self.recorder = recorder
        self._api_key: str | None = None
        self.restarts = 0
        self.capturing = False

    def status(self):
        return {"engine": "idle", "meeting": None}

    def start_capture(self):
        self.capturing = True
        return {"capturing": True}

    def capture_state(self):
        return {"capturing": self.capturing, "captured": None}

    def cancel_capture(self):
        self.capturing = False

    def request_restart(self):
        self.restarts += 1

    def api_key_set(self):
        return self._api_key is not None

    def set_api_key(self, key):
        self._api_key = key

    def clear_api_key(self):
        self._api_key = None


# ─── Fixtures and helpers ─────────────────────────────────────────────────────────


@pytest.fixture
def ctx(tmp_path):
    return StubContext(tmp_path / "history.jsonl")


@pytest.fixture
def server(ctx, tmp_path):
    srv = WebServer(ctx, WebConfig(port=0), discovery_path=tmp_path / "web.json")
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def request(server, method, path, *, headers=None, body=None, auth=True, host=None):
    """One HTTP request; returns (status, headers dict, body bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    hdrs = {}
    if auth:
        hdrs["Cookie"] = f"{COOKIE_NAME}={server.token}"
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
        hdrs.setdefault("Content-Type", "application/json")
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{server.port}")
        for key, value in hdrs.items():
            conn.putheader(key, value)
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def get_json(server, path, **kw):
    status, headers, body = request(server, "GET", path, **kw)
    return status, headers, json.loads(body) if body else None


# ─── Router ──────────────────────────────────────────────────────────────────────


def test_router_prefers_literal_segments_and_reports_405():
    router = Router()
    router.add("GET", "/api/meetings/{id}", lambda r: "item")
    router.add("GET", "/api/meetings/stats", lambda r: "stats")
    router.add("DELETE", "/api/meetings/{id}", lambda r: "delete")

    handler, params = router.match("GET", "/api/meetings/stats")
    assert handler(None) == "stats" and params == {}
    handler, params = router.match("GET", "/api/meetings/abc-1.2:x")
    assert handler(None) == "item" and params == {"id": "abc-1.2:x"}
    assert router.match("GET", "/api/meetings/a/b") is None
    assert router.match("PUT", "/api/meetings/abc") is None
    assert router.method_not_allowed("/api/meetings/abc")
    assert not router.method_not_allowed("/nowhere")
    assert router.allowed_methods("/api/meetings/abc") == ["DELETE", "GET"]


# ─── Auth and the bootstrap link ─────────────────────────────────────────────────


def test_ping_is_open(server):
    status, headers, body = get_json(server, "/api/ping", auth=False)
    assert status == 200
    assert body == {"app": "whisperlocal", "version": __version__, "running": True}
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Server"] == "WhisperLocal"


def test_api_requires_auth(server):
    status, _, body = get_json(server, "/api/status", auth=False)
    assert status == 401
    assert body == {"error": "unauthorized"}
    status, _, body = get_json(server, "/api/status", auth=False, headers={"Cookie": f"{COOKIE_NAME}=wrong"})
    assert status == 401
    status, _, body = get_json(server, "/api/status", auth=False, headers={"Authorization": "Bearer nope"})
    assert status == 401


def test_token_link_sets_cookie_and_redirects(server):
    status, headers, body = request(server, "GET", f"/?token={server.token}", auth=False)
    assert status == 302
    assert headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}={server.token};")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie

    # The cookie it handed out then opens the API.
    status, _, body = get_json(server, "/api/settings", auth=False, headers={"Cookie": cookie.split(";")[0]})
    assert status == 200
    assert set(body) == {"values", "defaults", "sources", "schema", "triggers", "api_key_set", "config_path", "running"}


def test_wrong_token_is_403_and_no_cookie(server):
    status, headers, body = request(server, "GET", "/?token=wrong", auth=False)
    assert status == 403
    assert "Set-Cookie" not in headers
    assert headers["Content-Type"].startswith("text/plain")


def test_root_without_session_is_401_page(server):
    status, headers, body = request(server, "GET", "/", auth=False)
    assert status == 401
    assert headers["Content-Type"].startswith("text/html")
    assert b"Open the dashboard from the WhisperLocal menu bar icon." in body


def test_root_with_session_serves_index(server):
    status, headers, body = request(server, "GET", "/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == "no-cache"
    assert b"WhisperLocal" in body
    status, _, body2 = request(server, "GET", "/index.html")
    assert status == 200 and body2 == body


def test_bearer_token_works(server):
    status, _, body = get_json(server, "/api/status", auth=False, headers={"Authorization": f"Bearer {server.token}"})
    assert status == 200
    assert body == {"engine": "idle", "meeting": None, "version": __version__, "running": True}


# ─── Host and origin checks ───────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["evil.example:80", "127.0.0.1:1", "", "10.0.0.5"])
def test_wrong_host_is_403(server, host):
    status, headers, body = get_json(server, "/api/ping", auth=False, host=host)
    assert status == 403
    assert body == {"error": "forbidden host"}


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}", "127.0.0.1", "localhost", "LOCALHOST:{port}"])
def test_loopback_hosts_are_accepted(server, host):
    status, _, _ = get_json(server, "/api/ping", auth=False, host=host.format(port=server.port))
    assert status == 200


def test_foreign_origin_is_403(server):
    status, headers, body = get_json(server, "/api/status", headers={"Origin": "http://evil.example"})
    assert status == 403
    assert body == {"error": "forbidden origin"}
    status, _, _ = get_json(server, "/api/status", headers={"Origin": "null"})
    assert status == 403
    status, _, _ = get_json(server, "/api/status", headers={"Origin": f"http://127.0.0.1:{server.port}"})
    assert status == 200
    status, _, _ = get_json(server, "/api/status", headers={"Origin": f"http://localhost:{server.port}"})
    assert status == 200


def test_no_cors_headers_anywhere(server):
    for path, auth in [("/api/ping", False), ("/api/status", True), ("/", True), ("/static/index.html", True), ("/nope", True)]:
        _, headers, _ = request(server, "GET", path, auth=auth, headers={"Origin": f"http://127.0.0.1:{server.port}"})
        assert not any(k.lower().startswith("access-control-") for k in headers), (path, headers)
    status, headers, _ = request(server, "OPTIONS", "/api/status", headers={"Access-Control-Request-Method": "PUT"})
    assert status == 405
    assert not any(k.lower().startswith("access-control-") for k in headers)


# ─── Static files ────────────────────────────────────────────────────────────────


def test_static_serves_package_files_with_auth(server):
    status, headers, body = request(server, "GET", "/static/index.html")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == "no-cache"
    status, _, _ = request(server, "GET", "/static/index.html", auth=False)
    assert status == 401


@pytest.mark.parametrize(
    "path",
    ["/static/../../etc/passwd", "/static/..%2F..%2Fetc%2Fpasswd", "/static/./index.html", "/static//index.html", "/static/", "/static/missing.js"],
)
def test_static_escapes_are_404(server, path):
    status, _, body = request(server, "GET", path)
    assert status == 404, (path, body)


# ─── Settings ────────────────────────────────────────────────────────────────────


def test_get_settings_shape(server, ctx):
    status, _, body = get_json(server, "/api/settings")
    assert status == 200
    assert body["values"]["model"] == "base"
    assert body["values"]["trigger_keys"] == ["fn"]
    assert body["defaults"]["web_port"] == 47311
    assert body["sources"]["model"] in ("default", "file", "env")
    assert body["schema"][0]["key"] == "trigger"
    assert {t["token"] for t in body["triggers"]} == set(cfg.TRIGGER_KEYS)
    assert set(body["triggers"][0]) == {"token", "label", "risky", "kind"}
    assert body["api_key_set"] is False
    assert body["config_path"].endswith("config.toml")
    assert body["running"] is True


def test_put_settings_applies(server, ctx):
    status, _, body = request(server, "PUT", "/api/settings", body={"changes": {"sounds": False}})
    payload = json.loads(body)
    assert status == 200, payload
    assert set(payload) == {"settings", "changed", "applied", "restart_required", "warnings", "persisted"}
    assert payload["changed"] == ["sounds"]
    assert payload["applied"] == {"live": ["sounds"], "listeners": [], "restart": []}
    assert payload["persisted"] is True
    assert payload["warnings"] == ["a warning"]
    assert payload["settings"]["model"] == "base"
    assert ctx.settings.applied == [{"sounds": False}]


def test_put_settings_bad_key_is_400(server):
    status, _, body = request(server, "PUT", "/api/settings", body={"changes": {"bad": 1}})
    assert status == 400
    assert json.loads(body) == {"error": "bad: unknown setting"}


def test_put_settings_env_override_is_409(server):
    status, _, body = request(server, "PUT", "/api/settings", body={"changes": {"model": "env-locked"}})
    assert status == 409
    assert "WHISPERLOCAL_MODEL" in json.loads(body)["error"]


@pytest.mark.parametrize("body", [None, b"", b"not json", b"[]", b'{"changes": "x"}', b'{"nope": {}}'])
def test_put_settings_bad_body_is_400(server, body):
    status, _, raw = request(server, "PUT", "/api/settings", body=body)
    assert status == 400, raw
    assert "error" in json.loads(raw)


def test_oversized_body_is_413(server):
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    try:
        head = (
            f"PUT /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
            f"Cookie: {COOKIE_NAME}={server.token}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {2 * 1024 * 1024}\r\n\r\n"
        )
        sock.sendall(head.encode() + b'{"changes": {}}')
        resp = http.client.HTTPResponse(sock)
        resp.begin()
        assert resp.status == 413
        assert "error" in json.loads(resp.read())
    finally:
        sock.close()


def test_capture_key_routes(server, ctx):
    status, _, body = request(server, "POST", "/api/settings/capture-key")
    assert status == 200 and json.loads(body) == {"capturing": True}
    status, _, body = get_json(server, "/api/settings/capture-key")
    assert status == 200 and body == {"capturing": True, "captured": None}
    status, _, body = request(server, "DELETE", "/api/settings/capture-key")
    assert status == 200 and json.loads(body) == {"ok": True}
    assert ctx.capturing is False


def test_capture_key_not_supported_is_503(server, ctx):
    def unsupported():
        raise web_api.NotSupported("no key capture in the CLI server")

    ctx.start_capture = unsupported
    status, _, body = request(server, "POST", "/api/settings/capture-key")
    assert status == 503
    assert json.loads(body) == {"error": "no key capture in the CLI server"}


def test_api_key_routes(server, ctx):
    status, _, body = request(server, "POST", "/api/apikey", body={"key": "  sk-123  "})
    assert status == 200 and json.loads(body) == {"api_key_set": True}
    assert ctx._api_key == "sk-123"
    status, _, body = request(server, "POST", "/api/apikey", body={"key": "   "})
    assert status == 400
    status, _, body = request(server, "POST", "/api/apikey", body={})
    assert status == 400
    status, _, body = request(server, "DELETE", "/api/apikey")
    assert status == 200 and json.loads(body) == {"api_key_set": False}
    assert ctx._api_key is None


def test_restart_responds_then_schedules(server, ctx):
    status, _, body = request(server, "POST", "/api/restart")
    assert status == 200 and json.loads(body) == {"ok": True}
    assert ctx.restarts == 1


def test_unknown_api_route_404_and_wrong_method_405(server):
    status, _, body = get_json(server, "/api/nothing")
    assert status == 404 and body == {"error": "not found"}
    status, headers, body = request(server, "DELETE", "/api/status")
    assert status == 405
    assert headers["Allow"] == "GET"


def test_handler_exceptions_are_500_json(server, ctx, capsys):
    def boom():
        raise RuntimeError("engine exploded")

    ctx.status = boom
    status, _, body = get_json(server, "/api/status")
    assert status == 500
    assert body == {"error": "engine exploded"}
    assert "RuntimeError: engine exploded" in capsys.readouterr().err


def test_handler_value_error_is_400(server, ctx):
    def bad():
        raise ValueError("no such thing")

    ctx.capture_state = bad
    status, _, body = get_json(server, "/api/settings/capture-key")
    assert status == 400 and body == {"error": "no such thing"}


# ─── Stats and history (analytics stubbed unless the real module exists) ─────────


@pytest.fixture
def analytics(monkeypatch):
    try:
        import whisperlocal.analytics as real  # noqa: F401

        return None  # the real module is used
    except ImportError:
        pass
    mod = types.ModuleType("whisperlocal.analytics")

    def build_dashboard(entries, *, days, typing_wpm, now=None):
        return {"count": len(entries), "days": days, "typing_wpm": typing_wpm}

    def recent(entries, *, limit, offset, q, status, app):
        return {"total": len(entries), "items": entries[offset : offset + limit], "q": q, "status": status, "app": app}

    mod.build_dashboard = build_dashboard
    mod.recent = recent
    mod.filter_window = lambda entries, days: entries
    monkeypatch.setitem(sys.modules, "whisperlocal.analytics", mod)
    return mod


def test_stats_without_history_file(server, ctx, analytics):
    assert not ctx.history_path.exists()
    status, _, body = get_json(server, "/api/stats?days=30&typing_wpm=55")
    assert status == 200, body
    assert body["skipped"] == 0
    assert body["history_enabled"] is True and body["history_text"] is True
    if analytics is not None:
        assert body["count"] == 0 and body["days"] == 30 and body["typing_wpm"] == 55.0


def test_stats_and_history_read_entries(server, ctx, analytics):
    import datetime

    now = datetime.datetime.now().astimezone().isoformat()
    ctx.history_path.write_text(
        json.dumps({"timestamp": now, "status": "ok", "text": "hello", "app": "Notes"}) + "\n"
        + "this line is broken\n",
        encoding="utf-8",
    )
    status, _, body = get_json(server, "/api/stats?days=all", headers={})
    assert status == 200, body
    if analytics is not None:
        assert body["count"] == 1 and body["days"] is None
    status, _, body = get_json(server, "/api/history?limit=10&offset=0&q=hel")
    assert status == 200, body
    if analytics is not None:
        assert body["total"] == 1 and body["items"][0]["text"] == "hello" and body["q"] == "hel"
    status, _, body = get_json(server, "/api/history?limit=zero")
    assert status == 400 and "limit" in body["error"]
    status, _, body = get_json(server, "/api/stats?days=0")
    assert status == 400


# ─── Meetings ────────────────────────────────────────────────────────────────────


def test_meetings_503_without_store(server):
    for method, path in [("GET", "/api/meetings"), ("GET", "/api/meetings/stats"), ("GET", "/api/meetings/m1"), ("DELETE", "/api/meetings/m1"), ("GET", "/api/meetings/m1/export"), ("POST", "/api/meetings/start"), ("POST", "/api/meetings/stop")]:
        status, _, body = request(server, method, path)
        assert status == 503, (method, path, body)
        assert "error" in json.loads(body)


@pytest.fixture
def meeting_server(tmp_path):
    ctx = StubContext(tmp_path / "history.jsonl", meetings=FakeMeetings(), recorder=FakeRecorder())
    srv = WebServer(ctx, WebConfig(port=0), discovery_path=tmp_path / "web.json")
    srv.start()
    try:
        yield srv, ctx
    finally:
        srv.stop()


def test_meetings_routes(meeting_server):
    server, ctx = meeting_server
    status, _, body = get_json(server, "/api/meetings?limit=5&days=7&q=stand")
    assert status == 200 and body["total"] == 1 and body["items"][0]["id"] == "m1"
    assert body["days"] == 7 and body["query"] == "stand"

    status, _, body = get_json(server, "/api/meetings/stats?days=30")
    assert status == 200 and body == {"count": 1, "days": 30}

    status, _, body = get_json(server, "/api/meetings/m1")
    assert status == 200 and body["title"] == "Standup"
    status, _, body = get_json(server, "/api/meetings/nope")
    assert status == 404 and body == {"error": "meeting not found"}

    status, headers, raw = request(server, "GET", "/api/meetings/m1/export?fmt=md")
    assert status == 200 and raw == b"# Standup\n"
    assert headers["Content-Type"] == "text/markdown"
    assert headers["Content-Disposition"] == 'attachment; filename="standup.md"'
    status, _, raw = request(server, "GET", "/api/meetings/m1/export?fmt=pdf")
    assert status == 400
    status, _, raw = request(server, "GET", "/api/meetings/nope/export")
    assert status == 404

    status, _, body = request(server, "POST", "/api/meetings/start", body={"title": "Sync"})
    assert status == 200 and json.loads(body) == {"recording": True, "trigger": "manual", "title": "Sync"}
    status, _, body = get_json(server, "/api/meetings/recording")
    assert status == 200 and body["recording"] is True
    status, _, body = request(server, "POST", "/api/meetings/stop")
    assert status == 200 and json.loads(body) == {"recording": False}

    status, _, body = request(server, "DELETE", "/api/meetings/m1")
    assert status == 200 and json.loads(body) == {"ok": True, "id": "m1"}
    status, _, body = request(server, "DELETE", "/api/meetings/m1")
    assert status == 404
    assert ctx.meetings.deleted == ["m1", "m1"]


def test_meeting_id_rejects_slashes(meeting_server):
    server, _ = meeting_server
    status, _, _ = request(server, "GET", "/api/meetings/a/b")
    assert status == 404


# ─── Lifecycle, discovery file, url() ────────────────────────────────────────────


def test_discovery_file_and_url(ctx, tmp_path):
    path = tmp_path / "nested" / "web.json"
    srv = WebServer(ctx, WebConfig(port=0), discovery_path=path)
    base = srv.start()
    try:
        assert base == f"http://127.0.0.1:{srv.port}"
        assert srv.port > 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        data = json.loads(path.read_text())
        assert set(data) == {"url", "token", "pid", "started_at"}
        assert data["url"] == base and data["token"] == srv.token and data["pid"] == os.getpid()
        assert read_discovery(path) == data
        assert srv.url() == f"http://127.0.0.1:{srv.port}/?token={srv.token}"
        assert srv.url("stats") == f"http://127.0.0.1:{srv.port}/?token={srv.token}#stats"
        assert srv.url("#settings") == f"http://127.0.0.1:{srv.port}/?token={srv.token}#settings"
    finally:
        srv.stop()
    assert not path.exists()
    assert read_discovery(path) is None
    assert not srv.running


def test_port_in_use_falls_back_to_free_port(ctx, tmp_path, capsys):
    first = WebServer(ctx, WebConfig(port=0), discovery_path=tmp_path / "a.json")
    first.start()
    try:
        second = WebServer(ctx, WebConfig(port=first.port), discovery_path=tmp_path / "b.json")
        second.start()
        try:
            assert second.port != first.port
            assert f"Port {first.port} is in use" in capsys.readouterr().err
        finally:
            second.stop()
    finally:
        first.stop()


def test_token_is_random_per_config():
    assert WebConfig().token != WebConfig().token
    assert len(WebConfig().token) >= 24


def test_ping_helper(server):
    assert ping(server.base_url) is True
    assert ping(server.base_url + "/") is True
    assert ping("") is False
    # A closed port answers nothing: False, not an exception.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    assert ping(f"http://127.0.0.1:{dead_port}", timeout=0.5) is False


# ─── Meeting store with dataclass records (the real MeetingStore shape) ──────────


def test_meeting_get_serialises_store_objects(tmp_path):
    from dataclasses import dataclass, field as dc_field

    @dataclass
    class Seg:
        start: float
        end: float
        speaker: str
        track: str
        text: str

        def to_dict(self):
            return {"start": self.start, "end": self.end, "speaker": self.speaker, "track": self.track, "text": self.text}

    @dataclass
    class Meeting:
        id: str
        title: str
        started_at: str
        tracks: list = dc_field(default_factory=list)
        stats: dict = dc_field(default_factory=dict)

        def to_dict(self):
            from dataclasses import asdict

            return asdict(self)

    class ObjectStore(FakeMeetings):
        def __init__(self):
            self.items = {"m1": Meeting("m1", "Standup", "2026-09-12T10:00:00+02:00", stats={"words_total": 2})}
            self.deleted = []

        def summary(self, meeting):
            return {"id": meeting.id, "words_total": meeting.stats.get("words_total", 0), "has_audio": False}

        def transcript(self, meeting_id):
            return [Seg(0.0, 1.5, "me", "mic", "hello there")]

    ctx = StubContext(tmp_path / "history.jsonl", meetings=ObjectStore())
    srv = WebServer(ctx, WebConfig(port=0), discovery_path=tmp_path / "web.json")
    srv.start()
    try:
        status, _, body = get_json(srv, "/api/meetings/m1")
        assert status == 200, body
        assert body["title"] == "Standup" and body["words_total"] == 2 and body["has_audio"] is False
        assert body["transcript"] == [{"start": 0.0, "end": 1.5, "speaker": "me", "track": "mic", "text": "hello there"}]
    finally:
        srv.stop()
