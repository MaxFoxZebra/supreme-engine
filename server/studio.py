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
SAFE_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
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
VERSION = "0.3.0"


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


def output_dir(path: Path) -> Path:
    """Where a document's render lands.

    Shared with the MCP server so that a CV rendered from Claude Desktop and
    one rendered by clicking Render end up as the same file, rather than two
    PDFs in different folders that quietly drift apart.
    """
    if path.parent.name in ("profile", "letters"):
        return WORKSPACE / "assets" / path.stem
    return path.parent / "output"


def render(path: Path) -> dict:
    return _shape(render_file(path, output_dir(path)))


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
                # Serve only the vendored assets, and only the shapes they take:
                # a file at the top level, or one inside a single named folder.
                # An allowlist rather than a blocklist, because a blocklist has
                # to know that a backslash is also a separator on Windows.
                parts = name.split("/")
                if len(parts) > 2 or not all(SAFE_ASSET.fullmatch(x) for x in parts):
                    return self._json({"error": "not found"}, 404)
                f = STATIC_DIR.joinpath(*parts)
                if not f.is_file() or STATIC_DIR.resolve() not in f.resolve().parents:
                    return self._json({"error": "not found"}, 404)
                if f.suffix == ".woff2":
                    return self._send(200, f.read_bytes(), "font/woff2")
                ctype = "application/javascript" if f.suffix == ".js" else "text/plain"
                return self._send(200, f.read_bytes(), ctype + "; charset=utf-8")
            if u.path == "/api/jobs":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json({"jobs": jobstore.list_jobs(
                    WORKSPACE, q.get("status", [None])[0], q.get("q", [None])[0],
                    q.get("node", [None])[0]),
                    "statuses": jobstore.STATUSES,
                    "nodes": jobstore.NODE_STATUSES,
                    "labels": jobstore.LABELS})
            if u.path == "/api/funnel":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json(jobstore.funnel(
                    WORKSPACE, q.get("since", [None])[0]))
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
   Workbench: dark chrome around warm paper.

   Two grounds and one accent. Everything that is *about* the work -- window
   chrome, navigation, the document list -- is dark. Everything that *is* the
   work -- the page, the form, the table -- sits on warm paper. Ochre marks
   exactly one thing at a time per region: the selected item, the primary
   action, or the live metric. Nothing else is coloured.

   Fonts are served from /static/fonts rather than Google, because the app is
   offline-first and a webview with no network should not fall back to Arial.
--------------------------------------------------------------------------- */
@font-face{font-family:'IBM Plex Sans';font-style:normal;font-weight:400 600;font-display:swap;
  src:url(/static/fonts/ibm-plex-sans-latin-var.woff2) format('woff2');
  unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,
  U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'IBM Plex Sans';font-style:normal;font-weight:400 600;font-display:swap;
  src:url(/static/fonts/ibm-plex-sans-latin-ext-var.woff2) format('woff2');
  unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,
  U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,
  U+2C60-2C7F,U+A720-A7FF}
@font-face{font-family:'IBM Plex Mono';font-style:normal;font-weight:400;font-display:swap;
  src:url(/static/fonts/ibm-plex-mono-latin-400.woff2) format('woff2');
  unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,
  U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'IBM Plex Mono';font-style:normal;font-weight:400;font-display:swap;
  src:url(/static/fonts/ibm-plex-mono-latin-ext-400.woff2) format('woff2');
  unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,
  U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,
  U+2C60-2C7F,U+A720-A7FF}
@font-face{font-family:'IBM Plex Mono';font-style:normal;font-weight:500;font-display:swap;
  src:url(/static/fonts/ibm-plex-mono-latin-500.woff2) format('woff2');
  unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,
  U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'IBM Plex Mono';font-style:normal;font-weight:500;font-display:swap;
  src:url(/static/fonts/ibm-plex-mono-latin-ext-500.woff2) format('woff2');
  unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,
  U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,
  U+2C60-2C7F,U+A720-A7FF}

:root{
  /* chrome */
  --c900:#161513; --c800:#1b1a17; --c700:#232220; --c650:#2b2a26; --c600:#33312b;
  --c550:#343229; --c500:#3a3833; --c450:#3d3b34; --c400:#4a4842; --c300:#7d7869;
  --c200:#a5a091; --c100:#cfcabd; --c050:#f0ede5; --cw:#f5f2ea; --c-hover:#5c594f;
  /* paper */
  --app:#f7f6f3; --panel:#f2efe8; --bar:#efece4; --canvas:#e4e1d8; --row-alt:#fbfaf7;
  --field:#ffffff; --page:#fffefc;
  --rule:#ddd8cc; --rule-strong:#d3cfc3; --bd-field:#cfcabd; --bd-inner:#e6e2d8;
  --t900:#1b1a17; --t800:#33302a; --t700:#4a463d; --t600:#615d52; --t500:#8a8577;
  --t400:#a8a294; --dot-idle:#b9b3a3; --dot-dead:#d6d1c4; --paper-hover:#e9e5da;
  /* accent -- used sparingly */
  --acc:#c08a3e; --acc-hover:#d09a4c; --acc-text:#9a5f1c; --acc-text-dark:#e8bc7c;
  --acc-wash:rgba(192,138,62,.10); --acc-ring:rgba(192,138,62,.18);
  --acc-line:rgba(192,138,62,.6);
  /* YAML syntax: the same muted ramp, retuned for a warm ground. Deliberately
     not ochre -- the accent already means "selected" everywhere else. */
  --tk-key:#3f5a73; --tk-str:#4a6b4f; --tk-num:#6a4a7a; --tk-bool:#a04a28;
  --tk-com:#a8a294; --tk-punc:#c0bbae; --tk-blk:#8a5a1e; --tk-sel:rgba(192,138,62,.22);
  --bad:#a33a22; --bad-bg:#f7ece7;
}

*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{
  background:var(--c700); color:var(--t900); overflow:hidden;
  display:flex; flex-direction:column;
  font:400 13px/1.5 'IBM Plex Sans',ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.mono{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Consolas,monospace}
button,select,input,textarea{font:inherit;color:inherit}
button{cursor:pointer;background:none;border:0;color:inherit;padding:0}
button:disabled{opacity:.4;cursor:default}
:focus{outline:none}
:focus-visible{outline:2px solid var(--acc);outline-offset:1px;border-radius:3px}
.grow{flex:1}
[hidden]{display:none!important}

/* ---------- title bar (46px) ------------------------------------------- */
#chrome{
  height:46px; flex:none; display:flex; align-items:center; gap:12px; padding:0 13px;
  background:var(--c700); border-bottom:1px solid #000;
  -webkit-app-region:drag; user-select:none;
}
#chrome button,#chrome input,#chrome .seg{-webkit-app-region:no-drag}
.lights{display:flex;gap:7px;padding-right:5px}
.lights button{width:11px;height:11px;border-radius:50%;background:var(--c400);
  transition:background .12s}
