"""Shared runtime state, YAML round-tripper, and actor-context isolation.

Both the desktop HTTP server and the MCP server run on top of studio_core.
Actor identity (whether an edit came from the user via HTTP or from an AI
client via MCP) is carried in contextvars with a process-wide fallback, so
embedded HTTP views served inside the MCP process always attribute user edits
to "you" while concurrent MCP tool calls attribute edits to the AI client.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
import sys
import threading
from pathlib import Path
from typing import Iterator

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap
except ImportError:  # pragma: no cover
    sys.stderr.write("ruamel.yaml is required.\n")
    raise SystemExit(1)

SERVER_ROOT = Path(__file__).resolve().parent.parent
for _cand in (SERVER_ROOT, SERVER_ROOT.parents[1] / "cv-studio-resume" / "scripts"):
    if (_cand / "cv_render.py").is_file() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))
        break
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from cv_render import render_file  # noqa: E402, F401

try:
    import jobs as jobstore
except ImportError:  # the store lives beside the packaged server
    jobstore = None

try:
    import cv_map
except ImportError:  # clicking the page is a bonus, not a requirement
    cv_map = None

import ats  # noqa: E402, F401
import backups  # noqa: E402, F401
import cjkfonts  # noqa: E402, F401
import history  # noqa: E402, F401
import importer  # noqa: E402, F401
import languages  # noqa: E402, F401
import letters  # noqa: E402, F401
import posting  # noqa: E402, F401
import review  # noqa: E402, F401
import sample  # noqa: E402, F401
import scans  # noqa: E402, F401
import themes  # noqa: E402, F401

# Vendored static assets. In a frozen build PyInstaller unpacks data files
# under _MEIPASS; in a source checkout they sit under server/static.
SAFE_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
STATIC_DIR = Path(getattr(sys, "_MEIPASS", str(SERVER_ROOT))) / "static"
if not STATIC_DIR.is_dir():
    STATIC_DIR = SERVER_ROOT / "static"

THEMES = [
    "classic", "ember", "engineeringclassic", "engineeringresumes", "harvard",
    "ink", "moderncv", "opal", "sb2nov",
]
PAGE_SIZES = ["a4", "us-letter"]
DEFAULT_WORKSPACE = Path.home() / "Documents" / "CV Studio"

EDITS_FILE = ".cvstudio-edits.json"
EDITS_KEEP = 200
EDITS_MAX_AGE = 60 * 60 * 24 * 30
EDITS_VALUE_CHARS = 300

PHOTO_FILE = "photo.jpg"
PHOTO_MAX = 20 * 1024 * 1024

CLIP_PORT = int(os.environ.get("CVSTUDIO_CLIP_PORT") or 47811)
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

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
          - What you achieved, with its real number or a scope you can count
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
    # RenderCV prints "Last updated in <month> <year>" at the top right of
    # page one. It dates the document rather than the work, and a CV that
    # announces it was last touched four months ago is answering a question
    # nobody asked.
    show_top_note: false
  colors:
    name: rgb(0, 0, 0)
    section_titles: rgb(0, 0, 0)
    headline: rgb(70, 70, 70)
    connections: rgb(70, 70, 70)
    links: rgb(0, 60, 120)
  # Set for applicant tracking systems: contact details as plain text rather
  # than icon glyphs, the full profile links, and the phone with its country
  # code. Design turns any of them back.
  header:
    connections:
      show_icons: false
      display_urls_instead_of_usernames: true
      phone_number_format: international
  # The dates stand on their own; "4 years 7 months" under each one is
  # arithmetic the reader does not need, and an ATS reads it as text.
  sections:
    show_time_spans_in: []
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
STARTER_CV_BEFORE = STARTER_CV.replace(
    "What you achieved, with its real number or a scope you can count",
    "An achievement with a number in it, because numbers get read first")

INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
CLIP_HTML = (STATIC_DIR / "clip.html").read_text(encoding="utf-8")
DOCS_HTML = (STATIC_DIR / "docs.html").read_text(encoding="utf-8")


class _OneAtATime:
    """A YAML object that one thread uses at a time.

    ruamel keeps the state of a load or a dump on the YAML object itself, and
    the server answers each request on a thread of its own (the connector runs
    tools on worker threads too).
    """

    def __init__(self, yaml: YAML):
        self._yaml = yaml
        self._lock = threading.RLock()

    def load(self, stream):
        with self._lock:
            return self._yaml.load(stream)

    def dump(self, data, stream=None, **kw):
        with self._lock:
            return self._yaml.dump(data, stream, **kw)


_yaml_rt = YAML(typ="rt")
_yaml_rt.preserve_quotes = True
_yaml_rt.width = 4096  # stop ruamel re-wrapping bullet text into hard breaks
_yaml_rt.indent(mapping=2, sequence=4, offset=2)
yaml_rt = _OneAtATime(_yaml_rt)


_ACTOR_IS_MCP: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "cvstudio_actor_is_mcp", default=None)
_ACTOR_CLIENT_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cvstudio_actor_client_id", default=None)
_ACTOR_CLIENT_AGENT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cvstudio_actor_client_agent", default=None)


class RuntimeContext:
    """Process runtime configuration and contextvar-isolated actor identity."""

    def __init__(self) -> None:
        self.workspace: Path = DEFAULT_WORKSPACE
        self.real_workspace: Path | None = None
        self.first_run: bool = False
        self.api_token: str | None = None
        self.version: str = "0.50.4"
        self.started: float | None = None
        self.open_network: bool = False
        self.own_ports: set[int] = {CLIP_PORT}
        self.clip_state: dict = {"ok": False, "why": "not started"}
        # Fallback defaults when not inside an explicit ctx.actor(...) scope.
        self._default_is_mcp: bool = False
        self._default_client_id: str | None = None
        self._default_client_agent: str | None = None

    @property
    def is_mcp(self) -> bool:
        v = _ACTOR_IS_MCP.get()
        return self._default_is_mcp if v is None else v

    @is_mcp.setter
    def is_mcp(self, value: bool) -> None:
        self._default_is_mcp = bool(value)

    @property
    def client_id(self) -> str | None:
        if _ACTOR_IS_MCP.get() is not None:
            return _ACTOR_CLIENT_ID.get()
        return self._default_client_id

    @client_id.setter
    def client_id(self, value: str | None) -> None:
        self._default_client_id = value

    @property
    def client_agent(self) -> str | None:
        if _ACTOR_IS_MCP.get() is not None:
            return _ACTOR_CLIENT_AGENT.get()
        return self._default_client_agent

    @client_agent.setter
    def client_agent(self, value: str | None) -> None:
        self._default_client_agent = value

    @contextlib.contextmanager
    def actor(self, *, is_mcp: bool, client_id: str | None = None,
              client_agent: str | None = None) -> Iterator[None]:
        """Scope actor identity to the current thread/task without touching globals."""
        t1 = _ACTOR_IS_MCP.set(is_mcp)
        t2 = _ACTOR_CLIENT_ID.set(client_id)
        t3 = _ACTOR_CLIENT_AGENT.set(client_agent)
        try:
            yield
        finally:
            _ACTOR_IS_MCP.reset(t1)
            _ACTOR_CLIENT_ID.reset(t2)
            _ACTOR_CLIENT_AGENT.reset(t3)

    def who(self, by: str | None = None) -> str:
        return by or self.client_id or ("ai" if self.is_mcp else "you")


ctx = RuntimeContext()


def safe_path(raw: str) -> Path:
    """Resolve `raw` under the active workspace, refusing traversal outside it."""
    p = (ctx.workspace / raw).resolve()
    root = ctx.workspace.resolve()
    if p != root and root not in p.parents:
        raise PermissionError("path outside the workspace")
    return p


def rel(p: Path) -> str:
    return p.resolve().relative_to(ctx.workspace.resolve()).as_posix()


def is_cv_yaml(p: Path) -> bool:
    """Quick shape check so non-CV YAML files in the workspace stay out of the list."""
    try:
        head = p.read_text(encoding="utf-8", errors="ignore")[:8000]
    except OSError:
        return False
    return bool(re.search(r"(?m)^cv:\s*$", head))


def is_letter(p: Path) -> bool:
    return p.suffix == letters.EXT


def to_plain(obj):
    """Strip ruamel CommentedMap/Seq wrappers into plain JSON-serialisable Python."""
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_plain(v) for v in obj]
    return obj
