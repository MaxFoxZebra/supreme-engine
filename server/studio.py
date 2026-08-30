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
import os
import re
import secrets
import socketserver
import subprocess
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

try:
    import jobs as jobstore
except ImportError:  # the store lives beside the packaged server
    jobstore = None

# Vendored d3 modules for the funnel chart. In a frozen build PyInstaller
# unpacks data files under _MEIPASS; in a checkout they sit next to this file.
STATIC_DIR = Path(getattr(sys, "_MEIPASS", str(_HERE))) / "static"
if not STATIC_DIR.is_dir():
    STATIC_DIR = _HERE / "static"

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
VERSION = "0.2.1"


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


STARTER_LETTER = """# A cover letter, written as a RenderCV document so it renders with the same
# letterhead and typography as your CV. The section title becomes the subject
# line. Keep it under about 350 words and on one page.
cv:
  name: Your Name
  location: City, Country
  email: you@example.com
  phone: "+33-6-12-34-56-78"

  sections:
    Re Solutions Engineer at Company:
      - Dear Hiring Team,
      - >-
        Open with something only you could write about this company: a specific
        problem their product implies, or a genuine connection to your work. If
        this paragraph could be pasted into another application, it is not
        doing its job.
      - >-
        Then the strongest matching evidence, with a number in it, and one line
        of context the CV had no room for: what was hard about it, what the
        constraint was.
      - >-
        If there is a real gap, address it once, briefly, from strength. Name
        the adjacent experience that transfers. Do not apologise for it.
      - >-
        Close with what you would like to happen next. No flourish.
      - Kind regards,
      - Your Name

design:
  theme: engineeringclassic
  page:
    size: a4
    top_margin: 2cm
    bottom_margin: 2cm
    left_margin: 2cm
    right_margin: 2cm
    show_footer: false
  colors:
    name: rgb(0, 0, 0)
    section_titles: rgb(0, 0, 0)
    connections: rgb(70, 70, 70)
  typography:
    line_spacing: 0.75em
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

def watch_parent(pid: int) -> None:
    """Exit when the process that launched us goes away.

    Without this the renderer can outlive the window that spawned it (a crash,
    a force-quit, a killed parent) and keep holding its own DLLs open. The
    visible symptom is an installer failing with "Error opening file for
    writing" on the next upgrade, which is a confusing way to learn about an
    orphaned process.
    """
    def alive() -> bool:
        if sys.platform == "win32":
            import ctypes
            SYNCHRONIZE = 0x00100000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not h:
                return False
            try:
                # WAIT_OBJECT_0 means the process has already exited.
                return ctypes.windll.kernel32.WaitForSingleObject(h, 0) != 0
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False

    def loop() -> None:
        while True:
            time.sleep(2)
            if not alive():
                os._exit(0)

    threading.Thread(target=loop, daemon=True).start()


def bootstrap(workspace: Path) -> bool:
    """Create and seed the workspace. Returns True when this was a first run."""
    created = not workspace.exists()
    (workspace / "profile").mkdir(parents=True, exist_ok=True)
    (workspace / "applications").mkdir(exist_ok=True)
    (workspace / "letters").mkdir(exist_ok=True)
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
    letters_dir = WORKSPACE / "letters"
    if letters_dir.is_dir():
        for f in sorted(letters_dir.glob("*.y*ml")):
            if not f.name.startswith(".") and is_cv_yaml(f):
                docs.append({"path": rel(f), "label": f.stem, "group": "Cover letters"})
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
    out_dir = (WORKSPACE / "assets" / path.stem) if path.parent.name in ("profile", "letters") else (path.parent / "output")
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
            "/api/jobs": {
                "get": {"summary": "List job applications", "responses": ok},
                "post": {"summary": "Create a job application",
                         "requestBody": body({"title": {"type": "string"},
                                              "company": {"type": "string"},
                                              "status": {"type": "string"}}),
                         "responses": ok}},
            "/api/jobs/update": {"post": {"summary":
                "Update a job; a status change appends to its history",
                "requestBody": body({"id": {"type": "string"},
                                     "status": {"type": "string"}}), "responses": ok}},
            "/api/funnel": {"get": {"summary":
                "Application funnel: node counts, flows and conversion rates",
                "responses": ok}},
            "/api/jobs/export": {"get": {"summary": "Export every job as JSON or CSV",
                "parameters": [{"name": "format", "in": "query",
                                "schema": {"type": "string", "enum": ["json", "csv"]}}],
                "responses": ok}},
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

DOCS_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CV Studio API</title>
<style>
:root{--paper:#f4f3ef;--card:#fbfaf7;--sunk:#eeece6;--ink:#15141a;--ink-2:#514f4a;
 --ink-3:#6d6a62;--rule:#e2dfd7;--rule-2:#eceae4;--ok:#3f7d52;--bad:#a33a22;--r:5px}
@media(prefers-color-scheme:dark){:root{--paper:#0f0f10;--card:#161617;--sunk:#1c1c1e;
 --ink:#e8e6e1;--ink-2:#a5a29a;--ink-3:#8a877f;--rule:#262628;--rule-2:#1f1f21;
 --ok:#7fb08c;--bad:#d98166}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);letter-spacing:-.004em;
 font:13.5px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Inter,system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:52px 28px 90px}
h1{font-size:21px;font-weight:560;margin:0 0 6px;letter-spacing:-.015em}
.lede{color:var(--ink-3);margin:0 0 8px;max-width:62ch}
.base{font:11.5px ui-monospace,Consolas,monospace;color:var(--ink-3);margin:0 0 40px}
.base b{color:var(--ink);font-weight:500}
section{border-top:1px solid var(--rule-2);padding:22px 0}
.row{display:flex;align-items:baseline;gap:12px;cursor:pointer}
.verb{font:10.5px ui-monospace,Consolas,monospace;font-weight:600;letter-spacing:.04em;
 color:var(--ink-3);width:44px;flex:none;text-transform:uppercase}
.verb.post{color:var(--ok)}
.path{font:12.5px ui-monospace,Consolas,monospace;color:var(--ink);font-weight:500}
.sum{color:var(--ink-3);font-size:12.5px;margin-left:auto;text-align:right}
.body{margin:16px 0 0 56px;display:none}
section.open .body{display:block}
h4{font-size:11.5px;font-weight:560;color:var(--ink-3);margin:0 0 7px}
table{width:100%;border-collapse:collapse;margin:0 0 16px}
td{padding:5px 0;border-bottom:1px solid var(--rule-2);vertical-align:top;font-size:12.5px}
td:first-child{width:150px;font:11.5px ui-monospace,Consolas,monospace;color:var(--ink)}
td:last-child{color:var(--ink-3)}
.req{color:var(--bad);font-size:10.5px;margin-left:5px}
textarea,input{width:100%;border:1px solid var(--rule);border-radius:var(--r);padding:8px 10px;
 background:var(--sunk);color:var(--ink);font:11.5px/1.6 ui-monospace,Consolas,monospace;
 resize:vertical}
textarea:focus,input:focus{background:var(--card);border-color:var(--ink-3);outline:none}
button{cursor:pointer;border:0;border-radius:var(--r);background:var(--ink);color:var(--paper);
 font:inherit;font-size:12.5px;font-weight:530;padding:6px 13px;margin:10px 0 0}
button:hover{opacity:.86}
button:disabled{opacity:.4;cursor:default}
pre.out{background:var(--sunk);border-radius:var(--r);padding:12px 14px;margin:12px 0 0;
 font:11.5px/1.65 ui-monospace,Consolas,monospace;max-height:340px;overflow:auto;white-space:pre-wrap}
.status{font:11.5px ui-monospace,Consolas,monospace;margin-left:10px}
.s-ok{color:var(--ok)} .s-bad{color:var(--bad)}
a{color:var(--ink)}
footer{margin-top:44px;padding-top:20px;border-top:1px solid var(--rule-2);
 color:var(--ink-3);font-size:12px}
</style></head><body><div class="wrap">
<h1>CV Studio API</h1>
<p class="lede">Read, edit and render CVs over HTTP. Everything runs on your machine;
the server accepts local connections only unless you start it with a token.</p>
<p class="base">Base <b id="base"></b> &nbsp;·&nbsp; <a href="/api/openapi.json">openapi.json</a></p>
<div id="eps"></div>
<footer id="foot"></footer>
</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const TOKEN=new URLSearchParams(location.search).get("token");
$("#base").textContent=location.origin;

/* A plausible body built from the schema, so Run works on the first click
   instead of making you author JSON before you can see anything. */
function sample(schema){
  const props=(schema&&schema.properties)||{};
  const out={};
  for(const k of Object.keys(props)){
    if(k==="path") out[k]="profile/my-cv.yaml";
    else if(props[k].type==="array") out[k]=[];
    else if(props[k].type==="string") out[k]="";
  }
  if("yaml" in out) delete out.yaml;
  if("patches" in out) delete out.patches;
  return out;
}

fetch("/api/openapi.json"+(TOKEN?"?token="+encodeURIComponent(TOKEN):""))
 .then(r=>r.json()).then(spec=>{
  $("#foot").textContent=spec.info.title+" v"+spec.info.version;
  const host=$("#eps");
  Object.entries(spec.paths).forEach(([path,ops])=>{
    Object.entries(ops).forEach(([verb,op])=>{
      const sec=document.createElement("section");
      const params=op.parameters||[];
      const bodySchema=((op.requestBody||{}).content||{})["application/json"];
      const props=bodySchema?bodySchema.schema.properties:null;
      sec.innerHTML=
        '<div class="row"><span class="verb '+verb+'">'+verb+'</span>'+
        '<span class="path">'+esc(path)+'</span>'+
        '<span class="sum">'+esc(op.summary||"")+'</span></div>'+
        '<div class="body">'+
        (params.length?'<h4>Query parameters</h4><table>'+params.map(p=>
          '<tr><td>'+esc(p.name)+(p.required?'<span class="req">required</span>':'')+
          '</td><td>'+esc((p.schema||{}).type||"")+'</td></tr>').join("")+'</table>':'')+
        (props?'<h4>Request body</h4><table>'+Object.entries(props).map(([k,v])=>
          '<tr><td>'+esc(k)+'</td><td>'+esc(v.description||v.type||"")+'</td></tr>').join("")+
          '</table><textarea rows="4">'+esc(JSON.stringify(sample(bodySchema.schema),null,2))+
          '</textarea>':'')+
        (params.length?'<h4>Try it</h4>'+params.map(p=>
          '<input data-p="'+esc(p.name)+'" placeholder="'+esc(p.name)+'" value="'+
          (p.name==="path"?"profile/my-cv.yaml":"")+'">').join(""):'')+
        '<div><button>Run</button><span class="status"></span></div>'+
        '<pre class="out" hidden></pre></div>';
      sec.querySelector(".row").onclick=()=>sec.classList.toggle("open");
      const btn=sec.querySelector("button"), out=sec.querySelector(".out"),
            st=sec.querySelector(".status"), ta=sec.querySelector("textarea");
      btn.onclick=async e=>{
        e.stopPropagation(); btn.disabled=true; st.textContent="…"; st.className="status";
        let url=location.origin+path, opts={method:verb.toUpperCase(),headers:{}};
        const qs=new URLSearchParams();
        sec.querySelectorAll("[data-p]").forEach(i=>{ if(i.value) qs.set(i.dataset.p,i.value) });
        if(TOKEN) opts.headers["X-API-Key"]=TOKEN;
        if(ta){ opts.headers["Content-Type"]="application/json"; opts.body=ta.value }
        if([...qs].length) url+="?"+qs.toString();
        const t0=performance.now();
        try{
          const r=await fetch(url,opts);
          const ms=Math.round(performance.now()-t0);
          const ct=r.headers.get("content-type")||"";
          let text;
          if(ct.includes("json")) text=JSON.stringify(await r.json(),null,2);
          else text="("+ct+", "+(r.headers.get("content-length")||"?")+" bytes)";
          st.textContent=r.status+" · "+ms+"ms";
          st.className="status "+(r.ok?"s-ok":"s-bad");
          out.hidden=false; out.textContent=text.slice(0,20000);
        }catch(err){
          st.textContent="failed"; st.className="status s-bad";
          out.hidden=false; out.textContent=String(err);
        }finally{ btn.disabled=false }
      };
      host.append(sec);
    });
  });
 })
 .catch(e=>{ $("#eps").textContent="Could not load the spec: "+e });
</script></body></html>"""


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
        if u.path.startswith("/api/") and u.path != "/api/docs" and not self._authed():
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
            if u.path.startswith("/static/"):
                name = u.path.split("/static/", 1)[1]
                # Serve only the vendored assets; never anything else on disk.
                if "/" in name or ".." in name:
                    return self._json({"error": "not found"}, 404)
                f = STATIC_DIR / name
                if not f.is_file():
                    return self._json({"error": "not found"}, 404)
                ctype = "application/javascript" if f.suffix == ".js" else "text/plain"
                return self._send(200, f.read_bytes(), ctype + "; charset=utf-8")
            if u.path == "/api/jobs":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json({"jobs": jobstore.list_jobs(
                    WORKSPACE, q.get("status", [None])[0], q.get("q", [None])[0],
                    q.get("node", [None])[0]),
                    "statuses": jobstore.STATUSES})
            if u.path == "/api/funnel":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json(jobstore.funnel(WORKSPACE))
            if u.path == "/api/jobs/export":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                fmt = q.get("format", ["json"])[0]
                body = jobstore.export(WORKSPACE, fmt).encode("utf-8")
                return self._send(200, body,
                                  "text/csv" if fmt == "csv" else "application/json")
            if u.path == "/api/docs":
                return self._send(200, DOCS_HTML.encode("utf-8"), "text/html; charset=utf-8")
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
            if u.path == "/api/jobs":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json(jobstore.add_job(WORKSPACE, payload))
            if u.path == "/api/jobs/update":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json(jobstore.update_job(
                    WORKSPACE, payload.pop("id", ""), payload))
            if u.path == "/api/reveal":
                target = WORKSPACE
                sub_ = payload.get("path")
                if sub_:
                    target = safe_path(sub_)
                try:
                    if sys.platform == "win32":
                        os.startfile(target)  # noqa: S606
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", str(target)])
                    else:
                        subprocess.Popen(["xdg-open", str(target)])
                except Exception as exc:
                    return self._json({"error": str(exc)}, 500)
                return self._json({"ok": True})
            if u.path == "/api/jobs/delete":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                jobstore.delete_job(WORKSPACE, payload.get("id", ""))
                return self._json({"ok": True})
            if u.path == "/api/new":
                raw = payload.get("name") or "untitled"
                name = "".join(c for c in raw if c.isalnum() or c in "-_ ").strip()
                if not name:
                    return self._json({"error": "Please give it a name."}, 400)
                kind = payload.get("kind") or "cv"
                folder = "letters" if kind == "letter" else "profile"
                dest = safe_path(f"{folder}/{name}.yaml")
                if dest.exists():
                    return self._json({"error": "Something with that name already exists."}, 409)
                src = payload.get("from")
                if src:
                    body = safe_path(src).read_text(encoding="utf-8")
                else:
                    body = STARTER_LETTER if kind == "letter" else STARTER_CV
                dest.write_text(body, encoding="utf-8")
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
/* ---------------------------------------------------------------------------
   Ink, paper, and as little else as possible.

   One neutral ramp does nearly all the work. There is no brand accent: the
   primary action is solid ink, focus is a hairline ring, and the only real
   colour on screen belongs to the document being edited. Status colours appear
   only where they carry meaning.
--------------------------------------------------------------------------- */
:root{
  --paper:#f4f3ef; --card:#fbfaf7; --sunk:#eeece6;
  --ink:#15141a; --ink-2:#514f4a; --ink-3:#6d6a62;
  --rule:#e2dfd7; --rule-2:#eceae4;
  --btn:#15141a; --btn-ink:#fbfaf7;
  --live:#3f7d52; --bad:#a33a22; --bad-bg:#f8ece8;
  --focus:#15141a;
  --r:5px; --r-lg:7px;
  --tk-key:#2b4c73; --tk-str:#3f6b48; --tk-num:#6b4a86; --tk-bool:#a33a22;
  --tk-com:#a09d95; --tk-punc:#b5b2aa; --tk-blk:#8a5a1e; --tk-sel:rgba(21,20,26,.12);
}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
  --paper:#0f0f10; --card:#161617; --sunk:#1c1c1e;
  --ink:#e8e6e1; --ink-2:#a5a29a; --ink-3:#8a877f;
  --rule:#262628; --rule-2:#1f1f21;
  --btn:#e8e6e1; --btn-ink:#0f0f10;
  --live:#7fb08c; --bad:#d98166; --bad-bg:#241713;
  --focus:#e8e6e1;
  --tk-key:#8fb4d9; --tk-str:#9ac4a4; --tk-num:#b9a0d4; --tk-bool:#d98166;
  --tk-com:#5f5c57; --tk-punc:#4f4d49; --tk-blk:#c9a06a; --tk-sel:rgba(232,230,225,.14);
}}

