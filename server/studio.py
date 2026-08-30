#!/usr/bin/env python3
"""CV Studio: local server behind the desktop app.

Runs entirely on 127.0.0.1. No account, no telemetry, no external services.
CVs are plain YAML files on disk; there is deliberately no database, so a user
can grep, diff, back up and read their CVs without this program.

Design notes:

* YAML round-trips through ruamel in 'rt' mode so comments survive edits made
  through the form. Structural edits replace a section subtree, so comments
  *inside* sections are the one thing that can be lost; comments in
  design/locale/settings always survive.
* All file access is confined to the workspace. Paths are resolved and checked
  against it, because a browser-facing server that reads arbitrary paths is a
  liability even on loopback.
* Rendering goes through cv_render, which calls RenderCV in-process when it is
  importable (the packaged app) and shells out to the CLI otherwise.
"""

from __future__ import annotations

import argparse
import http.server
import json
import mimetypes
import re
import secrets
import socketserver
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from ruamel.yaml import YAML
except ImportError:  # pragma: no cover
    sys.stderr.write("ruamel.yaml is required.\n")
    raise SystemExit(1)

_HERE = Path(__file__).resolve().parent
for _cand in (_HERE, _HERE.parents[1] / "cv-studio-resume" / "scripts"):
    if (_cand / "cv_render.py").is_file():
        sys.path.insert(0, str(_cand))
        break
from cv_render import render_file  # noqa: E402

THEMES = ["engineeringclassic", "engineeringresumes", "classic", "sb2nov", "moderncv"]
PAGE_SIZES = ["a4", "us-letter"]
DEFAULT_WORKSPACE = Path.home() / "Documents" / "CV Studio"

yaml_rt = YAML(typ="rt")
yaml_rt.preserve_quotes = True
yaml_rt.width = 4096  # stop ruamel re-wrapping bullet text into hard breaks
yaml_rt.indent(mapping=2, sequence=4, offset=2)

WORKSPACE: Path = DEFAULT_WORKSPACE
FIRST_RUN = False
API_TOKEN: str | None = None
VERSION = "0.1.0"


def server_launch() -> dict:
    """How to launch this server, for the Claude Desktop config snippet.

    MCP needs the executable and its arguments separately -- a single string
    holding "python script.py" is not runnable. Frozen builds are the exe
    itself; a source checkout needs the interpreter plus the script path.
    """
    if getattr(sys, "frozen", False):
        return {"command": str(Path(sys.executable).resolve()), "args": []}
    return {"command": str(Path(sys.executable).resolve()),
            "args": [str(Path(__file__).resolve())]}

STARTER_CV = """# Your CV. Every field here is editable in the Form tab.
# One YAML rule worth knowing: if a line of text contains a colon followed by a
# space, wrap it in a `>-` block (like the summary below) or the file won't parse.
cv:
  name: Your Name
  headline: Your Role
  location: City, Country
  email: you@example.com
  phone: "+33-6-12-34-56-78"
  social_networks:
    - network: LinkedIn
      username: your-handle

  sections:
    summary:
      - >-
        One or two sentences on what you do and what you are good at. Lead with
        evidence rather than adjectives.

    experience:
      - company: Company Name
        position: Your Title
        location: City, Country
        start_date: 2022-01
        end_date: present
        highlights:
          - An achievement with a number in it, because numbers get read first
          - What you actually changed, rather than what you were responsible for

    education:
      - institution: University
        area: Field of Study
        degree: MSc
        location: City, Country
        start_date: 2016-09
        end_date: 2021-06

    skills:
      - label: Core
        details: Skill, Skill, Skill

design:
  theme: engineeringclassic
  page:
    size: a4
    top_margin: 1.6cm
    bottom_margin: 1.6cm
    left_margin: 1.6cm
    right_margin: 1.6cm
    show_footer: false
  colors:
    name: rgb(0, 0, 0)
    section_titles: rgb(0, 0, 0)
    headline: rgb(70, 70, 70)
    connections: rgb(70, 70, 70)
    links: rgb(0, 60, 120)
  typography:
    line_spacing: 0.6em
    alignment: left
    font_family:
      body: Source Sans 3
      name: Source Sans 3

locale:
  language: english

settings:
  current_date: today
"""


# --------------------------------------------------------------------------
# workspace
# --------------------------------------------------------------------------

def bootstrap(workspace: Path) -> bool:
    """Create and seed the workspace. Returns True when this was a first run."""
    created = not workspace.exists()
    (workspace / "profile").mkdir(parents=True, exist_ok=True)
    (workspace / "applications").mkdir(exist_ok=True)
    (workspace / "assets").mkdir(exist_ok=True)
    if not any((workspace / "profile").glob("*.y*ml")):
        (workspace / "profile" / "my-cv.yaml").write_text(STARTER_CV, encoding="utf-8")
        created = True
    return created


def safe_path(raw: str) -> Path:
    p = (WORKSPACE / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if not str(p).startswith(str(WORKSPACE.resolve())):
        raise PermissionError("path outside the workspace")
    return p


def rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(WORKSPACE.resolve())).replace("\\", "/")
    except ValueError:
        return str(p)


def is_cv_yaml(p: Path) -> bool:
    if p.suffix not in (".yaml", ".yml"):
        return False
    try:
        head = p.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return False
    return "cv:" in head and ("sections:" in head or "design:" in head)


def list_documents() -> list[dict]:
    docs = []
    profile_dir = WORKSPACE / "profile"
    if profile_dir.is_dir():
        for f in sorted(profile_dir.glob("*.y*ml")):
            if not f.name.startswith(".") and is_cv_yaml(f):
                docs.append({"path": rel(f), "label": f.stem, "group": "My CVs"})
    apps = WORKSPACE / "applications"
    if apps.is_dir():
        for app_dir in sorted(apps.iterdir(), reverse=True):
            if not app_dir.is_dir():
                continue
            for f in sorted(app_dir.glob("*.y*ml")):
                if not f.name.startswith(".") and is_cv_yaml(f):
                    docs.append({"path": rel(f), "label": app_dir.name, "group": "Applications"})
    return docs


def font_families() -> list[str]:
    """Families shipped with RenderCV. Most of these are Google Fonts."""
    try:
        import rendercv_fonts
        base = Path(rendercv_fonts.__file__).resolve().parent
        # Font Awesome is an icon set used for the contact-line glyphs, not a
        # body typeface -- offering it in a font picker would only produce an
        # unreadable CV.
        names = sorted(d.name for d in base.iterdir()
                       if d.is_dir() and not d.name.startswith("__")
                       and "awesome" not in d.name.lower())
        if names:
            return names
    except Exception:
        pass
    return ["Source Sans 3", "Lato", "Open Sans", "Roboto", "EB Garamond"]


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------

