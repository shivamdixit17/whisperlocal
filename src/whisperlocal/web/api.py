"""The JSON API behind the dashboard.

``build_router(ctx)`` registers every route against an ``AppContext`` — the
menu bar app's live objects, or the standalone CLI server's read-only stand-in.
Handlers take a ``Request`` and return a JSON-able object (sent as 200) or a
``Response`` for anything else. Raise ``HTTPError(status, message)`` for a
specific failure, ``NotSupported`` when the context cannot do it (503).

The server maps ``KeyError``/``ValueError`` (``ConfigError`` included) to 400,
so handlers can let validation errors escape; only ``EnvOverrideError`` needs
its own status (409) and is handled here.

Modules written alongside this one (``settings_manager``, ``keymap``,
``analytics``, ``stats.load_entries``) are imported lazily inside the
handlers so the package imports cleanly without them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from whisperlocal import config as cfg
from whisperlocal.web.server import Router


# ─── Contracts ───────────────────────────────────────────────────────────────────


class AppContext(Protocol):
    """What the API needs from whoever hosts it."""

    settings: Any  # whisperlocal.settings_manager.SettingsManager
    version: str
    running: bool  # True inside the menu bar app, False for the standalone CLI server
    history_path: Path
    meetings: object | None
    recorder: object | None

    def status(self) -> dict: ...  # engine/meeting status; standalone returns {"running": False}
    def start_capture(self) -> dict: ...  # begins trigger-key capture; standalone raises NotSupported
    def capture_state(self) -> dict: ...
    def cancel_capture(self) -> None: ...
    def request_restart(self) -> None: ...  # schedule, do not block — the response must still go out
    def api_key_set(self) -> bool: ...
    def set_api_key(self, key: str) -> None: ...
    def clear_api_key(self) -> None: ...


class NotSupported(Exception):
    """The hosting context cannot do this (-> HTTP 503)."""


class HTTPError(Exception):
    """A specific failure status from a handler."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    params: dict[str, str]
    body: dict | list | None
    headers: Mapping

    def q1(self, name: str, default: str | None = None) -> str | None:
        """The first value of a query parameter, or the default."""
        values = self.query.get(name)
        if not values:
            return default
        return values[0]

    def q_int(self, name: str, default: int | None = None, *, minimum: int | None = None) -> int | None:
        raw = self.q1(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            raise HTTPError(400, f"{name}: expected a whole number, got {raw!r}") from None
        if minimum is not None and value < minimum:
            raise HTTPError(400, f"{name}: must be {minimum} or more")
        return value

    def q_float(self, name: str, default: float) -> float:
        raw = self.q1(name)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            raise HTTPError(400, f"{name}: expected a number, got {raw!r}") from None

    def q_days(self) -> int | None:
        """``?days=30`` -> 30; ``all``, blank or absent -> None (everything)."""
        raw = self.q1("days")
        if raw is None or raw == "" or raw.lower() == "all":
            return None
        return self.q_int("days", minimum=1)

    def body_dict(self) -> dict:
        if self.body is None:
            return {}
        if not isinstance(self.body, dict):
            raise HTTPError(400, "expected a JSON object body")
        return self.body


@dataclass
class Response:
    status: int = 200
    body: bytes | dict | list | str | None = None
    content_type: str = "application/json"
    headers: dict = field(default_factory=dict)


# ─── Helpers ─────────────────────────────────────────────────────────────────────


def _jsonable(value: object) -> object:
    if isinstance(value, (tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    return value


def _settings_manager_module():
    try:
        from whisperlocal import settings_manager
    except ImportError:
        return None
    return settings_manager


def settings_to_jsonable(settings: cfg.Settings) -> dict:
    sm = _settings_manager_module()
    if sm is not None and hasattr(sm, "to_jsonable"):
        return _jsonable(sm.to_jsonable(settings))
    return {f.name: _jsonable(getattr(settings, f.name)) for f in fields(cfg.Settings)}


def settings_defaults() -> dict:
    sm = _settings_manager_module()
    if sm is not None and hasattr(sm, "field_defaults"):
        return _jsonable(sm.field_defaults())
    return settings_to_jsonable(cfg.Settings())


def trigger_catalog() -> list[dict]:
    """``keymap.catalog()`` as dicts, with a config-only fallback."""
    try:
        from whisperlocal import keymap

        return [_jsonable(asdict(t)) if is_dataclass(t) else dict(t) for t in keymap.catalog()]
    except ImportError:
        pass
    out = []
    for token in cfg.TRIGGER_KEYS:
        if token == cfg.FN_KEY:
            kind = "fn"
        elif token in cfg.MOUSE_BUTTONS:
            kind = "mouse"
        else:
            kind = "key"
        out.append(
            {
                "token": token,
                "label": cfg.KEY_LABELS.get(token, token.upper()),
                "risky": token in cfg.RISKY_KEYS,
                "kind": kind,
            }
        )
    return out


def _is_env_override(exc: BaseException) -> bool:
    return any(klass.__name__ == "EnvOverrideError" for klass in type(exc).__mro__)


def _apply_result_to_dict(result: object) -> dict:
    """``ApplyResult`` (dataclass or duck) -> the PUT /api/settings body."""
    get = (lambda k, d=None: getattr(result, k, d)) if not isinstance(result, dict) else (lambda k, d=None: result.get(k, d))
    settings = get("settings")
    return {
        "settings": settings_to_jsonable(settings) if settings is not None else None,
        "changed": list(get("changed", []) or []),
        "applied": _jsonable(get("applied", {}) or {"live": [], "listeners": [], "restart": []}),
        "restart_required": bool(get("restart_required", False)),
        "warnings": list(get("warnings", []) or []),
        "persisted": bool(get("persisted", False)),
    }


def _load_entries(path: Path, days: int | None) -> tuple[list[dict], int]:
    """``stats.load_entries`` -> (entries, skipped); a missing file is empty."""
    if not path.exists():
        return [], 0
    from whisperlocal import stats

    loader = getattr(stats, "load_entries", None)
    if loader is not None:
        entries, skipped = loader(path, days)
        return list(entries), int(skipped)
    return list(stats.load(path, days)), 0


def _record(obj: object) -> dict:
    """A store object (dict, ``to_dict()`` dataclass, or plain dataclass) as JSON-able dict."""
    if isinstance(obj, dict):
        return dict(_jsonable(obj))  # type: ignore[arg-type]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return dict(_jsonable(to_dict()))  # type: ignore[arg-type]
    if is_dataclass(obj) and not isinstance(obj, type):
        return dict(_jsonable(asdict(obj)))  # type: ignore[arg-type]
    raise TypeError(f"cannot serialise {obj.__class__.__name__}")


def _export_disposition(filename: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in filename).strip() or "meeting"
    return f'attachment; filename="{safe}"'


# ─── Routes ──────────────────────────────────────────────────────────────────────


def build_router(ctx: AppContext) -> Router:
    router = Router()

    def meetings_store():
        store = getattr(ctx, "meetings", None)
        if store is None:
            raise NotSupported("meetings are not available in this context")
        return store

    def recorder():
        rec = getattr(ctx, "recorder", None)
        if rec is None:
            raise NotSupported("meeting recording is not available in this context")
        return rec

    # ── ping / status ────────────────────────────────────────────────────────

    def ping(req: Request) -> dict:
        return {"app": "whisperlocal", "version": ctx.version, "running": bool(ctx.running)}

    def status(req: Request) -> dict:
        out = dict(ctx.status() or {})
        out["version"] = ctx.version
        out["running"] = bool(ctx.running)
        return out

    # ── settings ─────────────────────────────────────────────────────────────

    def get_settings(req: Request) -> dict:
        from whisperlocal.web import settings_schema

        current = ctx.settings.current
        return {
            "values": settings_to_jsonable(current),
            "defaults": settings_defaults(),
            "sources": cfg.sources(),
            "schema": settings_schema.SCHEMA,
            "triggers": trigger_catalog(),
            "api_key_set": bool(ctx.api_key_set()),
            "config_path": str(cfg.config_path()),
            "running": bool(ctx.running),
        }

    def put_settings(req: Request) -> Response | dict:
        body = req.body_dict()
        changes = body.get("changes")
        if not isinstance(changes, dict):
            raise HTTPError(400, 'expected a body of the form {"changes": {...}}')
        persist = body.get("persist", True)
        if not isinstance(persist, bool):
            raise HTTPError(400, "persist: expected true or false")
        try:
            result = ctx.settings.apply(changes, persist=persist)
        except cfg.ConfigError as exc:
            status_code = 409 if _is_env_override(exc) else 400
            return Response(status=status_code, body={"error": str(exc)})
        return _apply_result_to_dict(result)

    def preview_settings(req: Request) -> dict:
        body = req.body_dict()
        changes = body.get("changes")
        if not isinstance(changes, dict):
            raise HTTPError(400, 'expected a body of the form {"changes": {...}}')
        settings, tiers = ctx.settings.preview(changes)
        return {"settings": settings_to_jsonable(settings), "tiers": _jsonable(tiers)}

    def capture_start(req: Request) -> dict:
        return dict(ctx.start_capture() or {})

    def capture_state(req: Request) -> dict:
        return dict(ctx.capture_state() or {})

    def capture_cancel(req: Request) -> dict:
        ctx.cancel_capture()
        return {"ok": True}

    # ── api key ──────────────────────────────────────────────────────────────

    def set_api_key(req: Request) -> dict:
        key = req.body_dict().get("key")
        if not isinstance(key, str) or not key.strip():
            raise HTTPError(400, "key: must be a non-empty string")
        ctx.set_api_key(key.strip())
        return {"api_key_set": True}

    def clear_api_key(req: Request) -> dict:
        ctx.clear_api_key()
        return {"api_key_set": False}

    # ── restart ──────────────────────────────────────────────────────────────

    def restart(req: Request) -> Response:
        response = Response(status=200, body={"ok": True})
        # Scheduled, never blocking: the response below must still be sent.
        ctx.request_restart()
        return response

    # ── stats / history ──────────────────────────────────────────────────────

    def stats(req: Request) -> dict:
        from whisperlocal import analytics

        days = req.q_days()
        typing_wpm = req.q_float("typing_wpm", 40.0)
        entries, skipped = _load_entries(Path(ctx.history_path), days)
        out = dict(analytics.build_dashboard(entries, days=days, typing_wpm=typing_wpm))
        current = ctx.settings.current
        out["skipped"] = skipped
        out["history_enabled"] = bool(current.history_enabled)
        out["history_text"] = bool(current.history_text)
        return out

    def history(req: Request) -> dict:
        from whisperlocal import analytics

        days = req.q_days()
        entries, skipped = _load_entries(Path(ctx.history_path), days)
        out = dict(
            analytics.recent(
                entries,
                limit=req.q_int("limit", 50, minimum=1),
                offset=req.q_int("offset", 0, minimum=0),
                q=req.q1("q") or None,
                status=req.q1("status") or None,
                app=req.q1("app") or None,
            )
        )
        out.setdefault("skipped", skipped)
        return out

    # ── meetings ─────────────────────────────────────────────────────────────

    def meetings_list(req: Request) -> dict:
        store = meetings_store()
        return dict(
            store.list(
                limit=req.q_int("limit", 50, minimum=1),
                offset=req.q_int("offset", 0, minimum=0),
                days=req.q_days(),
                query=req.q1("q") or None,
            )
        )

    def meetings_stats(req: Request) -> dict:
        return dict(meetings_store().stats(days=req.q_days()))

    def meeting_get(req: Request) -> dict:
        store = meetings_store()
        meeting = store.get(req.params["id"])
        if meeting is None:
            raise HTTPError(404, "meeting not found")
        record = _record(meeting)
        summary = getattr(store, "summary", None)
        if summary is not None and not isinstance(meeting, dict):
            # words_total, has_audio and friends, computed by the store.
            record.update(_record(summary(meeting)))
        if "transcript" not in record:
            transcript = getattr(store, "transcript", None)
            record["transcript"] = [_record(s) for s in transcript(req.params["id"])] if transcript else []
        return record

    def meeting_delete(req: Request) -> dict:
        if not meetings_store().delete(req.params["id"]):
            raise HTTPError(404, "meeting not found")
        return {"ok": True, "id": req.params["id"]}

    def meeting_export(req: Request) -> Response:
        fmt = (req.q1("fmt") or "md").lower()
        if fmt not in ("md", "txt", "json"):
            raise HTTPError(400, "fmt: choose md, txt or json")
        store = meetings_store()
        if store.get(req.params["id"]) is None:
            raise HTTPError(404, "meeting not found")
        filename, data, mimetype = store.export(req.params["id"], fmt)
        if isinstance(data, str):
            data = data.encode("utf-8")
        return Response(
            status=200,
            body=data,
            content_type=mimetype or "application/octet-stream",
            headers={"Content-Disposition": _export_disposition(filename)},
        )

    def meeting_start(req: Request) -> dict:
        title = req.body_dict().get("title")
        if title is not None and not isinstance(title, str):
            raise HTTPError(400, "title: expected a string")
        return dict(recorder().start(trigger="manual", title=(title or None)))

    def meeting_stop(req: Request) -> dict:
        return dict(recorder().stop())

    def meeting_status(req: Request) -> dict:
        return dict(recorder().status())

    # ── registration ─────────────────────────────────────────────────────────
    # Literal paths are registered before parameterised ones; the router
    # prefers literal segments anyway, so /api/meetings/stats never becomes
    # {id} = "stats".

    router.add("GET", "/api/ping", ping)
    router.add("GET", "/api/status", status)

    router.add("GET", "/api/settings", get_settings)
    router.add("PUT", "/api/settings", put_settings)
    router.add("POST", "/api/settings/preview", preview_settings)
    router.add("POST", "/api/settings/capture-key", capture_start)
    router.add("GET", "/api/settings/capture-key", capture_state)
    router.add("DELETE", "/api/settings/capture-key", capture_cancel)

    router.add("POST", "/api/apikey", set_api_key)
    router.add("DELETE", "/api/apikey", clear_api_key)

    router.add("POST", "/api/restart", restart)

    router.add("GET", "/api/stats", stats)
    router.add("GET", "/api/history", history)

    router.add("GET", "/api/meetings", meetings_list)
    router.add("GET", "/api/meetings/stats", meetings_stats)
    router.add("POST", "/api/meetings/start", meeting_start)
    router.add("POST", "/api/meetings/stop", meeting_stop)
    router.add("GET", "/api/meetings/recording", meeting_status)
    router.add("GET", "/api/meetings/{id}", meeting_get)
    router.add("DELETE", "/api/meetings/{id}", meeting_delete)
    router.add("GET", "/api/meetings/{id}/export", meeting_export)

    return router


__all__ = [
    "AppContext",
    "HTTPError",
    "NotSupported",
    "Request",
    "Response",
    "build_router",
    "settings_defaults",
    "settings_to_jsonable",
    "trigger_catalog",
]