*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{
  background:var(--paper); color:var(--ink); overflow:hidden;
  display:grid; grid-template-rows:auto 1fr;
  font:13.5px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Inter,system-ui,sans-serif;
  font-feature-settings:"tnum" 0; -webkit-font-smoothing:antialiased;
  letter-spacing:-0.004em;
}
:focus{outline:none}
:focus-visible{outline:1px solid var(--focus);outline-offset:2px;border-radius:3px}
button,select,input,textarea{font:inherit;color:inherit;letter-spacing:inherit}

/* ---------- title bar ------------------------------------------------- */
header{
  display:flex; align-items:center; gap:0; height:44px; padding:0 0 0 14px;
  background:var(--card); border-bottom:1px solid var(--rule);
  -webkit-app-region:drag; user-select:none;
}
header button,header select,header label,header .wctl{-webkit-app-region:no-drag}
.brand{
  display:flex; align-items:center; gap:7px; font-size:12.5px; font-weight:560;
  white-space:nowrap; color:var(--ink); margin-right:16px;
}
/* the one live spot of colour on the page */
.dot{width:5px;height:5px;border-radius:50%;background:var(--live);flex:none}
.sep{width:1px;height:16px;background:var(--rule);margin:0 12px;flex:none}
.ctl{display:flex;align-items:center;gap:6px;white-space:nowrap}
.ctl label{font-size:11.5px;color:var(--ink-3)}
header select{
  border:0;background:none;padding:3px 4px;border-radius:var(--r);
  font-size:12.5px;color:var(--ink);cursor:pointer;max-width:150px;
}
header select:hover{background:var(--sunk)}
header .ctl+.ctl{margin-left:14px}
.grow{flex:1}
button{cursor:pointer;background:none;border:0;border-radius:var(--r);color:var(--ink)}
button:disabled{opacity:.35;cursor:default}
.primary{
  background:var(--btn); color:var(--btn-ink); font-weight:530; font-size:12.5px;
  padding:5px 12px; border-radius:var(--r);
}
.primary:hover:not(:disabled){opacity:.86}
.ghost{color:var(--ink-2);padding:5px 9px;font-size:12.5px}
.ghost:hover:not(:disabled){background:var(--sunk);color:var(--ink)}
.mini{font-size:11.5px;padding:3px 8px;color:var(--ink-2)}
.mini:hover{background:var(--sunk);color:var(--ink)}
kbd{font:11px ui-monospace,Consolas,monospace;color:var(--ink-3);background:none;border:0;padding:0}
.dirty{width:5px;height:5px;border-radius:50%;background:var(--ink-3);margin:0 8px 0 10px;
  flex:none;opacity:0;transition:opacity .14s}