def to_plain(obj):
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_plain(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def load_doc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        data, err = yaml_rt.load(text), None
    except Exception as exc:
        data, err = None, str(exc)
    return {"yaml": text, "data": to_plain(data) if data else None, "parse_error": err}


def apply_patches(path: Path, patches: list[dict]) -> None:
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    for patch in patches:
        keys, value = patch.get("path") or [], patch.get("value")
        if not keys:
            continue
        node, ok = data, True
        for k in keys[:-1]:
            try:
                node = node[int(k)] if isinstance(node, list) else node[k]
            except (KeyError, IndexError, ValueError, TypeError):
                ok = False
                break
        if not ok or node is None:
            continue
        last = keys[-1]
        try:
            if isinstance(node, list):
                node[int(last)] = value
            else:
                node[last] = value
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    import io
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")


# Error text from RenderCV is precise but not friendly. These are the failures
# real users actually hit, with an explanation of what to do about it.
HINTS = (
    ("mapping values are not allowed",
     "A line of text contains a colon followed by a space, which YAML reads as a "
     "new field. Wrap that text in a >- block, or put it in quotes."),
    ("not a valid phone number",
     "Phone numbers are checked against real numbering plans, not just their shape. "
     "Use the full international form (e.g. +33-6-12-34-56-78) and make sure it is "
     "a number that could actually be dialled in that country."),
    # The same colon mistake inside a list of strings produces a *valid* YAML
    # dict rather than a syntax error, so it surfaces as a type complaint
    # instead. This is the form users hit most, because bullets get written
    # naturally as "Did the thing: with this result".
    ("input should be a valid string",
     "A bullet probably contains a colon followed by a space, so YAML turned it into "
     "a field instead of text. Put that bullet in quotes, or reword it to avoid the "
     "colon (an en dash reads well)."),
    ("entry type of this section",
     "The entries in this section are not all the same shape. Every entry in one "
     "section must be the same type. Look for a bullet that accidentally became "
     "a field."),
    ("not a valid email", "That email address is malformed. Check the @ and the domain."),
    ("could not find", "A referenced file is missing. Check the paths in your YAML."),
)


def friendly(error: str) -> str | None:
    """Match a hint against RenderCV's error output.

    The output is a box-drawn table, so a message is wrapped across lines and
    padded with spaces and border glyphs. Matching the raw text fails for any
    phrase long enough to wrap -- which is all the useful ones -- so flatten it
    to a single spaced line first.
    """
    flat = re.sub(r"[─-╿|]", " ", error)
    flat = re.sub(r"\s+", " ", flat).strip().lower()
    for needle, hint in HINTS:
        if needle in flat:
            return hint
    return None


def _shape(result: dict) -> dict:
    if not result.get("ok"):
        log = (result.get("log") or "render failed")[-3000:]
        return {"ok": False, "error": log, "hint": friendly(log)}
    stamp = int(time.time() * 1000)
    return {
        "ok": True,
        "pages": result.get("pages"),
        "ats_words": result.get("ats_word_count"),
        "pdf": rel(Path(result["pdf"])) if result.get("pdf") else None,
        "pngs": [f"/api/asset?path={rel(Path(p))}&v={stamp}"
                 for p in result.get("png_pages", [])],
    }


def render(path: Path) -> dict:
    out_dir = (path.parent / "output") if path.parent.name != "profile"         else (WORKSPACE / "assets" / path.stem)
    return _shape(render_file(path, out_dir))


def preview(path: Path, text: str | None = None,
            patches: list[dict] | None = None) -> dict:
    """Render unsaved editor content without writing to the user's file.

    The scratch file goes beside the original rather than into a temp directory
    because RenderCV resolves a `fonts/` folder relative to the input file;
    rendering elsewhere would silently drop any custom font.
    """
    tmp = path.parent / (".cvstudio-preview" + path.suffix)
    try:
        # Form edits arrive as patches, so start from the saved file and apply
        # them to the scratch copy; the YAML tab sends its text directly.
        tmp.write_text(text if text is not None
                       else path.read_text(encoding="utf-8"), encoding="utf-8")
        if patches:
            apply_patches(tmp, patches)
        return _shape(render_file(tmp, WORKSPACE / "assets" / ".preview"))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def available_themes() -> list[str]:
    """Themes RenderCV actually ships, asked at runtime rather than hardcoded.

    The docs list five; v2.8 ships nine. Reading the real list means the app
    does not go stale when RenderCV adds one.
    """
    try:
        from rendercv.schema.models.design.built_in_design import available_themes as at
        return list(at)
    except Exception:
        return THEMES


def _unwrap(spec: dict) -> dict:
    """Collapse Optional[...] / anyOf into the one meaningful variant."""
    for v in spec.get("anyOf", [spec]):
        if v.get("type") != "null":
            return v
    return spec


def _describe(spec: dict, defs: dict, path: list, group: str, depth: int = 0) -> list[dict]:
    """Turn one schema property into UI field descriptors.

    Nested models (font_family, font_size) are flattened one level so every
    setting reachable in the YAML is reachable in the interface.
    """
    v = _unwrap(spec)
    default = spec.get("default", v.get("default"))
    ref = v.get("$ref")
    target = defs.get(ref.split("/")[-1], {}) if ref else {}
    name = ref.split("/")[-1] if ref else ""

    if target.get("enum") or v.get("enum"):
        return [{"path": path, "kind": "enum",
                 "options": target.get("enum") or v.get("enum"), "default": default}]
    if "TypstDimension" in name:
        return [{"path": path, "kind": "dimension", "default": default}]
    if target.get("properties") and depth < 1:
        out = []
        for k, sub in target["properties"].items():
            out += _describe(sub, defs, path + [k], group, depth + 1)
        return out
    t = v.get("type") or target.get("type")
    if t == "boolean":
        return [{"path": path, "kind": "bool", "default": bool(default)}]
    if t == "array":
        return [{"path": path, "kind": "list", "default": default or []}]
    if group == "colors" or (isinstance(default, str) and default.startswith("rgb(")):
        return [{"path": path, "kind": "color", "default": default}]
    if t in ("integer", "number"):
        return [{"path": path, "kind": "number", "default": default}]
    return [{"path": path, "kind": "text", "default": default}]


def design_schema(theme: str) -> dict:
    """Every design option for a theme, described well enough to build a UI from."""
    try:
        from rendercv.schema.models.design.built_in_design import built_in_design_adapter
        sch = built_in_design_adapter.json_schema()
    except Exception as exc:
        return {"groups": [], "themes": available_themes(), "error": str(exc)}

    defs = sch.get("$defs", {})
    branch = None
    for b in sch.get("oneOf", []):
        model = defs.get(b.get("$ref", "").split("/")[-1], {})
        if (model.get("properties", {}).get("theme", {}) or {}).get("const") == theme:
            branch = model
            break
    if branch is None:
        return {"groups": [], "themes": available_themes()}

    groups = []
    for gname, gspec in branch.get("properties", {}).items():
        if gname == "theme":
            continue
        v = _unwrap(gspec)
        ref = v.get("$ref")
        model = defs.get(ref.split("/")[-1], {}) if ref else {}
        fields = []
        for fname, fspec in (model.get("properties") or {}).items():
            fields += _describe(fspec, defs, [gname, fname], gname)
        if fields:
            groups.append({"name": gname, "fields": fields})
    return {"groups": groups, "themes": available_themes()}


def openapi_spec() -> dict:
    """Describe the local API so it can be driven by other tools."""
    def body(props):
        return {"required": True, "content": {"application/json": {
            "schema": {"type": "object", "properties": props}}}}
    ok = {"200": {"description": "OK"}}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "CV Studio local API",
            "version": VERSION,
            "description": (
                "Read, edit and render CVs. Binds to 127.0.0.1 by default with no "
                "authentication. Start the server with --token to require an "
                "X-API-Key header; a token is generated automatically when "
                "binding beyond loopback."),
        },
        "paths": {
            "/api/state": {"get": {"summary": "Workspace, documents, themes, fonts",
                                   "responses": ok}},
            "/api/doc": {"get": {"summary": "Read one CV",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}}], "responses": ok}},
            "/api/save": {"post": {"summary": "Save a CV (whole yaml, or field patches)",
                "requestBody": body({
                    "path": {"type": "string"},
                    "yaml": {"type": "string", "description": "Replace the whole file"},
                    "patches": {"type": "array", "description":
                        "Field edits; preserves comments",
                        "items": {"type": "object", "properties": {
                            "path": {"type": "array", "items": {}},
                            "value": {}}}}}), "responses": ok}},
            "/api/render": {"post": {"summary": "Render to PDF and PNG",
                "requestBody": body({"path": {"type": "string"}}), "responses": ok}},
            "/api/preview": {"post": {"summary":
                "Render unsaved content without writing the file",
                "requestBody": body({"path": {"type": "string"},
                                     "yaml": {"type": "string"},
                                     "patches": {"type": "array", "items": {}}}),
                "responses": ok}},
            "/api/new": {"post": {"summary": "Create a CV, blank or duplicated",
                "requestBody": body({"name": {"type": "string"},
                                     "from": {"type": "string"}}), "responses": ok}},
            "/api/asset": {"get": {"summary": "Fetch a rendered PDF or PNG",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}}], "responses": ok}},
        },
        "components": {"securitySchemes": {"apiKey": {
            "type": "apiKey", "in": "header", "name": "X-API-Key"}}},
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "CVStudio"

    def log_message(self, *args):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _authed(self) -> bool:
        """No token means loopback-only and open; a token means always required.

        A token is mandatory when binding beyond loopback, because at that point
        anything on the network could otherwise read and rewrite the user's CVs.
        """
        if API_TOKEN is None:
            return True
        supplied = self.headers.get("X-API-Key") or parse_qs(
            urlparse(self.path).query).get("token", [None])[0]
        return supplied == API_TOKEN

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/") and not self._authed():
            return self._json({"error": "unauthorised: supply X-API-Key"}, 401)
        try:
            if u.path == "/":
                page = INDEX_HTML.replace(
                    "__API_TOKEN__", json.dumps(API_TOKEN))
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._json({
                    "documents": list_documents(),
                    "themes": available_themes(),
                    "page_sizes": PAGE_SIZES,
                    "fonts": font_families(),
                    "workspace": str(WORKSPACE),
                    "first_run": FIRST_RUN,
                    "version": VERSION,
                    "platform": sys.platform,
                    "server_launch": server_launch(),
                    "api_token": API_TOKEN,
                    "port": self.server.server_address[1],
                })
            if u.path == "/api/design-schema":
                return self._json(design_schema(q.get("theme", ["classic"])[0]))
            if u.path == "/api/openapi.json":
                return self._json(openapi_spec())
            if u.path == "/api/doc":
                return self._json(load_doc(safe_path(q["path"][0])))
            if u.path == "/api/asset":
                p = safe_path(q["path"][0])
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                return self._send(200, p.read_bytes(), ctype)
            return self._json({"error": "not found"}, 404)
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 403)
        except FileNotFoundError:
            return self._json({"error": "file not found"}, 404)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/") and not self._authed():
            return self._json({"error": "unauthorised: supply X-API-Key"}, 401)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return self._json({"error": "bad request"}, 400)
        try:
            if u.path == "/api/save":
                p = safe_path(payload["path"])
                if "yaml" in payload:
                    p.write_text(payload["yaml"], encoding="utf-8")
                elif "patches" in payload:
                    apply_patches(p, payload["patches"])
                return self._json({"ok": True, **load_doc(p)})
            if u.path == "/api/render":
                return self._json(render(safe_path(payload["path"])))
            if u.path == "/api/preview":
                return self._json(preview(safe_path(payload["path"]),
                                          payload.get("yaml"),
                                          payload.get("patches")))
            if u.path == "/api/new":
                raw = payload.get("name") or "new-cv"
                name = "".join(c for c in raw if c.isalnum() or c in "-_ ").strip()
                if not name:
                    return self._json({"error": "Please give the CV a name."}, 400)
                dest = safe_path(f"profile/{name}.yaml")
                if dest.exists():
                    return self._json({"error": "A CV with that name already exists."}, 409)
                src = payload.get("from")
                dest.write_text(
                    safe_path(src).read_text(encoding="utf-8") if src else STARTER_CV,
                    encoding="utf-8")
                return self._json({"ok": True, "path": rel(dest)})
            return self._json({"error": "not found"}, 404)
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 403)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