.lights button:hover{background:#6c6960}
.lights #w-close:hover{background:#c0392b}

/* segmented control, dark */
.seg{display:flex;gap:1px;background:var(--c900);border-radius:5px;padding:2px}
.seg button{padding:4px 13px;border-radius:4px;color:var(--c200);font-size:12.5px;
  white-space:nowrap}
.seg button:hover{color:var(--c050)}
.seg button[aria-selected=true]{background:var(--c500);color:var(--c050);font-weight:500}
.seg.tight button{padding:4px 12px;font-size:12px}

.doctitle{flex:1;display:flex;align-items:baseline;justify-content:center;gap:9px;
  min-width:0;overflow:hidden}
.doctitle .t{font-size:13px;font-weight:500;color:var(--c050);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.doctitle .f{font-size:11px;color:var(--c300);white-space:nowrap;flex:none}

.search{display:flex;align-items:center;gap:8px;width:220px;border:1px solid var(--c500);
  border-radius:5px;background:var(--c900);padding:4px 10px}
.search input{border:0;background:none;font-size:12.5px;color:var(--c050);width:100%;padding:0}
.search input::placeholder{color:var(--c300)}
.search:focus-within{border-color:var(--acc)}

.cbtn{font-size:12px;color:var(--c100);padding:4px 11px;border:1px solid var(--c500);
  border-radius:5px;white-space:nowrap}
.cbtn:hover:not(:disabled){border-color:var(--c-hover);color:#fff}
.cbtn.icon{padding:4px 8px;display:grid;place-items:center}
.pbtn{font-size:12px;color:var(--c800);padding:5px 13px;border-radius:5px;background:var(--acc);
  font-weight:500;white-space:nowrap}
.pbtn:hover:not(:disabled){background:var(--acc-hover)}

/* ---------- shell ------------------------------------------------------ */
main{flex:1;min-height:0;display:flex;background:var(--app)}
.view{flex:1;display:flex;min-height:0;min-width:0}
.rail{flex:none;background:var(--c650);border-right:1px solid #000;display:flex;
  flex-direction:column;min-height:0;overflow-y:auto}
.rail-cvs{width:230px} .rail-jobs{width:196px;padding:12px 7px;gap:1px}
.rail-label{padding:14px 13px 7px;font-size:10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--c300)}
.rail-jobs .rail-label{padding:5px 9px 7px}
.rail-jobs .rail-label+.rail-label,.rail-jobs .rail-label:not(:first-child){padding-top:16px}
.rail-list{display:flex;flex-direction:column;padding:0 7px}

/* a sidebar row: 3px marker, label, mono count */
.row{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:5px;
  text-align:left;width:100%}
.row:hover{background:var(--c550)}
.row .mark{width:3px;height:15px;border-radius:2px;background:transparent;flex:none}
.row .lbl{font-size:13px;color:var(--c100);flex:1;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.row .ct{font-size:10.5px;color:var(--c300);flex:none}
.row.sel{background:var(--c450)}
.row.sel .mark{background:var(--acc)}
.row.sel .lbl{color:var(--cw)}
.row.sel .ct{color:var(--c200)}

/* outline rows sit one level in; the active section shows its entries */
.orow{display:flex;justify-content:space-between;gap:8px;padding:5px 8px 5px 20px;
  border-radius:5px;font-size:12.5px;color:var(--c100);text-align:left;width:100%}
.orow:hover{background:var(--c550)}
.orow.sel{background:var(--c550);color:var(--cw)}
.orow .ct{font-size:10.5px;color:var(--c300);flex:none}
.orow.sel .ct{color:var(--c200)}
.okids{display:flex;flex-direction:column;padding:2px 0}
.okid{padding:4px 8px 4px 32px;font-size:12px;color:var(--c200);text-align:left;width:100%;
  border-radius:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.okid:hover{background:var(--c550)}
.okid.sel{color:var(--acc)}

/* page budget */
.budget{padding:13px;border-top:1px solid var(--c800);display:flex;flex-direction:column;
  gap:7px;flex:none}
.budget .brow{display:flex;justify-content:space-between;align-items:baseline}
.budget .pp{font-size:12.5px;color:var(--cw)}
.budget .ww{font-size:11px;color:var(--c300)}
.budget .bar{display:flex;gap:2px}
.budget .bar i{height:5px;flex:1;background:var(--c450)}
.budget .bar i.on{background:var(--acc)}
.budget .cap{font-size:11.5px;color:var(--c200);line-height:1.4}

/* ---------- centre column ---------------------------------------------- */
.centre{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0;
  background:var(--canvas)}
.subbar{height:33px;flex:none;display:flex;align-items:center;gap:11px;padding:0 12px;
  background:var(--bar);border-bottom:1px solid var(--rule-strong)}
.seg.light{background:#dedbd0}
.seg.light button{color:var(--t600);padding:3px 12px;font-size:12px}
.seg.light button:hover{color:var(--t900)}
.seg.light button[aria-selected=true]{background:#fff;color:var(--t900);font-weight:500}
.meta{font-size:11px;color:var(--t500);display:flex;align-items:center;gap:6px}
.meta button{font-size:12px;color:var(--t500);padding:0 3px;line-height:1}
.meta button:hover:not(:disabled){color:var(--t900)}

.pane{flex:1;min-height:0;overflow:auto}
.pane-page{display:grid;place-items:center;padding:22px}
.pg{display:block;background:var(--page);box-shadow:0 12px 28px rgba(30,26,18,.32)}
.pane-form{background:var(--app);padding:16px 20px 40px}
.pane-yaml{background:var(--app);padding:0;overflow:hidden;display:flex;flex-direction:column}

/* ---------- form -------------------------------------------------------- */
.grp{border:0;border-top:1px solid var(--rule);margin:0;background:none}
.grp:first-of-type{border-top:0}
.grp>summary{list-style:none;cursor:pointer;padding:12px 0 10px;display:flex;align-items:center;
  gap:8px;font-size:12px;font-weight:600;color:var(--t900);text-transform:capitalize}
.grp>summary::-webkit-details-marker{display:none}
.grp>summary .chev{transition:transform .15s;color:var(--t500);flex:none}
.grp[open]>summary .chev{transform:rotate(90deg)}
.grp>summary .count{margin-left:auto;font-size:10.5px;color:var(--t500);font-weight:400;
  text-transform:none}
.grp .body{padding:0 0 14px}
.entry{border-left:1px solid var(--rule);padding:2px 0 2px 14px;margin:12px 0}
.entry-hd{font-size:12.5px;font-weight:600;margin-bottom:8px;display:flex;gap:8px;
  align-items:baseline}
.entry-hd button{font-size:12px;color:var(--acc-text);margin-left:auto}
.entry-hd button:hover{text-decoration:underline}

.fg{display:grid;grid-template-columns:70px 1fr;gap:8px 10px;align-items:center}
.fg.wide{grid-template-columns:120px 1fr;align-items:start}
.fg.w88{grid-template-columns:88px 1fr;gap:10px 12px}
.fg>label{font-size:12px;color:var(--t600);text-align:right;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.fg.wide>label{padding-top:6px}
.inp,.fg input,.fg select,.fg textarea{background:var(--field);border:1px solid var(--bd-field);
  border-radius:4px;padding:5px 8px;font-size:12.5px;color:var(--t900);width:100%;min-width:0}
.fg textarea{font:12.5px/1.5 inherit;resize:vertical;min-height:56px}
.fg .mono,.fg input.mono,.fg textarea.mono{font-family:'IBM Plex Mono',ui-monospace,Consolas,monospace;
  font-size:12px}
.fg input:hover,.fg select:hover,.fg textarea:hover{border-color:var(--t400)}
.fg input:focus,.fg select:focus,.fg textarea:focus{border-color:var(--acc);
  box-shadow:0 0 0 3px var(--acc-ring)}
.fg input[readonly]{background:var(--bar);border-color:var(--rule);color:var(--t600)}

/* ---------- inspector --------------------------------------------------- */
.insp{flex:none;background:var(--panel);border-left:1px solid var(--rule-strong);
  display:flex;flex-direction:column;min-height:0}
.insp-cvs{width:312px} .insp-jobs{width:284px} .insp-funnel{width:296px}
.insp-head{height:33px;flex:none;display:flex;align-items:center;justify-content:space-between;
  gap:10px;padding:0 13px;background:var(--bar);border-bottom:1px solid var(--rule-strong)}
.insp-head b{font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.insp-head .mono{font-size:10.5px;color:var(--t500);flex:none}
.insp-body{flex:1;min-height:0;overflow-y:auto;padding:14px;display:flex;
  flex-direction:column;gap:14px}
.block{display:flex;flex-direction:column;gap:7px}
.block.ruled{padding-top:12px;border-top:1px solid var(--rule)}
.blabel{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--t500)}

.card{border:1px solid var(--bd-field);border-radius:4px;background:var(--field);overflow:hidden}
.card>*+*{border-top:1px solid var(--bd-inner)}
.crow{display:flex;gap:9px;padding:8px 10px;align-items:flex-start}
.crow .cidx{font-size:10.5px;color:var(--t400);padding-top:2px;flex:none;width:11px}
.crow.on{background:var(--acc-wash)}
.crow.on .cidx{color:var(--acc-text)}
.crow textarea{flex:1;border:0;background:none;resize:none;overflow:hidden;padding:0;
  font:12.5px/1.45 inherit;color:var(--t900);min-width:0}
.drow{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 10px}
.drow>span{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drow select{border:0;background:none;font-size:12.5px;color:var(--t900);flex:1;min-width:0;
  padding:0;cursor:pointer}
.drow select.empty{color:var(--t400)}
.alink{font-size:12px;color:var(--acc-text);flex:none}
.alink:hover{text-decoration:underline}
.muted{color:var(--t400)}

.mini{width:24px;height:21px;display:grid;place-items:center;border:1px solid var(--bd-field);
  border-radius:4px;background:var(--field);font-size:13px;color:var(--t700);flex:none}
.mini:hover:not(:disabled){background:var(--paper-hover)}
.obtn{font-size:12px;padding:5px 11px;border:1px solid var(--bd-field);border-radius:4px;
  background:var(--field);color:var(--t700)}
.obtn:hover:not(:disabled){background:var(--paper-hover)}

/* five bars for a 0-5 fit score */
.fit{display:flex;gap:3px}
.fit button{width:16px;height:5px;background:var(--rule);border-radius:1px;padding:0}
.fit button.on{background:var(--acc)}
.dot{width:6px;height:6px;border-radius:50%;flex:none;background:var(--dot-idle)}
.dot.live{background:var(--acc)} .dot.dead{background:var(--dot-dead)}

/* history timeline */
.tl{display:flex;flex-direction:column}
.tli{display:flex;gap:10px}
.tli .spine{display:flex;flex-direction:column;align-items:center;width:9px;flex:none}
.tli .spine i{width:7px;height:7px;border-radius:50%;background:#c8c2b3;margin-top:4px;flex:none}
.tli .spine u{flex:1;width:1px;background:var(--rule)}
.tli:last-child .spine u{display:none}
.tli:last-child .spine i{background:var(--acc)}
.tli .ev{display:flex;justify-content:space-between;gap:10px;flex:1;font-size:12.5px;
  padding-bottom:8px}
.tli:last-child .ev{padding-bottom:0}
.tli .ev span:first-child{color:var(--t600)}
.tli:last-child .ev span:first-child{color:var(--t900)}
.tli .ev .when{font-size:11px;color:var(--t500);flex:none}
.note{font-size:12.5px;color:var(--t700);line-height:1.55}
.hr{height:1px;background:var(--rule);margin:3px 0}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:12.5px}
.kv span:first-child{color:var(--t600)}
.kv .v{font-size:12px;flex:none}
.kv .v.acc{color:var(--acc-text)}

/* ---------- status bar (23px) ------------------------------------------- */
#status{height:23px;flex:none;display:flex;align-items:center;gap:8px;padding:0 12px;
  background:var(--c700);font-size:10.5px;color:var(--c300);user-select:none}
#status .sep::before{content:"\00b7"}
#status .warn{color:var(--acc-text-dark)}

/* ---------- jobs table --------------------------------------------------- */
.tablewrap{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0;
  background:var(--app)}
.thead,.trow{display:grid;grid-template-columns:2fr 1.5fr 1.2fr 1fr .85fr .85fr;
  align-items:center}
.thead{height:26px;flex:none;background:var(--bar);border-bottom:1px solid var(--rule-strong);
  font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--t500)}
.thead>*,.trow>*{padding:0 12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tbody{flex:1;min-height:0;overflow-y:auto}
.trow{height:32px;font-size:12.5px;border-bottom:1px solid var(--bd-inner);width:100%;
  text-align:left;color:var(--t900)}
.trow:nth-child(even){background:var(--row-alt)}
.trow:hover{background:#f1eee6}
.trow .role{display:flex;gap:8px;align-items:baseline;min-width:0}
.trow .role b{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trow .role i{font-style:normal;font-size:11.5px;color:var(--t500);flex:none}
.trow .docs{font-size:11px;color:var(--acc-text)}
.trow .docs:hover{text-decoration:underline}
.trow .st{display:flex;align-items:center;gap:7px}
.trow .money{font-size:11.5px}
.trow .when{font-size:12px;color:var(--t600)}
.trow.dead{color:var(--t600)}
.trow.dead .money,.trow.dead .when{color:var(--t600)}
.trow .when.none,.trow .money.none{color:var(--t400)}
.trow .when.due{color:var(--acc-text)}
.trow.sel,.trow.sel:hover,.trow.sel:nth-child(even){background:var(--c450);color:var(--cw)}
.trow.sel .role i{color:#b3ae9f}
.trow.sel .docs{color:#e0d9c7}
.trow.sel .when,.trow.sel .money{color:var(--cw)}
.trow.sel .when.due{color:var(--acc-text-dark)}
.trow.sel .when.none,.trow.sel .money.none{color:#928d80}

/* ---------- funnel ------------------------------------------------------- */
.fn-left{flex:1;min-width:0;padding:22px 24px;display:flex;flex-direction:column;gap:14px;
  background:var(--app);overflow:auto}
.fn-head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.fn-head b{font-size:15px;font-weight:600}
.fn-head span{font-size:12.5px;color:var(--t600)}
#chart svg{width:100%;height:auto;display:block}
.sk-link{transition:opacity .15s}
.sk-hit{cursor:pointer}
.sk-hit:hover .sk-node{opacity:.8}
.sk-dim{opacity:.25}
.sk-label{font:12px 'IBM Plex Sans',sans-serif;fill:#33312b}

/* ---------- sheets and overlays ------------------------------------------ */
.scrim{position:fixed;inset:0;background:rgba(0,0,0,.34);z-index:39}
.sheet{position:fixed;left:50%;top:46px;transform:translateX(-50%);width:620px;
  max-width:calc(100% - 40px);max-height:calc(100% - 66px);overflow-y:auto;
  background:var(--app);border-radius:0 0 9px 9px;box-shadow:0 22px 48px rgba(0,0,0,.45);
  padding:22px 24px;display:flex;flex-direction:column;gap:16px;z-index:40}
.sheet h3{margin:0;font-size:15px;font-weight:600}
.sheet p{margin:4px 0 0;font-size:12.5px;color:var(--t600);line-height:1.45}
.sheet .foot{display:flex;justify-content:flex-end;gap:8px;padding-top:2px}
.sheet .foot .left{margin-right:auto}
.sbtn{font-size:12.5px;padding:6px 16px;border:1px solid var(--bd-field);border-radius:5px;
  background:var(--field);color:var(--t900)}
.sbtn:hover:not(:disabled){background:var(--paper-hover)}
.sbtn.primary{background:var(--acc);color:var(--c800);font-weight:500;border-color:var(--acc);
  padding:6px 18px}
.sbtn.primary:hover:not(:disabled){background:var(--acc-hover);border-color:var(--acc-hover)}
.sbtn.danger{border-color:transparent;color:var(--bad);background:none}
.sbtn.danger:hover{background:var(--bad-bg)}

/* segmented control on paper */
.seg.paper{background:var(--canvas);width:fit-content}
.seg.paper button{color:var(--t700);padding:4px 15px;font-size:12.5px}
.seg.paper button[aria-selected=true]{background:#fff;color:var(--t900);font-weight:500}
.seg.paper.acc button[aria-selected=true]{background:var(--acc);color:var(--c800)}

.check{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--t800);
  cursor:pointer}
.check input{position:absolute;opacity:0;width:0;height:0}
.check i{width:15px;height:15px;border-radius:3px;border:1px solid var(--bd-field);
  background:var(--field);display:grid;place-items:center;font-size:10px;font-style:normal;
  color:transparent;flex:none}
.check input:checked+i{background:var(--acc);border-color:var(--acc);color:var(--c800)}
.check input:focus-visible+i{outline:2px solid var(--acc);outline-offset:2px}

/* full-window overlay: Design and Settings */
.ovl{position:fixed;inset:0;z-index:45;background:var(--app);display:flex;
  flex-direction:column}
.ovl-bar{height:42px;flex:none;display:flex;align-items:center;gap:12px;padding:0 13px;
  background:var(--c700);-webkit-app-region:drag}
.ovl-bar button{-webkit-app-region:no-drag}
.ovl-bar .ttl{font-size:13px;font-weight:500;color:var(--c050)}
.ovl-body{flex:1;min-height:0;display:flex}
.dz-left{flex:1;min-width:0;padding:22px 24px;display:flex;flex-direction:column;gap:16px;
  background:var(--bar);overflow-y:auto}
.themegrid{display:grid;grid-template-columns:repeat(5,1fr);gap:16px}
.thumbwrap{display:flex;flex-direction:column;gap:8px;align-items:center}
.thumb{width:100%;aspect-ratio:.73;background:var(--page);border:1px solid var(--rule);
  box-shadow:0 6px 14px -8px rgba(30,26,18,.3);padding:10px 9px;display:flex;
  flex-direction:column;gap:4px}
.thumbwrap.sel .thumb{border-color:transparent;outline:2px solid var(--acc);
  box-shadow:0 6px 14px -6px rgba(30,26,18,.4)}
.thumb i{display:block;background:#e0dcd1;flex:none}
.thumb i.ink{background:#15140f}
.thumbcap{display:flex;align-items:baseline;gap:6px}
.thumbcap span{font-size:12.5px;color:var(--t700)}
.thumbcap em{font-size:10.5px;font-style:normal;color:var(--t500)}
.thumbwrap.sel .thumbcap span{color:var(--t900);font-weight:500}
.thumbwrap.sel .thumbcap em{color:var(--acc-text)}

.slider{display:flex;align-items:center;gap:11px}
.slider input[type=range]{flex:1;-webkit-appearance:none;appearance:none;background:none;
  height:14px;margin:0}
.slider input[type=range]::-webkit-slider-runnable-track{height:3px;border-radius:2px;
  background:linear-gradient(to right,var(--acc) var(--fill,50%),var(--rule) var(--fill,50%))}
.slider input[type=range]::-moz-range-track{height:3px;border-radius:2px;background:var(--rule)}
.slider input[type=range]::-moz-range-progress{height:3px;border-radius:2px;background:var(--acc)}
.slider input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;
  border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(30,26,18,.45);margin-top:-5.5px;
  cursor:pointer}
.slider input[type=range]::-moz-range-thumb{width:14px;height:14px;border:0;border-radius:50%;
  background:#fff;box-shadow:0 1px 3px rgba(30,26,18,.45);cursor:pointer}
.slider .val{font-size:12px;color:var(--t700);flex:none;min-width:52px;text-align:right}

.dgrid{display:grid;grid-template-columns:158px 1fr;gap:10px 12px;align-items:center}
.dgrid>label{font-size:12px;color:var(--t600);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.dctl{display:flex;align-items:center;gap:7px;min-width:0}
.dctl input[type=text],.dctl input[type=number],.dctl select{background:var(--field);
  border:1px solid var(--bd-field);border-radius:4px;padding:4px 7px;font-size:12.5px;
  min-width:0;flex:1}
.dctl input:focus,.dctl select:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-ring)}
.dctl input[type=number]{max-width:84px;flex:none}
.dctl select.unit{max-width:66px;flex:none}
.dctl input[type=color]{width:22px;height:22px;padding:0;border:1px solid var(--bd-field);
  border-radius:3px;background:none;cursor:pointer;flex:none}
.dctl input[type=checkbox]{width:15px;height:15px;accent-color:var(--acc);cursor:pointer}
.dctl .hex{font-size:11px;color:var(--t500);flex:none}

/* ---------- settings ----------------------------------------------------- */
.set-wrap{flex:1;min-height:0;overflow-y:auto;padding:26px 30px 60px}
.set-inner{display:grid;grid-template-columns:146px minmax(0,1fr);gap:34px;max-width:860px}
.set-rail{display:flex;flex-direction:column;gap:1px;position:sticky;top:0;align-self:start}
.set-rail button{text-align:left;padding:6px 10px;font-size:13px;color:var(--t600);
  border-radius:5px}
.set-rail button:hover{background:var(--bar);color:var(--t900)}
.set-rail button[aria-selected=true]{color:var(--t900);font-weight:500;background:var(--bar)}
.sp h3{font-size:15px;font-weight:600;margin:0 0 4px}
.sp-lede{color:var(--t600);font-size:12.5px;line-height:1.6;margin:0 0 16px;max-width:62ch}
.sp-note{color:var(--t600);font-size:12px;line-height:1.6;margin:14px 0 0;max-width:62ch}
.srow{display:flex;align-items:center;gap:20px;padding:13px 0;border-top:1px solid var(--rule)}
.srow>div{flex:1;min-width:0}
.srow b{display:block;font-size:13px;font-weight:500;margin-bottom:2px}
.srow span{display:block;color:var(--t600);font-size:12px;line-height:1.55;
  overflow-wrap:anywhere}
.srow select,.srow input{border:1px solid var(--bd-field);border-radius:4px;padding:5px 8px;
  background:var(--field);font-size:12.5px}
.btnlink{text-decoration:none;color:var(--t700)}
.steps{margin:0 0 14px;padding-left:18px;font-size:13px;line-height:1.7}
.steps li{margin-bottom:4px}.steps li::marker{color:var(--t500)}
pre.code{background:var(--bar);border:1px solid var(--rule);border-radius:4px;padding:12px 14px;
  font:11.5px/1.7 'IBM Plex Mono',ui-monospace,Consolas,monospace;overflow-x:auto;margin:0 0 8px;
  white-space:pre}
.tgl{position:relative;display:inline-block;width:34px;height:20px;flex:none;cursor:pointer}
.tgl input{opacity:0;width:0;height:0;position:absolute}
.tgl i{position:absolute;inset:0;background:var(--rule);border-radius:99px;transition:background .16s}
.tgl i::before{content:"";position:absolute;width:14px;height:14px;left:3px;top:3px;
  background:#fff;border-radius:50%;transition:transform .16s}
.tgl input:checked+i{background:var(--acc)}
.tgl input:checked+i::before{transform:translateX(14px)}
.tgl input:focus-visible+i{outline:2px solid var(--acc);outline-offset:2px}

/* ---------- yaml editor --------------------------------------------------- */
.edwrap{position:relative;flex:1;min-height:0;background:var(--app)}
.edwrap pre,.edwrap textarea{position:absolute;inset:0;margin:0;padding:16px 18px;border:0;
  font:12.5px/1.7 'IBM Plex Mono',ui-monospace,Consolas,monospace;white-space:pre;
  overflow:auto;tab-size:2}
.edwrap pre{pointer-events:none;color:var(--t900)}
.edwrap textarea{background:transparent;color:transparent;caret-color:var(--t900);resize:none}
.edwrap textarea::selection{background:var(--tk-sel)}
.t-key{color:var(--tk-key)}.t-str{color:var(--tk-str)}.t-num{color:var(--tk-num)}
.t-bool{color:var(--tk-bool)}.t-com{color:var(--tk-com)}
.t-punc{color:var(--tk-punc)}.t-blk{color:var(--tk-blk)}
.yamlerr{flex:none;background:var(--bad-bg);border-bottom:1px solid #e6cfc5;padding:8px 18px;
  font-size:12px;color:var(--bad);display:flex;gap:10px;align-items:baseline}
.yamlerr button{font-size:12px;color:var(--bad);text-decoration:underline;flex:none;
  margin-left:auto}

/* ---------- states -------------------------------------------------------- */
.empty{padding:56px 26px;color:var(--t600);max-width:48ch}
.empty h3{margin:0 0 6px;font-size:13.5px;color:var(--t900);font-weight:600}
.empty p{margin:0;font-size:13px;line-height:1.7}
.empty .cta{margin-top:18px}
.err{margin:20px;background:var(--bad-bg);border-left:2px solid var(--bad);padding:14px 16px;
  color:var(--bad);max-width:70ch}
.err h4{margin:0 0 6px;font-size:12.5px;font-weight:600}
.err .hint{color:var(--t900);margin:8px 0 0;font-size:12.5px;line-height:1.65}
.err pre{margin:10px 0 0;white-space:pre-wrap;font:11px/1.55 'IBM Plex Mono',Consolas,monospace;
  max-height:190px;overflow:auto;color:var(--t600)}
.spin{display:inline-block;width:9px;height:9px;border:1.5px solid var(--rule);
  border-top-color:var(--t500);border-radius:50%;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.skel{background:var(--canvas);border-radius:4px;animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}

#toasts{position:fixed;bottom:33px;right:14px;z-index:60;display:flex;flex-direction:column;
  gap:6px;align-items:flex-end;pointer-events:none}
.toast{background:var(--c700);color:var(--c050);border-radius:5px;padding:7px 12px;
  font-size:12.5px;max-width:340px;box-shadow:0 8px 24px rgba(0,0,0,.3);animation:rise .16s ease-out}
.toast.bad{background:var(--bad);color:#fff}
@keyframes rise{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

@media(max-width:1100px){
  .rail-cvs{width:200px}.insp-cvs{width:270px}.insp-jobs{width:250px}
  .themegrid{grid-template-columns:repeat(3,1fr)}
}
@media(max-width:880px){
  .insp{display:none}
  .thead,.trow{grid-template-columns:2fr 1.4fr 1.2fr .9fr}
  .thead>div:nth-child(n+5),.trow>div:nth-child(n+5){display:none}
  .set-inner{grid-template-columns:1fr;gap:16px}
  .set-rail{flex-direction:row;flex-wrap:wrap;position:static}
}
</style></head><body>

<header id="chrome" data-tauri-drag-region>
  <div class="lights" id="lights" hidden>
    <button id="w-close" title="Close" aria-label="Close"></button>
    <button id="w-min" title="Minimise" aria-label="Minimise"></button>
    <button id="w-max" title="Maximise" aria-label="Maximise"></button>
  </div>
  <div class="seg" id="nav" role="tablist" aria-label="View">
    <button role="tab" data-view="cvs" aria-selected="true">CVs</button>
    <button role="tab" data-view="jobs" aria-selected="false">Jobs</button>
    <button role="tab" data-view="funnel" aria-selected="false">Funnel</button>
  </div>

  <div class="doctitle" id="doctitle"><span class="t"></span><span class="f mono"></span></div>
  <div class="grow" id="chrome-gap" hidden></div>

  <div class="seg tight" id="range" hidden role="tablist" aria-label="Date range">
    <button role="tab" data-since="" aria-selected="true">All time</button>
    <button role="tab" data-since="6m" aria-selected="false">6 months</button>
    <button role="tab" data-since="30d" aria-selected="false">30 days</button>
  </div>
  <label class="search" id="search" hidden><svg width="12" height="12" viewBox="0 0 24 24"
      fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"
      style="flex:none;color:var(--c300)"><circle cx="11" cy="11" r="7"/>
      <path d="M20 20l-4-4"/></svg>
    <input id="jobq" type="search" placeholder="Search jobs" aria-label="Search jobs"></label>

  <button class="cbtn" id="btn-design" title="Theme, typeface and page size">Design</button>
  <button class="cbtn icon" id="btn-settings" title="Settings, setup and help"
    aria-label="Settings"><svg width="14" height="14" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/>
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65
    1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9
    19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0
    .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65
    0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0
    0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2
    2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1
    0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
  <button class="cbtn" id="btn-pdf" disabled>Export PDF&#8230;</button>
  <button class="pbtn" id="btn-render">Render</button>
  <button class="pbtn" id="btn-newjob" hidden>New job&#8230;</button>
</header>

<main>
  <!-- ---------------------------------------------------------------- CVs -->
  <section class="view" id="v-cvs">
    <aside class="rail rail-cvs">
      <div class="rail-label mono">Documents</div>
      <div class="rail-list" id="doclist"></div>
      <div class="rail-label mono" id="outline-label">Outline</div>
      <div class="rail-list" id="outline"></div>
      <div class="grow"></div>
      <div class="budget" id="budget" hidden>
        <div class="brow"><span class="pp"></span><span class="ww mono"></span></div>
        <div class="bar"><i></i><i></i><i></i><i></i><i></i><i></i></div>
        <div class="cap"></div>
      </div>
    </aside>

    <div class="centre">
      <div class="subbar">
        <div class="seg light" id="edtabs" role="tablist" aria-label="Preview mode">
          <button role="tab" data-tab="page" aria-selected="true">Page</button>
          <button role="tab" data-tab="form" aria-selected="false">Form</button>
          <button role="tab" data-tab="yaml" aria-selected="false">YAML</button>
        </div>
        <div class="grow"></div>
        <div class="meta mono" id="pmeta">
          <button id="pg-prev" title="Previous page" aria-label="Previous page">&#8249;</button>
          <span id="pg-idx">&#8212;</span>
          <button id="pg-next" title="Next page" aria-label="Next page">&#8250;</button>
          <span>&#183;</span>
          <button id="z-out" title="Zoom out" aria-label="Zoom out">&#8722;</button>
          <span id="z-lvl">100%</span>
          <button id="z-in" title="Zoom in" aria-label="Zoom in">+</button>
        </div>
      </div>
      <div class="pane pane-page" id="pane-page"></div>
      <div class="pane pane-form" id="pane-form" hidden></div>
      <div class="pane pane-yaml" id="pane-yaml" hidden>
        <div class="yamlerr" id="yamlerr" hidden><span></span>
          <button type="button">Go to line</button></div>
        <div class="edwrap"><pre id="hl" aria-hidden="true"></pre>
          <textarea id="yaml" spellcheck="false" aria-label="CV source"></textarea></div>
      </div>
    </div>

    <aside class="insp insp-cvs">
      <div class="insp-head"><b id="insp-title">Nothing selected</b>
        <span class="mono" id="insp-meta"></span></div>
      <div class="insp-body" id="insp-body"></div>
    </aside>
  </section>

  <!-- --------------------------------------------------------------- Jobs -->
  <section class="view" id="v-jobs" hidden>
    <aside class="rail rail-jobs">
      <div class="rail-label mono">Status</div>
      <div id="statuslist"></div>
      <div class="rail-label mono">Saved</div>
      <div id="savedlist"></div>
    </aside>
    <div class="tablewrap">
      <div class="thead"><span>Role</span><span>Documents</span><span>Status</span>
        <span>Salary</span><span>Applied</span><span>Follow-up</span></div>
      <div class="tbody" id="jobrows"></div>
    </div>
    <aside class="insp insp-jobs">
      <div class="insp-head"><b id="jinsp-title">No application selected</b></div>
      <div class="insp-body" id="jinsp-body"></div>
    </aside>
  </section>

  <!-- ------------------------------------------------------------- Funnel -->
  <section class="view" id="v-funnel" hidden>
    <div class="fn-left">
      <div class="fn-head"><b id="fn-total"></b><span id="fn-sub"></span></div>
      <div id="chart"></div>
    </div>
    <aside class="insp insp-funnel">
      <div class="insp-head"><b>Rates</b></div>
      <div class="insp-body" id="fn-rates"></div>
    </aside>
  </section>
</main>

<footer id="status"><span id="st-left"></span><div class="grow"></div>
  <span id="st-right" class="mono"></span></footer>

<div id="toasts" aria-live="polite"></div>
<div class="scrim" id="scrim" hidden></div>
<div class="sheet" id="sheet" hidden role="dialog" aria-modal="true"
  aria-labelledby="sheet-title"></div>

<!-- ------------------------------------------------------------- Design -->
<div class="ovl" id="ovl-design" hidden>
  <div class="ovl-bar" data-tauri-drag-region><span class="ttl">Design</span>
    <div class="grow"></div><button class="cbtn" data-close-ovl>Done</button></div>
  <div class="ovl-body">
    <div class="dz-left">
      <span class="blabel mono">Theme</span>
      <div class="themegrid" id="themegrid"></div>
      <div class="hr"></div>
      <div class="fg w88" id="dz-basics" style="max-width:520px"></div>
      <div id="dz-advanced"></div>
    </div>
    <aside class="insp insp-funnel">
      <div class="insp-head"><b>Effect on this CV</b></div>
      <div class="insp-body" id="dz-effect"></div>
    </aside>
  </div>
</div>

<!-- ----------------------------------------------------------- Settings -->
<div class="ovl" id="ovl-settings" hidden>
  <div class="ovl-bar" data-tauri-drag-region><span class="ttl">Settings</span>
    <div class="grow"></div><button class="cbtn" data-close-ovl>Done</button></div>
  <div class="set-wrap"><div class="set-inner">
    <nav class="set-rail" id="set-rail">
      <button data-s="workspace" aria-selected="true">Workspace</button>
      <button data-s="editor" aria-selected="false">Editor</button>
      <button data-s="ai" aria-selected="false">Claude Desktop</button>
      <button data-s="api" aria-selected="false">API</button>
      <button data-s="updates" aria-selected="false">Updates</button>
      <button data-s="about" aria-selected="false">About</button>
    </nav>
    <div>
      <section class="sp" id="sp-workspace">
        <h3>Workspace</h3>
        <p class="sp-lede">Everything lives in one folder you own. CVs and letters are
          plain YAML; applications are a single SQLite file. Copy the folder and you
          have copied everything.</p>
        <div class="srow"><div><b>Folder</b><span id="s-ws" class="mono"></span></div>
          <button class="obtn" id="s-open">Open folder</button></div>
        <div class="srow"><div><b>Documents</b><span id="s-count"></span></div></div>
        <div class="srow"><div><b>Applications</b><span>Exported as JSON or CSV so the
          database is never a lock-in.</span></div>
          <button class="obtn" id="s-exp">Export JSON</button></div>
      </section>

      <section class="sp" id="sp-editor" hidden>
        <h3>Editor</h3>
        <p class="sp-lede">Live preview re-renders a scratch copy as you type, so your
          file is only written when you actually save.</p>
        <div class="srow"><div><b>Live preview</b><span>Re-render while typing.</span></div>
          <label class="tgl"><input type="checkbox" id="s-live"><i></i></label></div>
        <div class="srow"><div><b>Idle before re-rendering</b>
          <span>Longer if renders feel busy on your machine.</span></div>
          <select id="s-delay"><option value="400">0.4s</option><option value="700">0.7s</option>
            <option value="1200">1.2s</option><option value="2000">2s</option></select></div>
        <div class="srow"><div><b>Theme for new documents</b>
          <span>Applied when you create a CV or a letter.</span></div>
          <select id="s-deftheme"></select></div>
      </section>

      <section class="sp" id="sp-ai" hidden>
        <h3>Claude Desktop</h3>
        <p class="sp-lede">CV Studio ships an MCP server, so an AI client can read, edit
          and render your CVs. <code>render_cv</code> returns the page as an image, so the
          model can look at the result rather than guess from the source.</p>
        <ol class="steps"><li>Open Claude Desktop settings, then Developer, then Edit config.</li>
          <li>Paste this in and restart Claude Desktop.</li></ol>
        <pre class="code" id="s-mcp"></pre>
        <button class="obtn" data-copy="s-mcp">Copy</button>
      </section>

      <section class="sp" id="sp-api" hidden>
        <h3>API</h3>
        <p class="sp-lede">The same server answers a small HTTP API, so scripts and other
          tools can drive it.</p>
        <div class="srow"><div><b>Base URL</b><span id="s-base" class="mono"></span></div>
          <a class="obtn btnlink" id="s-spec" target="_blank" rel="noreferrer">Open reference</a></div>
        <div class="srow"><div><b>Authentication</b><span id="s-auth"></span></div></div>
        <pre class="code" id="s-curl"></pre>
        <button class="obtn" data-copy="s-curl">Copy</button>
      </section>

      <section class="sp" id="sp-updates" hidden>
        <h3>Updates</h3>
        <p class="sp-lede">Updates are signed with the key baked into this build, so a
          compromised release host cannot push a package this app will install.</p>
        <div class="srow"><div><b>Version</b><span id="s-ver"></span></div>
          <button class="obtn" id="s-check">Check now</button></div>
        <div class="srow"><div><b>Status</b><span id="u-state">Not checked yet.</span></div></div>
        <div id="u-actions"></div>
      </section>

      <section class="sp" id="sp-about" hidden>
        <h3>About</h3>
        <p class="sp-lede">A local CV editor with live PDF preview, built on RenderCV and
          Typst. Everything runs on your machine: no account, no server, no telemetry.</p>
        <p class="sp-note">MIT licensed. Bundles RenderCV (MIT), Typst (Apache-2.0), the
          RenderCV font set and IBM Plex (SIL Open Font License), and d3-sankey (ISC).</p>
      </section>
    </div>
  </div></div>
</div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const API_TOKEN=__API_TOKEN__;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const tok=()=>API_TOKEN?"&token="+encodeURIComponent(API_TOKEN):"";

/* One object holds everything the three screens share. Selection, filters and
   the last good render all live here so that switching views never throws work
   away: the funnel can hand a status filter to Jobs, and Jobs can hand a
   document to the editor, without either reloading. */
const S={
  view:"cvs", state:null,
  path:null, doc:null, data:null, tab:"page",
  dirty:false, savedAt:null, busy:false,
  pdf:null, render:null, renderMs:null, live:"idle", liveMsg:"",
  page:0, zoom:1, zoomAuto:true, fill:null,
  sel:null, openSection:null,
  pages:{},                 /* path -> page count, learned as things render */
  themePages:{},            /* theme -> page count for the open document */
  jobs:[], statuses:[], nodes:{}, labels:{}, jready:false,
  jfilter:{kind:"all", value:""}, jsel:null,
  funnel:null, since:"", fnode:null,
  schema:null, schemaTheme:null,
};
const DZ={theme:null, family:null, page:null, size:null};

function toast(msg,bad){
  const t=document.createElement("div");
  t.className="toast"+(bad?" bad":""); t.textContent=msg;
  $("#toasts").append(t);
  setTimeout(()=>{t.style.transition="opacity .3s";t.style.opacity="0";
    setTimeout(()=>t.remove(),320)}, bad?5200:2200);
}
window.studioError=m=>{$("#pane-page").innerHTML=
  '<div class="err"><h4>Could not start</h4><p>'+esc(m)+'</p></div>'};

const api=async(u,o)=>{
  o=o||{};
  if(API_TOKEN){ o.headers=Object.assign({},o.headers,{"X-API-Key":API_TOKEN}) }
  const r=await fetch(u,o);
  const j=await r.json().catch(()=>({error:"The renderer sent an unreadable response."}));
  if(j&&j.error&&!("ok"in j)) throw new Error(j.error);
  return j;
};
const post=(u,body)=>api(u,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(body)});

/* ---- small shared formatters ---- */
const MONTHS=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function shortDate(iso){
  if(!iso) return "";
  const d=new Date(String(iso).slice(0,19));
  if(isNaN(d)) return String(iso).slice(0,10);
  return d.getDate()+" "+MONTHS[d.getMonth()]+
    (d.getFullYear()!==new Date().getFullYear()?" "+String(d.getFullYear()).slice(2):"");
}
const prettyStatus=s=>{
  const map={pending:"Draft", applied:"Awaiting reply", interviewing:"Interviewing",
    offer:"Offer", accepted:"Accepted", refused:"Declined", rejected:"Rejected",
    ghosted:"Ghosted", rejected_interviewing:"Rejected after interview",
    ghosted_interviewing:"Ghosted after interview"};
  return map[s]||String(s).replace(/_/g," ");
};
/* Live means an application can still turn into a job; dead means it cannot. */
const LIVE_STATUS=new Set(["interviewing","offer","accepted"]);
const DEAD_STATUS=new Set(["pending","refused","rejected","ghosted",
  "rejected_interviewing","ghosted_interviewing"]);
const statusTone=s=>LIVE_STATUS.has(s)?"live":DEAD_STATUS.has(s)?"dead":"";
const money=j=>{
  const v=j.salary_offered||j.salary_expected;
  if(!v) return null;
  const sym={EUR:"€",GBP:"£",USD:"$"}[j.salary_currency]||"";
  return sym+Number(v).toLocaleString("en-GB")+(sym?"":" "+(j.salary_currency||""));
};
const appliedAt=j=>{
  const h=j.status_history||[];
  for(const e of h) if(e.status==="applied") return e.at;
  return j.status==="pending"?null:j.created_at;
};
const isoToday=()=>new Date().toISOString().slice(0,10);

/* ---- view switching ---------------------------------------------------- */
function setView(v){
  S.view=v;
  $$("#nav button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.view===v)));
  ["cvs","jobs","funnel"].forEach(k=>{ $("#v-"+k).hidden = k!==v });
  $("#doctitle").hidden = v!=="cvs";
  $("#chrome-gap").hidden = v==="cvs";
  $("#search").hidden = v!=="jobs";
  $("#range").hidden = v!=="funnel";
  $("#btn-design").hidden = v!=="cvs";
  $("#btn-pdf").hidden = v!=="cvs";
  $("#btn-render").hidden = v!=="cvs";
  $("#btn-newjob").hidden = v!=="jobs";
  if(v==="jobs") loadJobs();
  if(v==="funnel") loadFunnel();
  paintStatus();
}
$$("#nav button").forEach(b=>b.onclick=()=>setView(b.dataset.view));

/* ---- status bar --------------------------------------------------------- */
function paintStatus(){
  const L=$("#st-left"), R=$("#st-right");
  if(S.view==="cvs"){
    const bits=[];
    if(S.renderMs!=null) bits.push("rendered "+(S.renderMs/1000).toFixed(2)+"s");
    if(S.live==="working") bits.push("rendering…");
    else if(S.live==="bad") bits.push(S.liveMsg||"not valid yet");
    if(S.dirty) bits.push("unsaved changes");
    else if(S.savedAt) bits.push("saved "+ago(S.savedAt)+" ago");
    L.className=S.live==="bad"||S.dirty?"warn":"";
    L.textContent=bits.join(" · ")||"ready";
    L.classList.add("mono");
    R.textContent=(S.state&&S.state.workspace)||"";
  }else if(S.view==="jobs"){
    L.className="mono";
    L.textContent=S.jobs.length+" job"+(S.jobs.length===1?"":"s")+
      (S.jsel?" · 1 selected":"");
    R.textContent="applications.db";
  }else{
    L.className="mono";
    L.textContent="click a band to filter the Jobs list";
    R.textContent="";
  }
}
function ago(t){
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<60) return s+"s";
  if(s<3600) return Math.round(s/60)+"m";
  return Math.round(s/3600)+"h";
}
setInterval(()=>{ if(S.view==="cvs"&&!S.dirty&&S.savedAt) paintStatus() },10000);

/* ---- window chrome ------------------------------------------------------
   The page is served from the local server, so the Tauri API is only there
   when running inside the app. In a plain browser the traffic lights would be
   decoration that does nothing, so they stay hidden. */
(function(){
  const T=window.__TAURI__;
  if(!T||!T.window) return;
  const win=T.window.getCurrentWindow();
  $("#lights").hidden=false;
  $("#w-min").onclick=()=>win.minimize();
  $("#w-max").onclick=()=>win.toggleMaximize();
  $("#w-close").onclick=()=>win.close();
  $("#chrome").addEventListener("dblclick",e=>{
    if(e.target.closest("button,select,input,label")) return;
    win.toggleMaximize();
  });
})();

/* ---- overlays and sheets ------------------------------------------------ */
function closeOverlays(){
  $("#ovl-design").hidden=true; $("#ovl-settings").hidden=true;
}
$$("[data-close-ovl]").forEach(b=>b.onclick=closeOverlays);

let sheetOnClose=null;
function openSheet(html,onClose){
  $("#sheet").innerHTML=html;
  $("#sheet").hidden=false; $("#scrim").hidden=false;
  sheetOnClose=onClose||null;
  const first=$("#sheet input,#sheet select,#sheet button");
  if(first) first.focus();
}
function closeSheet(){
  $("#sheet").hidden=true; $("#scrim").hidden=true; $("#sheet").innerHTML="";
  if(sheetOnClose){ const f=sheetOnClose; sheetOnClose=null; f() }
}
$("#scrim").onclick=closeSheet;
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"){
    if(!$("#sheet").hidden) return closeSheet();
    if(!$("#ovl-design").hidden||!$("#ovl-settings").hidden) return closeOverlays();
  }
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){ e.preventDefault(); save() }
});
window.addEventListener("beforeunload",e=>{if(S.dirty){e.preventDefault();e.returnValue=""}});

/* ---- boot --------------------------------------------------------------- */
async function boot(){
  let d;
  try{ d=await api("/api/state") }catch(e){ return window.studioError(e.message) }
  S.state=d;
  renderDocs(d.documents);
  loadJobs(true);
  if(d.documents.length) openDoc(d.documents[0].path);
  else{
    $("#pane-page").innerHTML='<div class="empty"><h3>No CVs yet</h3>'+
      '<p>Create one to get started. It is saved as a plain YAML file in your '+
      'workspace, so you always own it. No database, nothing locked in.</p>'+
      '<div class="cta"><button class="sbtn primary" id="firstcta">Create a CV</button></div></div>';
    $("#firstcta").onclick=()=>newDocumentSheet();
    $("#btn-render").disabled=true;
  }
  paintStatus();
  if(d.first_run) toast("Workspace created at "+d.workspace);
}

/* =========================================================================
   Editor
   ========================================================================= */

/* The form, the inspector and the live preview all read and write one working
   copy of the document rather than each other's DOM. That is what makes the
   inspector and the Form tab edit the same field without fighting: whichever
   is on screen renders from the model, and both write back into it. */
const getAt=(o,p)=>p.reduce((x,k)=>(x==null?undefined:x[k]),o);
function setAt(o,p,v){
  let n=o;
  for(let i=0;i<p.length-1;i++){ if(n==null) return; n=n[p[i]] }
  if(n!=null) n[p[p.length-1]]=v;
}
const HEADER_KEYS=["name","headline","location","email","phone","website"];

/* Every leaf the form exposes, in one place, so a save writes exactly the
   fields the user could have edited -- no more, no less. Arrays of scalars
   count as one leaf, which is what lets a bullet be added or removed. */
function leafPaths(){
  const cv=S.data&&S.data.cv; if(!cv) return [];
  const out=[];
  HEADER_KEYS.forEach(k=>{
    if(k in cv||["name","headline","location","email"].includes(k)) out.push(["cv",k]);
  });
  const sections=cv.sections||{};
  for(const name of Object.keys(sections)){
    (sections[name]||[]).forEach((it,i)=>{
      if(it===null||typeof it!=="object") out.push(["cv","sections",name,i]);
      else for(const k of Object.keys(it)) out.push(["cv","sections",name,i,k]);
    });
  }
  return out;
}
const sectionLabel=n=>String(n).replace(/_/g," ").replace(/^./,c=>c.toUpperCase());
function entryTitle(it,i){
  if(it===null||typeof it!=="object")
    return String(it||"").split(/\s+/).slice(0,4).join(" ")||("item "+(i+1));
  return it.company||it.institution||it.name||it.label||it.position||("entry "+(i+1));
}
function wordsIn(v){
  if(v==null) return 0;
  if(Array.isArray(v)) return v.reduce((a,x)=>a+wordsIn(x),0);
  if(typeof v==="object") return Object.values(v).reduce((a,x)=>a+wordsIn(x),0);
  return String(v).trim()?String(v).trim().split(/\s+/).length:0;
}

/* ---- documents ---------------------------------------------------------- */
function renderDocs(docs){
  S.state.documents=docs;
  if(!docs.length){
    $("#doclist").innerHTML='<p style="color:var(--c300);font-size:12.5px;padding:6px 8px">'+
      'Nothing here yet.</p>';
    return;
  }
  $("#doclist").innerHTML=docs.map(d=>{
    const label=d.group==="Cover letters"?"Letter — "+d.label:d.label;
    const pp=S.pages[d.path];
    return '<button class="row'+(d.path===S.path?" sel":"")+'" data-path="'+esc(d.path)+'"'+
      ' title="'+esc(d.path)+'"><span class="mark"></span>'+
      '<span class="lbl">'+esc(label)+'</span>'+
      '<span class="ct mono">'+(pp?pp+"pp":"")+'</span></button>';
  }).join("")+
  '<button class="row" id="doc-new"><span class="mark"></span>'+
  '<span class="lbl" style="color:var(--c200)">+ New document…</span></button>';
  $$("#doclist [data-path]").forEach(b=>b.onclick=()=>{
    if(b.dataset.path===S.path) return;
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    openDoc(b.dataset.path);
  });
  $("#doc-new").onclick=()=>newDocumentSheet();
}

async function openDoc(path){
  closeOverlays();
  setView("cvs");
  S.path=path; S.dirty=false; S.savedAt=null; S.sel=null; S.openSection=null;
  S.render=null; S.renderMs=null; S.fill=null; S.themePages={}; S.zoomAuto=true;
  /* The Design panel still holds the last document's controls, and its inputs
     are read straight into the patch list. Empty it until it is rebuilt. */
  $("#dz-basics").innerHTML=""; $("#dz-advanced").innerHTML="";
  $("#pane-page").innerHTML='<div class="skel" style="width:472px;height:668px"></div>';
  $("#btn-render").disabled=false;
  renderDocs(S.state.documents);
  try{
    const doc=await api("/api/doc?path="+encodeURIComponent(path));
    S.doc=doc;
    S.data=doc.data?JSON.parse(JSON.stringify(doc.data)):null;
    $("#yaml").value=doc.yaml; paint();
    const dz=(doc.data&&doc.data.design)||{};
    DZ.theme=dz.theme||null;
    DZ.page=(dz.page&&dz.page.size)||null;
    DZ.family=(dz.typography&&dz.typography.font_family&&dz.typography.font_family.body)||
              dz.font_family||(dz.text&&dz.text.font_family)||null;
    DZ.size=null;
    setYamlError(doc.parse_error);
    paintTitle();
    buildOutline();
    selectDefault();
    buildForm();
    doRender();
  }catch(e){ toast(e.message,true) }
}

function paintTitle(){
  const cv=(S.data&&S.data.cv)||{};
  const doc=(S.state.documents||[]).find(d=>d.path===S.path);
  const link=linkedJob();
  const name=link?(link.title+" — "+link.company)
                 :(cv.headline?cv.headline+(cv.name?" — "+cv.name:""):(cv.name||(doc&&doc.label)||""));
  $("#doctitle .t").textContent=name||"";
  $("#doctitle .f").textContent=S.path?S.path.split("/").pop():"";
}

/* ---- outline ------------------------------------------------------------ */
function buildOutline(){
  const cv=S.data&&S.data.cv;
  const host=$("#outline");
  if(!cv){ host.innerHTML=''; $("#outline-label").hidden=true; return }
  $("#outline-label").hidden=false;
  const sections=cv.sections||{};
  let h='<button class="orow'+(S.sel&&S.sel.kind==="header"?" sel":"")+
        '" data-o="header"><span>Header</span></button>';
  for(const name of Object.keys(sections)){
    const list=sections[name]||[];
    const on=S.openSection===name;
    h+='<button class="orow'+(on?" sel":"")+'" data-o="section" data-name="'+esc(name)+'">'+
       '<span>'+esc(sectionLabel(name))+'</span>'+
       '<span class="ct mono">'+list.length+'</span></button>';
    if(on&&list.length){
      h+='<div class="okids">'+list.map((it,i)=>
        '<button class="okid'+(S.sel&&S.sel.kind==="entry"&&S.sel.name===name&&S.sel.i===i
          ?" sel":"")+'" data-o="entry" data-name="'+esc(name)+'" data-i="'+i+'">'+
        esc(entryTitle(it,i))+'</button>').join("")+'</div>';
    }
  }
  host.innerHTML=h;
  host.querySelectorAll("[data-o]").forEach(b=>b.onclick=()=>{
    const k=b.dataset.o;
    if(k==="header") select({kind:"header"});
    else if(k==="section") select({kind:"section", name:b.dataset.name});
    else select({kind:"entry", name:b.dataset.name, i:+b.dataset.i});
  });
}

function selectDefault(){
  const cv=S.data&&S.data.cv;
  if(!cv) return select(null);
  const first=Object.keys(cv.sections||{})[0];
  if(first) select({kind:"section", name:first}); else select({kind:"header"});
}

/* Selecting anywhere -- outline, inspector, or the Form tab -- moves the same
   selection, so the three surfaces always agree on what is being edited. */
function select(sel){
  if(sel&&sel.kind==="section"){
    const list=((S.data.cv.sections||{})[sel.name])||[];
    S.openSection=sel.name;
    sel=list.length?{kind:"entry",name:sel.name,i:0}:{kind:"section",name:sel.name};
  }else if(sel&&sel.kind==="entry"){ S.openSection=sel.name }
  S.sel=sel;
  buildOutline();
  buildInspector();
}

/* ---- shared field markup ------------------------------------------------ */
function inputFor(path,value,opts){
  opts=opts||{};
  const p=esc(JSON.stringify(path));
  const mono=opts.mono?" mono":"";
  if(Array.isArray(value))
    return '<textarea data-p='+"'"+p+"'"+' data-kind="lines" rows="3">'+
      esc(value.join("\n"))+'</textarea>';
  if(opts.multi)
    return '<textarea data-p='+"'"+p+"'"+' rows="3" class="'+mono.trim()+'">'+
      esc(value==null?"":value)+'</textarea>';
  return '<input data-p='+"'"+p+"'"+' class="'+mono.trim()+'" value="'+
    esc(value==null?"":value)+'">';
}
function fieldRow(label,path,value,opts){
  return '<label title="'+esc(label)+'">'+esc(String(label).replace(/_/g," "))+'</label>'+
    inputFor(path,value,opts);
}
const MONO_KEYS=/^(start_date|end_date|date|phone|url|website|doi)$/;

/* One handler for every bound control on the page: write into the working
   copy, mark dirty, and let the debounce decide when to re-render. */
function bindFields(root){
  root.addEventListener("input",e=>{
    const el=e.target;
    if(el.dataset.p!==undefined){
      const path=JSON.parse(el.dataset.p);
      let v=el.value;
      if(el.dataset.kind==="lines") v=v.split("\n").map(x=>x.trim()).filter(Boolean);
      else{
        const was=getAt(S.doc.data,path);
        if(typeof was==="number"&&v.trim()!==""&&!isNaN(v)) v=Number(v);
      }
      setAt(S.data,path,v);
      touch();
    }else if(el.closest("[data-arr]")){
      const card=el.closest("[data-arr]");
      setAt(S.data,JSON.parse(card.dataset.arr),
        [...card.querySelectorAll("textarea")].map(t=>t.value));
      autoGrow(el); touch();
    }
  });
}
function autoGrow(el){ el.style.height="0"; el.style.height=el.scrollHeight+"px" }
const touch=()=>{ S.dirty=true; paintStatus(); scheduleLive() };

/* ---- inspector ----------------------------------------------------------- */
function buildInspector(){
  const head=$("#insp-title"), meta=$("#insp-meta"), body=$("#insp-body");
  const cv=S.data&&S.data.cv;
  if(!cv){
    head.textContent="No form"; meta.textContent="";
    body.innerHTML='<p class="note">This file has a YAML error, so it cannot be parsed '+
      'into fields. Switch to the YAML tab to fix it.</p>';
    return;
  }
  const sel=S.sel;
  if(!sel){ head.textContent="Nothing selected"; meta.textContent=""; body.innerHTML=""; return }

  if(sel.kind==="header"){
    head.textContent="Header"; meta.textContent=wordsIn(
      Object.fromEntries(HEADER_KEYS.map(k=>[k,cv[k]])))+" words";
    body.innerHTML='<div class="fg">'+HEADER_KEYS.map(k=>
      fieldRow(k,["cv",k],cv[k],{mono:MONO_KEYS.test(k)})).join("")+'</div>'+linkedBlock();
    wireInspector(); return;
  }
  const list=(cv.sections||{})[sel.name]||[];
  if(sel.kind==="section"||!list.length){
    head.textContent=sectionLabel(sel.name);
    meta.textContent=list.length+" item"+(list.length===1?"":"s");
    body.innerHTML='<p class="note muted">This section is empty. Add entries in the '+
      'YAML tab.</p>'+linkedBlock();
    wireInspector(); return;
  }
  const it=list[sel.i];
  head.textContent=sectionLabel(sel.name)+" — "+entryTitle(it,sel.i);
  meta.textContent=wordsIn(it)+" words";

  if(it===null||typeof it!=="object"){
    body.innerHTML='<div class="block"><span class="blabel mono">Text</span>'+
      inputFor(["cv","sections",sel.name,sel.i],it,{multi:true})+'</div>'+linkedBlock();
    wireInspector(); return;
  }
  const scalars=Object.keys(it).filter(k=>!Array.isArray(it[k]));
  const arrays=Object.keys(it).filter(k=>Array.isArray(it[k]));
  let h='';
  if(scalars.length) h+='<div class="fg">'+scalars.map(k=>
    fieldRow(k,["cv","sections",sel.name,sel.i,k],it[k],{mono:MONO_KEYS.test(k)})).join("")+'</div>';
  arrays.forEach(k=>{ h+=arrayBlock(k,["cv","sections",sel.name,sel.i,k],it[k]) });
  body.innerHTML=h+linkedBlock();
  wireInspector();
}

/* Bullets are a card of rows rather than one blob of text: the row you are
   editing is the one highlighted, and it can be added to or taken away. */
function arrayBlock(label,path,list){
  const p=esc(JSON.stringify(path));
  return '<div class="block"><span class="blabel mono">'+esc(label.replace(/_/g," "))+'</span>'+
    '<div class="card" data-arr='+"'"+p+"'"+'>'+
    (list.length?list.map((x,i)=>
      '<div class="crow" data-i="'+i+'"><span class="cidx mono">'+(i+1)+'</span>'+
      '<textarea rows="1">'+esc(x==null?"":x)+'</textarea></div>').join("")
      :'<div class="crow"><span class="cidx mono">1</span><textarea rows="1"></textarea></div>')+
    '</div><div style="display:flex;gap:6px">'+
    '<button class="mini" data-arr-add title="Add">+</button>'+
    '<button class="mini" data-arr-del title="Remove the selected one">−</button></div></div>';
}
function wireInspector(){
  const body=$("#insp-body");
  body.querySelectorAll(".crow textarea").forEach(t=>{
    autoGrow(t);
    t.onfocus=()=>{
      body.querySelectorAll(".crow").forEach(r=>r.classList.remove("on"));
      t.closest(".crow").classList.add("on");
    };
  });
  body.querySelectorAll(".block").forEach(block=>{
    const card=block.querySelector("[data-arr]");
    if(!card) return;
    const path=JSON.parse(card.dataset.arr);
    const add=block.querySelector("[data-arr-add]"), del=block.querySelector("[data-arr-del]");
    if(add) add.onclick=()=>{
      setAt(S.data,path,(getAt(S.data,path)||[]).concat([""]));
      touch(); buildInspector();
      const rows=$("#insp-body").querySelectorAll("[data-arr] textarea");
      if(rows.length) rows[rows.length-1].focus();
    };
    if(del) del.onclick=()=>{
      const arr=(getAt(S.data,path)||[]).slice();
      if(arr.length<2) return toast("Keep at least one line, or clear its text.");
      const on=card.querySelector(".crow.on");
      arr.splice(on?+on.dataset.i:arr.length-1,1);
      setAt(S.data,path,arr); touch(); buildInspector();
    };
  });
  const show=body.querySelector("[data-show-job]");
  if(show) show.onclick=()=>{ selectJob(show.dataset.showJob); setView("jobs") };
  const link=body.querySelector("[data-link-job]");
  if(link) link.onclick=()=>newJobSheet({cv_path:S.path});
}

/* The application this document was written for. Knowing it here is what
   makes "Show in Jobs" possible without hunting through the table. */
function linkedJob(){
  return S.jobs.find(j=>j.cv_path===S.path||j.letter_path===S.path)||null;
}
function linkedBlock(){
  const j=linkedJob();
  let inner;
  if(j){
    inner='<div class="card"><div class="drow">'+
      '<span style="display:flex;align-items:center;gap:8px;min-width:0">'+
      '<span class="dot '+statusTone(j.status)+'"></span>'+
      '<span style="overflow:hidden;text-overflow:ellipsis">'+esc(j.company)+' — '+
      esc(prettyStatus(j.status))+'</span></span>'+
      '<button class="alink" data-show-job="'+esc(j.id)+'">Show in Jobs</button>'+
      '</div></div>';
  }else if(S.jready){
    inner='<div class="card"><div class="drow"><span class="muted">Not linked to an '+
      'application</span><button class="alink" data-link-job>Add one</button></div></div>';
  }else inner='';
  return inner?'<div class="block ruled"><span class="blabel mono">Linked application</span>'+
    inner+'</div>':'';
}

/* ---- the Form tab: the same fields, whole document at once ---------------- */
function buildForm(){
  const cv=S.data&&S.data.cv;
  if(!cv){ $("#pane-form").innerHTML='<div class="empty"><h3>Can\'t show a form</h3>'+
    '<p>This file has a YAML error, so it cannot be parsed into fields. Switch to the '+
    'YAML tab to fix it.</p></div>'; return }
  const chev='<svg class="chev" width="11" height="11" viewBox="0 0 24 24" fill="none" '+
    'stroke="currentColor" stroke-width="3"><path d="M9 18l6-6-6-6"/></svg>';
  let h='<details class="grp" open><summary>'+chev+'Header</summary><div class="body">'+
    '<div class="fg wide">'+HEADER_KEYS.filter(k=>k in cv||
      ["name","headline","location","email"].includes(k)).map(k=>
      fieldRow(k,["cv",k],cv[k],{mono:MONO_KEYS.test(k)})).join("")+'</div></div></details>';
  const sections=cv.sections||{};
  for(const name of Object.keys(sections)){
    const list=sections[name]||[];
    h+='<details class="grp" open><summary>'+chev+esc(sectionLabel(name))+
      '<span class="count">'+list.length+' item'+(list.length===1?"":"s")+
      '</span></summary><div class="body">';
    list.forEach((it,i)=>{
      if(it===null||typeof it!=="object"){
        h+='<div class="fg wide">'+fieldRow("text "+(i+1),["cv","sections",name,i],it,
          {multi:true})+'</div>';
      }else{
        h+='<div class="entry"><div class="entry-hd"><b>'+esc(entryTitle(it,i))+'</b>'+
          '<button data-focus="'+esc(name)+'" data-i="'+i+'">Inspect</button></div>'+
          '<div class="fg wide">'+Object.keys(it).map(k=>
            fieldRow(k,["cv","sections",name,i,k],it[k],{mono:MONO_KEYS.test(k)})).join("")+
          '</div></div>';
      }
    });
    h+='</div></details>';
  }
  $("#pane-form").innerHTML=h;
  $$("#pane-form [data-focus]").forEach(b=>b.onclick=()=>
    select({kind:"entry",name:b.dataset.focus,i:+b.dataset.i}));
}
bindFields($("#pane-form"));
bindFields($("#insp-body"));

/* ---- tabs, zoom and paging ------------------------------------------------ */
$$("#edtabs button").forEach(b=>b.onclick=()=>{
  S.tab=b.dataset.tab;
  $$("#edtabs button").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
  $("#pane-page").hidden=S.tab!=="page";
  $("#pane-form").hidden=S.tab!=="form";
  $("#pane-yaml").hidden=S.tab!=="yaml";
  $("#pmeta").style.visibility=S.tab==="page"?"":"hidden";
  if(S.tab==="yaml") paint();
  if(S.tab==="form") buildForm();   /* re-read the model, in case the inspector moved on */
});
$("#z-in").onclick=()=>setZoom(S.zoom+.12);
$("#z-out").onclick=()=>setZoom(S.zoom-.12);
$("#pg-prev").onclick=()=>setPage(S.page-1);
$("#pg-next").onclick=()=>setPage(S.page+1);
function setZoom(z){ S.zoom=Math.min(3,Math.max(.35,z)); S.zoomAuto=false; paintPage() }
function setPage(i){
  const n=(S.render&&S.render.pngs.length)||0;
  S.page=Math.min(Math.max(0,i),Math.max(0,n-1)); paintPage();
}

/* ---- rendering ------------------------------------------------------------ */
async function save(){
  if(!S.path||S.busy) return;
  S.busy=true; $("#btn-render").disabled=true;
  try{
    const body=S.tab==="yaml" ? {path:S.path,yaml:$("#yaml").value}
                              : {path:S.path,patches:collectPatches()};
    const r=await post("/api/save",body);
    S.doc=r; S.data=r.data?JSON.parse(JSON.stringify(r.data)):null;
    S.dirty=false; S.savedAt=Date.now();
    $("#yaml").value=r.yaml; paint(); setYamlError(r.parse_error);
    buildOutline(); buildInspector(); if(S.tab==="form") buildForm();
    await doRender();
  }catch(e){ toast(e.message,true) }
  finally{ S.busy=false; $("#btn-render").disabled=false; paintStatus() }
}
$("#btn-render").onclick=save;

function collectPatches(){
  const out=leafPaths().map(p=>({path:p,value:getAt(S.data,p)}));
  return out.concat(designPatches());
}

async function doRender(){
  if(!S.path) return;
  const t0=performance.now();
  $("#pane-page").innerHTML='<div class="skel" style="width:472px;height:668px"></div>';
  try{
    const r=await post("/api/render",{path:S.path});
    S.renderMs=Math.round(performance.now()-t0);
    if(!r.ok){
      S.pdf=null; $("#btn-pdf").disabled=true;
      $("#pane-page").innerHTML='<div class="err"><h4>This CV didn\'t render</h4>'+
        (r.hint?'<div class="hint">'+esc(r.hint)+'</div>':"")+
        '<pre>'+esc(r.error||"")+'</pre></div>';
      S.render=null; paintBudget(); paintStatus();
      return;
    }
    await adoptRender(r);
  }catch(e){
    S.render=null;
    $("#pane-page").innerHTML='<div class="err"><h4>Render failed</h4><pre>'+
      esc(e.message)+'</pre></div>';
  }
  paintStatus();
}

/* Every good render updates the same three things: the pages on screen, the
   page budget, and what we know about this document and this theme. */
async function adoptRender(r){
  S.render=r; S.pdf=r.pdf; $("#btn-pdf").disabled=!r.pdf;
  const grew=S.pages[S.path]!==r.pages;
  S.pages[S.path]=r.pages;
  if(DZ.theme) S.themePages[DZ.theme]=r.pages;
  if(S.page>=r.pngs.length) S.page=Math.max(0,r.pngs.length-1);
  paintPage();
  if(grew) renderDocs(S.state.documents);
  S.fill=r.pngs.length?await measureFill(r.pngs[r.pngs.length-1]+tok()):null;
  paintBudget();
  if(!$("#ovl-design").hidden){ paintThemes(); paintEffect() }
}

function paintPage(){
  const host=$("#pane-page"), r=S.render;
  if(!r||!r.pngs.length){ $("#pg-idx").textContent="—"; return }
  const url=r.pngs[S.page]+tok();
  if(S.zoomAuto){
    /* Fit the page to the column the first time, which is what "96%" in the
       design is: a whole page, as large as it goes. */
    const h=host.clientHeight-44;
    if(h>120) S.zoom=Math.max(.35,Math.min(2,h/668));
  }
  const w=Math.round(472*S.zoom);
  host.innerHTML='<img class="pg" src="'+url+'" alt="Page '+(S.page+1)+'" style="width:'+
    w+'px;height:auto">';
  $("#pg-idx").textContent=(S.page+1)+" / "+r.pngs.length;
  $("#z-lvl").textContent=Math.round(S.zoom*100)+"%";
  $("#pg-prev").disabled=S.page===0;
  $("#pg-next").disabled=S.page>=r.pngs.length-1;
}

/* How full the last page is, measured off the rendered image rather than
   guessed from a word count. The top margin tells us where the bottom margin
   is, which keeps page numbering in the footer from reading as content. */
function measureFill(url){
  return new Promise(res=>{
    const img=new Image();
    img.onload=()=>{
      try{
        const w=Math.min(200,img.naturalWidth);
        const h=Math.max(1,Math.round(img.naturalHeight*w/img.naturalWidth));
        const c=document.createElement("canvas"); c.width=w; c.height=h;
        const x=c.getContext("2d",{willReadFrequently:true});
        x.fillStyle="#fff"; x.fillRect(0,0,w,h);
        x.drawImage(img,0,0,w,h);
        const d=x.getImageData(0,0,w,h).data;
        const inked=[];
        for(let y=0;y<h;y++){
          for(let px=0;px<w;px++){
            const i=(y*w+px)*4;
            if(d[i]<212||d[i+1]<212||d[i+2]<212){ inked.push(y); break }
          }
        }
        if(!inked.length) return res(0);
        const top=inked[0];
        const limit=h-top;                       /* the matching bottom margin */
        let bottom=top;
        for(const y of inked){ if(y<=limit) bottom=y }
        const usable=Math.max(1,h-2*top);
        res(Math.max(0,Math.min(1,(bottom-top)/usable)));
      }catch(err){ res(null) }
    };
    img.onerror=()=>res(null);
    img.src=url;
  });
}

function paintBudget(){
  const b=$("#budget"), r=S.render;
  if(!r){ b.hidden=true; return }
  b.hidden=false;
  b.querySelector(".pp").textContent=r.pages+" page"+(r.pages===1?"":"s");
  b.querySelector(".ww").textContent=(r.ats_words||0)+" words";
  const pct=S.fill==null?null:Math.round(S.fill*100);
  const on=pct==null?0:Math.round(S.fill*6);
  [...b.querySelectorAll(".bar i")].forEach((el,i)=>el.classList.toggle("on",i<on));
  b.querySelector(".bar").style.visibility=pct==null?"hidden":"";
  b.querySelector(".cap").textContent=fillCaption(r.pages,pct);
}
/* The sentence changes with the number but never claims more than the
   measurement supports. */
function fillCaption(pages,pct){
  if(pct==null) return "Page fill could not be measured.";
  const p="Page "+pages+" is "+pct+"% full";
  if(pct>=96) return p+" — no room left on it.";
  if(pages>1&&pct<=35) return p+" — most of the last page is empty.";
  return p+".";
}

/* ---- live preview ---------------------------------------------------------
   A debounce so a render starts only once typing pauses, and a token so a slow
   render finishing after a newer one cannot overwrite the fresher result.
   Errors while mid-edit leave the last good page on screen rather than
   flashing a red panel at every keystroke. */
let liveTimer=null, liveToken=0;
function scheduleLive(){
  if(prefs().live===false||!S.path) return;
  clearTimeout(liveTimer);
  liveTimer=setTimeout(runLive, prefs().delay||700);
}
async function runLive(){
  if(!S.path||prefs().live===false) return;
  const token=++liveToken;
  S.live="working"; paintStatus();
  const body=S.tab==="yaml" ? {path:S.path,yaml:$("#yaml").value}
                            : {path:S.path,patches:collectPatches()};
  const t0=performance.now();
  try{
    const r=await post("/api/preview",body);
    if(token!==liveToken) return;
    if(r.ok){
      S.renderMs=Math.round(performance.now()-t0);
      S.live="ok"; await adoptRender(r);
    }else{ S.live="bad"; S.liveMsg=r.hint||"not valid yet" }
  }catch(e){ if(token===liveToken){ S.live="bad"; S.liveMsg=e.message } }
  paintStatus();
}

$("#btn-pdf").onclick=()=>{ if(S.pdf)
  window.open("/api/asset?path="+encodeURIComponent(S.pdf)+tok()) };

/* ---- YAML: highlighting painted behind a transparent-text textarea, so
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
  if(/^[|>][-+]?\d*$/.test(t))                     inner='<span class="t-blk">'+t+'</span>';
  else if(/^".*"$/.test(t)||/^'.*'$/.test(t))      inner='<span class="t-str">'+t+'</span>';
  else if(/^(true|false|null|~|yes|no)$/i.test(t)) inner='<span class="t-bool">'+t+'</span>';
  else if(/^-?\d+(\.\d+)?$/.test(t))               inner='<span class="t-num">'+t+'</span>';
  else if(/^\d{4}-\d{2}(-\d{2})?$/.test(t))        inner='<span class="t-num">'+t+'</span>';
  else                                             inner=t;
  return lead+inner+tail;
}
function hlLine(line){
  const s=line.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  const whole=s.match(/^(\s*)(#.*)$/);
  if(whole) return whole[1]+'<span class="t-com">'+whole[2]+'</span>';
  let code=s, comment="";
  const ci=commentAt(s);
  if(ci>=0){ code=s.slice(0,ci); comment='<span class="t-com">'+s.slice(ci)+'</span>' }
  const m=code.match(/^(\s*)((?:-\s+)?)(.*)$/);
  let out=m[1]+(m[2]?'<span class="t-punc">'+m[2]+'</span>':"");
  const kv=m[3].match(/^([^:\s][^:]*?)(:)(\s*)(.*)$/);
  out += kv ? '<span class="t-key">'+kv[1]+'</span><span class="t-punc">:</span>'+kv[3]+
              hlScalar(kv[4])
            : hlScalar(m[3]);
  return out+comment;
}
function paint(){
  const ta=$("#yaml");
  /* trailing spacer keeps both layers the same height so the caret stays put */
  $("#hl").innerHTML=ta.value.split("\n").map(hlLine).join("\n")+"\n ";
  $("#hl").scrollTop=ta.scrollTop; $("#hl").scrollLeft=ta.scrollLeft;
}
$("#yaml").addEventListener("input",()=>{ paint(); touch() });
$("#yaml").addEventListener("scroll",()=>{
  $("#hl").scrollTop=$("#yaml").scrollTop; $("#hl").scrollLeft=$("#yaml").scrollLeft });

/* A parse error names a line; saying which one, and being able to jump to it,
   is most of the fix. The preview keeps the last good page meanwhile. */
function setYamlError(err){
  const box=$("#yamlerr");
  if(!err){ box.hidden=true; return }
  const line=(/line (\d+)/i.exec(err)||[])[1];
  box.hidden=false;
  box.querySelector("span").textContent=
    (line?"Line "+line+": ":"")+String(err).split("\n")[0].slice(0,180);
  const go=box.querySelector("button");
  go.hidden=!line;
  go.onclick=()=>{
    const ta=$("#yaml"), lines=ta.value.split("\n");
    const at=lines.slice(0,Math.max(0,+line-1)).join("\n").length+(line>1?1:0);
    ta.focus(); ta.setSelectionRange(at,at+(lines[+line-1]||"").length);
    ta.scrollTop=Math.max(0,(+line-4)*21);
  };
  if(S.tab!=="yaml") toast("This file has a YAML error. Open the YAML tab to fix it.",true);
}

/* =========================================================================
   Jobs
   ========================================================================= */
async function loadJobs(quiet){
  try{
    const d=await api("/api/jobs");
    S.jobs=d.jobs; S.statuses=d.statuses; S.nodes=d.nodes||{}; S.labels=d.labels||{};
    S.jready=true;
  }catch(e){
    S.jready=false;
    if(!quiet) $("#jobrows").innerHTML='<div class="empty"><h3>Could not load</h3><p>'+
      esc(e.message)+'</p></div>';
    return;
  }
  if(S.view==="jobs") drawJobs();
  if(S.view==="cvs"&&S.path){ paintTitle(); buildInspector() }
  paintStatus();
}

/* The sidebar is the filter. Statuses come from the store rather than a list
   written here, so a status added to jobs.py shows up without a UI change. */
function statusCounts(){
  const c={};
  S.jobs.forEach(j=>{ c[j.status]=(c[j.status]||0)+1 });
  return c;
}
const NEEDS_FOLLOWUP=j=>j.followup_date&&j.followup_date<=isoToday()&&!DEAD_STATUS.has(j.status);
const NO_LETTER=j=>!j.letter_path;
const SAVED={"Needs follow-up":NEEDS_FOLLOWUP,"No cover letter":NO_LETTER};

function drawRail(){
  const c=statusCounts(), f=S.jfilter;
  const row=(label,count,kind,value)=>
    '<button class="row'+(f.kind===kind&&f.value===value?" sel":"")+'" data-k="'+kind+
    '" data-v="'+esc(value)+'"><span class="lbl">'+esc(label)+'</span>'+
    (count==null?"":'<span class="ct mono">'+count+'</span>')+'</button>';
  let h=row("All",S.jobs.length,"all","");
  S.statuses.forEach(s=>{ if(c[s]) h+=row(prettyStatus(s),c[s],"status",s) });
  if(S.fnode&&S.labels[S.fnode]) h+=row(S.labels[S.fnode],null,"node",S.fnode);
  $("#statuslist").innerHTML=h;
  $("#savedlist").innerHTML=Object.keys(SAVED).map(k=>
    row(k,S.jobs.filter(SAVED[k]).length,"saved",k)).join("");
  $$("#statuslist [data-k],#savedlist [data-k]").forEach(b=>b.onclick=()=>{
    S.jfilter={kind:b.dataset.k,value:b.dataset.v};
    if(b.dataset.k!=="node") S.fnode=null;
    drawJobs();
  });
}

function visibleJobs(){
  const f=S.jfilter, q=($("#jobq").value||"").trim().toLowerCase();
  let rows=S.jobs;
  if(f.kind==="status") rows=rows.filter(j=>j.status===f.value);
  else if(f.kind==="node"){
    const want=new Set(S.nodes[f.value]||[]);
    rows=rows.filter(j=>want.has(j.status));
  }else if(f.kind==="saved"&&SAVED[f.value]) rows=rows.filter(SAVED[f.value]);
  if(q) rows=rows.filter(j=>(j.company+" "+j.title+" "+(j.notes||"")+" "+(j.source||""))
    .toLowerCase().includes(q));
  return rows;
}

function drawJobs(){
  drawRail();
  const rows=visibleJobs();
  const docName=p=>p?p.split("/").pop():null;
  $("#jobrows").innerHTML=rows.length?rows.map(j=>{
    const cv=docName(j.cv_path), letter=docName(j.letter_path);
    const docs=cv?esc(cv)+(letter?" +letter":""):null;
    const sal=money(j), ap=appliedAt(j);
    const due=j.followup_date&&j.followup_date<=isoToday();
    return '<button class="trow'+(DEAD_STATUS.has(j.status)?" dead":"")+
      (S.jsel===j.id?" sel":"")+'" data-id="'+esc(j.id)+'">'+
      '<span class="role"><b>'+esc(j.title)+'</b><i>'+esc(j.company)+'</i></span>'+
      '<span>'+(docs?'<span class="docs mono" data-open="'+esc(j.cv_path)+'">'+docs+'</span>'
                    :'<span class="muted" style="font-size:12px">no CV yet</span>')+'</span>'+
      '<span class="st"><span class="dot '+statusTone(j.status)+'"></span>'+
        esc(prettyStatus(j.status))+'</span>'+
      '<span class="money mono'+(sal?"":" none")+'">'+(sal?esc(sal):"—")+'</span>'+
      '<span class="when'+(ap?"":" none")+'">'+(ap?esc(shortDate(ap)):"—")+'</span>'+
      '<span class="when'+(j.followup_date?(due?" due":""):" none")+'">'+
        (j.followup_date?esc(shortDate(j.followup_date)):"—")+'</span>'+
      '</button>';
  }).join(""):'<div class="empty"><h3>'+
    (S.jobs.length?"Nothing matches":"No applications yet")+'</h3><p>'+
    (S.jobs.length?"Try another filter, or clear the search."
      :"Add the roles you are applying for. Once a few have moved through the stages, "+
       "the funnel will show where they actually go.")+'</p></div>';

  $$("#jobrows [data-id]").forEach(b=>b.onclick=e=>{
    if(e.target.closest("[data-open]")) return;
    selectJob(b.dataset.id);
  });
  $$("#jobrows [data-open]").forEach(a=>a.onclick=e=>{
    e.stopPropagation();
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    openDoc(a.dataset.open);
  });
  if(S.jsel&&!rows.some(j=>j.id===S.jsel)) S.jsel=null;
  drawJobInspector();
  paintStatus();
}
$("#jobq").addEventListener("input",()=>drawJobs());

function selectJob(id){
  S.jsel=id;
  const j=S.jobs.find(x=>x.id===id);
  /* A job reached from the editor or the funnel may be filtered out of the
     current view; widen the filter rather than selecting something invisible. */
  if(j&&!visibleJobs().some(x=>x.id===id)){
    S.jfilter={kind:"all",value:""}; $("#jobq").value="";
  }
  drawJobs();
  const row=$("#jobrows .trow.sel");
  if(row) row.scrollIntoView({block:"nearest"});
}

const JOB_GRID=[
  ["company","Company","text"],["title","Role","text"],["status","Status","status"],
  ["source","Source","text"],["score","Fit","fit"],
  ["salary_expected","Salary","number"],["followup_date","Follow-up","date"],
  ["location","Location","text"],["url","Link","text"],
];
function drawJobInspector(){
  const j=S.jobs.find(x=>x.id===S.jsel);
  const head=$("#jinsp-title"), body=$("#jinsp-body");
  if(!j){
    head.textContent="No application selected";
    body.innerHTML='<p class="note muted">Pick a row to edit it, or add one with '+
      '“New job…”.</p>';
    return;
  }
  head.textContent=j.company+" — "+j.title;
  head.title=j.company+" — "+j.title;

  const grid=JOB_GRID.map(([k,label,kind])=>{
    let ctl;
    if(kind==="status") ctl='<select data-j="status">'+S.statuses.map(s=>
      '<option value="'+s+'"'+(s===j.status?" selected":"")+'>'+esc(prettyStatus(s))+
      '</option>').join("")+'</select>';
    else if(kind==="fit") ctl='<div class="fit" role="group" aria-label="Fit">'+
      [1,2,3,4,5].map(n=>'<button data-fit="'+n+'"'+((j.score||0)>=n?' class="on"':"")+
      ' title="'+n+' of 5" aria-label="'+n+' of 5"></button>').join("")+'</div>';
    else ctl='<input data-j="'+k+'"'+(kind==="date"?' type="date"':"")+
      (kind==="number"?' type="number" class="mono"':"")+' value="'+
      esc(j[k]==null?"":j[k])+'">';
    return '<label>'+esc(label)+'</label>'+ctl;
  }).join("");

  const docRow=(label,key,group)=>{
    const linked=j[key];
    return '<div class="drow"><select data-j="'+key+'" class="'+(linked?"":"empty")+'">'+
      '<option value="">'+(key==="cv_path"?"no CV yet":"no cover letter")+'</option>'+
      ((S.state&&S.state.documents||[]).filter(d=>d.group===group).map(d=>
        '<option value="'+esc(d.path)+'"'+(d.path===linked?" selected":"")+'>'+
        esc(d.label)+'</option>').join(""))+'</select>'+
      (linked?'<button class="alink" data-open-doc="'+esc(linked)+'">Open</button>':"")+
      '</div>';
  };
  const posting=j.url?'<div class="drow"><span class="muted">'+
    esc(j.url.replace(/^https?:\/\//,"").slice(0,40))+'</span>'+
    '<a class="alink" href="'+esc(j.url)+'" target="_blank" rel="noreferrer">Open</a></div>':"";

  const hist=(j.status_history||[]);
  const timeline=hist.length?'<div class="tl">'+hist.map(h=>
    '<div class="tli"><div class="spine"><i></i><u></u></div>'+
    '<div class="ev"><span>'+esc(prettyStatus(h.status))+'</span>'+
    '<span class="when mono">'+esc(shortDate(h.at))+'</span></div></div>').join("")+'</div>'
    :'<p class="note muted">No history yet.</p>';

  body.innerHTML=
    '<div class="fg" style="grid-template-columns:64px 1fr">'+grid+'</div>'+
    '<div class="block"><span class="blabel mono">Documents</span><div class="card">'+
      docRow("CV","cv_path","My CVs")+docRow("Cover letter","letter_path","Cover letters")+
      posting+'</div></div>'+
    '<div class="block"><span class="blabel mono">History</span>'+timeline+'</div>'+
    '<div class="block"><span class="blabel mono">Notes</span>'+
      '<textarea data-j="notes" rows="3" style="min-height:54px">'+esc(j.notes||"")+
      '</textarea></div>'+
    '<div class="block ruled"><button class="sbtn danger" id="job-del">Delete this '+
      'application</button></div>';

  body.querySelectorAll("[data-j]").forEach(el=>{
    el.onchange=()=>{
      let v=el.value;
      if(el.type==="number") v=v===""?null:Number(v);
      saveJob(j.id,{[el.dataset.j]:v===""?null:v});
    };
  });
  body.querySelectorAll("[data-fit]").forEach(b=>b.onclick=()=>{
    const n=+b.dataset.fit;
    saveJob(j.id,{score:j.score===n?null:n});
  });
  body.querySelectorAll("[data-open-doc]").forEach(b=>b.onclick=()=>{
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    openDoc(b.dataset.openDoc);
  });
  $("#job-del").onclick=async()=>{
    if(!confirm("Delete this application? This cannot be undone.")) return;
    try{
      await post("/api/jobs/delete",{id:j.id});
      S.jsel=null; await loadJobs(); toast("Deleted");
    }catch(e){ toast(e.message,true) }
  };
}

async function saveJob(id,patch){
  try{
    const updated=await post("/api/jobs/update",Object.assign({id},patch));
    const i=S.jobs.findIndex(x=>x.id===id);
    if(i>=0) S.jobs[i]=updated;
    /* Sorting is by updated_at, so an edit moves the row; redraw the whole
       table rather than leaving a stale order behind. */
    S.jobs.sort((a,b)=>String(b.updated_at).localeCompare(String(a.updated_at)));
    drawJobs();
    if(S.view==="cvs") buildInspector();
    S.funnel=null;
  }catch(e){ toast(e.message,true) }
}

/* ---- new job sheet -------------------------------------------------------- */
function newJobSheet(seed){
  seed=seed||{};
  const docs=g=>(S.state&&S.state.documents||[]).filter(d=>d.group===g);
  openSheet(
    '<div><h3 id="sheet-title">New application</h3><p>Only the company and the role are '+
    'required. Everything else can come later.</p></div>'+
    '<div class="fg w88">'+
      '<label>Company</label><input id="nj-company" autocomplete="off">'+
      '<label>Role</label><input id="nj-title" autocomplete="off">'+
      '<label>Status</label><select id="nj-status">'+S.statuses.map(s=>
        '<option value="'+s+'">'+esc(prettyStatus(s))+'</option>').join("")+'</select>'+
      '<label>Source</label><input id="nj-source" autocomplete="off" '+
        'placeholder="LinkedIn, referral, careers page…">'+
      '<label>Salary</label><input id="nj-salary" type="number" class="mono">'+
      '<label>Follow-up</label><input id="nj-followup" type="date">'+
      '<label>Link</label><input id="nj-url" autocomplete="off" placeholder="https://">'+
      '<label>CV</label><select id="nj-cv"><option value="">Not linked</option>'+
        docs("My CVs").map(d=>'<option value="'+esc(d.path)+'"'+
          (d.path===seed.cv_path?" selected":"")+'>'+esc(d.label)+'</option>').join("")+
        '</select>'+
      '<label>Cover letter</label><select id="nj-letter"><option value="">Not linked</option>'+
        docs("Cover letters").map(d=>'<option value="'+esc(d.path)+'">'+esc(d.label)+
          '</option>').join("")+'</select>'+
      '<label>Notes</label><textarea id="nj-notes" rows="3"></textarea>'+
    '</div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="nj-go">Add</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#nj-go").onclick=async()=>{
    const v=id=>$("#"+id).value.trim();
    if(!v("nj-company")||!v("nj-title")) return toast("Company and role are required",true);
    try{
      const j=await post("/api/jobs",{
        company:v("nj-company"), title:v("nj-title"), status:$("#nj-status").value,
        source:v("nj-source")||null, url:v("nj-url")||null,
        salary_expected:v("nj-salary")?Number(v("nj-salary")):null,
        followup_date:v("nj-followup")||null, notes:v("nj-notes")||null,
        cv_path:$("#nj-cv").value||null, letter_path:$("#nj-letter").value||null});
      closeSheet(); await loadJobs(); S.funnel=null;
      setView("jobs"); selectJob(j.id); toast("Added "+j.company);
    }catch(e){ toast(e.message,true) }
  };
  $("#nj-company").focus();
}
$("#btn-newjob").onclick=()=>newJobSheet();

/* =========================================================================
   Funnel

   The layout is the real d3-sankey, vendored locally rather than approximated,
   so the ribbon geometry is correct. Scripts load on first open, so opening
   the editor pays nothing for a screen that may never be used.
   ========================================================================= */
let d3ready=null;
function loadScript(src){
  return new Promise((res,rej)=>{
    const el=document.createElement("script");
    el.src=src; el.onload=res; el.onerror=()=>rej(new Error("could not load "+src));
    document.head.append(el);
  });
}
function ensureD3(){
  if(!d3ready) d3ready=(async()=>{
    /* order matters: sankey needs array, shape needs path */
    for(const m of ["d3-array","d3-path","d3-shape","d3-sankey"])
      await loadScript("/static/"+m+".min.js");
  })();
  return d3ready;
}

/* One ochre path through the chart: the applications that are still worth
   something. Totals are dark; every other outcome is neutral. */
const FN_POSITIVE=new Set(["interview_s","offer_s","accepted"]);
const FN_TOTAL=new Set(["all","applied_s"]);
const fnTone=id=>FN_POSITIVE.has(id)?"#c08a3e":FN_TOTAL.has(id)?"#33312b":"#a9a394";

$$("#range button").forEach(b=>b.onclick=()=>{
  $$("#range button").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
  S.since=b.dataset.since; S.funnel=null; loadFunnel();
});
function sinceDate(){
  if(!S.since) return null;
  const d=new Date();
  if(S.since==="6m") d.setMonth(d.getMonth()-6); else d.setDate(d.getDate()-30);
  return d.toISOString().slice(0,10);
}

async function loadFunnel(){
  const host=$("#chart");
  if(S.funnel) return drawFunnel();
  host.innerHTML='<p class="note"><span class="spin"></span> Loading…</p>';
  try{
    await ensureD3();
    const since=sinceDate();
    S.funnel=await api("/api/funnel"+(since?"?since="+since:""));
  }catch(e){
    host.innerHTML='<div class="empty"><h3>Could not load the funnel</h3><p>'+
      esc(e.message)+'</p></div>';
    return;
  }
  drawFunnel();
}

function drawFunnel(){
  const f=S.funnel, t=f.totals, host=$("#chart");
  $("#fn-total").textContent=t.total+" application"+(t.total===1?"":"s");
  $("#fn-sub").textContent=[
    t.applied+" sent",
    t.interviewed+" reached an interview",
    t.offers+" offer"+(t.offers===1?"":"s")].join(" · ");
  drawRates();

  if(!t.total){
    host.innerHTML='<div class="empty"><h3>Nothing tracked yet</h3><p>Add applications '+
      'and this will show how far they get: how many reach an interview, how many '+
      'convert to an offer, and where the rest drop out.</p></div>';
    return;
  }
  const nodes=f.nodes.filter(n=>n.count>0);
  const idx=new Map(nodes.map((n,i)=>[n.id,i]));
  const links=f.links.filter(l=>idx.has(l.source)&&idx.has(l.target))
    .map(l=>({source:idx.get(l.source),target:idx.get(l.target),value:l.value,
              sid:l.source,tid:l.target}));
  if(!links.length){ host.innerHTML=""; return }

  const W=Math.max(680,host.clientWidth||1000);
  /* Flat and airy like the design rather than a wall of ribbon: the height
     follows the node count, but stops well short of filling the pane. */
  const H=Math.max(280,Math.min(440,nodes.length*28));
  /* The right-hand pad is where the terminal labels live: they sit outside the
     sankey extent, so the layout has to stop short of the edge. */
  const PAD=Math.max(180,Math.min(300,W*0.22));
  /* Left alignment, not justify: a stage sits at its distance from the start,
     so "Rejected" lands in the column it happened in rather than being pushed
     to the right-hand edge with every other dead end. */
  const layout=d3.sankey().nodeWidth(9).nodePadding(14).nodeAlign(d3.sankeyLeft)
    .extent([[4,22],[W-PAD,H-10]]);
  const graph=layout({nodes:nodes.map(n=>({...n})),links:links.map(l=>({...l}))});
  const path=d3.sankeyLinkHorizontal();
  const touches=l=>!S.fnode||l.sid===S.fnode||l.tid===S.fnode;

  const bands=graph.links.map(l=>
    '<path class="sk-link'+(touches(l)?"":" sk-dim")+'" d="'+path(l)+'" fill="none" stroke="'+
    (FN_POSITIVE.has(l.tid)&&FN_POSITIVE.has(l.sid)||FN_POSITIVE.has(l.tid)?"#c08a3e":"#a9a394")+
    '" stroke-opacity=".34" stroke-width="'+Math.max(1,l.width)+'"><title>'+
    esc(l.source.label)+' → '+esc(l.target.label)+': '+l.value+'</title></path>').join("");

  /* The bar alone is a 9px target, so each node gets a hit area over its label
     too -- clicking a band is how you get to the jobs behind it. */
  const bars=graph.nodes.map(n=>{
    const h=Math.max(1,n.y1-n.y0), dim=S.fnode&&S.fnode!==n.id?" sk-dim":"";
    return '<g class="sk-hit'+dim+'" data-node="'+esc(n.id)+'" role="button" tabindex="0">'+
      '<title>'+esc(n.label)+': '+n.count+' — click to list them</title>'+
      '<rect x="'+(n.x0-6)+'" y="'+(n.y0-8)+'" width="'+((n.x1-n.x0)+PAD)+'" height="'+
      (h+16)+'" fill="transparent"/>'+
      '<rect class="sk-node" x="'+n.x0+'" y="'+n.y0+'" width="'+(n.x1-n.x0)+'" height="'+h+
      '" fill="'+fnTone(n.id)+'"/></g>';
  }).join("");

  const labels=graph.nodes.map(n=>{
    const dim=S.fnode&&S.fnode!==n.id?"sk-dim":"";
    const text=esc(n.label)+" · "+n.count;
    /* The first column has no room to its right, so its label sits above. */
    return n.x0<8
      ? '<text class="sk-label '+dim+'" x="'+n.x0+'" y="'+(n.y0-8)+'">'+text+'</text>'
      /* Near the top of a tall node rather than its middle, so a label never
         sits marooned in the centre of a thick band. */
      : '<text class="sk-label '+dim+'" x="'+(n.x1+9)+'" y="'+
        Math.min((n.y0+n.y1)/2,n.y0+11)+'" dominant-baseline="middle">'+text+'</text>';
  }).join("");

  host.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet" '+
    'role="img" aria-label="Application funnel">'+bands+bars+
    '<g pointer-events="none">'+labels+'</g></svg>';
  host.querySelectorAll("[data-node]").forEach(g=>{
    const pick=()=>{
      const id=g.dataset.node;
      S.fnode=id;
      S.jfilter={kind:"node",value:id};
      S.jsel=null;
      $("#jobq").value="";
      setView("jobs");
    };
    g.onclick=pick;
    g.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); pick() } };
  });
}

function drawRates(){
  const t=S.funnel.totals, c=S.funnel.by_status||{};
  const rate=(label,value,accent)=>
    '<div class="kv"><span>'+label+'</span><span class="v mono'+(accent?" acc":"")+'">'+
    value+'</span></div>';
  const reply=t.median_reply_days==null?"—"
    :t.median_reply_days+" day"+(t.median_reply_days===1?"":"s");
  $("#fn-rates").innerHTML=
    rate("Applied → interview",t.interview_rate+"%")+
    rate("Interview → offer",t.offer_rate+"%",t.offers>0)+
    rate("Offer → accepted",t.accept_rate+"%")+
    rate("Median reply time",reply)+
    '<div class="hr"></div>'+
    readings(t,c).map(p=>'<div class="note">'+esc(p)+'</div>').join("")+
    '<div class="hr"></div>'+
    '<div style="display:flex;gap:7px"><button class="obtn" id="ex-csv">Export CSV</button>'+
    '<button class="obtn" id="ex-json">JSON</button></div>';
  $("#ex-csv").onclick=()=>window.open("/api/jobs/export?format=csv"+tok());
  $("#ex-json").onclick=()=>window.open("/api/jobs/export?format=json"+tok());
}

/* Two short readings of the numbers. Each one is only shown when the data
   actually supports it, so the panel says less on a thin week rather than
   inventing something. */
function readings(t,c){
  const out=[];
  const early=c.rejected||0, late=c.rejected_interviewing||0;
  if(early+late){
    out.push(early+" rejection"+(early===1?"":"s")+" came before any interview and "+
      late+" after. Only the first group is a CV problem.");
  }
  const ghost=(c.ghosted||0)+(c.ghosted_interviewing||0);
  if(ghost) out.push(ghost+" application"+(ghost===1?"":"s")+" went unanswered — "+
    Math.round(ghost/Math.max(1,t.applied)*100)+"% of everything sent.");
  if(t.replied) out.push(t.replied+" of "+t.applied+" applications have had a reply.");
  const waiting=c.applied||0;
  if(waiting&&out.length<2) out.push(waiting+" "+(waiting===1?"is":"are")+
    " still waiting for a first answer.");
  if(!out.length) out.push("Not enough has happened yet to read anything into.");
  return out.slice(0,2);
}

/* Re-lay on resize. Debounced, because a sankey layout on every pixel of a
   window drag is wasted work. */
let sizeTimer=null;
window.addEventListener("resize",()=>{
  if(S.view==="funnel"&&S.funnel){
    clearTimeout(sizeTimer); sizeTimer=setTimeout(drawFunnel,140);
  }
  if(S.view==="cvs"&&S.zoomAuto) paintPage();
});

/* =========================================================================
   New document
   ========================================================================= */
const slug=s=>String(s||"").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g,"")
  .replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"");
/* "Senior Engineer, Metrics" at Datadog becomes metrics-datadog: the part
   after the last comma is the bit that distinguishes one role from another. */
function derivedName(role,company){
  const tail=String(role||"").split(",").pop();
  const bits=[slug(tail),slug(company)].filter(Boolean);
  return bits.join("-")||"untitled";
}
function newDocumentSheet(){
  const all=(S.state&&S.state.documents)||[];
  const forKind=k=>all.filter(d=>(d.group==="Cover letters")===(k==="letter"));
  openSheet(
    '<div><h3 id="sheet-title">New document</h3><p>Duplicating copies the YAML and its '+
    'comments. The original is untouched.</p></div>'+
    '<div class="fg w88">'+
      '<label>Kind</label><div class="seg paper acc" id="nd-kind" role="tablist">'+
        '<button role="tab" data-kind="cv" aria-selected="true">CV</button>'+
        '<button role="tab" data-kind="letter" aria-selected="false">Cover letter</button>'+
      '</div>'+
      '<label>Base on</label><select id="nd-base"></select>'+
      '<label>Company</label><input id="nd-company" autocomplete="off">'+
      '<label>Role</label><input id="nd-role" autocomplete="off">'+
      '<label>Save as</label><input id="nd-name" readonly class="mono">'+
      '<div></div><label class="check"><input type="checkbox" id="nd-draft" checked>'+
        '<i>✓</i>Add a Draft row to the Jobs list</label>'+
    '</div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="nd-go">Create</button></div>');
  let kind=(S.path&&S.path.startsWith("letters/"))?"letter":"cv";
  const sync=()=>{
    $("#nd-name").value=derivedName($("#nd-role").value,$("#nd-company").value)+".yaml";
  };
  /* Basing a letter on a CV produces nonsense, so the list follows the kind.
     The document you have open is the obvious thing to duplicate. */
  const fillBase=()=>{
    $("#nd-base").innerHTML='<option value="">A blank starter</option>'+
      forKind(kind).map(d=>'<option value="'+esc(d.path)+'"'+
        (d.path===S.path?" selected":"")+'>'+esc(d.label)+
        (S.pages[d.path]?" — "+S.pages[d.path]+" page"+(S.pages[d.path]===1?"":"s"):"")+
        '</option>').join("");
  };
  $$("#nd-kind button").forEach(b=>{
    b.setAttribute("aria-selected",String(b.dataset.kind===kind));
    b.onclick=()=>{
      kind=b.dataset.kind;
      $$("#nd-kind button").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
      $("#nd-draft").parentElement.style.opacity=kind==="cv"?"":".5";
      $("#nd-draft").disabled=kind!=="cv";
      fillBase();
    };
  });
  fillBase();
  $("#nd-company").oninput=sync; $("#nd-role").oninput=sync; sync();
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#nd-go").onclick=async()=>{
    const company=$("#nd-company").value.trim(), role=$("#nd-role").value.trim();
    const name=derivedName(role,company);
    if(name==="untitled") return toast("Give it a company or a role to name it after",true);
    try{
      const r=await post("/api/new",{name,kind,from:$("#nd-base").value||null,
        theme:prefs().theme||null});
      if(kind==="cv"&&$("#nd-draft").checked&&company&&role){
        try{ await post("/api/jobs",{company,title:role,status:"pending",cv_path:r.path}) }
        catch(e){ toast("Document created, but the Jobs row failed: "+e.message,true) }
      }
      closeSheet();
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      await loadJobs(true);
      openDoc(r.path); toast("Created "+name);
    }catch(e){ toast(e.message,true) }
  };
  $("#nd-company").focus();
}

/* =========================================================================
   Design
   ========================================================================= */
/* Thumbnails are drawn rather than screenshotted: a few bars in the shape of
   each theme's real layout. Anything RenderCV adds later falls back to the
   plain single-column sketch instead of showing nothing. */
const THUMBS={
  classic:{bars:[[5,"66%",1],[1,"100%",1],[3,"100%"],[3,"88%"],[3,"94%"],[4,"40%",1,3],
    [3,"92%"]]},
  sb2nov:{centre:true,bars:[[5,"56%",0,0,"#27496d"],[3,"70%"],[6,"100%",0,3,"#e7ecf2"],
    [3,"100%"],[3,"86%"]]},
  engineeringclassic:{bars:[[5,"60%",1],[3,"100%",0,3],[3,"82%"],[3,"96%"]]},
  engineeringresumes:{bars:[[7,"100%",1],[3,"74%",0,3],[3,"92%"]]},
  moderncv:{split:true},
  _default:{bars:[[5,"60%",1],[3,"100%",0,3],[3,"86%"],[3,"94%"]]},
};
function thumbHTML(theme){
  const t=THUMBS[theme]||THUMBS._default;
  if(t.split) return '<div class="thumb" style="flex-direction:row;gap:7px">'+
    '<div style="width:32%;display:flex;flex-direction:column;gap:4px">'+
    '<i style="height:4px;background:#3f6b4d"></i><i style="height:3px"></i>'+
    '<i style="height:3px"></i></div>'+
    '<div style="flex:1;display:flex;flex-direction:column;gap:4px">'+
    '<i class="ink" style="height:5px;width:80%"></i><i style="height:3px"></i>'+
    '<i style="height:3px"></i></div></div>';
  return '<div class="thumb"'+(t.centre?' style="align-items:center"':"")+'>'+
    t.bars.map(([h,w,ink,mt,bg])=>'<i'+(ink?' class="ink"':"")+' style="height:'+h+
      'px;width:'+w+(mt?';margin-top:'+mt+"px":"")+(bg?";background:"+bg:"")+'"></i>').join("")+
    '</div>';
}
const themeLabel=t=>t.replace(/^engineeringclassic$/,"Engineering")
  .replace(/^engineeringresumes$/,"Engineering résumés")
  .replace(/^(.)/,c=>c.toUpperCase());

$("#btn-design").onclick=()=>openDesign();
async function openDesign(){
  if(!S.path) return toast("Open a document first");
  $("#ovl-settings").hidden=true; $("#ovl-design").hidden=false;
  paintThemes(); paintEffect();
  await ensureSchema();
  paintBasics(); paintAdvanced();
}
async function ensureSchema(){
  const theme=DZ.theme||(S.state.themes||[])[0];
  if(S.schema&&S.schemaTheme===theme) return;
  try{ S.schema=await api("/api/design-schema?theme="+encodeURIComponent(theme));
       S.schemaTheme=theme }
  catch(e){ S.schema={groups:[]}; S.schemaTheme=theme }
}
function paintThemes(){
  const themes=(S.state&&S.state.themes)||[];
  $("#themegrid").innerHTML=themes.map(t=>{
    const pp=S.themePages[t];
    return '<div class="thumbwrap'+(t===DZ.theme?" sel":"")+'"><button data-theme="'+esc(t)+
      '" style="display:contents" aria-label="'+esc(themeLabel(t))+'">'+thumbHTML(t)+
      '</button><div class="thumbcap"><span>'+esc(themeLabel(t))+'</span>'+
      '<em class="mono">'+(pp?pp+"pp":"")+'</em></div></div>';
  }).join("");
  $$("#themegrid [data-theme]").forEach(b=>b.onclick=()=>{
    DZ.theme=b.dataset.theme; S.schema=null;
    paintThemes(); touch();
    ensureSchema().then(()=>{ paintBasics(); paintAdvanced() });
  });
}

/* The one design field whose YAML path is not fixed across RenderCV versions
   is the body size, so it is found in the schema rather than assumed. When it
   cannot be found the slider is left out instead of writing a guess. */
function schemaFields(){
  const out=[];
  ((S.schema&&S.schema.groups)||[]).forEach(g=>g.fields.forEach(f=>out.push(f)));
  return out;
}
function famPaths(){
  const found=schemaFields().map(f=>f.path).filter(p=>
    (p[0]==="text"&&p[1]==="font_family")||(p[0]==="typography"&&p[1]==="font_family"));
  return found.length?found
    :[["typography","font_family","body"],["typography","font_family","name"]];
}
function sizeField(){
  const fields=schemaFields().filter(f=>{
    const p=f.path;
    return (p[0]==="typography"&&p[1]==="font_size")||(p[0]==="text"&&p[1]==="font_size");
  });
  return fields.find(f=>f.path[f.path.length-1]==="body")||fields[0]||null;
}
const PT=v=>{ const m=/([\d.]+)/.exec(String(v==null?"":v)); return m?parseFloat(m[1]):null };

function paintBasics(){
  const fonts=(S.state&&S.state.fonts)||[];
  const sizeF=sizeField();
  const cur=(S.data&&S.data.design)||{};
  if(DZ.size==null&&sizeF) DZ.size=getAt(cur,sizeF.path)!=null
    ? String(getAt(cur,sizeF.path)) : (sizeF.default!=null?String(sizeF.default):"10pt");
  const pt=PT(DZ.size)||10;
  let h='<label for="dz-face">Typeface</label>'+
    '<select id="dz-face">'+fonts.map(f=>'<option'+(f===DZ.family?" selected":"")+'>'+
      esc(f)+'</option>').join("")+'</select>';
  if(sizeF) h+='<label for="dz-size">Body size</label>'+
    '<div class="slider"><input type="range" id="dz-size" min="8" max="14" step="0.5" '+
    'value="'+pt+'" aria-label="Body size in points">'+
    '<span class="val mono" id="dz-size-v">'+pt+' pt</span></div>';
  h+='<label>Page</label><div class="seg paper" id="dz-page" role="tablist">'+
    ((S.state&&S.state.page_sizes)||["a4","us-letter"]).map(p=>
      '<button role="tab" data-page="'+esc(p)+'" aria-selected="'+String(p===DZ.page)+'">'+
      (p==="a4"?"A4":"US Letter")+'</button>').join("")+'</div>';
  $("#dz-basics").innerHTML=h;

  const face=$("#dz-face");
  face.style.fontFamily="'"+(DZ.family||"")+"', serif";
  face.onchange=()=>{ DZ.family=face.value;
    face.style.fontFamily="'"+face.value+"', serif"; touch() };
  const sz=$("#dz-size");
  if(sz){
    const fill=()=>sz.style.setProperty("--fill",
      ((sz.value-sz.min)/(sz.max-sz.min)*100)+"%");
    fill();
    sz.oninput=()=>{ fill(); $("#dz-size-v").textContent=sz.value+" pt" };
    sz.onchange=()=>{ DZ.size=sz.value+"pt"; touch() };
  }
  $$("#dz-page button").forEach(b=>b.onclick=()=>{
    DZ.page=b.dataset.page;
    $$("#dz-page button").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
    touch();
  });
}

/* Every other design option, generated from RenderCV's own schema rather than
   a hand-written list, so it stays correct when RenderCV adds or renames one. */
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
function paintAdvanced(){
  const host=$("#dz-advanced"), groups=(S.schema&&S.schema.groups)||[];
  if(!groups.length){
    host.innerHTML='<p class="sp-note">RenderCV did not offer a schema for this theme, '+
      'so only the settings above are available. Everything else can still be edited '+
      'in the YAML tab.</p>';
    return;
  }
  const cur=(S.data&&S.data.design)||{};
  const sizeP=sizeField()&&sizeField().path.join(".");
  const famP=new Set(famPaths().map(p=>p.join(".")));
  const chev='<svg class="chev" width="11" height="11" viewBox="0 0 24 24" fill="none" '+
    'stroke="currentColor" stroke-width="3"><path d="M9 18l6-6-6-6"/></svg>';
  let h='';
  groups.forEach(g=>{
    const fields=g.fields.filter(f=>{
      const p=f.path.join(".");
      return p!==sizeP&&!famP.has(p)&&p!=="page.size";
    });
    if(!fields.length) return;
    h+='<details class="grp"><summary>'+chev+esc(g.name.replace(/_/g," "))+
      '<span class="count">'+fields.length+'</span></summary><div class="body">'+
      '<div class="dgrid">'+fields.map(f=>{
        const raw=getAt(cur,f.path);
        const v=(raw===undefined||raw===null)?f.default:raw;
        const dp=esc(JSON.stringify(f.path));
        const label=esc(f.path[f.path.length-1].replace(/_/g," "));
        let ctl;
        if(f.kind==="color")
          ctl='<input type="color" data-d='+"'"+dp+"'"+' data-kind="color" value="'+
            rgb2hex(v)+'"><span class="hex mono">'+esc(String(v==null?"":v))+'</span>';
        else if(f.kind==="dimension"){
          const d=splitDim(v);
          ctl='<input type="number" step="0.05" data-d='+"'"+dp+"'"+
            ' data-kind="dimension" value="'+esc(d.n)+'"><select class="unit">'+
            UNITS.map(x=>'<option'+(x===d.u?" selected":"")+'>'+x+'</option>').join("")+
            '</select>';
        }
        else if(f.kind==="enum")
          ctl='<select data-d='+"'"+dp+"'"+' data-kind="enum">'+(f.options||[]).map(o=>
            '<option'+(String(o)===String(v)?" selected":"")+'>'+esc(o)+'</option>')
            .join("")+'</select>';
        else if(f.kind==="bool")
          ctl='<input type="checkbox" data-d='+"'"+dp+"'"+' data-kind="bool"'+
            (v?" checked":"")+'>';
        else if(f.kind==="number")
          ctl='<input type="number" data-d='+"'"+dp+"'"+' data-kind="number" value="'+
            esc(v==null?"":v)+'">';
        else if(f.kind==="list")
          ctl='<input type="text" data-d='+"'"+dp+"'"+' data-kind="list" value="'+
            esc((v||[]).join(", "))+'" placeholder="comma separated">';
        else
          ctl='<input type="text" data-d='+"'"+dp+"'"+' data-kind="text" value="'+
            esc(v==null?"":v)+'">';
        return '<label title="'+esc(f.path.join("."))+'">'+label+'</label>'+
          '<div class="dctl">'+ctl+'</div>';
      }).join("")+'</div></div></details>';
  });
  host.innerHTML='<div style="margin-top:6px">'+h+'</div>';
}
function advancedPatches(){
  return $$("#dz-advanced [data-d]").map(el=>{
    const path=JSON.parse(el.dataset.d), kind=el.dataset.kind;
    let v;
    if(kind==="color") v=hex2rgb(el.value);
    else if(kind==="bool") v=el.checked;
    else if(kind==="number") v=el.value===""?null:Number(el.value);
    else if(kind==="list") v=el.value.split(",").map(x=>x.trim()).filter(Boolean);
    else if(kind==="dimension"){
      if(el.value==="") return null;
      const u=el.parentElement.querySelector("select.unit");
      v=String(el.value)+((u&&u.value)||"cm");
    }
    else v=el.value;
    return {path:["design"].concat(path),value:v};
  }).filter(Boolean);
}
function designPatches(){
  const out=[];
  if(DZ.theme) out.push({path:["design","theme"],value:DZ.theme});
  if(DZ.page)  out.push({path:["design","page","size"],value:DZ.page});
  if(DZ.family) famPaths().forEach(p=>out.push({path:["design"].concat(p),value:DZ.family}));
  const sf=sizeField();
  if(sf&&DZ.size) out.push({path:["design"].concat(sf.path),value:DZ.size});
  return out.concat(advancedPatches());
}
$("#dz-advanced").addEventListener("input",e=>{
  if(e.target.dataset&&e.target.dataset.kind==="color"){
    const sp=e.target.parentElement.querySelector(".hex");
    if(sp) sp.textContent=hex2rgb(e.target.value);
  }
  touch();
});
$("#dz-advanced").addEventListener("change",touch);

/* What the current combination actually costs, read off the last render
   rather than predicted. */
function paintEffect(){
  const r=S.render;
  if(!r){ $("#dz-effect").innerHTML='<p class="note muted">Nothing rendered yet.</p>';
    return }
  const pct=S.fill==null?null:Math.round(S.fill*100);
  const known=Object.keys(S.themePages).filter(t=>t!==DZ.theme);
  const shortest=known.sort((a,b)=>S.themePages[a]-S.themePages[b])[0];
  let note;
  if(shortest&&S.themePages[shortest]<r.pages)
    note=themeLabel(shortest)+" rendered this CV on "+S.themePages[shortest]+
      " page"+(S.themePages[shortest]===1?"":"s")+", against "+r.pages+" here.";
  else if(known.length)
    note="No other theme tried so far renders this CV any shorter.";
  else
    note="Pick another theme to see what it does to the page count — each one is "+
      "rendered for real, so the number is the number.";
  $("#dz-effect").innerHTML=
    '<div class="kv"><span>Pages</span><span class="v mono">'+r.pages+'</span></div>'+
    '<div class="kv"><span>Page '+r.pages+' fill</span><span class="v mono'+
      (pct!=null&&pct>=90?" acc":"")+'">'+(pct==null?"—":pct+"%")+'</span></div>'+
    '<div class="kv"><span>Words</span><span class="v mono">'+(r.ats_words||0)+
      '</span></div>'+
    '<div class="hr"></div><div class="note">'+esc(note)+'</div>';
}

/* =========================================================================
   Settings and updates
   ========================================================================= */
/* Preferences are per-machine conveniences, so they live in localStorage
   rather than in the workspace: a workspace copied to another machine should
   carry documents, not window preferences. Reads are guarded because storage
   throws outright in some privacy modes. */
const PREFS_KEY="cvstudio.prefs";
function prefs(){
  try{ return JSON.parse(localStorage.getItem(PREFS_KEY)||"{}") }catch(e){ return {} }
}
function setPref(k,v){
  try{ const p=prefs(); p[k]=v; localStorage.setItem(PREFS_KEY,JSON.stringify(p)) }catch(e){}
}

$("#btn-settings").onclick=()=>{
  if($("#ovl-settings").hidden){
    $("#ovl-design").hidden=true; $("#ovl-settings").hidden=false; fillSettings();
  }else closeOverlays();
};
$$("#set-rail button").forEach(b=>b.onclick=()=>{
  $$("#set-rail button").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
  ["workspace","editor","ai","api","updates","about"].forEach(k=>
    $("#sp-"+k).hidden = k!==b.dataset.s);
  if(b.dataset.s==="updates") checkUpdates(true);
});
$$("[data-copy]").forEach(b=>b.onclick=async()=>{
  try{ await navigator.clipboard.writeText($("#"+b.dataset.copy).textContent);
       toast("Copied") }
  catch(e){ toast("Select the text and copy manually",true) }
});

function fillSettings(){
  const st=S.state||{}, base=location.origin, pr=prefs();
  $("#s-ws").textContent=st.workspace||"";
  $("#s-count").textContent=(st.documents||[]).length+" documents";
  $("#s-base").textContent=base;
  $("#s-spec").href=base+"/api/docs"+(st.api_token?"?token="+
    encodeURIComponent(st.api_token):"");
  $("#s-ver").textContent="CV Studio "+(st.version||"");
  $("#s-open").onclick=async()=>{
    try{ await post("/api/reveal",{}) }catch(e){ toast(e.message,true) }
  };
  $("#s-exp").onclick=()=>window.open("/api/jobs/export?format=json"+tok());
  $("#s-check").onclick=()=>checkUpdates(true);

  const live=$("#s-live");
  live.checked=pr.live!==false;
  live.onchange=()=>setPref("live",live.checked);
  const delay=$("#s-delay");
  delay.value=String(pr.delay||700);
  delay.onchange=()=>setPref("delay",Number(delay.value));
  const dt=$("#s-deftheme");
  if(dt&&!dt.dataset.filled){
    dt.innerHTML=(st.themes||[]).map(t=>"<option>"+esc(t)+"</option>").join("");
    dt.dataset.filled="1";
  }
  if(dt){ dt.value=pr.theme||(st.themes||[])[0]||"";
          dt.onchange=()=>setPref("theme",dt.value) }

  /* Claude Desktop needs command and args separately; a single string holding
     "python script.py" is not runnable. */
  const L=st.server_launch||{command:"cv-studio-server",args:[]};
  const args=[...(L.args||[]),"--mcp"];
  if(st.workspace) args.push("--workspace",st.workspace);
  $("#s-mcp").textContent=JSON.stringify(
    {mcpServers:{"cv-studio":{command:L.command,args}}},null,2);

  const auth=st.api_token?' \\\n  -H "X-API-Key: '+st.api_token+'"':"";
  $("#s-curl").textContent=
    "curl "+base+"/api/state"+auth+"\n\n"+
    "curl -X POST "+base+"/api/render"+auth+" \\\n"+
    '  -H "Content-Type: application/json" \\\n'+
    "  -d '{\"path\":\"profile/my-cv.yaml\"}'";
  $("#s-auth").textContent=st.api_token
    ? "An X-API-Key header is required; the key is shown in the example below."
    : "None needed. The server accepts local connections only. Start it with "+
      "--token to require a key, or --host to expose it, which forces one.";
}

/* Tauri's updater verifies a signature against the public key baked into the
   build, so a compromised release host still cannot push a package this app
   will install. */
async function checkUpdates(loud){
  const T=window.__TAURI__;
  const st=$("#u-state"), act=$("#u-actions");
  if(!T||!T.updater){
    if(st) st.textContent="Updates are available in the desktop app only.";
    return;
  }
  if(st) st.innerHTML='<span class="spin"></span> Checking for updates…';
  if(act) act.innerHTML="";
  try{
    const up=await T.updater.check();
    if(!up){ if(st) st.textContent="You are on the latest version."; return }
    if(st) st.innerHTML="Version <b>"+esc(up.version)+"</b> is available."+
      (up.body?'<br>'+esc(up.body).slice(0,300):"");
    if(act){
      act.innerHTML='<button class="sbtn primary" id="u-go">Download and install</button>';
      $("#u-go").onclick=async()=>{
        $("#u-go").disabled=true;
        let total=0, got=0;
        try{
          await up.downloadAndInstall(e=>{
            if(e.event==="Started") total=e.data.contentLength||0;
            if(e.event==="Progress"){
              got+=e.data.chunkLength||0;
              st.textContent=total?"Downloading "+Math.round(got/total*100)+"%"
                                  :"Downloading…";
            }
            if(e.event==="Finished") st.textContent="Installing…";
          });
          st.textContent="Restarting…";
          if(T.process&&T.process.relaunch) await T.process.relaunch();
        }catch(err){ st.textContent="Update failed: "+err; $("#u-go").disabled=false }
      };
    }
    if(!loud) toast("Version "+up.version+" is available (Settings to install)");
  }catch(e){
    /* A 404 here almost always means no release has been published yet, or the
       repository is private so the asset cannot be fetched without credentials.
       Saying that is more useful than relaying the transport error. */
    const raw=String(e);
    if(st) st.textContent=/release JSON|404|not found/i.test(raw)
      ? "No published release to update to yet. Updates begin working once a version "+
        "is tagged and the release is publicly downloadable."
      : "Could not check for updates: "+raw;
  }
}
setTimeout(()=>{ if(window.__TAURI__&&window.__TAURI__.updater) checkUpdates(false) },4000);

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
