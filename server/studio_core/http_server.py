"""Declarative HTTP router and threaded local server for CV Studio.

Each Server instance can carry its own ServerConfig (API token, allowed origin
ports, embedded mode), so the MCP connector can serve CV Studio in-process on
a daemon thread without spawning a second Python process or mutating global
auth/actor state. Every HTTP request executes inside `ctx.actor(is_mcp=False)`
so edits made by the user in the browser or embedded view are always attributed
to "you".
"""

from __future__ import annotations

import base64
import dataclasses
import http.server
import json
import mimetypes
import os
import socketserver
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import connectors as _conn
from . import documents as _docs
from . import history_scans as _hist
from . import letters_prep as _lp
from . import openapi as _openapi
from . import provenance as _prov
from . import translations as _trans
from . import workspace as _ws
from .context import (
    ALLOWED_HOSTS,
    CLIP_HTML,
    CLIP_PORT,
    DOCS_HTML,
    INDEX_HTML,
    LOOPBACK_NAMES,
    PAGE_SIZES,
    SAFE_ASSET,
    STARTER_CV,
    STATIC_DIR,
    backups,
    ctx,
    importer,
    is_letter,
    jobstore,
    languages,
    letters,
    posting,
    rel,
    safe_path,
    scans,
)


@dataclasses.dataclass
class ServerConfig:
    """Per-server HTTP security configuration (falls back to ctx when None)."""
    api_token: str | None = None
    own_ports: set[int] | None = None
    open_network: bool | None = None
    embedded: bool = False


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_cls=None, config: ServerConfig | None = None):
        self.config = config
        super().__init__(server_address, handler_cls or Handler)


GET_ROUTES: dict[str, Callable] = {}
POST_ROUTES: dict[str, Callable] = {}


def get(path: str):
    def wrap(fn: Callable) -> Callable:
        GET_ROUTES[path] = fn
        return fn
    return wrap


def post(path: str):
    def wrap(fn: Callable) -> Callable:
        POST_ROUTES[path] = fn
        return fn
    return wrap


def _require_jobs(h: Handler) -> bool:
    if jobstore is None:
        h._json({"error": "job store unavailable"}, 501)
        return False
    return True


def _render_html_with_boot(template: str, token: str | None) -> bytes:
    page = template.replace(
        "__API_TOKEN__", json.dumps(token)).replace(
        "__PREFS__", json.dumps(_ws.load_prefs()).replace("</", "<\\/"))
    return page.encode("utf-8")


# ---------------------------------------------------------------------------
# GET routes
# ---------------------------------------------------------------------------

@get("/")
def _get_index(h: Handler, q: dict):
    h._send(200, _render_html_with_boot(INDEX_HTML, h._token()), "text/html; charset=utf-8")


@get("/clip")
def _get_clip_page(h: Handler, q: dict):
    h._send(200, _render_html_with_boot(CLIP_HTML, h._token()), "text/html; charset=utf-8")


@get("/api/docs")
def _get_docs(h: Handler, q: dict):
    h._send(200, DOCS_HTML.encode("utf-8"), "text/html; charset=utf-8")


@get("/api/openapi.json")
def _get_openapi(h: Handler, q: dict):
    h._json(_openapi.openapi_spec())


@get("/api/state")
def _get_state(h: Handler, q: dict):
    _lp.convert_legacy_letters()
    h._json({
        "documents": _ws.list_documents(),
        "base": _prov.base_cv(),
        "languages": languages.catalogue(),
        "photo": _ws.photo_info(),
        "themes": _docs.available_themes(),
        "page_sizes": PAGE_SIZES,
        "fonts": _ws.font_families(),
        "workspace": str(ctx.workspace),
        "sample": _ws.in_sample(),
        "first_run": ctx.first_run and not _ws.in_sample(),
        "starter": _ws.starter_untouched(),
        "version": ctx.version,
        "platform": sys.platform,
        "server_launch": _conn.server_launch(),
        "api_token": h._token(),
        "port": h.server.server_address[1],
    })