INDEX_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CV Studio</title>
<style>
:root{
 --bg:#f2f2ef;--panel:#fff;--sunk:#fafaf8;--ink:#17160f;--muted:#6d6c64;--faint:#96958c;
 --line:#e3e2dc;--line2:#eeede8;--accent:#1f6f4f;--accent-ink:#fff;--warn:#a8412a;--warn-bg:#fdf1ee;
 --r:9px;--shadow:0 1px 2px rgba(0,0,0,.05),0 6px 18px rgba(0,0,0,.05);
 --tk-key:#0f6fa8;--tk-str:#2f7d43;--tk-num:#7b46c9;--tk-bool:#a8412a;
 --tk-com:#8a887f;--tk-punc:#a3a29a;--tk-blk:#b8641a;--tk-sel:rgba(31,111,79,.16)}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
 --bg:#111110;--panel:#1a1a18;--sunk:#141413;--ink:#eeece4;--muted:#9b998f;--faint:#6f6d64;
 --line:#2a2a25;--line2:#232320;--accent:#7fd0a6;--accent-ink:#10100f;--warn:#e8907a;--warn-bg:#2a1b16;
 --shadow:0 1px 2px rgba(0,0,0,.4),0 6px 18px rgba(0,0,0,.3);
 --tk-key:#7cc2e6;--tk-str:#93d5a2;--tk-num:#c6a4f2;--tk-bool:#e8907a;
 --tk-com:#6a6860;--tk-punc:#5d5b54;--tk-blk:#e0b07a;--tk-sel:rgba(127,208,166,.2)}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);overflow:hidden;display:grid;grid-template-rows:auto 1fr;
 font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
 -webkit-font-smoothing:antialiased}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:5px}

header{display:flex;align-items:center;gap:9px;padding:9px 14px;background:var(--panel);
 border-bottom:1px solid var(--line);flex-wrap:wrap;min-height:52px}
.brand{display:flex;align-items:center;gap:8px;font-weight:650;font-size:13.5px;
 letter-spacing:-.01em;margin-right:4px;white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--accent);flex:none}
.sep{width:1px;height:20px;background:var(--line);margin:0 2px}
.ctl{display:flex;align-items:center;gap:5px}
.ctl label{font-size:11px;color:var(--faint)}
select,button,input[type=text]{font:inherit;font-size:13px;color:var(--ink);background:var(--panel);
 border:1px solid var(--line);border-radius:7px;padding:5px 8px}
select{max-width:170px;cursor:pointer}
select:hover,button:hover:not(:disabled){border-color:var(--muted)}
button{cursor:pointer;white-space:nowrap}
button:disabled{opacity:.45;cursor:default}
.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.primary:hover:not(:disabled){filter:brightness(1.07)}
.ghost{background:none;border-color:transparent;color:var(--muted)}
.ghost:hover:not(:disabled){background:var(--sunk);color:var(--ink)}
.grow{flex:1}
kbd{font:11px ui-monospace,Consolas,monospace;background:var(--sunk);border:1px solid var(--line);
 border-bottom-width:2px;border-radius:4px;padding:1px 4px;color:var(--muted)}
/* Custom title bar controls. Sized to the Windows convention (46x32) so the
   window feels native even though the frame is drawn by the app. */
.wctl{display:flex;gap:0;margin-left:6px;margin-right:-14px;align-self:stretch}
.wctl[hidden]{display:none}
.wctl button{width:46px;border:0;border-radius:0;background:none;color:var(--muted);
 display:grid;place-items:center;padding:0;align-self:stretch}
.wctl button:hover{background:var(--sunk);color:var(--ink)}
.wctl #w-close:hover{background:#c42b1c;color:#fff}
header{-webkit-app-region:drag}
header button,header select,header label,header .wctl{-webkit-app-region:no-drag}
.dirty{width:7px;height:7px;border-radius:50%;background:var(--accent);flex:none;opacity:0;
 transition:opacity .15s}
.dirty.on{opacity:1}

main{display:grid;grid-template-columns:214px 6px minmax(320px,1fr) 6px minmax(340px,1.05fr);
 overflow:hidden;min-height:0}
.gut{cursor:col-resize;position:relative}
.gut::after{content:"";position:absolute;inset:0 2px;border-radius:3px}
.gut:hover::after,.gut.drag::after{background:var(--accent);opacity:.35}
aside,.mid,.prev{min-height:0}
aside{background:var(--panel);border-right:1px solid var(--line);padding:9px 0 30px;overflow-y:auto}
.mid{border-right:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden}
.prev{background:var(--sunk);overflow-y:auto}

.side-hd{display:flex;align-items:center;justify-content:space-between;padding:4px 8px 8px 12px}
.side-hd b{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint);font-weight:650}
aside h2{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint);
 margin:12px 12px 5px;font-weight:650}
.doc{display:flex;align-items:center;gap:7px;width:calc(100% - 12px);margin:1px 6px;padding:6px 9px;
 border:0;background:none;border-radius:6px;text-align:left;font-size:13px;color:var(--ink)}
.doc:hover{background:var(--sunk)}
.doc.active{background:var(--accent);color:var(--accent-ink);font-weight:600}
.doc svg{flex:none;opacity:.5}
.doc.active svg{opacity:.9}
.doc span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.tabs{display:flex;gap:2px;padding:9px 12px 0;background:var(--bg);flex:none}
.tab{border:1px solid transparent;background:none;padding:6px 12px;border-radius:7px 7px 0 0;
 color:var(--muted);font-size:13px}
.tab[aria-selected=true]{background:var(--panel);color:var(--ink);font-weight:600;
 border-color:var(--line);border-bottom-color:var(--panel);margin-bottom:-1px}
.pane{flex:1;min-height:0;overflow-y:auto;padding:13px;background:var(--panel);
 border-top:1px solid var(--line)}
#pane-yaml{padding:0;overflow:hidden;display:flex}
/* after #pane-yaml, so the id rule cannot out-specify hiding it */
.pane[hidden],#pane-yaml[hidden]{display:none}

.edwrap{position:relative;flex:1;min-height:0;background:var(--panel)}
.edwrap pre,.edwrap textarea{position:absolute;inset:0;margin:0;padding:13px 15px;border:0;
 font:12.5px/1.62 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre;overflow:auto;tab-size:2}
.edwrap pre{pointer-events:none;color:var(--ink)}
.edwrap textarea{background:transparent;color:transparent;caret-color:var(--accent);resize:none;outline:none}
.edwrap textarea::selection{background:var(--tk-sel)}
.t-key{color:var(--tk-key)}.t-str{color:var(--tk-str)}.t-num{color:var(--tk-num)}
.t-bool{color:var(--tk-bool)}.t-com{color:var(--tk-com);font-style:italic}
.t-punc{color:var(--tk-punc)}.t-blk{color:var(--tk-blk);font-weight:600}