.dirty.on{opacity:1}
.wctl{display:flex;margin-left:8px;align-self:stretch}
.wctl[hidden]{display:none}
.wctl button{width:44px;border-radius:0;color:var(--ink-3);display:grid;place-items:center}
.wctl button:hover{background:var(--sunk);color:var(--ink)}
.wctl #w-close:hover{background:#c0392b;color:#fff}

/* ---------- shell ------------------------------------------------------ */
main{display:grid;grid-template-columns:196px 1px minmax(320px,1fr) 1px minmax(340px,1.02fr);
  overflow:hidden;min-height:0}
.gut{cursor:col-resize;background:var(--rule);position:relative}
.gut::after{content:"";position:absolute;inset:0 -3px}
.gut:hover,.gut.drag{background:var(--ink-3)}
aside,.mid,.prev{min-height:0}
aside{background:var(--card);padding:14px 0 24px;overflow-y:auto}
.mid{display:flex;flex-direction:column;overflow:hidden;background:var(--card)}
.prev{background:var(--paper);overflow-y:auto}

/* ---------- documents -------------------------------------------------- */
.side-hd{display:flex;align-items:center;justify-content:space-between;padding:0 8px 8px 16px}
.side-hd b{font-size:12px;font-weight:560;color:var(--ink);letter-spacing:-0.005em}
aside h2{font-size:11px;font-weight:450;color:var(--ink-3);margin:18px 16px 5px;
  letter-spacing:0}
.doc{display:flex;align-items:center;gap:8px;width:100%;padding:5px 16px;
  text-align:left;font-size:13px;color:var(--ink-2);position:relative;border-radius:0}
.doc:hover{background:var(--sunk);color:var(--ink)}
.doc.active{color:var(--ink);font-weight:530;background:none}
/* a hairline marker rather than a filled block */
.doc.active::before{content:"";position:absolute;left:0;top:4px;bottom:4px;width:2px;
  background:var(--ink)}
.doc svg{display:none}
.doc span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---------- tabs ------------------------------------------------------- */
.tabs{display:flex;gap:18px;padding:0 18px;height:40px;align-items:center;
  border-bottom:1px solid var(--rule);flex:none}
.tab{padding:0;font-size:12.5px;color:var(--ink-3);height:39px;border-radius:0;
  border-bottom:1px solid transparent}
.tab:hover{color:var(--ink-2)}
.tab[aria-selected=true]{color:var(--ink);font-weight:530;border-bottom-color:var(--ink)}
.pane{flex:1;min-height:0;overflow-y:auto;padding:18px}
#pane-yaml{padding:0;overflow:hidden;display:flex}
.pane[hidden],#pane-yaml[hidden]{display:none}

/* ---------- editor ----------------------------------------------------- */
.edwrap{position:relative;flex:1;min-height:0;background:var(--card)}
.edwrap pre,.edwrap textarea{position:absolute;inset:0;margin:0;padding:16px 18px;border:0;
  font:12.5px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre;
  overflow:auto;tab-size:2;letter-spacing:0}
.edwrap pre{pointer-events:none;color:var(--ink)}
.edwrap textarea{background:transparent;color:transparent;caret-color:var(--ink);resize:none}
.edwrap textarea::selection{background:var(--tk-sel)}
.t-key{color:var(--tk-key)}.t-str{color:var(--tk-str)}.t-num{color:var(--tk-num)}
.t-bool{color:var(--tk-bool)}.t-com{color:var(--tk-com)}
.t-punc{color:var(--tk-punc)}.t-blk{color:var(--tk-blk)}

/* ---------- groups: rules and space, not cards ------------------------- */
.grp{border:0;border-top:1px solid var(--rule-2);border-radius:0;background:none;margin:0}
.grp:first-of-type{border-top:0}
.grp>summary{list-style:none;cursor:pointer;padding:13px 0 11px;display:flex;align-items:center;
  text-transform:capitalize;
  gap:8px;font-size:12px;font-weight:560;color:var(--ink);background:none;border:0;
  letter-spacing:-0.005em}
.grp>summary::-webkit-details-marker{display:none}
.grp>summary:hover{color:var(--ink)}
.grp>summary .chev{transition:transform .15s;color:var(--ink-3)}
.grp[open]>summary .chev{transform:rotate(90deg)}
.grp>summary .count{margin-left:auto;font-size:11px;color:var(--ink-3);font-weight:400}
.grp .body{padding:0 0 16px}