@get("/api/jobs")
def _get_jobs(h: Handler, q: dict):
    if not _require_jobs(h):
        return
    jobs_out = jobstore.list_jobs(
        ctx.workspace, q.get("status", [None])[0], q.get("q", [None])[0],
        q.get("node", [None])[0])
    for j in jobs_out:
        if not j.get("language"):
            j["language_guess"] = languages.detect(
                f"{j.get('title') or ''} {j.get('description') or ''}")
        j["logo_url"] = _ws.logo_url(j.get("logo"))
    base = _lp._base_sig() if any(j.get("match") for j in jobs_out) else (None, None)
    for j in jobs_out:
        if j.get("match"):
            j["match"]["stale"] = _lp.match_stale(j["match"], j, base)
    h._json({
        "jobs": jobs_out,
        "statuses": jobstore.STATUSES,
        "nodes": jobstore.NODE_STATUSES,
        "labels": jobstore.LABELS,
        "lifecycle": {
            "next": jobstore.NEXT,
            "live": sorted(jobstore.LIVE),
            "terminal": sorted(jobstore.TERMINAL),
        },
        "logos": _ws.list_logos(),
    })


@get("/api/funnel")
def _get_funnel(h: Handler, q: dict):
    if not _require_jobs(h):
        return
    h._json(jobstore.funnel(ctx.workspace, q.get("since", [None])[0]))


@get("/api/search")
def _get_search(h: Handler, q: dict):
    h._json({"documents": _ws.search_documents(q.get("q", [""])[0])})


@get("/api/alerts")
def _get_alerts(h: Handler, q: dict):
    if not _require_jobs(h):
        return
    h._json(jobstore.alerts(ctx.workspace))


@get("/api/calendar.ics")
def _get_calendar_ics(h: Handler, q: dict):
    if not _require_jobs(h):
        return
    body = jobstore.ics(ctx.workspace, q.get("id", [None])[0]).encode("utf-8")
    h._send(200, body, "text/calendar; charset=utf-8")


@get("/api/backups")
def _get_backups(h: Handler, q: dict):
    h._json({
        "backups": backups.listing(_hist.backup_dir(), ctx.workspace),
        "folder": str(backups.folder(_hist.backup_dir(), ctx.workspace)),
        "keep": backups.KEEP,
        "sample": _ws.in_sample(),
    })


def _resolve_history_path(q: dict) -> str | None:
    path = (q.get("path") or [""])[0]
    if not path and q.get("job") and jobstore is not None:
        found = jobstore._find(ctx.workspace, q["job"][0])
        if not found:
            return None
        path = f"{jobstore.DIR}/{found[0]}"
    return path


@get("/api/history/recent")
def _get_history_recent(h: Handler, q: dict):
    try:
        h._json({"changes": _hist.history_recent()})
    except (ValueError, OSError, KeyError) as exc:
        h._json({"error": str(exc)}, 400)


@get("/api/history")
def _get_history(h: Handler, q: dict):
    try:
        path = _resolve_history_path(q)
        if path is None:
            return h._json({"error": "No such application."}, 404)
        h._json(_hist.history_of(path))
    except (ValueError, OSError, KeyError) as exc:
        h._json({"error": str(exc)}, 400)


@get("/api/history/diff")
def _get_history_diff(h: Handler, q: dict):
    try:
        path = _resolve_history_path(q)
        if path is None:
            return h._json({"error": "No such application."}, 404)
        h._json(_hist.history_diff(path, (q.get("id") or [""])[0],
                                   (q.get("against") or ["before"])[0]))
    except (ValueError, OSError, KeyError) as exc:
        h._json({"error": str(exc)}, 400)


@get("/api/scans")
def _get_scans(h: Handler, q: dict):
    h._json(_hist.scans_state())


@get("/api/jobs/trash")
def _get_jobs_trash(h: Handler, q: dict):
    if not _require_jobs(h):
        return
    h._json({"trash": jobstore.list_trash(ctx.workspace), "days": jobstore.TRASH_DAYS})