.grp{border:1px solid var(--line);border-radius:var(--r);margin-bottom:11px;background:var(--panel);
 overflow:hidden}
.grp>summary{list-style:none;cursor:pointer;padding:9px 12px;display:flex;align-items:center;gap:8px;
 font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:650;
 background:var(--sunk);border-bottom:1px solid transparent}
.grp>summary::-webkit-details-marker{display:none}
.grp[open]>summary{border-bottom-color:var(--line)}
.grp>summary .chev{transition:transform .15s;opacity:.6}
.grp[open]>summary .chev{transform:rotate(90deg)}
.grp>summary .count{margin-left:auto;font-size:10.5px;color:var(--faint);letter-spacing:0;
 text-transform:none}
.grp .body{padding:11px 12px}
.f{display:grid;grid-template-columns:104px 1fr;gap:9px;align-items:start;margin-bottom:8px}
.f>label{color:var(--muted);font-size:12px;padding-top:6px;overflow:hidden;text-overflow:ellipsis}
.f input,.f textarea{width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 9px;
 background:var(--sunk);color:var(--ink);font:inherit;font-size:13px}
.f input:focus,.f textarea:focus{background:var(--panel);border-color:var(--accent);outline:none}
.f textarea{font:12.5px/1.6 ui-monospace,Consolas,monospace;resize:vertical;min-height:66px}
.dgrid{display:grid;grid-template-columns:158px 1fr;gap:9px;align-items:center;margin-bottom:7px}
.dgrid>label{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dctl{display:flex;align-items:center;gap:6px;min-width:0}
.dctl input[type=text],.dctl input[type=number],.dctl select{border:1px solid var(--line);
 border-radius:6px;padding:5px 8px;background:var(--sunk);color:var(--ink);font:inherit;
 font-size:12.5px;min-width:0;flex:1}
.dctl input:focus,.dctl select:focus{background:var(--panel);border-color:var(--accent);outline:none}
.dctl input[type=number]{max-width:92px;flex:none}
.dctl select.unit{max-width:72px;flex:none}
.dctl input[type=color]{width:34px;height:26px;padding:0;border:1px solid var(--line);
 border-radius:6px;background:none;cursor:pointer;flex:none}
.dctl input[type=checkbox]{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}
.dctl .hex{font:11.5px ui-monospace,Consolas,monospace;color:var(--muted);flex:none}
.dnote{color:var(--faint);font-size:12px;margin:0 0 11px;line-height:1.6}
.entry{border:1px solid var(--line);border-radius:8px;padding:10px 11px;margin:9px 0;background:var(--sunk)}
.entry-hd{display:flex;align-items:center;gap:8px;margin-bottom:9px}
.entry-hd b{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.pbar{position:sticky;top:0;z-index:3;display:flex;align-items:center;gap:10px;padding:9px 14px;
 background:var(--sunk);border-bottom:1px solid var(--line);font-size:12px;color:var(--muted);flex-wrap:wrap}
.pbar b{color:var(--ink)}
.pill{display:inline-flex;align-items:center;gap:5px;background:var(--panel);border:1px solid var(--line);
 border-radius:99px;padding:3px 9px}
.live-ok{color:var(--accent);border-color:var(--accent)}
.live-working{color:var(--muted)}
.live-bad{color:var(--warn);border-color:var(--warn);max-width:230px;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
.zoom{display:flex;align-items:center;gap:2px;margin-left:auto}
.zoom button{padding:2px 8px;font-size:13px}
.pages{padding:14px}
.pg{width:100%;display:block;margin:0 auto 13px;background:#fff;border:1px solid var(--line);
 border-radius:7px;box-shadow:var(--shadow)}
.err{margin:14px;background:var(--warn-bg);border:1px solid var(--warn);border-radius:var(--r);
 padding:13px 15px;color:var(--warn)}
.err h4{margin:0 0 7px;font-size:13px}
.err .hint{color:var(--ink);background:var(--panel);border-radius:6px;padding:9px 11px;margin:9px 0 0;
 font-size:12.5px;line-height:1.6}
.err pre{margin:9px 0 0;white-space:pre-wrap;font:11.5px/1.5 ui-monospace,Consolas,monospace;
 max-height:210px;overflow:auto;opacity:.85}

.empty{padding:44px 26px;text-align:center;color:var(--muted)}
.empty h3{margin:0 0 7px;font-size:15px;color:var(--ink)}
.empty p{margin:0 auto;max-width:44ch;font-size:13px;line-height:1.65}
.empty .cta{margin-top:16px}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--line);
 border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
.skel{background:linear-gradient(90deg,var(--line2),var(--line),var(--line2));background-size:200% 100%;
 animation:sh 1.3s ease-in-out infinite;border-radius:7px}
@keyframes sh{0%{background-position:200% 0}100%{background-position:-200% 0}}

#toasts{position:fixed;bottom:18px;right:18px;z-index:50;display:flex;flex-direction:column;gap:8px;
 align-items:flex-end;pointer-events:none}
.toast{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);
 border-radius:8px;padding:9px 13px;box-shadow:var(--shadow);font-size:13px;max-width:340px;
 animation:in .18s ease-out}
.toast.bad{border-left-color:var(--warn);color:var(--warn)}
@keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

dialog{border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--ink);
 padding:0;box-shadow:0 18px 60px rgba(0,0,0,.28);max-width:420px;width:calc(100% - 40px)}
dialog::backdrop{background:rgba(0,0,0,.42)}
dialog .dh{padding:16px 18px 0}
dialog h3{margin:0 0 5px;font-size:15px}
dialog p{margin:0;color:var(--muted);font-size:13px;line-height:1.6}
dialog .db{padding:14px 18px}
dialog input{width:100%}
dialog .df{display:flex;justify-content:flex-end;gap:8px;padding:0 18px 16px}
dialog.wide{max-width:660px}
.stabs{display:flex;gap:2px;padding:12px 18px 0;border-bottom:1px solid var(--line)}
.stab{border:0;background:none;padding:7px 11px;border-radius:7px 7px 0 0;color:var(--muted);
 font-size:12.5px;cursor:pointer}
.stab[aria-selected=true]{color:var(--ink);font-weight:600;background:var(--sunk)}
.sbody{max-height:56vh;overflow-y:auto;font-size:13px;line-height:1.65}
.sbody[hidden]{display:none}
.sbody h4{margin:16px 0 6px;font-size:12.5px;letter-spacing:.01em}
.sbody h4:first-child{margin-top:0}
.sbody p{margin:0 0 8px;color:var(--ink)}
.sbody p.muted{color:var(--muted);font-size:12.5px}
.sbody ol{margin:0 0 10px;padding-left:20px}
.sbody li{margin-bottom:5px}
.sbody code{background:var(--sunk);border:1px solid var(--line);border-radius:4px;
 padding:1px 5px;font:11.5px ui-monospace,Consolas,monospace}
pre.code{background:var(--sunk);border:1px solid var(--line);border-radius:8px;padding:11px 13px;
 font:11.5px/1.6 ui-monospace,Consolas,monospace;overflow-x:auto;margin:0 0 8px;white-space:pre}
table.api{width:100%;border-collapse:collapse;margin:0 0 10px}
table.api td{padding:5px 0;border-bottom:1px solid var(--line2);vertical-align:top}
table.api td:first-child{width:210px;white-space:nowrap}
table.api td:last-child{color:var(--muted);font-size:12.5px}
</style></head><body>

<header data-tauri-drag-region>
  <div class="brand" data-tauri-drag-region><span class="dot"></span>CV Studio</div>
  <div class="sep"></div>
  <div class="ctl"><label for="theme">Theme</label><select id="theme"></select></div>
  <div class="ctl"><label for="font">Font</label><select id="font"></select></div>
  <div class="ctl"><label for="pagesize">Page</label><select id="pagesize"></select></div>
  <label class="ctl" style="gap:5px;cursor:pointer" title="Re-render as you type, without saving">
    <input type="checkbox" id="live" checked style="accent-color:var(--accent)">
    <span style="font-size:12px;color:var(--muted)">Live</span></label>
  <span class="dirty" id="dirty" title="Unsaved changes"></span>
  <button id="save" class="primary">Save &amp; Render</button>
  <span class="grow"></span>
  <span style="font-size:11.5px;color:var(--faint)"><kbd>Ctrl</kbd>+<kbd>S</kbd> saves</span>
  <button id="pdf" class="ghost" disabled>Open PDF</button>
  <button id="settings" class="ghost" title="Settings, setup and help" aria-label="Settings">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="2"><circle cx="12" cy="12" r="3"/>
     <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65
     1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9
     19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0
     .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65
     0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0
     0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2
     2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1
     0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
  <div class="wctl" id="wctl" hidden>
    <button id="w-min" title="Minimise" aria-label="Minimise"><svg width="10" height="10"
      viewBox="0 0 10 10"><rect x="0" y="4.5" width="10" height="1" fill="currentColor"/></svg></button>
    <button id="w-max" title="Maximise" aria-label="Maximise"><svg width="10" height="10"
      viewBox="0 0 10 10"><rect x="0.5" y="0.5" width="9" height="9" fill="none"
      stroke="currentColor"/></svg></button>
    <button id="w-close" title="Close" aria-label="Close"><svg width="10" height="10"
      viewBox="0 0 10 10"><path d="M0 0 L10 10 M10 0 L0 10" stroke="currentColor"
      fill="none"/></svg></button>
  </div>