/* ---------- fields ----------------------------------------------------- */
.f{display:grid;grid-template-columns:104px 1fr;gap:12px;align-items:start;margin-bottom:8px}
.f>label{color:var(--ink-3);font-size:12px;padding-top:6px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.f input,.f textarea{width:100%;border:1px solid var(--rule-2);border-radius:var(--r);
  padding:5px 8px;background:var(--sunk);color:var(--ink);font-size:13px}
.f input:hover,.f textarea:hover{border-color:var(--rule)}
.f input:focus,.f textarea:focus{background:var(--card);border-color:var(--ink-3);outline:none}
.f textarea{font:12.5px/1.65 ui-monospace,Consolas,monospace;resize:vertical;min-height:62px;
  letter-spacing:0}
.entry{border:0;border-left:1px solid var(--rule);border-radius:0;padding:2px 0 2px 14px;
  margin:14px 0;background:none}
.entry-hd{display:flex;align-items:center;gap:8px;margin-bottom:9px}
.entry-hd b{font-size:12.5px;font-weight:560}

/* ---------- design controls -------------------------------------------- */
.dgrid{display:grid;grid-template-columns:158px 1fr;gap:12px;align-items:center;margin-bottom:6px}
.dgrid>label{color:var(--ink-3);font-size:12px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.dctl{display:flex;align-items:center;gap:7px;min-width:0}
.dctl input[type=text],.dctl input[type=number],.dctl select{border:1px solid var(--rule-2);
  border-radius:var(--r);padding:4px 7px;background:var(--sunk);color:var(--ink);
  font-size:12.5px;min-width:0;flex:1}
.dctl input:hover,.dctl select:hover{border-color:var(--rule)}
.dctl input:focus,.dctl select:focus{background:var(--card);border-color:var(--ink-3)}
.dctl input[type=number]{max-width:84px;flex:none}
.dctl select.unit{max-width:66px;flex:none}
.dctl input[type=color]{width:22px;height:22px;padding:0;border:1px solid var(--rule);
  border-radius:3px;background:none;cursor:pointer;flex:none}
.dctl input[type=checkbox]{width:15px;height:15px;accent-color:var(--ink);cursor:pointer}
.dctl .hex{font:11px ui-monospace,Consolas,monospace;color:var(--ink-3);flex:none}
.dnote{color:var(--ink-3);font-size:12px;margin:0 0 4px;line-height:1.65;max-width:56ch}

/* ---------- analytics -------------------------------------------------- */
.side-foot{margin-top:18px;padding-top:10px;border-top:1px solid var(--rule-2)}
.analytics{grid-column:3 / -1;overflow-y:auto;background:var(--paper);padding:28px 34px 70px}
/* Content stretches with the window but stops before lines get unreadable.
   The chart is exempt: it should take everything it can get. */
.an-inner{max-width:1180px}
.chart{max-width:none}
.analytics[hidden]{display:none}
body.analytics-on .mid,body.analytics-on .prev,
body.analytics-on .gut[data-target="1"]{display:none}
.an-h{font-size:15px;font-weight:560;margin:0 0 3px;letter-spacing:-.01em}
.an-sub{color:var(--ink-3);font-size:12.5px;margin:0 0 26px;max-width:60ch}
.stats{display:flex;gap:34px;flex-wrap:wrap;margin:0 0 30px}
.stat b{display:block;font-size:23px;font-weight:500;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;line-height:1.2}
.stat span{font-size:11.5px;color:var(--ink-3)}
.chart{margin:0 0 12px;overflow-x:auto}
.chart svg{display:block;max-width:100%;height:auto}
.sk-node{stroke:none}
.sk-link{fill:none;transition:opacity .15s}
.sk-link:hover{opacity:.95!important}
.sk-label{font:11.5px ui-sans-serif,-apple-system,"Segoe UI",sans-serif;fill:var(--ink)}
.sk-hit{cursor:pointer}
.sk-hit:hover .sk-node{fill-opacity:.95}
.sk-dim{opacity:.22}
.sel-bar{display:flex;align-items:baseline;gap:12px;margin:20px 0 10px;flex-wrap:wrap}
.sel-bar h4{margin:0;font-size:13px;font-weight:560}
.sel-bar span{color:var(--ink-3);font-size:12px}
.sel-bar button{margin-left:auto}
.sk-count{font:11px ui-monospace,Consolas,monospace;fill:var(--ink-3)}
.an-note{color:var(--ink-3);font-size:12px;margin:14px 0 0;max-width:64ch;line-height:1.65}
.an-actions{margin-top:22px;display:flex;gap:8px}

/* ---------- settings --------------------------------------------------- */
/* A fixed content column overflowed in a narrow window and pushed the controls
   off-screen; let it flex, and stack the rail when there is no room beside it. */
.set-wrap{display:grid;grid-template-columns:146px minmax(0,1fr);gap:34px;max-width:820px}
.set-rail{display:flex;flex-direction:column;gap:1px;position:sticky;top:30px;align-self:start}
@media(max-width:820px){
  .set-wrap{grid-template-columns:1fr;gap:16px}
  .set-rail{flex-direction:row;flex-wrap:wrap;position:static;gap:2px;
    border-bottom:1px solid var(--rule);padding-bottom:10px;margin-bottom:4px}
  .srow{flex-wrap:wrap;gap:10px}
}
.set-rail button{text-align:left;padding:6px 10px;font-size:13px;color:var(--ink-3);
  border-radius:var(--r);position:relative}
.set-rail button:hover{background:var(--card);color:var(--ink)}
.set-rail button[aria-selected=true]{color:var(--ink);font-weight:530;background:var(--card)}
.sp h3{font-size:15px;font-weight:560;margin:0 0 4px;letter-spacing:-.01em}
.sp[hidden]{display:none}
.sp-lede{color:var(--ink-3);font-size:12.5px;line-height:1.65;margin:0 0 18px;max-width:60ch}
.sp-note{color:var(--ink-3);font-size:12px;line-height:1.65;margin:14px 0 0;max-width:60ch}
/* a settings row: label and explanation on the left, the control on the right */
.srow{display:flex;align-items:center;gap:20px;padding:13px 0;border-top:1px solid var(--rule-2)}
.srow>div{flex:1;min-width:0}
.srow b{display:block;font-size:13px;font-weight:530;margin-bottom:2px}
.srow span{display:block;color:var(--ink-3);font-size:12px;line-height:1.55;
  overflow-wrap:anywhere}
.srow .mono{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}
.srow select{border:1px solid var(--rule);border-radius:var(--r);padding:5px 8px;
  background:var(--sunk);color:var(--ink);font-size:12.5px}
.btnlink{text-decoration:none;padding:5px 9px;font-size:12.5px;display:inline-block}
.steps{margin:0 0 14px;padding-left:18px;font-size:13px;line-height:1.7}
.steps li{margin-bottom:4px} .steps li::marker{color:var(--ink-3)}
/* a switch reads as a setting; a bare checkbox reads as a form field */
.tgl{position:relative;display:inline-block;width:34px;height:20px;flex:none;cursor:pointer}
.tgl input{opacity:0;width:0;height:0}
.tgl span{position:absolute;inset:0;background:var(--rule);border-radius:99px;
  transition:background .16s}
.tgl span::before{content:"";position:absolute;width:14px;height:14px;left:3px;top:3px;
  background:var(--card);border-radius:50%;transition:transform .16s}
.tgl input:checked+span{background:var(--ink)}
.tgl input:checked+span::before{transform:translateX(14px)}
.tgl input:focus-visible+span{outline:1px solid var(--focus);outline-offset:2px}

/* ---------- jobs table ------------------------------------------------- */
.jt{width:100%;border-collapse:collapse;margin:0 0 8px}
.jt th{text-align:left;font-size:11px;font-weight:450;color:var(--ink-3);padding:0 10px 8px 0;
  border-bottom:1px solid var(--rule)}
.jt td{padding:9px 10px 9px 0;border-bottom:1px solid var(--rule-2);font-size:13px;
  vertical-align:middle}
.jt tr:hover td{background:var(--card)}
.jt .co{font-weight:530}
.jt .ro{color:var(--ink-3)}
.jt .when{color:var(--ink-3);font-size:11.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.jt .links{display:flex;gap:10px}
.jt .links a{color:var(--ink-3);text-decoration:none;font-size:11.5px;white-space:nowrap}
.jt .links a:hover{color:var(--ink);text-decoration:underline}
.jt .links .none{color:var(--rule)}
@media(max-width:900px){
  .jt .ro,.jt th:nth-child(2){display:none}      /* role folds into company */
  .jt .when,.jt th:nth-child(4){display:none}
}
.jt select{border:1px solid transparent;border-radius:var(--r);padding:3px 6px;font-size:12px;
  background:var(--sunk);color:var(--ink);max-width:150px}
.jt select:hover{border-color:var(--rule)}
.jt .edit{opacity:0;font-size:11.5px}
.jt tr:hover .edit{opacity:1}
/* status dot carries the outcome, so the table scans without reading */
.sd{display:inline-block;width:5px;height:5px;border-radius:50%;margin-right:7px;flex:none}
.jt-bar{display:flex;align-items:center;gap:10px;margin:0 0 18px}
.jt-bar input{flex:1;max-width:280px;border:1px solid var(--rule);border-radius:var(--r);
  padding:6px 9px;background:var(--sunk);color:var(--ink);font-size:13px}
.jt-bar input:focus{background:var(--card);border-color:var(--ink-3);outline:none}

/* ---------- preview ---------------------------------------------------- */
.pbar{position:sticky;top:0;z-index:3;display:flex;align-items:baseline;gap:16px;
  padding:13px 20px;background:var(--paper);border-bottom:1px solid var(--rule);
  font-size:12px;color:var(--ink-3);flex-wrap:wrap;font-variant-numeric:tabular-nums}
.pbar b{color:var(--ink);font-weight:560}
/* stats read as a sentence, not as chips */
.pill{display:inline;background:none;border:0;border-radius:0;padding:0;color:inherit}
.pill+.pill{margin-left:0}
#pstats .pill+.pill::before{content:"·";margin:0 8px;color:var(--rule)}
.live-ok{color:var(--live)}
.live-working{color:var(--ink-3)}
.live-bad{color:var(--bad);max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.zoom{display:flex;align-items:center;gap:1px;margin-left:auto}
.zoom button{padding:1px 7px;font-size:13px;color:var(--ink-3)}
.zoom button:hover{color:var(--ink);background:var(--sunk)}
.pages{padding:20px}
.pg{width:100%;display:block;margin:0 auto 16px;background:#fff;border:1px solid var(--rule);
  border-radius:2px;box-shadow:0 1px 1px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.06)}
.err{margin:20px;background:var(--bad-bg);border:0;border-left:2px solid var(--bad);
  border-radius:0;padding:14px 16px;color:var(--bad)}
.err h4{margin:0 0 6px;font-size:12.5px;font-weight:560}
.err .hint{color:var(--ink);background:none;border-radius:0;padding:0;margin:8px 0 0;
  font-size:12.5px;line-height:1.65;max-width:62ch}
.err pre{margin:10px 0 0;white-space:pre-wrap;font:11px/1.55 ui-monospace,Consolas,monospace;
  max-height:190px;overflow:auto;color:var(--ink-3)}

/* ---------- states ----------------------------------------------------- */
.empty{padding:64px 24px;text-align:left;color:var(--ink-3);max-width:46ch}
.empty h3{margin:0 0 6px;font-size:13.5px;color:var(--ink);font-weight:560}
.empty p{margin:0;font-size:13px;line-height:1.7}
.empty .cta{margin-top:18px}
.spin{display:inline-block;width:9px;height:9px;border:1.5px solid var(--rule);
  border-top-color:var(--ink-3);border-radius:50%;animation:sp .8s linear infinite;
  vertical-align:0}
@keyframes sp{to{transform:rotate(360deg)}}
/* a quiet pulse instead of a sweeping gradient */
.skel{background:var(--sunk);border-radius:var(--r);animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}

/* ---------- toasts ----------------------------------------------------- */
#toasts{position:fixed;bottom:16px;right:16px;z-index:50;display:flex;flex-direction:column;
  gap:6px;align-items:flex-end;pointer-events:none}
.toast{background:var(--btn);color:var(--btn-ink);border:0;border-radius:var(--r);
  padding:7px 12px;font-size:12.5px;max-width:330px;box-shadow:0 6px 20px rgba(0,0,0,.18);
  animation:rise .16s ease-out}
.toast.bad{background:var(--bad);color:#fff}
@keyframes rise{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* ---------- dialogs ---------------------------------------------------- */
dialog{border:1px solid var(--rule);border-radius:var(--r-lg);background:var(--card);
  color:var(--ink);padding:0;box-shadow:0 20px 60px rgba(0,0,0,.24);max-width:430px;
  width:calc(100% - 40px)}
dialog::backdrop{background:rgba(0,0,0,.3)}
dialog.wide{max-width:640px}
dialog .dh{padding:20px 22px 0}
dialog h3{margin:0 0 5px;font-size:14px;font-weight:560}
dialog p{margin:0;color:var(--ink-3);font-size:12.5px;line-height:1.65}
dialog .db{padding:16px 22px}
dialog input{width:100%;border:1px solid var(--rule);border-radius:var(--r);padding:7px 9px;
  background:var(--sunk);color:var(--ink);font-size:13px}
dialog input:focus{background:var(--card);border-color:var(--ink-3);outline:none}
dialog .df{display:flex;justify-content:flex-end;gap:6px;padding:0 22px 18px}
.stabs{display:flex;gap:18px;padding:16px 22px 0;border-bottom:1px solid var(--rule)}
.stab{padding:0 0 10px;font-size:12.5px;color:var(--ink-3);border-bottom:1px solid transparent;
  border-radius:0;margin-bottom:-1px}
.stab:hover{color:var(--ink-2)}
.stab[aria-selected=true]{color:var(--ink);font-weight:530;border-bottom-color:var(--ink);
  background:none}
.sbody{max-height:56vh;overflow-y:auto;font-size:13px;line-height:1.7}
.sbody[hidden]{display:none}
.sbody h4{margin:20px 0 6px;font-size:12.5px;font-weight:560;letter-spacing:-0.005em}
.sbody h4:first-child{margin-top:0}
.sbody p{margin:0 0 9px;color:var(--ink);font-size:13px}
.sbody p.muted{color:var(--ink-3);font-size:12.5px}
.sbody ol{margin:0 0 10px;padding-left:18px}
.sbody li{margin-bottom:5px}
.sbody li::marker{color:var(--ink-3)}
.sbody code{background:var(--sunk);border:0;border-radius:3px;padding:1px 5px;
  font:11.5px ui-monospace,Consolas,monospace}
pre.code{background:var(--sunk);border:0;border-radius:var(--r);padding:13px 15px;
  font:11.5px/1.7 ui-monospace,Consolas,monospace;overflow-x:auto;margin:0 0 8px;white-space:pre}
table.api{width:100%;border-collapse:collapse;margin:0 0 12px}
table.api td{padding:6px 0;border-bottom:1px solid var(--rule-2);vertical-align:top}
table.api td:first-child{width:206px;white-space:nowrap}
table.api td:first-child code{background:none;padding:0;color:var(--ink)}
table.api td:last-child{color:var(--ink-3);font-size:12.5px}
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
    <div class="side-foot">
      <button class="doc" id="nav-jobs"><span>Applications</span></button>
      <button class="doc" id="nav-analytics"><span>Analytics</span></button>
    </div>
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
  <div class="analytics" id="analytics" hidden></div>
  <div class="analytics" id="jobsview" hidden></div>
  <div class="analytics" id="setview" hidden>
    <div class="set-wrap">
      <nav class="set-rail" id="set-rail">
        <button data-s="workspace" aria-selected="true">Workspace</button>
        <button data-s="editor" aria-selected="false">Editor</button>
        <button data-s="ai" aria-selected="false">Claude Desktop</button>
        <button data-s="api" aria-selected="false">API</button>
        <button data-s="updates" aria-selected="false">Updates</button>
        <button data-s="about" aria-selected="false">About</button>
      </nav>
      <div class="set-body">

        <section class="sp" id="sp-workspace">
          <h3>Workspace</h3>
          <p class="sp-lede">Everything lives in one folder you own. CVs and letters are
            plain YAML; applications are a single SQLite file. Copy the folder and you
            have copied everything.</p>
          <div class="srow">
            <div><b>Folder</b><span id="s-ws" class="mono"></span></div>
            <button class="ghost" id="s-open">Open folder</button>
          </div>
          <div class="srow"><div><b>Documents</b><span id="s-count"></span></div></div>
          <div class="srow"><div><b>Applications database</b>
            <span>applications.db, queryable and exportable</span></div>
            <button class="ghost" id="s-exp">Export JSON</button></div>
        </section>

        <section class="sp" id="sp-editor" hidden>
          <h3>Editor</h3>
          <p class="sp-lede">Defaults for how the editor behaves. These are remembered
            on this machine.</p>
          <div class="srow">
            <div><b>Live preview</b><span>Re-render as you type. Your file is only
              written when you save.</span></div>
            <label class="tgl"><input type="checkbox" id="s-live"><span></span></label>
          </div>
          <div class="srow">
            <div><b>Preview delay</b><span>How long to wait after you stop typing.</span></div>
            <select id="s-delay">
              <option value="400">400 ms</option><option value="700">700 ms</option>
              <option value="1200">1.2 s</option><option value="2000">2 s</option>
            </select>
          </div>
          <div class="srow">
            <div><b>New documents use</b><span>Theme applied to anything you create.</span></div>
            <select id="s-deftheme"></select>
          </div>
        </section>

        <section class="sp" id="sp-ai" hidden>
          <h3>Claude Desktop</h3>
          <p class="sp-lede">CV Studio includes an MCP server. Once connected, Claude can
            read your CVs, edit fields, create tailored copies, and see the rendered page
            to check the layout.</p>
          <ol class="steps">
            <li>Open Claude Desktop, then <b>Settings, Developer, Edit Config</b>.</li>
            <li>Paste this in and restart Claude Desktop.</li>
          </ol>
          <pre id="s-mcp" class="code"></pre>
          <button class="ghost" data-copy="s-mcp">Copy config</button>
          <p class="sp-note">This is the same program you are using now, started in a
            different mode. Nothing extra to install.</p>
        </section>

        <section class="sp" id="sp-api" hidden>
          <h3>API</h3>
          <p class="sp-lede">Drive CV Studio from your own scripts. It accepts local
            connections only, unless started with a token.</p>
          <div class="srow"><div><b>Base URL</b><span id="s-base" class="mono"></span></div>
            <a class="ghost btnlink" id="s-spec" href="#" target="_blank">API reference</a></div>
          <div class="srow"><div><b>Authentication</b><span id="s-auth"></span></div></div>
          <pre id="s-curl" class="code"></pre>
          <button class="ghost" data-copy="s-curl">Copy example</button>
        </section>

        <section class="sp" id="sp-updates" hidden>
          <h3>Updates</h3>
          <div class="srow"><div><b>Version</b><span id="s-ver"></span></div>
            <button class="ghost" id="s-check">Check now</button></div>
          <div class="srow"><div><b>Status</b><span id="u-state">Not checked yet.</span></div></div>
          <div id="u-actions"></div>
          <p class="sp-note">Updates are signed. An installed copy verifies each package
            against a key built into it, so a compromised release host cannot push
            anything it will accept.</p>
        </section>

        <section class="sp" id="sp-about" hidden>
          <h3>About</h3>
          <p class="sp-lede">A local CV editor built on RenderCV and Typst. No account,
            no telemetry, no network access except when you check for updates.</p>
          <div class="srow"><div><b>Licence</b><span>MIT</span></div></div>
          <div class="srow"><div><b>Bundled</b><span>RenderCV (MIT), Typst (Apache-2.0),
            d3-sankey (BSD-3-Clause), RenderCV fonts (OFL / Apache-2.0)</span></div></div>
        </section>

      </div>
    </div>
  </div>
</main>

<div id="toasts" aria-live="polite"></div>

<dialog id="jobdlg">
  <div class="dh"><h3 id="job-title">Add an application</h3>
    <p>Only the company and role are required. Everything else can come later.</p></div>
  <div class="db" id="job-form"></div>
  <div class="df">
    <button id="job-del" class="ghost" style="margin-right:auto" hidden>Delete</button>
    <button id="job-cancel" class="ghost">Cancel</button>
    <button id="job-save" class="primary">Save</button>
  </div>
</dialog>

<dialog id="newdlg">
  <div class="dh"><h3>New document</h3><p>Saved as a plain YAML file in your workspace.
   A cover letter renders with the same letterhead as your CV, so the two arrive
   looking like a pair.</p></div>
  <div class="db">
    <div class="f" style="grid-template-columns:74px 1fr;margin-bottom:10px">
      <label>Type</label>
      <select id="newkind"><option value="cv">CV</option>
        <option value="letter">Cover letter</option></select>
    </div>
    <div class="f" style="grid-template-columns:74px 1fr">
      <label>Name</label>
      <input type="text" id="newname" placeholder="e.g. adyen-solutions-engineer"
        autocomplete="off">
    </div>
  </div>
  <div class="df">
    <button id="newcancel" class="ghost">Cancel</button>
    <button id="newdup">Duplicate current</button>
    <button id="newblank" class="primary">Create</button>
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
  closeAnalytics();
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
  liveTimer=setTimeout(runLive, prefs().delay || 700);
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

/* ---- applications ----
   The records live in SQLite rather than YAML because they are queried,
   filtered and appended to on every status change. A status change appends to
   the job's history rather than replacing it, which is what makes the funnel
   meaningful later. */
const JOB_FIELDS=[
  ["company","Company"],["title","Role"],["location","Location"],["url","Posting URL"],
  ["source","Source"],["salary_expected","Salary expected"],["followup_date","Follow up on"],
  ["notes","Notes"],
];
/* Documents are listed fresh each time the dialog opens, so a CV created a
   minute ago is selectable without reloading the app. */
function docOptions(group,current){
  const docs=((S.state&&S.state.documents)||[]).filter(d=>d.group===group);
  return '<option value="">Not linked</option>'+docs.map(d=>
    '<option value="'+esc(d.path)+'"'+(d.path===current?" selected":"")+'>'+
    esc(d.label)+'</option>').join("");
}
const STATUS_TONE=id=>
  id==="accepted"?"var(--live)":
  /^(rejected|refused)/.test(id)?"var(--bad)":
  /ghosted/.test(id)?"var(--ink-3)":"var(--ink)";
const prettyStatus=s=>s.replace(/_/g," ").replace(/\bs\b/,"");

let JOBS=[], STATUSES=[];

async function openJobs(){
  document.body.classList.add("analytics-on");
  $$(".doc").forEach(b=>b.classList.remove("active"));
  $("#nav-jobs").classList.add("active");
  $("#analytics").hidden=true; $("#setview").hidden=true;
  const host=$("#jobsview"); host.hidden=false;
  host.innerHTML='<p class="an-sub"><span class="spin"></span> Loading…</p>';
  try{
    const d=await api("/api/jobs");
    JOBS=d.jobs; STATUSES=d.statuses;
  }catch(e){ host.innerHTML='<div class="empty"><h3>Could not load</h3><p>'+esc(e.message)+
    '</p></div>'; return }
  drawJobs();
}

function drawJobs(filter){
  const host=$("#jobsview");
  const rows=(filter?JOBS.filter(j=>(j.company+" "+j.title+" "+(j.notes||""))
    .toLowerCase().includes(filter.toLowerCase())):JOBS);
  host.innerHTML=
    '<div class="an-inner">'+
    '<h2 class="an-h">Applications</h2>'+
    '<p class="an-sub">Every role you are tracking. Changing a status here records it '+
    'with a timestamp, which is what the funnel is built from.</p>'+
    '<div class="jt-bar"><input id="job-q" placeholder="Filter by company, role or notes" '+
    'value="'+esc(filter||"")+'">'+
    '<button class="primary" id="job-add">Add application</button></div>'+
    (rows.length?
      '<table class="jt"><thead><tr><th>Company</th><th>Role</th><th>Status</th>'+
      '<th>Updated</th><th>Documents</th><th></th></tr></thead><tbody>'+
      rows.map(j=>
        '<tr data-id="'+esc(j.id)+'">'+
        '<td class="co"><span class="sd" style="background:'+STATUS_TONE(j.status)+'"></span>'+
        esc(j.company)+'</td>'+
        '<td class="ro">'+esc(j.title)+'</td>'+
        '<td><select data-status-for="'+esc(j.id)+'">'+
          STATUSES.map(s=>'<option value="'+s+'"'+(s===j.status?" selected":"")+'>'+
            esc(prettyStatus(s))+'</option>').join("")+'</select></td>'+
        '<td class="when">'+esc((j.updated_at||"").slice(0,10))+'</td>'+
        '<td><div class="links">'+
          (j.cv_path?'<a href="#" data-open="'+esc(j.cv_path)+'">CV</a>'
                    :'<span class="none">CV</span>')+
          (j.letter_path?'<a href="#" data-open="'+esc(j.letter_path)+'">Letter</a>'
                        :'<span class="none">Letter</span>')+
        '</div></td>'+
        '<td><button class="ghost edit" data-edit="'+esc(j.id)+'">Edit</button></td>'+
        '</tr>').join("")+'</tbody></table>'
      :'<div class="empty"><h3>'+(filter?"Nothing matches":"No applications yet")+'</h3>'+
       '<p>'+(filter?"Try a different filter.":
         "Add the roles you are applying for. Once a few have moved through the stages, "+
         "Analytics will show where they actually go.")+'</p></div>')+'</div>';

  $("#job-add").onclick=()=>editJob(null);
  const q=$("#job-q");
  q.oninput=()=>{ const v=q.value; drawJobs(v); const nq=$("#job-q");
    nq.focus(); nq.setSelectionRange(v.length,v.length) };
  $$("[data-edit]").forEach(b=>b.onclick=()=>editJob(b.dataset.edit));
  // Clicking a linked document leaves the tracker and opens it in the editor,
  // which is the whole point of linking them.
  $$("[data-open]").forEach(a=>a.onclick=e=>{ e.preventDefault(); openDoc(a.dataset.open) });
  $$("[data-status-for]").forEach(sel=>sel.onchange=async()=>{
    try{
      await api("/api/jobs/update",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({id:sel.dataset.statusFor,status:sel.value})});
      const d=await api("/api/jobs"); JOBS=d.jobs;
      toast("Status updated");
      drawJobs($("#job-q") ? $("#job-q").value : "");
    }catch(e){ toast(e.message,true) }
  });
}

async function editJob(id){
  const j=id?JOBS.find(x=>x.id===id):null;
  try{ S.state=await api("/api/state") }catch{}   // pick up newly created documents
  $("#job-title").textContent=j?"Edit application":"Add an application";
  $("#job-del").hidden=!j;
  $("#job-form").innerHTML=
    JOB_FIELDS.map(([k,label])=>
      '<div class="f" style="grid-template-columns:120px 1fr"><label>'+esc(label)+'</label>'+
      (k==="notes"
        ? '<textarea data-j="'+k+'" rows="3">'+esc(j?(j[k]||""):"")+'</textarea>'
        : '<input data-j="'+k+'"'+(k==="followup_date"?' type="date"':"")+
          ' value="'+esc(j?(j[k]==null?"":j[k]):"")+'">')+
      '</div>').join("")+
    '<div class="f" style="grid-template-columns:120px 1fr"><label>Status</label>'+
    '<select data-j="status">'+STATUSES.map(s=>'<option value="'+s+'"'+
      (j&&s===j.status?" selected":"")+'>'+esc(prettyStatus(s))+'</option>').join("")+
    '</select></div>'+
    '<div class="f" style="grid-template-columns:120px 1fr"><label>CV</label>'+
    '<select data-j="cv_path">'+docOptions("My CVs",j&&j.cv_path)+'</select></div>'+
    '<div class="f" style="grid-template-columns:120px 1fr"><label>Cover letter</label>'+
    '<select data-j="letter_path">'+docOptions("Cover letters",j&&j.letter_path)+
    '</select></div>';
  const dlg=$("#jobdlg");
  $("#job-cancel").onclick=()=>dlg.close();
  $("#job-del").onclick=async()=>{
    if(!confirm("Delete this application? This cannot be undone.")) return;
    try{
      await api("/api/jobs/delete",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({id:j.id})});
      dlg.close(); const d=await api("/api/jobs"); JOBS=d.jobs; drawJobs(); toast("Deleted");
    }catch(e){ toast(e.message,true) }
  };
  $("#job-save").onclick=async()=>{
    const data={};
    $$("#job-form [data-j]").forEach(el=>{
      let v=el.value.trim();
      if(el.dataset.j==="salary_expected") v=v===""?null:Number(v)||null;
      data[el.dataset.j]=v===""?null:v;
    });
    if(!data.company||!data.title){ toast("Company and role are required",true); return }
    try{
      if(j) await api("/api/jobs/update",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({id:j.id,...data})});
      else await api("/api/jobs",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
      dlg.close();
      const d=await api("/api/jobs"); JOBS=d.jobs; drawJobs();
      toast(j?"Saved":"Added "+data.company);
    }catch(e){ toast(e.message,true) }
  };
  dlg.showModal();
}
$("#nav-jobs").onclick=openJobs;

/* Re-lay the funnel on resize. Debounced, because sankey layout on every
   pixel of a drag is wasted work. */
let sizeTimer=null;
window.addEventListener("resize",()=>{
  if($("#analytics").hidden||!LAST_FUNNEL) return;
  clearTimeout(sizeTimer);
  sizeTimer=setTimeout(()=>drawFunnel(LAST_FUNNEL),140);
});

/* ---- analytics ----
   The funnel uses the real d3-sankey layout, vendored locally (60KB, BSD/ISC)
   rather than reimplemented, so the flow geometry is correct rather than
   approximated. Scripts load on first open so the editor start-up pays nothing
   for a view that may never be used. */
let d3ready=null;
function loadScript(src){
  return new Promise((res,rej)=>{
    const el=document.createElement("script");
    el.src=src; el.onload=res; el.onerror=()=>rej(new Error("could not load "+src));
    document.head.append(el);
  });
}
async function ensureD3(){
  if(d3ready) return d3ready;
  // order matters: sankey depends on array, shape depends on path
  d3ready=(async()=>{
    for(const m of ["d3-array","d3-path","d3-shape","d3-sankey"]){
      await loadScript("/static/"+m+".min.js");
    }
  })();
  return d3ready;
}

/* Colour appears only where it carries meaning: an outcome. Everything still
   in flight stays in the neutral ink ramp. */
function flowTone(id){
  if(id==="accepted") return "var(--live)";
  if(id==="rejected") return "var(--bad)";
  if(id==="ghosted"||id==="refused") return "var(--ink-3)";
  return "var(--ink)";
}
const FLOW_ALPHA={all:.10,pending:.16,applied_s:.30,awaiting:.22,interview_s:.34,
  still_iv:.26,offer_s:.40,deciding:.30,accepted:.55,refused:.20,rejected:.42,ghosted:.18};

async function openAnalytics(){
  document.body.classList.add("analytics-on");
  $$(".doc").forEach(b=>b.classList.remove("active"));
  $("#nav-analytics").classList.add("active");
  $("#jobsview").hidden=true; $("#setview").hidden=true;
  const host=$("#analytics"); host.hidden=false;
  host.innerHTML='<p class="an-sub"><span class="spin"></span> Loading…</p>';
  let f;
  try{
    await ensureD3();
    f=await api("/api/funnel");
  }catch(e){ host.innerHTML='<div class="empty"><h3>Could not load analytics</h3><p>'+
    esc(e.message)+'</p></div>'; return }

  if(!f.totals.total){
    host.innerHTML='<h2 class="an-h">Applications</h2>'+
      '<div class="empty"><h3>Nothing tracked yet</h3><p>Add applications and the funnel '+
      'will show how far they get: how many reach an interview, how many convert to an '+
      'offer, and where the rest drop out.</p></div>';
    return;
  }
  const t=f.totals;
  host.innerHTML=
    '<h2 class="an-h">Applications</h2>'+
    '<p class="an-sub">Where your applications actually go. Each band is sized by how '+
    'many roles reached that stage.</p>'+
    '<div class="stats">'+
      stat(t.total,"tracked")+stat(t.applied,"applied")+
      stat(t.interviewed,"interviewed")+stat(t.offers,"offers")+
      stat(t.interview_rate+"%","applied to interview")+
      stat(t.offer_rate+"%","interview to offer")+
    '</div><div class="chart" id="chart"></div><div id="drill"></div>'+
    '<p class="an-note">Rejections and ghostings are shown separately for each stage, '+
    'because being turned down after interviews means something very different from '+
    'never hearing back at all.</p>'+
    '<div class="an-actions">'+
      '<button class="ghost" id="ex-json">Export JSON</button>'+
      '<button class="ghost" id="ex-csv">Export CSV</button></div>';
  $("#ex-json").onclick=()=>window.open("/api/jobs/export?format=json"+tok());
  $("#ex-csv").onclick=()=>window.open("/api/jobs/export?format=csv"+tok());
  drawFunnel(f);
}
const tok=()=>API_TOKEN?"&token="+encodeURIComponent(API_TOKEN):"";
const stat=(v,l)=>'<div class="stat"><b>'+esc(v)+'</b><span>'+esc(l)+'</span></div>';

let LAST_FUNNEL=null, SEL_NODE=null;
function drawFunnel(f){
  LAST_FUNNEL=f;
  const nodes=f.nodes.filter(n=>n.count>0);
  const idx=new Map(nodes.map((n,i)=>[n.id,i]));
  const links=f.links.filter(l=>idx.has(l.source)&&idx.has(l.target))
    .map(l=>({source:idx.get(l.source),target:idx.get(l.target),value:l.value,tid:l.target}));
  if(!links.length){ $("#chart").innerHTML=""; return }

  /* Fill the window rather than sitting at a fixed 840px in the corner. The
     right pad is reserved for the terminal labels, which live outside the
     sankey extent. */
  const host=$("#chart");
  const W=Math.max(620,(host&&host.clientWidth)||840);
  const H=Math.max(300,Math.min(620,nodes.length*46));
  const PAD=Math.max(150,Math.min(230,W*0.17));
  const layout=d3.sankey().nodeWidth(9).nodePadding(15).nodeAlign(d3.sankeyJustify)
    .extent([[0,8],[W-PAD,H-8]]);
  const graph=layout({nodes:nodes.map(n=>({...n})),links:links.map(l=>({...l}))});
  const path=d3.sankeyLinkHorizontal();

  /* When a stage is selected, everything that does not touch it fades back so
     the chosen path reads at a glance. */
  const touches=l=>!SEL_NODE||l.source.id===SEL_NODE||l.target.id===SEL_NODE;
  const band=graph.links.map(l=>{
    const tone=flowTone(l.tid), a=FLOW_ALPHA[l.tid]??.28;
    return '<path class="sk-link'+(touches(l)?"":" sk-dim")+'" d="'+path(l)+
      '" stroke="'+tone+'" stroke-opacity="'+a+
      '" stroke-width="'+Math.max(1,l.width)+'"><title>'+esc(l.source.label)+' \u2192 '+
      esc(l.target.label)+': '+l.value+'</title></path>';
  }).join("");

  /* The bar alone is a 9px target, so each node gets a generous invisible hit
     area covering its label too. */
  const rect=graph.nodes.map(n=>{
    const dim=SEL_NODE&&SEL_NODE!==n.id?" sk-dim":"";
    const h=Math.max(1,n.y1-n.y0);
    return '<g class="sk-hit'+dim+'" data-node="'+esc(n.id)+'" role="button" tabindex="0">'+
      '<title>'+esc(n.label)+': '+n.count+' \u2014 click to list them</title>'+
      '<rect x="'+(n.x0-6)+'" y="'+(n.y0-6)+'" width="'+((n.x1-n.x0)+PAD)+
      '" height="'+(h+12)+'" fill="transparent"/>'+
      '<rect class="sk-node" x="'+n.x0+'" y="'+n.y0+'" width="'+(n.x1-n.x0)+
      '" height="'+h+'" fill="'+flowTone(n.id)+
      '" fill-opacity="'+(n.id===SEL_NODE?1:(n.id==="accepted"||n.id==="rejected"?.9:.55))+
      '"/></g>';
  }).join("");

  const text=graph.nodes.map(n=>{
    const y=(n.y0+n.y1)/2, x=n.x1+9;
    const dim=SEL_NODE&&SEL_NODE!==n.id?" sk-dim":"";
    return '<g class="'+dim.trim()+'" pointer-events="none">'+
      '<text class="sk-label" x="'+x+'" y="'+(y-1)+'" dominant-baseline="middle">'+
      esc(n.label)+'</text>'+
      '<text class="sk-count" x="'+x+'" y="'+(y+12)+'" dominant-baseline="middle">'+
      n.count+'</text></g>';
  }).join("");

  host.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="'+H+
    '" preserveAspectRatio="xMidYMid meet" role="img" '+
    'aria-label="Application funnel">'+band+rect+text+'</svg>';
  host.querySelectorAll("[data-node]").forEach(g=>{
    const pick=()=>selectNode(g.dataset.node===SEL_NODE?null:g.dataset.node);
    g.onclick=pick;
    g.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); pick() } };
  });
}