@get("/api/jobs/export")
def _get_jobs_export(h: Handler, q: dict):
    if not _require_jobs(h):
        return
    fmt = q.get("format", ["json"])[0]
    body = jobstore.export(ctx.workspace, fmt).encode("utf-8")
    h._send(200, body, "text/csv" if fmt == "csv" else "application/json")


@get("/api/design-schema")
def _get_design_schema(h: Handler, q: dict):
    h._json(_docs.design_schema(q.get("theme", ["classic"])[0]))


@get("/api/doc")
def _get_doc(h: Handler, q: dict):
    p = safe_path(q["path"][0])
    h._json(_lp.load_letter(p) if is_letter(p) else _docs.load_doc(p))


@get("/api/posting")
def _get_posting(h: Handler, q: dict):
    try:
        h._json(posting.read((q.get("url") or [""])[0]))
    except ValueError as exc:
        h._json({"error": str(exc)}, 422)


@get("/api/clip")
def _get_clip_info(h: Handler, q: dict):
    h._json({
        "port": CLIP_PORT,
        "ok": ctx.clip_state["ok"],
        "why": ctx.clip_state["why"],
        "bookmarklet": _lp.bookmarklet(),
    })


@get("/api/jobs/prep")
def _get_jobs_prep(h: Handler, q: dict):
    try:
        h._json(_lp.interview_prep((q.get("id") or [""])[0]))
    except ValueError as exc:
        h._json({"error": str(exc)}, 404)


@get("/api/jobs/draft")
def _get_jobs_draft(h: Handler, q: dict):
    try:
        h._json(_lp.draft_context((q.get("id") or [""])[0]))
    except ValueError as exc:
        h._json({"error": str(exc)}, 404)


@get("/api/pack/info")
def _get_pack_info(h: Handler, q: dict):
    try:
        info = _lp.pack_info((q.get("job") or [""])[0])
        h._json({k: info[k] for k in ("name", "cv", "letter", "posting")})
    except ValueError as exc:
        h._json({"error": str(exc)}, 422)


@get("/api/pack")
def _get_pack(h: Handler, q: dict):
    try:
        data, ctype, fname = _lp.application_pack(
            (q.get("job") or [""])[0],
            (q.get("format") or ["pdf"])[0],
            (q.get("name") or [None])[0],
            (q.get("posting") or [""])[0] in ("1", "true"),
        )
    except ValueError as exc:
        return h._json({"error": str(exc)}, 422)
    h._attachment(data, ctype, fname)


@get("/api/letter/export")
def _get_letter_export(h: Handler, q: dict):
    p = safe_path(q["path"][0])
    try:
        data, ctype, fname = _lp.letter_export(p, (q.get("format") or ["pdf"])[0])
    except ValueError as exc:
        return h._json({"error": str(exc)}, 422)
    h._attachment(data, ctype, fname)


@get("/api/ai")
def _get_ai(h: Handler, q: dict):
    h._json({"clients": _conn.ai_clients()})


@get("/api/pulse")
def _get_pulse(h: Handler, q: dict):
    h._json(_ws.pulse())


@get("/api/basediff")
def _get_basediff(h: Handler, q: dict):
    h._json(_prov.base_diff(safe_path(q["path"][0])))


@get("/api/thumb")
def _get_thumb(h: Handler, q: dict):
    h._json(_docs.thumb(safe_path(q["path"][0])))


@get("/api/skills")
def _get_skills(h: Handler, q: dict):
    h._json(_conn.skills_list())


@get("/api/asset")
def _get_asset(h: Handler, q: dict):
    p = safe_path(q["path"][0])
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    h._send(200, p.read_bytes(), ctype)


# ---------------------------------------------------------------------------
# POST routes
# ---------------------------------------------------------------------------