</header>

<main>
  <aside>
    <div class="side-hd"><b>Documents</b><button id="new" class="ghost" title="New CV">+ New</button></div>
    <div id="files"></div>
  </aside>
  <div class="gut" data-target="0"></div>
  <div class="mid">
    <div class="tabs" role="tablist">
      <button class="tab" role="tab" aria-selected="true" data-tab="form">Form</button>
      <button class="tab" role="tab" aria-selected="false" data-tab="design">Design</button>
      <button class="tab" role="tab" aria-selected="false" data-tab="yaml">YAML</button>
    </div>
    <div class="pane" id="pane-form"></div>
    <div class="pane" id="pane-design" hidden></div>
    <div class="pane" id="pane-yaml" hidden>
      <div class="edwrap"><pre id="hl" aria-hidden="true"></pre>
      <textarea id="yaml" spellcheck="false" aria-label="CV source"></textarea></div>
    </div>
  </div>
  <div class="gut" data-target="1"></div>
  <div class="prev" id="preview"></div>
</main>

<div id="toasts" aria-live="polite"></div>

<dialog id="setdlg" class="wide">
  <div class="dh"><h3>Settings &amp; setup</h3>
    <p>Everything here runs on your machine. Nothing is sent anywhere.</p></div>
  <div class="stabs" role="tablist">
    <button class="stab" aria-selected="true" data-s="start">Getting started</button>
    <button class="stab" aria-selected="false" data-s="ai">Claude Desktop</button>
    <button class="stab" aria-selected="false" data-s="api">API</button>
    <button class="stab" aria-selected="false" data-s="about">About</button>
  </div>
  <div class="db sbody" id="s-start">
    <h4>Writing a CV</h4>
    <ol>
      <li>Pick a CV on the left, or press <b>+ New</b>. Duplicate an existing one to
          tailor it for a specific job, one file per application.</li>
      <li>Edit in the <b>Form</b> tab, or the <b>YAML</b> tab for full control.</li>
      <li>With <b>Live</b> on, the preview re-renders as you type. Your file is only
          written when you press <b>Save</b> (<kbd>Ctrl</kbd>+<kbd>S</kbd>).</li>
      <li>Watch the page count. Two pages is normal for ten years of experience;
          one page if the employer asks for it.</li>
    </ol>
    <h4>Two mistakes that break rendering</h4>
    <p><b>A colon inside a bullet.</b> <code>- Built the thing: it worked</code> is read
    by YAML as a field, not text. Put it in quotes, or use an en dash instead.</p>
    <p><b>An implausible phone number.</b> Numbers are checked against real numbering
    plans, not just their shape, so a well-formed but unassigned number is rejected.
    Use the full international form.</p>
    <h4>Where your files live</h4>
    <p>Plain YAML in <code id="s-ws"></code>, with no database. Back them up by copying the
    folder; put it in git and you get full history.</p>
  </div>
  <div class="db sbody" id="s-ai" hidden>
    <h4>Let Claude Desktop edit and render your CVs</h4>
    <p>CV Studio includes an MCP server. Once connected, Claude can read your CVs, edit
    fields, create tailored copies, and <b>see</b> the rendered page to check the layout.</p>
    <ol>
      <li>Open Claude Desktop → <b>Settings → Developer → Edit Config</b>.</li>
      <li>Paste this in, then restart Claude Desktop.</li>
    </ol>
    <pre id="s-mcp" class="code"></pre>
    <button class="mini" data-copy="s-mcp">Copy config</button>
    <p class="muted">The tools then appear under the connectors icon. This is the same
    program you are using now, started in a different mode. Nothing extra to install.</p>
  </div>
  <div class="db sbody" id="s-api" hidden>
    <h4>Local HTTP API</h4>
    <p>Drive CV Studio from your own scripts. It listens on loopback only.</p>
    <table class="api">
      <tr><td><code>GET /api/state</code></td><td>Workspace, documents, themes, fonts</td></tr>
      <tr><td><code>GET /api/doc?path=</code></td><td>Read one CV</td></tr>
      <tr><td><code>POST /api/save</code></td><td>Save whole YAML, or field patches</td></tr>
      <tr><td><code>POST /api/render</code></td><td>Render to PDF + PNG</td></tr>
      <tr><td><code>POST /api/preview</code></td><td>Render unsaved content</td></tr>
      <tr><td><code>POST /api/new</code></td><td>Create a CV</td></tr>
      <tr><td><code>GET /api/asset?path=</code></td><td>Fetch a rendered file</td></tr>
    </table>
    <p>Base URL <code id="s-base"></code> · full spec at
      <a id="s-spec" href="#" target="_blank">/api/openapi.json</a></p>
    <h4>Example</h4>
    <pre id="s-curl" class="code"></pre>
    <button class="mini" data-copy="s-curl">Copy</button>
    <p class="muted" id="s-auth"></p>
  </div>
  <div class="db sbody" id="s-about" hidden>
    <h4>CV Studio <span id="s-ver"></span></h4>
    <p>A local CV editor built on RenderCV and Typst. No account, no telemetry,
    no network access.</p>
    <p class="muted">MIT licensed. Bundles RenderCV (MIT), Typst (Apache-2.0) and the
    RenderCV font set (SIL Open Font License / Apache-2.0).</p>
  </div>
  <div class="df"><button id="setclose" class="primary">Done</button></div>
</dialog>

<dialog id="newdlg">
  <div class="dh"><h3>New CV</h3><p>Saved as a plain YAML file in your workspace. Duplicate the
   one you have open, or start from a blank template.</p></div>
  <div class="db"><input type="text" id="newname" placeholder="e.g. cv-english" autocomplete="off"></div>
  <div class="df">
    <button id="newcancel" class="ghost">Cancel</button>
    <button id="newdup">Duplicate current</button>
    <button id="newblank" class="primary">Create blank</button>
  </div>
</dialog>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const API_TOKEN=__API_TOKEN__;
const S={path:null,doc:null,tab:"form",dirty:false,pdf:null,zoom:1,busy:false};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function toast(msg,bad){
  const t=document.createElement("div");
  t.className="toast"+(bad?" bad":""); t.textContent=msg;
  $("#toasts").append(t);
  setTimeout(()=>{t.style.transition="opacity .3s";t.style.opacity="0";
    setTimeout(()=>t.remove(),320)}, bad?5200:2200);
}
window.studioError=m=>{$("#preview").innerHTML=
  `<div class="err"><h4>Could not start</h4><p>${esc(m)}</p></div>`};

const api=async(u,o)=>{
  o=o||{};
  if(API_TOKEN){ o.headers=Object.assign({},o.headers,{"X-API-Key":API_TOKEN}) }
  const r=await fetch(u,o);
  const j=await r.json().catch(()=>({error:"The renderer sent an unreadable response."}));
  if(j&&j.error&&!("ok"in j)) throw new Error(j.error);
  return j;
};
const fill=(el,items)=>el.innerHTML=items.map(t=>`<option>${esc(t)}</option>`).join("");

async function boot(){
  let d;
  try{ d=await api("/api/state") }catch(e){ return window.studioError(e.message) }
  S.state=d;
  fill($("#theme"),d.themes); fill($("#pagesize"),d.page_sizes); fill($("#font"),d.fonts);
  renderSidebar(d.documents);
  if(d.documents.length) openDoc(d.documents[0].path);
  else $("#preview").innerHTML=`<div class="empty"><h3>No CVs yet</h3>
    <p>Create one to get started. It is saved as a plain YAML file in your workspace,
    so you always own it. No database, nothing locked in.</p>
    <div class="cta"><button class="primary" id="firstcta">Create a CV</button></div></div>`;
  const cta=$("#firstcta"); if(cta) cta.onclick=()=>$("#new").click();
  if(d.first_run) toast("Workspace created at "+d.workspace);
}