async function selectNode(id){
  SEL_NODE=id;
  if(LAST_FUNNEL) drawFunnel(LAST_FUNNEL);
  const panel=$("#drill");
  if(!panel) return;
  if(!id){ panel.innerHTML=""; return }
  const label=((LAST_FUNNEL&&LAST_FUNNEL.nodes)||[]).find(n=>n.id===id);
  panel.innerHTML='<div class="sel-bar"><h4>'+esc(label?label.label:id)+'</h4>'+
    '<span><span class="spin"></span> loading</span></div>';
  let jobs=[];
  try{ jobs=(await api("/api/jobs?node="+encodeURIComponent(id))).jobs }
  catch(e){ panel.innerHTML='<p class="an-note">'+esc(e.message)+'</p>'; return }
  panel.innerHTML=
    '<div class="sel-bar"><h4>'+esc(label?label.label:id)+'</h4>'+
    '<span>'+jobs.length+' application'+(jobs.length===1?"":"s")+'</span>'+
    '<button class="ghost" id="drill-clear">Clear</button></div>'+
    (jobs.length?
      '<table class="jt"><tbody>'+jobs.map(x=>
        '<tr data-jid="'+esc(x.id)+'"><td class="co">'+
        '<span class="sd" style="background:'+STATUS_TONE(x.status)+'"></span>'+
        esc(x.company)+'</td><td class="ro">'+esc(x.title)+'</td>'+
        '<td class="ro">'+esc(prettyStatus(x.status))+'</td>'+
        '<td class="when">'+esc((x.updated_at||"").slice(0,10))+'</td></tr>').join("")+
      '</tbody></table>'
      :'<p class="an-note">Nothing at this stage yet.</p>');
  const clear=$("#drill-clear"); if(clear) clear.onclick=()=>selectNode(null);
  // Opening one from here needs the full list loaded, since the tracker edits
  // from JOBS rather than refetching a single record.
  panel.querySelectorAll("[data-jid]").forEach(tr=>tr.onclick=async()=>{
    try{ JOBS=(await api("/api/jobs")).jobs; STATUSES=(await api("/api/jobs")).statuses }catch{}
    editJob(tr.dataset.jid);
  });
}