@post("/api/save")
def _post_save(h: Handler, payload: dict):
    p = safe_path(payload["path"])
    if is_letter(p):
        return h._json({"ok": True, **_lp.save_letter(p, payload)})
    wrote = {}
    if "yaml" in payload:
        wrote = _docs.write_doc(p, payload["yaml"], "save")
    elif "patches" in payload:
        wrote = _docs.save_patches(p, payload["patches"])
    if payload.get("ops"):
        did = _docs.apply_ops(p, payload["ops"], "save")
        wrote = {
            "applied": (wrote.get("applied") or []) + did["applied"],
            "missed": (wrote.get("missed") or []) + did["missed"],
            "changed": (wrote.get("changed") or []) + did["changed"],
        }
    h._json({"ok": True, **wrote, **_docs.load_doc(p)})


@post("/api/skills/package")
def _post_skills_package(h: Handler, payload: dict):
    try:
        h._json(_conn.package_skills())
    except (ValueError, OSError) as exc:
        h._json({"error": str(exc)}, 400)


@post("/api/skills/install")
def _post_skills_install(h: Handler, payload: dict):
    client = payload.get("client", "")
    if not _conn.client_skills_dir(client):
        return h._json({"error": f"unknown client: {client}"}, 400)
    try:
        h._json(_conn.install_skills(client))
    except (ValueError, OSError) as exc:
        h._json({"error": str(exc)}, 400)


@post("/api/ai/connect")
def _post_ai_connect(h: Handler, payload: dict):
    try:
        h._json(_conn.ai_connect(payload.get("client", "")))
    except (ValueError, OSError) as exc:
        h._json({"error": str(exc)}, 400)


@post("/api/basediff/revert")
def _post_basediff_revert(h: Handler, payload: dict):
    p = safe_path(payload["path"])
    try:
        d = _prov.base_revert(p, list(payload.get("ids") or []), payload.get("sig"))
    except ValueError as exc:
        return h._json({"error": str(exc), "diff": _prov.base_diff(p)}, 409)
    h._json({"ok": True, "diff": d, "doc": _docs.load_doc(p)})


@post("/api/review")
def _post_review(h: Handler, payload: dict):
    p = safe_path(payload["path"])
    try:
        r = _prov.review_resolve(
            p, list(payload.get("ids") or []),
            str(payload.get("action") or ""), payload.get("sig"))
    except ValueError as exc:
        return h._json({"error": str(exc), "review": _prov.review_payload(p)}, 409)
    if r.get("deleted"):
        return h._json({"ok": True, **r})
    h._json({"ok": True, **r, "doc": _lp.load_letter(p) if is_letter(p) else _docs.load_doc(p)})


@post("/api/render")
def _post_render(h: Handler, payload: dict):
    p = safe_path(payload["path"])
    h._json(_lp.render_letter(p) if is_letter(p) else _docs.render(p))


@post("/api/letter/new")
def _post_letter_new(h: Handler, payload: dict):
    h._json(_lp.new_letter(payload.get("job_id"), payload.get("name")))


@post("/api/preview")
def _post_preview(h: Handler, payload: dict):
    h._json(_docs.preview(safe_path(payload["path"]), payload.get("yaml"), payload.get("patches")))


@post("/api/photo")
def _post_photo(h: Handler, payload: dict):
    try:
        saved = _ws.save_photo(base64.b64decode(str(payload.get("data") or ""), validate=False))
        ref = _ws.photo_ref(safe_path(payload["path"])) if payload.get("path") else None
        h._json({"ok": True, "photo": saved, "ref": ref})
    except ValueError as exc:
        h._json({"ok": False, "error": str(exc)})


@post("/api/photo/remove")
def _post_photo_remove(h: Handler, payload: dict):
    h._json(_ws.remove_photo())


@post("/api/language/add")
def _post_language_add(h: Handler, payload: dict):
    try:
        h._json({"ok": True, **_trans.add_language(
            safe_path(payload["path"]), str(payload.get("language") or ""))})
    except ValueError as exc:
        h._json({"ok": False, "error": str(exc)})


@post("/api/language/done")
def _post_language_done(h: Handler, payload: dict):
    h._json(_trans.mark_translation_current(safe_path(payload["path"])))


@post("/api/language/set")
def _post_language_set(h: Handler, payload: dict):
    h._json({"ok": True, **_trans.set_cv_language(
        safe_path(payload.get("path", "")), payload.get("language", ""))})