function renderSidebar(docs){
  const groups={};
  docs.forEach(x=>{(groups[x.group] ||= []).push(x)});
  const icon=`<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
   stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
   <path d="M14 2v6h6"/></svg>`;
  $("#files").innerHTML = docs.length ? Object.entries(groups).map(([g,items])=>
    `<h2>${esc(g)}</h2>`+items.map(i=>
      `<button class="doc" data-path="${esc(i.path)}">${icon}<span>${esc(i.label)}</span></button>`
    ).join("")).join("")
   : `<p style="color:var(--faint);font-size:12.5px;padding:10px 12px">No documents yet.</p>`;
}
$("#files").onclick=e=>{
  const b=e.target.closest("[data-path]");
  if(b && (!S.dirty || confirm("You have unsaved changes. Discard them?"))) openDoc(b.dataset.path);
};

async function openDoc(path){
  S.path=path; S.dirty=false; markDirty();
  $$(".doc").forEach(b=>b.classList.toggle("active",b.dataset.path===path));
  $("#pane-form").innerHTML=`<div class="skel" style="height:118px;margin-bottom:11px"></div>
    <div class="skel" style="height:190px"></div>`;
  try{
    const doc=await api("/api/doc?path="+encodeURIComponent(path));
    S.doc=doc; $("#yaml").value=doc.yaml; paint();
    const dz=doc.data?.design||{};
    if(dz.theme) $("#theme").value=dz.theme;
    if(dz.page&&dz.page.size) $("#pagesize").value=dz.page.size;
    const fam=dz.typography&&dz.typography.font_family&&dz.typography.font_family.body;
    if(fam) $("#font").value=fam;
    if(doc.parse_error) toast("This file has a YAML error. Fix it in the YAML tab.",true);
    if($("#pane-design").dataset.built) buildDesign();
    buildForm(); doRender();
  }catch(e){ toast(e.message,true) }
}

function field(label,value,path,multi){
  const p=esc(JSON.stringify(path));
  return `<div class="f"><label title="${esc(label)}">${esc(label)}</label>`+
    (multi?`<textarea data-path='${p}' rows="3">${esc(value)}</textarea>`
          :`<input data-path='${p}' value="${esc(value)}">`)+`</div>`;
}
function buildForm(){
  const cv=S.doc&&S.doc.data&&S.doc.data.cv;
  if(!cv){$("#pane-form").innerHTML=`<div class="empty"><h3>Can't show a form</h3>
    <p>This file has a YAML error, so it can't be parsed into fields.
    Switch to the YAML tab to fix it.</p></div>`;return}
  const chev=`<svg class="chev" width="11" height="11" viewBox="0 0 24 24" fill="none"
   stroke="currentColor" stroke-width="3"><path d="M9 18l6-6-6-6"/></svg>`;
  let h=`<details class="grp" open><summary>${chev}Header</summary><div class="body">`;
  ["name","headline","location","email","phone","website"].forEach(k=>{
    if(k in cv||["name","headline","location","email"].includes(k)) h+=field(k,cv[k],["cv",k]);
  });
  h+=`</div></details>`;
  const sections=cv.sections||{};
  for(const sname of Object.keys(sections)){
    const list=sections[sname]||[];
    h+=`<details class="grp" open><summary>${chev}${esc(sname)}
      <span class="count">${list.length} item${list.length===1?"":"s"}</span></summary><div class="body">`;
    list.forEach((it,i)=>{
      if(typeof it==="string"||it===null){
        h+=field("text "+(i+1),it,["cv","sections",sname,i],true);
      }else{
        const title=it.company||it.institution||it.name||it.label||("entry "+(i+1));
        h+=`<div class="entry"><div class="entry-hd"><b>${esc(title)}</b></div>`;
        for(const k of Object.keys(it)){
          const v=it[k];
          h+= Array.isArray(v) ? field(k,v.join("\n"),["cv","sections",sname,i,k],true)
                               : field(k,v,["cv","sections",sname,i,k]);
        }
        h+=`</div>`;
      }
    });
    h+=`</div></details>`;
  }
  $("#pane-form").innerHTML=h;
}
$("#pane-form").addEventListener("input",()=>{S.dirty=true;markDirty();scheduleLive()});
const markDirty=()=>$("#dirty").classList.toggle("on",S.dirty);

function collectPatches(){
  const out=[];
  $$("#pane-form [data-path]").forEach(el=>{
    const path=JSON.parse(el.dataset.path);
    let node=S.doc.data;
    for(const k of path){ node = node==null ? undefined : node[k] }
    let v=el.value;
    if(Array.isArray(node)) v=v.split("\n").map(s=>s.trim()).filter(Boolean);
    else if(typeof node==="number"&&v.trim()!==""&&!isNaN(v)) v=Number(v);
    out.push({path,value:v});
  });
  out.push({path:["design","theme"],value:$("#theme").value});
  out.push({path:["design","page","size"],value:$("#pagesize").value});
  out.push({path:["design","typography","font_family","body"],value:$("#font").value});
  out.push({path:["design","typography","font_family","name"],value:$("#font").value});
  // Appended after the header shortcuts so an explicit Design-tab choice wins.
  collectDesignPatches().forEach(function(d){
    out.push({path:["design"].concat(d.path),value:d.value});
  });
  return out;
}

async function save(){
  if(!S.path||S.busy) return;
  S.busy=true; $("#save").disabled=true;
  try{
    const body=S.tab==="yaml" ? {path:S.path,yaml:$("#yaml").value}
                              : {path:S.path,patches:collectPatches()};
    const r=await api("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body)});
    S.doc=r; S.dirty=false; markDirty();
    $("#yaml").value=r.yaml; paint();
    if(S.tab!=="yaml") buildForm();
    await doRender();
  }catch(e){ toast(e.message,true) }
  finally{ S.busy=false; $("#save").disabled=false }
}

async function doRender(){
  if(!S.path) return;
  const pv=$("#preview");
  pv.innerHTML=`<div class="pbar"><span class="spin"></span> Rendering…</div>
    <div class="pages"><div class="skel" style="aspect-ratio:1/1.414"></div></div>`;
  try{
    const r=await api("/api/render",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:S.path})});
    if(!r.ok){
      S.pdf=null; $("#pdf").disabled=true;
      pv.innerHTML=`<div class="err"><h4>This CV didn't render</h4>
        ${r.hint?`<div class="hint">${esc(r.hint)}</div>`:""}
        <pre>${esc(r.error||"")}</pre></div>`;
      return;
    }
    S.pdf=r.pdf; $("#pdf").disabled=!r.pdf;
    const tq=API_TOKEN?"&token="+encodeURIComponent(API_TOKEN):"";
    pv.innerHTML=`<div class="pbar">
      <span id="pstats"><span class="pill"><b>${r.pages}</b> page${r.pages>1?"s":""}</span>
      <span class="pill"><b>${r.ats_words}</b> words an ATS reads</span></span>
      <span class="pill live-ok" id="livebadge">live</span>
      <span class="zoom"><button class="ghost" id="zo" title="Zoom out">−</button>
      <span id="zl" style="min-width:44px;text-align:center">${Math.round(S.zoom*100)}%</span>
      <button class="ghost" id="zi" title="Zoom in">+</button></span></div>
      <div class="pages" id="pages">${r.pngs.map(u=>
        `<img class="pg" src="${u}${tq}" alt="CV page" style="width:${S.zoom*100}%">`).join("")}</div>`;
    $("#zi").onclick=()=>setZoom(S.zoom+.15); $("#zo").onclick=()=>setZoom(S.zoom-.15);
  }catch(e){
    pv.innerHTML=`<div class="err"><h4>Render failed</h4><pre>${esc(e.message)}</pre></div>`;
  }
}
/* Live preview. Two things make this usable rather than annoying:
   a debounce so a render starts only once typing pauses, and a token so a slow
   render that finishes after a newer one cannot overwrite the fresher result.
   Transient errors while mid-edit leave the last good page on screen instead of
   flashing a red panel at every keystroke. */