function closeAnalytics(){
  document.body.classList.remove("analytics-on");
  SEL_NODE=null;
  $("#analytics").hidden=true;
  $("#jobsview").hidden=true;
  $("#setview").hidden=true;
  $("#nav-analytics").classList.remove("active");
  $("#nav-jobs").classList.remove("active");
}
$("#nav-analytics").onclick=openAnalytics;

/* ---- updates ----
   Tauri's updater verifies a signature against the public key baked into the
   build, so a compromised release host still cannot push a package this app
   will install. Checks run quietly on launch and only speak up when there is
   something to install. */
async function checkUpdates(loud){
  const T=window.__TAURI__;
  const st=$("#u-state"), act=$("#u-actions");
  if(!T||!T.updater){
    if(st){st.textContent="Updates are available in the desktop app only.";}
    return;
  }
  if(st) st.innerHTML='<span class="spin"></span> Checking for updates…';
  if(act) act.innerHTML="";
  try{
    const up=await T.updater.check();
    if(!up){
      if(st) st.textContent="You are on the latest version.";
      return;
    }
    if(st) st.innerHTML="Version <b>"+esc(up.version)+"</b> is available."+
      (up.body?'<br><span class="muted">'+esc(up.body).slice(0,300)+"</span>":"");
    if(act){
      act.innerHTML='<button class="primary" id="u-go">Download and install</button>';
      $("#u-go").onclick=async()=>{
        $("#u-go").disabled=true;
        let total=0, got=0;
        try{
          await up.downloadAndInstall(e=>{
            if(e.event==="Started") total=e.data.contentLength||0;
            if(e.event==="Progress"){
              got+=e.data.chunkLength||0;
              st.textContent=total?`Downloading ${Math.round(got/total*100)}%`:"Downloading…";
            }
            if(e.event==="Finished") st.textContent="Installing…";
          });
          st.textContent="Restarting…";
          if(T.process&&T.process.relaunch) await T.process.relaunch();
        }catch(err){
          st.textContent="Update failed: "+err;
          $("#u-go").disabled=false;
        }
      };
    }
    if(!loud) toast("Version "+up.version+" is available (Settings to install)");
  }catch(e){
    /* A 404 here almost always means no release has been published yet, or the
       repository is private so the asset cannot be fetched without credentials.
       Saying that is more useful than relaying the transport error. */
    const raw=String(e);
    const missing=/release JSON|404|not found/i.test(raw);
    if(st){
      st.innerHTML = missing
        ? 'No published release to update to yet.<br><span class="muted">Updates begin '+
          'working once a version is tagged and the release is publicly downloadable.</span>'
        : "Could not check for updates: "+esc(raw);
    }
  }
}
/* Quiet check shortly after launch, so it never blocks first paint. */
setTimeout(()=>{ if(window.__TAURI__&&window.__TAURI__.updater) checkUpdates(false) }, 4000);