@post("/api/import")
def _post_import(h: Handler, payload: dict):
    raw = str(payload.get("data") or "")
    if len(raw) > 56_000_000:
        return h._json({
            "ok": False,
            "error": "That file is over 40 MB. LinkedIn's basic archive is much smaller: "
                     "choose Profile, Positions, Education and Skills when you request it.",
        })
    try:
        data = base64.b64decode(raw, validate=False)
        h._json(importer.import_file(str(payload.get("name") or ""), data))
    except importer.ImportError_ as exc:
        h._json({"ok": False, "error": str(exc)})


@post("/api/theme-preview")
def _post_theme_preview(h: Handler, payload: dict):
    h._json(_docs.theme_preview(
        safe_path(payload["path"]), str(payload.get("theme") or ""), payload.get("patches")))


@post("/api/jobs")
def _post_jobs(h: Handler, payload: dict):
    if not _require_jobs(h):
        return
    if not payload.get("logo") and payload.get("company"):
        payload["logo"] = _ws.stored_logo(payload["company"])
    h._json(jobstore.add_job(ctx.workspace, payload))


@post("/api/jobs/prep")
def _post_jobs_prep(h: Handler, payload: dict):
    try:
        h._json(_lp.save_interview_prep(payload.get("id", ""), payload.get("prep") or {}))
    except ValueError as exc:
        h._json({"error": str(exc)}, 404)


@post("/api/jobs/update")
def _post_jobs_update(h: Handler, payload: dict):
    if not _require_jobs(h):
        return
    h._json(jobstore.update_job(ctx.workspace, payload.pop("id", ""), payload))


@post("/api/jobs/delete")
def _post_jobs_delete(h: Handler, payload: dict):
    if not _require_jobs(h):
        return
    jobstore.delete_job(ctx.workspace, payload.get("id", ""))
    h._json({"ok": True})


@post("/api/jobs/restore")
def _post_jobs_restore(h: Handler, payload: dict):
    if not _require_jobs(h):
        return
    try:
        h._json(jobstore.restore_job(ctx.workspace, payload.get("id", "")))
    except ValueError as exc:
        h._json({"error": str(exc)}, 404)


@post("/api/ats")
def _post_ats(h: Handler, payload: dict):
    h._json(_docs.ats_report(
        safe_path(payload["path"]), payload.get("job_id"), payload.get("posting")))


@post("/api/ats/fix")
def _post_ats_fix(h: Handler, payload: dict):
    p = safe_path(payload["path"])
    _docs.ats_fix(p, payload.get("fix", ""))
    h._json({"ok": True, **_docs.ats_report(p, payload.get("job_id"), payload.get("posting"))})


@post("/api/open")
def _post_open(h: Handler, payload: dict):
    link = str(payload.get("url") or "")
    if urlparse(link).scheme not in ("http", "https", "mailto"):
        return h._json({"error": "Only web links can be opened."}, 400)
    webbrowser.open(link)
    h._json({"ok": True})


@post("/api/download")
def _post_download(h: Handler, payload: dict):
    try:
        data = base64.b64decode(str(payload.get("data") or ""), validate=True)
        saved = _ws.save_download(str(payload.get("name") or ""), data)
    except ValueError as exc:
        return h._json({"error": str(exc)}, 400)
    _ws.show_file(saved)
    h._json({"ok": True, "name": saved.name, "path": str(saved)})


@post("/api/reveal")
def _post_reveal(h: Handler, payload: dict):
    target = ctx.workspace
    sub_ = payload.get("path")
    if payload.get("logs"):
        target = _ws.log_path().parent
        target.mkdir(parents=True, exist_ok=True)
    elif payload.get("scans"):
        target = _hist.scan_folder()
        target.mkdir(parents=True, exist_ok=True)
    elif sub_:
        target = safe_path(sub_)
    try:
        if sys.platform == "win32":
            os.startfile(target)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as exc:
        return h._json({"error": str(exc)}, 500)
    h._json({"ok": True})