let liveTimer=null, liveToken=0;
function scheduleLive(){
  if(!$("#live").checked||!S.path) return;
  clearTimeout(liveTimer);
  liveTimer=setTimeout(runLive,700);
}
async function runLive(){
  if(!S.path||!$("#live").checked) return;
  const token=++liveToken;
  setLiveBadge("working");
  const body=S.tab==="yaml" ? {path:S.path,yaml:$("#yaml").value}
                            : {path:S.path,patches:collectPatches()};
  try{
    const r=await api("/api/preview",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(token!==liveToken) return;              // a newer render already won
    if(r.ok){ paintPages(r); setLiveBadge("ok") }
    else setLiveBadge("bad", r.hint||"Not valid yet");
  }catch(e){ if(token===liveToken) setLiveBadge("bad",e.message) }
}
function setLiveBadge(state,msg){
  const el=$("#livebadge"); if(!el) return;
  el.className="pill live-"+state;
  el.textContent = state==="working" ? "rendering…"
                 : state==="ok" ? "live"
                 : (msg||"not valid yet");
  el.title = state==="bad" ? (msg||"") : "";
}
function paintPages(r){
  S.pdf=r.pdf; $("#pdf").disabled=!r.pdf;
  const c=$("#pages"); if(!c) return;
  const tq=API_TOKEN?"&token="+encodeURIComponent(API_TOKEN):"";
  c.innerHTML=r.pngs.map(u=>
    `<img class="pg" src="${u}${tq}" alt="CV page" style="width:${S.zoom*100}%">`).join("");
  const st=$("#pstats");
  if(st) st.innerHTML=`<span class="pill"><b>${r.pages}</b> page${r.pages>1?"s":""}</span>
    <span class="pill"><b>${r.ats_words}</b> words an ATS reads</span>`;
}

function setZoom(z){
  S.zoom=Math.min(2.5,Math.max(.4,z));
  $$("#pages .pg").forEach(i=>i.style.width=(S.zoom*100)+"%");
  $("#zl").textContent=Math.round(S.zoom*100)+"%";
}

/* ---- YAML highlighting: painted behind a transparent-text textarea so
   native undo, selection and IME keep working ---- */
function commentAt(s){
  let q=null;
  for(let i=0;i<s.length;i++){
    const c=s[i];
    if(q){ if(c===q) q=null; continue }
    if(c==='"'||c==="'"){ q=c; continue }
    if(c==="#"&&(i===0||/\s/.test(s[i-1]))) return i;
  }
  return -1;
}
function hlScalar(v){
  if(!v) return "";
  const t=v.trim(); if(!t) return v;
  const lead=v.slice(0,v.indexOf(t[0])), tail=v.slice(lead.length+t.length);
  let inner;
  if(/^[|>][-+]?\d*$/.test(t))                     inner=`<span class="t-blk">${t}</span>`;
  else if(/^".*"$/.test(t)||/^'.*'$/.test(t))      inner=`<span class="t-str">${t}</span>`;
  else if(/^(true|false|null|~|yes|no)$/i.test(t)) inner=`<span class="t-bool">${t}</span>`;
  else if(/^-?\d+(\.\d+)?$/.test(t))               inner=`<span class="t-num">${t}</span>`;
  else if(/^\d{4}-\d{2}(-\d{2})?$/.test(t))        inner=`<span class="t-num">${t}</span>`;
  else                                             inner=t;
  return lead+inner+tail;
}
function hlLine(line){
  const s=line.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  const whole=s.match(/^(\s*)(#.*)$/);
  if(whole) return `${whole[1]}<span class="t-com">${whole[2]}</span>`;
  let code=s, comment="";
  const ci=commentAt(s);
  if(ci>=0){ code=s.slice(0,ci); comment=`<span class="t-com">${s.slice(ci)}</span>` }
  const m=code.match(/^(\s*)((?:-\s+)?)(.*)$/);
  let out=m[1]+(m[2]?`<span class="t-punc">${m[2]}</span>`:"");
  const kv=m[3].match(/^([^:\s][^:]*?)(:)(\s*)(.*)$/);
  out += kv ? `<span class="t-key">${kv[1]}</span><span class="t-punc">:</span>${kv[3]}${hlScalar(kv[4])}`
            : hlScalar(m[3]);
  return out+comment;
}
function paint(){
  const ta=$("#yaml");
  /* trailing spacer keeps both layers the same height so the caret stays aligned */
  $("#hl").innerHTML=ta.value.split("\n").map(hlLine).join("\n")+"\n ";
  $("#hl").scrollTop=ta.scrollTop; $("#hl").scrollLeft=ta.scrollLeft;
}

/* ---- resizable panes ---- */
$$(".gut").forEach(g=>g.addEventListener("mousedown",e=>{
  e.preventDefault(); g.classList.add("drag");
  const startX=e.clientX, main=$("main");
  const w=[...main.children].map(c=>c.getBoundingClientRect().width);
  const idx=+g.dataset.target;
  const move=ev=>{
    const dx=ev.clientX-startX;
    if(idx===0){
      const a=Math.min(340,Math.max(150,w[0]+dx));
      main.style.gridTemplateColumns=`${a}px 6px minmax(320px,1fr) 6px minmax(340px,1.05fr)`;
    }else{
      const a=main.children[0].getBoundingClientRect().width;
      const b=Math.min(900,Math.max(300,w[2]+dx));
      main.style.gridTemplateColumns=`${a}px 6px ${b}px 6px minmax(300px,1fr)`;
    }
  };
  const up=()=>{g.classList.remove("drag");
    document.removeEventListener("mousemove",move); document.removeEventListener("mouseup",up)};
  document.addEventListener("mousemove",move); document.addEventListener("mouseup",up);
}));

/* ---- wiring ---- */
$$(".tab").forEach(b=>b.onclick=()=>{
  S.tab=b.dataset.tab;
  $$(".tab").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
  $("#pane-form").hidden=S.tab!=="form";
  $("#pane-design").hidden=S.tab!=="design";
  $("#pane-yaml").hidden=S.tab!=="yaml";
  if(S.tab==="yaml") paint();
  if(S.tab==="design" && !$("#pane-design").dataset.built){
    $("#pane-design").dataset.built="1"; buildDesign();
  }
});
$("#save").onclick=save;
const touch=()=>{S.dirty=true;markDirty();scheduleLive()};
$("#theme").onchange=function(){touch(); if($("#pane-design").dataset.built) buildDesign()}; $("#pagesize").onchange=touch; $("#font").onchange=touch;
$("#yaml").addEventListener("input",()=>{paint();touch();scheduleLive()});
$("#yaml").addEventListener("scroll",()=>{
  $("#hl").scrollTop=$("#yaml").scrollTop; $("#hl").scrollLeft=$("#yaml").scrollLeft});
$("#pdf").onclick=()=>{if(S.pdf)window.open("/api/asset?path="+encodeURIComponent(S.pdf)
  +(API_TOKEN?"&token="+encodeURIComponent(API_TOKEN):""))};
document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){e.preventDefault();save()}
});
window.addEventListener("beforeunload",e=>{if(S.dirty){e.preventDefault();e.returnValue=""}});

/* ---- design panel, generated from RenderCV's own schema ----
   Built from the schema rather than a hand-written field list, so every option
   that exists in the YAML has a control here, and it stays correct when
   RenderCV adds or renames one. */
const UNITS=["cm","mm","in","pt","em","px"];
const rgb2hex=v=>{
  const m=/rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)/.exec(v||"");
  if(!m) return (v&&v[0]==="#")?v:"#000000";
  return "#"+[1,2,3].map(i=>(+m[i]).toString(16).padStart(2,"0")).join("");
};
const hex2rgb=h=>{
  const n=parseInt((h||"#000000").slice(1),16);
  return "rgb("+((n>>16)&255)+", "+((n>>8)&255)+", "+(n&255)+")";
};
const splitDim=v=>{
  const m=/^\s*(-?[\d.]+)\s*([a-z%]*)\s*$/i.exec(String(v==null?"":v));
  return m?{n:m[1],u:m[2]||"cm"}:{n:"",u:"cm"};
};
const atPath=(o,path)=>path.reduce((x,k)=>(x==null?undefined:x[k]),o);