/* ---- settings ----
   Preferences are per-machine conveniences, so they live in localStorage
   rather than in the workspace: a workspace copied to another machine should
   carry documents, not window preferences. Reads are guarded because storage
   throws outright in some privacy modes. */
const PREFS_KEY="cvstudio.prefs";
function prefs(){
  try{ return JSON.parse(localStorage.getItem(PREFS_KEY)||"{}") }catch{ return {} }
}
function setPref(k,v){
  try{ const p=prefs(); p[k]=v; localStorage.setItem(PREFS_KEY,JSON.stringify(p)) }catch{}
}

function openSettings(){
  document.body.classList.add("analytics-on");
  $$(".doc").forEach(b=>b.classList.remove("active"));
  $("#analytics").hidden=true; $("#jobsview").hidden=true;
  $("#setview").hidden=false;
  fillSettings();
}
$("#settings").onclick=()=>{ if($("#setview").hidden) openSettings(); else closeAnalytics() };

$$("#set-rail button").forEach(b=>b.onclick=()=>{
  $$("#set-rail button").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
  ["workspace","editor","ai","api","updates","about"].forEach(k=>
    $("#sp-"+k).hidden = k!==b.dataset.s);
  if(b.dataset.s==="updates") checkUpdates(true);
});

$$("[data-copy]").forEach(b=>b.onclick=async()=>{
  try{ await navigator.clipboard.writeText($("#"+b.dataset.copy).textContent);
       toast("Copied") }
  catch{ toast("Select the text and copy manually",true) }
});