@post("/api/doc/rename")
def _post_doc_rename(h: Handler, payload: dict):
    try:
        h._json(_hist.rename_document(payload.get("path", ""), payload.get("name", "")))
    except FileNotFoundError:
        h._json({"error": "That document no longer exists."}, 404)
    except ValueError as exc:
        h._json({"error": str(exc)}, 409)


@post("/api/doc/delete")
def _post_doc_delete(h: Handler, payload: dict):
    try:
        h._json(_hist.delete_document(payload.get("path", "")))
    except FileNotFoundError:
        h._json({"error": "That document no longer exists."}, 404)
    except ValueError as exc:
        h._json({"error": str(exc)}, 409)


@post("/api/new")
def _post_new(h: Handler, payload: dict):
    raw = payload.get("name") or "untitled"
    name = "".join(c for c in raw if c.isalnum() or c in "-_ ").strip()
    if not name:
        return h._json({"error": "Please give it a name."}, 400)
    kind = payload.get("kind") or "cv"
    if kind == "letter" and not payload.get("from"):
        return h._json(_lp.new_letter(payload.get("job_id"), name))
    folder = "letters" if kind == "letter" else "profile"
    src_ext = Path(str(payload.get("from") or "")).suffix
    dest = safe_path(f"{folder}/{name}{src_ext if src_ext == letters.EXT else '.yaml'}")
    if dest.exists():
        return h._json({"error": "Something with that name already exists."}, 409)
    src = payload.get("from")
    body = safe_path(src).read_text(encoding="utf-8") if src else STARTER_CV
    dest.write_text(body, encoding="utf-8")
    _prov.note_lineage(dest, rel(safe_path(src)) if src else None)
    h._json({"ok": True, "path": rel(dest)})


@post("/api/history/restore")
def _post_history_restore(h: Handler, payload: dict):
    try:
        h._json(_hist.restore_version(payload.get("path", ""), str(payload.get("id", ""))))
    except (ValueError, OSError) as exc:
        h._json({"error": str(exc)}, 400)


@post("/api/scans/record")
def _post_scans_record(h: Handler, payload: dict):
    try:
        found = payload.get("jobs")
        if not isinstance(found, list) or not all(isinstance(j, dict) for j in found):
            raise ValueError("jobs is a list of jobs, each with a company, a role and a link.")
        h._json({"ok": True, **_hist.scan_record(found, payload.get("scan"), payload.get("found"))})
    except (ValueError, OSError) as exc:
        h._json({"error": str(exc)}, 400)


@post("/api/scans/add")
def _post_scans_add(h: Handler, payload: dict):
    try:
        h._json(_hist.scan_add(str(payload.get("key", ""))))
    except (ValueError, OSError) as exc:
        h._json({"error": str(exc)}, 400)


@post("/api/scans/decide")
def _post_scans_decide(h: Handler, payload: dict):
    try:
        h._json({"ok": True, **_hist.scan_decide(
            payload.get("keys") or [], str(payload.get("status", "")))})
    except (ValueError, OSError) as exc:
        h._json({"error": str(exc)}, 400)


@post("/api/scans/undo")
def _post_scans_undo(h: Handler, payload: dict):
    try:
        h._json({"ok": True, **_hist.scan_undo(
            str(payload.get("key", "")), payload.get("job_id") or None)})
    except (ValueError, OSError) as exc:
        h._json({"error": str(exc)}, 400)


@post("/api/scans/create")
def _post_scans_create(h: Handler, payload: dict):
    try:
        h._json({"ok": True, "file": str(scans.create(_hist.scan_folder()))})
    except (ValueError, OSError) as exc:
        h._json({"error": str(exc)}, 400)


@post("/api/backups/new")
def _post_backups_new(h: Handler, payload: dict):
    if _ws.in_sample():
        return h._json({"error": "Sample data is not backed up; it is made fresh each time."}, 409)
    h._json({"ok": True, **backups.make(_hist.backup_dir(), ctx.workspace, "manual")})