async function buildDesign(){
  const host=$("#pane-design"), theme=$("#theme").value;
  host.innerHTML='<div class="dnote"><span class="spin"></span> loading options for '+esc(theme)+'…</div>';
  let sch;
  try{ sch=await api("/api/design-schema?theme="+encodeURIComponent(theme)) }
  catch(e){ host.innerHTML='<div class="empty"><p>'+esc(e.message)+'</p></div>'; return }
  const cur=(S.doc&&S.doc.data&&S.doc.data.design)||{};
  const chev='<svg class="chev" width="11" height="11" viewBox="0 0 24 24" fill="none" '+
    'stroke="currentColor" stroke-width="3"><path d="M9 18l6-6-6-6"/></svg>';
  const count=sch.groups.reduce((a,g)=>a+g.fields.length,0);
  let h='<div class="dnote">Every control writes straight into the <code>design</code> '+
        'block of your YAML. '+count+' options for this theme.</div>';
  sch.groups.forEach(function(g,gi){
    h+='<details class="grp"'+(gi<3?" open":"")+'><summary>'+chev+
       esc(g.name.replace(/_/g," "))+'<span class="count">'+g.fields.length+'</span>'+
       '</summary><div class="body">';
    g.fields.forEach(function(f){
      const raw=atPath(cur,f.path);
      const v=(raw===undefined||raw===null)?f.default:raw;
      const dp=esc(JSON.stringify(f.path));
      const label=esc(f.path[f.path.length-1].replace(/_/g," "));
      let ctl="";
      if(f.kind==="color"){
        ctl='<input type="color" data-dpath=\''+dp+'\' data-kind="color" value="'+rgb2hex(v)+'">'+
            '<span class="hex">'+esc(String(v==null?"":v))+'</span>';
      }else if(f.kind==="dimension"){
        const d=splitDim(v);
        ctl='<input type="number" step="0.05" data-dpath=\''+dp+'\' data-kind="dimension" value="'+esc(d.n)+'">'+
            '<select class="unit" data-unit-for=\''+dp+'\'>'+
            UNITS.map(function(x){return '<option'+(x===d.u?" selected":"")+'>'+x+'</option>'}).join("")+
            '</select>';
      }else if(f.kind==="enum"){
        ctl='<select data-dpath=\''+dp+'\' data-kind="enum">'+
            (f.options||[]).map(function(o){
              return '<option'+(String(o)===String(v)?" selected":"")+'>'+esc(o)+'</option>'}).join("")+
            '</select>';
      }else if(f.kind==="bool"){
        ctl='<input type="checkbox" data-dpath=\''+dp+'\' data-kind="bool"'+(v?" checked":"")+'>';
      }else if(f.kind==="number"){
        ctl='<input type="number" data-dpath=\''+dp+'\' data-kind="number" value="'+esc(v==null?"":v)+'">';
      }else if(f.kind==="list"){
        ctl='<input type="text" data-dpath=\''+dp+'\' data-kind="list" value="'+
            esc((v||[]).join(", "))+'" placeholder="comma separated">';
      }else{
        ctl='<input type="text" data-dpath=\''+dp+'\' data-kind="text" value="'+esc(v==null?"":v)+'">';
      }
      h+='<div class="dgrid"><label title="'+esc(f.path.join("."))+'">'+label+'</label>'+
         '<div class="dctl">'+ctl+'</div></div>';
    });
    h+='</div></details>';
  });
  host.innerHTML=h;
}

function collectDesignPatches(){
  const out=[];
  $$("#pane-design [data-dpath]").forEach(function(el){
    const path=JSON.parse(el.dataset.dpath), kind=el.dataset.kind;
    let v;
    if(kind==="color") v=hex2rgb(el.value);
    else if(kind==="bool") v=el.checked;
    else if(kind==="number") v=el.value===""?null:Number(el.value);
    else if(kind==="list") v=el.value.split(",").map(function(x){return x.trim()}).filter(Boolean);
    else if(kind==="dimension"){
      if(el.value==="") return;
      const u=el.parentElement.querySelector("select.unit");
      v=String(el.value)+((u&&u.value)||"cm");
    }
    else v=el.value;
    out.push({path:path,value:v});
  });
  return out;
}

$("#pane-design").addEventListener("input",function(e){
  if(e.target.dataset && e.target.dataset.kind==="color"){
    const sp=e.target.parentElement.querySelector(".hex");
    if(sp) sp.textContent=hex2rgb(e.target.value);
  }
  S.dirty=true;markDirty();scheduleLive();
});
$("#pane-design").addEventListener("change",function(){S.dirty=true;markDirty();scheduleLive()});

/* ---- custom title bar ----
   The page is served from the local server, so the Tauri API is only present
   when running inside the app. In a plain browser the controls stay hidden and
   everything else works unchanged. */
(function(){
  const T=window.__TAURI__;
  if(!T||!T.window) return;
  const win=T.window.getCurrentWindow();
  $("#wctl").hidden=false;
  $("#w-min").onclick=()=>win.minimize();
  $("#w-max").onclick=()=>win.toggleMaximize();
  $("#w-close").onclick=()=>win.close();
  /* Double-clicking the bar should maximise, which is what a native frame does
     and what people try first. */
  $("header").addEventListener("dblclick",e=>{
    if(e.target.closest("button,select,input,label")) return;
    win.toggleMaximize();
  });
})();

/* ---- settings ---- */
const setdlg=$("#setdlg");
$("#settings").onclick=()=>{fillSettings();setdlg.showModal()};
$("#setclose").onclick=()=>setdlg.close();
$$(".stab").forEach(b=>b.onclick=()=>{
  $$(".stab").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
  ["start","ai","api","about"].forEach(k=>$("#s-"+k).hidden = k!==b.dataset.s);
});
$$("[data-copy]").forEach(b=>b.onclick=async()=>{
  try{ await navigator.clipboard.writeText($("#"+b.dataset.copy).textContent);
       toast("Copied") }
  catch{ toast("Select the text and copy manually",true) }
});

function fillSettings(){
  const st=S.state||{};
  const base=location.origin;
  $("#s-ws").textContent = st.workspace||"";
  $("#s-base").textContent = base;
  $("#s-spec").href = base+"/api/openapi.json"+(st.api_token?"?token="+encodeURIComponent(st.api_token):"");
  $("#s-ver").textContent = "v"+(st.version||"");

  /* Claude Desktop needs a JSON-escaped command path; on Windows that means
     doubled backslashes, which is the single most common reason a pasted
     config silently fails. */
  const L=st.server_launch||{command:"cv-studio-server",args:[]};
  const args=[...(L.args||[]),"--mcp"];
  if(st.workspace) args.push("--workspace",st.workspace);
  const cfg={mcpServers:{"cv-studio":{command:L.command,args}}};
  $("#s-mcp").textContent=JSON.stringify(cfg,null,2);

  const auth = st.api_token ? ` \
  -H "X-API-Key: ${st.api_token}"` : "";
  $("#s-curl").textContent =
    `curl ${base}/api/state${auth}

`+
    `curl -X POST ${base}/api/render${auth} \
`+
    `  -H "Content-Type: application/json" \
`+
    `  -d '{"path":"profile/my-cv.yaml"}'`;
  $("#s-auth").textContent = st.api_token
    ? "This server requires an X-API-Key header, shown above."
    : "No key needed: the server accepts only local connections. Start it with "+
      "--token to require one, or --host to expose it (which forces a token).";
}

const dlg=$("#newdlg");
$("#new").onclick=()=>{$("#newname").value="";dlg.showModal();$("#newname").focus()};
$("#newcancel").onclick=()=>dlg.close();
async function create(from){
  const name=$("#newname").value.trim();
  if(!name){ $("#newname").focus(); return }
  try{
    const r=await api("/api/new",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name,from})});
    dlg.close();
    const st=await api("/api/state"); renderSidebar(st.documents);
    openDoc(r.path); toast("Created "+name);
  }catch(e){ toast(e.message,true) }
}
$("#newblank").onclick=()=>create(null);
$("#newdup").onclick=()=>create(S.path);
$("#newname").addEventListener("keydown",e=>{if(e.key==="Enter")create(null)});

boot();
</script></body></html>"""


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    global WORKSPACE, FIRST_RUN, API_TOKEN
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="CV Studio local server")
    ap.add_argument("--workspace", "--career-dir", dest="workspace", default=None)
    ap.add_argument("--port", type=int, default=8722)
    ap.add_argument("--host", default="127.0.0.1",
                    help="Bind address. Anything other than loopback forces a token.")
    ap.add_argument("--token", default=None,
                    help="Require this X-API-Key on every /api request.")
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    WORKSPACE = Path(args.workspace).resolve() if args.workspace else DEFAULT_WORKSPACE
    FIRST_RUN = bootstrap(WORKSPACE)

    API_TOKEN = args.token
    if args.host not in ("127.0.0.1", "localhost", "::1") and not API_TOKEN:
        # Reachable from the network without a token would mean anyone on it can
        # read and rewrite the user's CVs, so generate one rather than allow it.
        API_TOKEN = secrets.token_urlsafe(24)
        print(f"Generated API token: {API_TOKEN}")

    url = f"http://{args.host}:{args.port}/"
    # Loopback only: this reads and writes files and has no authentication.
    with Server((args.host, args.port), Handler) as httpd:
        print(f"CV Studio  -> {url}")
        if API_TOKEN:
            print("Auth       : X-API-Key required")
        print(f"Workspace  : {WORKSPACE}")
        print(f"Documents  : {len(list_documents())}")
        if args.open:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