function fillSettings(){
  const st=S.state||{}, base=location.origin, pr=prefs();

  $("#s-ws").textContent = st.workspace||"";
  $("#s-count").textContent = (st.documents||[]).length+" documents";
  $("#s-base").textContent = base;
  $("#s-spec").href = base+"/api/docs"+(st.api_token?"?token="+encodeURIComponent(st.api_token):"");
  $("#s-ver").textContent = "CV Studio "+(st.version||"");

  $("#s-open").onclick=async()=>{
    try{ await api("/api/reveal",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({})}); }
    catch(e){ toast(e.message,true) }
  };
  $("#s-exp").onclick=()=>window.open("/api/jobs/export?format=json"+tok());
  $("#s-check").onclick=()=>checkUpdates(true);

  /* Editor preferences */
  const live=$("#s-live");
  live.checked = pr.live!==false;
  live.onchange=()=>{ setPref("live",live.checked); $("#live").checked=live.checked };
  const delay=$("#s-delay");
  delay.value=String(pr.delay||700);
  delay.onchange=()=>setPref("delay",Number(delay.value));
  const dt=$("#s-deftheme");
  if(dt && !dt.dataset.filled){
    dt.innerHTML=(st.themes||[]).map(t=>"<option>"+esc(t)+"</option>").join("");
    dt.dataset.filled="1";
  }
  if(dt) { dt.value = pr.theme || (st.themes||[])[0] || ""; dt.onchange=()=>setPref("theme",dt.value) }

  /* Claude Desktop needs command and args separately; a single string holding
     "python script.py" is not runnable. */
  const L=st.server_launch||{command:"cv-studio-server",args:[]};
  const args=[...(L.args||[]),"--mcp"];
  if(st.workspace) args.push("--workspace",st.workspace);
  $("#s-mcp").textContent=JSON.stringify(
    {mcpServers:{"cv-studio":{command:L.command,args}}},null,2);

  const auth = st.api_token ? ' \\\n  -H "X-API-Key: '+st.api_token+'"' : "";
  $("#s-curl").textContent =
    "curl "+base+"/api/state"+auth+"\n\n"+
    "curl -X POST "+base+"/api/render"+auth+" \\\n"+
    '  -H "Content-Type: application/json" \\\n'+
    "  -d '{\"path\":\"profile/my-cv.yaml\"}'";
  $("#s-auth").textContent = st.api_token
    ? "An X-API-Key header is required; the key is shown in the example below."
    : "None needed. The server accepts local connections only. Start it with "+
      "--token to require a key, or --host to expose it, which forces one.";
}

/* Apply saved preferences at startup so they are not settings in name only. */
(function(){
  const pr=prefs();
  if(pr.live===false) $("#live").checked=false;
})();

const dlg=$("#newdlg");
$("#new").onclick=()=>{$("#newname").value="";dlg.showModal();$("#newname").focus()};
$("#newcancel").onclick=()=>dlg.close();
async function create(from){
  const name=$("#newname").value.trim();
  if(!name){ $("#newname").focus(); return }
  try{
    const r=await api("/api/new",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name,from,kind:$("#newkind").value,
                           theme:prefs().theme||null})});
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
    global WORKSPACE, FIRST_RUN, API_TOKEN, VERSION
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
    ap.add_argument("--app-version", default=None,
                    help="Version reported by the shell, so this cannot drift.")
    ap.add_argument("--parent-pid", type=int, default=None,
                    help="Exit when this process does, so we cannot be orphaned.")
    args = ap.parse_args()

    if args.app_version:
        VERSION = args.app_version
    if args.parent_pid:
        watch_parent(args.parent_pid)

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