@post("/api/backups/restore")
def _post_backups_restore(h: Handler, payload: dict):
    if _ws.in_sample():
        return h._json({"error": "Go back to your workspace to restore it."}, 409)
    try:
        h._json({"ok": True, **backups.restore(
            _hist.backup_dir(), ctx.workspace, payload.get("name", ""))})
    except FileNotFoundError:
        h._json({"error": "That backup no longer exists."}, 404)


@post("/api/prefs")
def _post_prefs(h: Handler, payload: dict):
    if isinstance(payload.get("replace"), dict):
        return h._json({"ok": True, "prefs": _ws.save_prefs(payload["replace"], replace=True)})
    h._json({"ok": True, "prefs": _ws.save_prefs(payload.get("set") or {})})


@post("/api/sample")
def _post_sample(h: Handler, payload: dict):
    if payload.get("on"):
        try:
            n = int(payload.get("applications") or 64)
        except (TypeError, ValueError):
            n = 64
        return h._json({"ok": True, **_ws.open_sample(n)})
    _ws.close_sample()
    h._json({"ok": True})


@post("/api/base")
def _post_base(h: Handler, payload: dict):
    _prov.set_base_cv(payload.get("path") or None)
    h._json({"ok": True, "base": _prov.base_cv()})


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "CVStudio"

    QUIET_POSTS = (
        "/api/preview", "/api/theme-preview", "/api/prefs", "/api/open",
        "/api/reveal", "/api/skills/package", "/api/skills/install",
        "/api/ai/connect", "/api/sample", "/api/ats", "/api/download",
    )

    def log_message(self, *args):
        pass

    def _cfg(self) -> ServerConfig | None:
        return getattr(self.server, "config", None)

    def _token(self) -> str | None:
        cfg = self._cfg()
        return cfg.api_token if cfg is not None else ctx.api_token

    def _own_ports(self) -> set[int]:
        cfg = self._cfg()
        return cfg.own_ports if (cfg is not None and cfg.own_ports is not None) else ctx.own_ports

    def _open_network(self) -> bool:
        cfg = self._cfg()
        return cfg.open_network if (cfg is not None and cfg.open_network is not None) else ctx.open_network

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        if getattr(self, "_defer_send", False):
            self._deferred = (code, body, ctype)
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _attachment(self, data: bytes, ctype: str, fname: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _addressed_here(self) -> bool:
        if self._open_network():
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
        return name in ALLOWED_HOSTS

    def _from_here(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None and not self._open_network():
            o = urlparse(origin)
            if (o.scheme != "http" or (o.hostname or "") not in LOOPBACK_NAMES
                    or o.port not in self._own_ports()):
                return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json" or int(self.headers.get("Content-Length") or 0) == 0

    def _authed(self) -> bool:
        token = self._token()
        if token is None:
            return True
        supplied = self.headers.get("X-API-Key") or parse_qs(
            urlparse(self.path).query).get("token", [None])[0]
        return supplied == token

    def do_GET(self):
        with ctx.actor(is_mcp=False, client_id=None, client_agent=None):
            self._do_get()

    def _do_get(self):
        if not self._addressed_here():
            return self._json({"error": "not found"}, 404)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/") and u.path != "/api/docs" and not self._authed():
            return self._json({"error": "unauthorised: supply X-API-Key"}, 401)
        try:
            if u.path.startswith("/cvfont/"):
                return self._cv_font(unquote(u.path[len("/cvfont/"):]))
            if u.path.startswith("/static/"):
                return self._static(u.path.split("/static/", 1)[1])
            fn = GET_ROUTES.get(u.path)
            if fn is not None:
                return fn(self, q)
            return self._json({"error": "not found"}, 404)
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 403)
        except FileNotFoundError:
            return self._json({"error": "file not found"}, 404)
        except Exception as exc:
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _static(self, name: str):
        parts = name.split("/")
        if len(parts) > 2 or not all(SAFE_ASSET.fullmatch(x) for x in parts):
            return self._json({"error": "not found"}, 404)
        f = STATIC_DIR.joinpath(*parts)
        if not f.is_file() or STATIC_DIR.resolve() not in f.resolve().parents:
            return self._json({"error": "not found"}, 404)
        if f.suffix == ".woff2":
            return self._send(200, f.read_bytes(), "font/woff2")
        if f.suffix == ".png":
            return self._send(200, f.read_bytes(), "image/png")
        ctype = {".js": "application/javascript", ".css": "text/css"}.get(f.suffix, "text/plain")
        return self._send(200, f.read_bytes(), ctype + "; charset=utf-8")

    def _cv_font(self, name: str):
        try:
            import rendercv_fonts
            root = Path(rendercv_fonts.__file__).parent
        except Exception:
            return self._json({"error": "not found"}, 404)
        families = {d.name: d for d in root.iterdir() if d.is_dir() and not d.name.startswith("_")}
        if name.endswith(".css"):
            fam = families.get(name[:-4])
            if not fam:
                return self._send(200, b"", "text/css")
            rules = []
            for f in sorted(fam.glob("*.[ot]tf")):
                n = f.stem.lower()
                weight = (700 if "bold" in n and "semi" not in n else
                          600 if "semibold" in n else
                          500 if "medium" in n else
                          300 if "light" in n else 400)
                style = "italic" if "italic" in n else "normal"
                rules.append(
                    f"@font-face{{font-family:'{fam.name}';src:url('/cvfont/"
                    f"{quote(fam.name)}/{quote(f.name)}');font-weight:{weight};"
                    f"font-style:{style}}}")
            return self._send(200, "\n".join(rules).encode("utf-8"), "text/css; charset=utf-8")
        fam_name, _, file = name.partition("/")
        fam = families.get(fam_name)
        f = fam / file if fam and "/" not in file and "\\" not in file else None
        if not f or not f.is_file() or f.suffix not in (".ttf", ".otf"):
            return self._json({"error": "not found"}, 404)
        return self._send(200, f.read_bytes(), "font/ttf" if f.suffix == ".ttf" else "font/otf")

    def do_POST(self):
        with ctx.actor(is_mcp=False, client_id=None, client_agent=None):
            path = urlparse(self.path).path
            look = path.startswith("/api/") and path not in self.QUIET_POSTS
            if look:
                _hist.note_history("elsewhere")
            # Buffer the HTTP response until post-write history has committed,
            # so a client that immediately queries /api/history sees its save.
            self._defer_send = look
            self._deferred = None
            try:
                self._do_post()
            finally:
                self._defer_send = False
                if look:
                    _hist.note_history("you", tool=path.removeprefix("/api/"))
                if self._deferred is not None:
                    code, body, ctype = self._deferred
                    self._deferred = None
                    self._send(code, body, ctype)

    def _do_post(self):
        if not (self._addressed_here() and self._from_here()):
            return self._json({"error": "not found"}, 404)
        u = urlparse(self.path)
        if u.path.startswith("/api/") and not self._authed():
            return self._json({"error": "unauthorised: supply X-API-Key"}, 401)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return self._json({"error": "bad request"}, 400)
        try:
            fn = POST_ROUTES.get(u.path)
            if fn is not None:
                return fn(self, payload)
            return self._json({"error": "not found"}, 404)
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 403)
        except Exception as exc:
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def make_clip_server(port: int) -> Server:
    """Create the loopback listener for the Save to CV Studio bookmarklet."""
    return Server(("127.0.0.1", port), Handler)


def start_embedded_server(port: int, token: str) -> Server:
    """Start CV Studio's HTTP interface in-process on a daemon thread.

    Used by MCP's `open_studio` tool so the view beside the chat starts in
    milliseconds with zero duplicate Python process and clean actor isolation
    (HTTP requests run as "you" while MCP tool calls run as the AI client).
    """
    cfg = ServerConfig(
        api_token=token,
        own_ports={port},
        open_network=False,
        embedded=True,
    )
    srv = Server(("127.0.0.1", port), Handler, config=cfg)
    threading.Thread(target=srv.serve_forever, daemon=True, name="embedded-studio").start()
    return srv
