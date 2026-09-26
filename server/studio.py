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
import hashlib
import http.server
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import zipfile
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap
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

try:
    import cv_map
except ImportError:  # clicking the page is a bonus, not a requirement
    cv_map = None

import ats  # noqa: E402
import cjkfonts  # noqa: E402
import sample  # noqa: E402
import importer  # noqa: E402
import languages  # noqa: E402
import letters  # noqa: E402
import backups  # noqa: E402

# Vendored d3 modules for the funnel chart. In a frozen build PyInstaller
# unpacks data files under _MEIPASS; in a checkout they sit next to this file.
SAFE_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
STATIC_DIR = Path(getattr(sys, "_MEIPASS", str(_HERE))) / "static"
if not STATIC_DIR.is_dir():
    STATIC_DIR = _HERE / "static"

THEMES = ["classic", "ember", "engineeringclassic", "engineeringresumes", "harvard", "ink",
          "moderncv", "opal", "sb2nov"]
PAGE_SIZES = ["a4", "us-letter"]
DEFAULT_WORKSPACE = Path.home() / "Documents" / "CV Studio"

yaml_rt = YAML(typ="rt")
yaml_rt.preserve_quotes = True
yaml_rt.width = 4096  # stop ruamel re-wrapping bullet text into hard breaks
yaml_rt.indent(mapping=2, sequence=4, offset=2)

WORKSPACE: Path = DEFAULT_WORKSPACE
FIRST_RUN = False
API_TOKEN: str | None = None
VERSION = "0.25.0"

# Which AI client this process is serving, when it is serving one. The app
# writes the client configs itself, so it can name the client in the args it
# installs and the server knows who it is before any handshake. Set from
# --client; the MCP handshake is the fallback for a config written by hand.
CLIENT_ID: str | None = None
CLIENT_AGENT: str | None = None
# Whether this process is the MCP server rather than the app's own. It decides
# what an edit is attributed to when the client is one we have no mark for: an
# unrecognised client is still not the user, and saying it was would be the one
# mistake these marks must never make.
IS_MCP = False


def server_launch() -> dict:
    """How to launch this server, for the Claude Desktop config snippet.

    MCP needs the executable and its arguments separately -- a single string
    holding "python script.py" is not runnable. Frozen builds are the exe
    itself; a source checkout needs the interpreter plus the script path.
    """
    if getattr(sys, "frozen", False):
        return {"command": str(Path(sys.executable).resolve()), "args": []}
    # server_main.py, not this file: --mcp is routed there, and studio.py's own
    # argument parser rejects it outright. Pointing a client at the wrong one
    # produces a server that exits before it says hello.
    entry = Path(__file__).resolve().parent / "server_main.py"
    return {"command": str(Path(sys.executable).resolve()),
            "args": [str(entry)]}


# --------------------------------------------------------------------------
# AI clients
#
# The MCP server is this same program started with --mcp, in a process the AI
# client launches and owns. The app therefore cannot talk to it -- the only
# things the two halves share are the workspace folder and the client's config
# file. Both are enough: one says whether the connection exists, the other says
# what the model has been doing in there.
#
# Four clients, four config formats, and TOML twice over is not one format.
# Claude Desktop keeps JSON. OpenAI keeps TOML at ~/.codex/config.toml, which
# the ChatGPT desktop app, Codex CLI and the IDE extension all read, so
# configuring it once covers all three. Mistral Vibe keeps TOML too, at
# ~/.vibe/config.toml, but as an array of tables with the server's name inside
# each one rather than in its header, so it needs its own reader and writer.
# Hermes Agent keeps YAML, in its own home, shared by its desktop app, its TUI
# and its CLI.
#
# Le Chat is absent on purpose. Its custom connectors take an https URL to a
# remote MCP server, and this one is local and speaks stdio, so the only way to
# reach it would be to put the user's CVs on the public internet.
# --------------------------------------------------------------------------

MCP_KEY = "cv-studio"
ACTIVITY_FILE = ".cvstudio-mcp.json"
ACTIVITY_KEEP = 20

# Where a document came from, and who last wrote each of its fields. Local
# bookkeeping between the two halves, exactly like ACTIVITY_FILE: delete it and
# the app loses its marks, not your CV. Deliberately *not* in the YAML --
# comments there would fight edit_cv_fields' ruamel round-trip, pollute files
# the user owns, and put provenance one theme away from printing.
EDITS_FILE = ".cvstudio-edits.json"
EDITS_KEEP = 200            # per document, a backstop under the per-field rule
EDITS_MAX_AGE = 60 * 60 * 24 * 30
EDITS_VALUE_CHARS = 300     # how much of a before/after value is worth keeping


def _claude_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Claude" / "claude_desktop_config.json"


def _openai_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def ai_entry(client: str | None = None) -> dict:
    """The MCP server entry to install, in the shape both formats share.

    A client needs the executable and its arguments separately, which is also
    why the workspace is passed as an argument rather than assumed: one
    configured server serves exactly one workspace.

    `--client` is how the app tells the server which client it is about to be
    launched by. MCP does pass `clientInfo` down at initialize, and the server
    falls back to it, but this is the only answer available before the first
    handshake and the only one that survives a client reporting a name nobody
    here recognises. It is written because *this app* wrote the config, so it
    is knowledge rather than a guess.
    """
    launch = server_launch()
    args = [*launch["args"], "--mcp", "--workspace", str(WORKSPACE)]
    if client:
        args += ["--client", client]
    return {"command": launch["command"], "args": args}


# ---- JSON, the way Claude Desktop keeps it -------------------------------

def _json_read(path: Path) -> dict | None:
    raw = path.read_text(encoding="utf-8").strip()
    config = json.loads(raw) if raw else {}
    if not isinstance(config, dict):
        raise ValueError("the config file is not a JSON object")
    entry = (config.get("mcpServers") or {}).get(MCP_KEY)
    return entry if isinstance(entry, dict) else None


def _json_write(path: Path, entry: dict) -> None:
    raw = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    config = json.loads(raw) if raw else {}
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError('"mcpServers" is not a JSON object')
    servers[MCP_KEY] = entry
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _json_snippet(entry: dict) -> str:
    return json.dumps({"mcpServers": {MCP_KEY: entry}}, indent=2)


# ---- TOML, the way OpenAI keeps it ---------------------------------------

def _toml_head(line: str) -> str | None:
    """The table name on this line, if the line opens one."""
    s = line.strip()
    if not s.startswith("["):
        return None
    s = s[2:] if s.startswith("[[") else s[1:]
    end = s.find("]")
    if end < 0:
        return None
    return s[:end].strip().replace('"', "").replace("'", "")


def _toml_span(text: str, header: str) -> tuple[int, int] | None:
    """The line range of one table, from its header to the next one."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        name = _toml_head(line)
        if name is None:
            continue
        if start is None:
            if name == header:
                start = i
        else:
            return start, i
    return (start, len(lines)) if start is not None else None


def _toml_read(path: Path) -> dict | None:
    import tomllib
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entry = (data.get("mcp_servers") or {}).get(MCP_KEY)
    return entry if isinstance(entry, dict) else None


def _toml_snippet(entry: dict) -> str:
    # JSON string escaping is a subset of TOML's basic strings, which is what
    # matters on Windows, where every path is full of backslashes.
    args = ", ".join(json.dumps(a) for a in entry["args"])
    return (f"[mcp_servers.{MCP_KEY}]\n"
            f"command = {json.dumps(entry['command'])}\n"
            f"args = [{args}]\n")


def _toml_write(path: Path, entry: dict) -> None:
    """Rewrite our own table and nothing else.

    Reading the file into a dict and writing it back out would reformat it and
    drop every comment in it. This is a file people hand-edit, so the edit is
    made in the text: replace our table where it already is, or append one.
    """
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    table = _toml_snippet(entry)
    span = _toml_span(text, f"mcp_servers.{MCP_KEY}")
    if span:
        lines = text.splitlines(keepends=True)
        lines[span[0]:span[1]] = [table]
        out = "".join(lines)
    else:
        out = text
        if out and not out.endswith("\n"):
            out += "\n"
        if out.strip():
            out += "\n"
        out += table
    path.write_text(out, encoding="utf-8")


# ---- TOML again, the way Mistral Vibe keeps it ---------------------------
#
# Same language, different shape. Codex names each server in the table header,
# `[mcp_servers.cv-studio]`. Vibe keeps an array of tables, `[[mcp_servers]]`,
# with the name as a field inside, so ours cannot be found by its header: every
# entry shares one. It is found by reading the name out of each block.

def _vibe_config_path() -> Path:
    return Path.home() / ".vibe" / "config.toml"


def _vibe_read(path: Path) -> dict | None:
    """Ours, reduced to the shape every other reader returns.

    `name` and `transport` live inside the table here rather than in its
    header, but they are how the entry is addressed, not part of it. Returning
    them would mean this never compares equal to what ai_entry() asks for, and
    the read-back check in ai_connect would reject every write as wrong.
    """
    import tomllib
    servers = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers")
    if not isinstance(servers, list):
        return None
    for entry in servers:
        if isinstance(entry, dict) and entry.get("name") == MCP_KEY:
            return {"command": entry.get("command"), "args": entry.get("args")}
    return None


def _vibe_snippet(entry: dict) -> str:
    args = ", ".join(json.dumps(a) for a in entry["args"])
    return (f"[[mcp_servers]]\n"
            f"name = {json.dumps(MCP_KEY)}\n"
            f'transport = "stdio"\n'
            f"command = {json.dumps(entry['command'])}\n"
            f"args = [{args}]\n")


def _vibe_span(text: str) -> tuple[int, int] | None:
    """The line range of the `[[mcp_servers]]` block that is ours."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines + [""]):
        head = _toml_head(line) if i < len(lines) else ""
        if head is None:
            continue
        if start is not None:
            body = "".join(lines[start:i])
            if re.search(r'^\s*name\s*=\s*["\']' + re.escape(MCP_KEY) + r'["\']',
                         body, re.M):
                return start, i
            start = None
        if head == "mcp_servers":
            start = i
    return None


def _vibe_write(path: Path, entry: dict) -> None:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""

    # A fresh Vibe config ships `mcp_servers = []`. TOML will not accept that
    # key and `[[mcp_servers]]` tables in the same document, so the empty list
    # has to go or the file stops parsing.
    text, dropped = re.subn(
        r'^[ \t]*mcp_servers[ \t]*=[ \t]*\[[ \t]*\][ \t]*\r?\n?', "",
        text, flags=re.M)

    # Only ever the empty one. A populated `mcp_servers = [...]` is somebody
    # keeping their servers as an inline array, which cannot coexist with the
    # tables written below: deleting it would take their other servers with it,
    # and leaving it makes the file unparseable. Say so instead of doing either.
    if not dropped and re.search(r'^[ \t]*mcp_servers[ \t]*=', text, re.M):
        raise ValueError(
            "this config keeps its servers as an inline mcp_servers array, "
            "which cannot be added to without rewriting the rest of it. Add "
            "the entry by hand, or empty that array first")

    table = _vibe_snippet(entry)
    span = _vibe_span(text)
    if span:
        lines = text.splitlines(keepends=True)
        lines[span[0]:span[1]] = [table]
        out = "".join(lines)
    else:
        out = text
        if out and not out.endswith("\n"):
            out += "\n"
        if out.strip():
            out += "\n"
        out += table
    path.write_text(out, encoding="utf-8")


# ---- YAML, the way Hermes Agent keeps it ---------------------------------
#
# A mapping keyed by server name, like Claude Desktop's, but in YAML -- and one
# file for the desktop app, the TUI and the CLI alike, so configuring it once
# covers all three. ruamel is already here for the CVs, so this is the one
# client config the app can edit without flattening the comments and ordering
# around it: the entry is updated in place and everything else in the file is
# left exactly as it was.

def _hermes_config_path() -> Path:
    """config.yaml inside Hermes' home, resolved the way Hermes resolves it.

    HERMES_HOME wins -- it is also how a named profile is selected, as
    <root>/profiles/<name> -- then the platform default, which is not the same
    shape on Windows as elsewhere. Guessing ~/.hermes there would write a file
    Hermes never reads, and the app would then report itself connected.
    """
    home = (os.environ.get("HERMES_HOME") or "").strip()
    if home:
        base = Path(os.path.expandvars(os.path.expanduser(home)))
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or
                    Path.home() / "AppData" / "Local") / "hermes"
    else:
        base = Path.home() / ".hermes"
    return base / "config.yaml"


def _yaml_servers(data):
    """The mcp_servers mapping, complaining rather than guessing."""
    if not hasattr(data, "get"):
        raise ValueError("the config file is not a YAML mapping")
    servers = data.get("mcp_servers")
    if servers is None:
        return None
    if not hasattr(servers, "get"):
        raise ValueError('"mcp_servers" is not a YAML mapping')
    return servers


def _yaml_read(path: Path) -> dict | None:
    """Ours, reduced to the shape every other reader returns.

    Hermes takes a dozen optional keys per server. They belong to whoever set
    them, not to this app, so they are not reported: ai_connect compares what
    it reads against what ai_entry() asks for, and an entry carrying a
    `timeout` the user added would never compare equal, so every write would be
    read back as wrong and rolled back.

    `enabled: false` is the exception, and it is reported. A disabled server is
    one Hermes will not start, so an app that called that connected would be
    lying; surfacing it makes the entry differ from what is wanted, which is
    what sends it to _yaml_write to be turned back on.
    """
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    if data is None:
        return None
    servers = _yaml_servers(data)
    entry = servers.get(MCP_KEY) if servers is not None else None
    if not hasattr(entry, "get"):
        return None
    ours = {"command": to_plain(entry.get("command")),
            "args": to_plain(entry.get("args"))}
    if entry.get("enabled") is False:
        ours["enabled"] = False
    return ours


def _yaml_write(path: Path, entry: dict) -> None:
    """Set our command and args, and touch nothing else.

    Hermes takes a dozen optional keys per server -- timeouts, tool filters,
    whether to start it lazily. Replacing the whole entry would throw away
    whatever the user had set there, so only the two fields this app owns are
    written.

    `enabled: false` is the one other key touched, and only when it is already
    there and false. Pressing Set up on a server that is switched off and
    leaving it switched off is not setting it up.
    """
    import io as _io

    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    data = yaml_rt.load(text) if text.strip() else None
    if data is None:
        data = {}
    servers = _yaml_servers(data)
    if servers is None:
        data["mcp_servers"] = {}
        servers = data["mcp_servers"]
    ours = servers.get(MCP_KEY)
    if not hasattr(ours, "get"):
        ours = {}
        servers[MCP_KEY] = ours
    ours["command"] = entry["command"]
    ours["args"] = list(entry["args"])
    if ours.get("enabled") is False:
        ours["enabled"] = True
    buf = _io.StringIO()
    yaml_rt.dump(data, buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8")


def _yaml_snippet(entry: dict) -> str:
    import io as _io

    buf = _io.StringIO()
    yaml_rt.dump({"mcp_servers": {MCP_KEY: {
        "command": entry["command"], "args": list(entry["args"])}}}, buf)
    return buf.getvalue().rstrip("\n")


AI_CLIENTS = {
    "claude": {
        "label": "Claude Desktop",
        "path": _claude_config_path,
        "read": _json_read, "write": _json_write, "snippet": _json_snippet,
        "restart": "Restart Claude Desktop, and the tools appear under the "
                   "connectors icon.",
        "manual": "Claude Desktop → Settings → Developer → Edit config.",
    },
    "openai": {
        "label": "ChatGPT / Codex",
        "path": _openai_config_path,
        "read": _toml_read, "write": _toml_write, "snippet": _toml_snippet,
        "restart": "Restart the ChatGPT app, or start a new Codex session.",
        "manual": "The ChatGPT app, Codex CLI and the Codex IDE extension "
                  "share this file, so this configures all three.",
    },
    "hermes": {
        "label": "Hermes Agent",
        "path": _hermes_config_path,
        "read": _yaml_read, "write": _yaml_write, "snippet": _yaml_snippet,
        "restart": "Start Hermes, or run /reload-mcp in a session that is "
                   "already open, and the tools appear in the registry.",
        "manual": "Hermes Desktop, the TUI and the CLI all read one config, "
                  "so this configures all three. It lives in Hermes' home: "
                  "HERMES_HOME if you have set one, otherwise ~/.hermes, or "
                  "%LOCALAPPDATA%\\hermes on Windows.",
    },
    "mistral": {
        "label": "Mistral Vibe",
        "path": _vibe_config_path,
        "read": _vibe_read, "write": _vibe_write, "snippet": _vibe_snippet,
        "restart": "Start a new Vibe session and the tools are there.",
        "manual": "Vibe keeps an array of tables, so this adds one entry "
                  "rather than a named table. Le Chat cannot be set up here: "
                  "its custom connectors take an https URL to a remote server, "
                  "and this one is local and speaks stdio.",
    },
}


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
            os.path.realpath(b))
    except OSError:
        return os.path.normcase(a) == os.path.normcase(b)


def ai_config_path(client: str) -> Path:
    """Where a client keeps its MCP server list.

    The override exists so a dev server, or a test, can be pointed at a copy:
    connecting writes into a file the user has other servers in, and that is not
    something to practise on.
    """
    override = os.environ.get(f"CVSTUDIO_{client.upper()}_CONFIG")
    return Path(override) if override else AI_CLIENTS[client]["path"]()


def ai_status(client: str, seen: dict | None = None) -> dict:
    """Whether a client is pointed at this build and this workspace.

    An entry that exists but names a different copy of CV Studio, or a
    different workspace, is its own answer: reporting that as "connected" is how
    someone ends up editing one set of CVs and looking at another.
    """
    spec = AI_CLIENTS[client]
    path = ai_config_path(client)
    want = ai_entry(client)
    if seen is None:
        seen = mcp_activity().get("seen") or {}
    heard = seen.get(client) or {}
    out = {"id": client, "label": spec["label"], "config_path": str(path),
           "config_exists": path.is_file(), "state": "absent",
           "workspace": None, "command": None, "error": None,
           "last_seen": heard.get("at"), "agent": heard.get("agent"),
           "restart": spec["restart"], "manual": spec["manual"],
           "snippet": spec["snippet"](want)}
    if not out["config_exists"]:
        return out
    try:
        entry = spec["read"](path)
    except Exception as exc:
        out["state"], out["error"] = "unreadable", str(exc)
        return out
    if not entry or not entry.get("command"):
        return out

    args = [str(a) for a in (entry.get("args") or [])]
    workspace = (args[args.index("--workspace") + 1]
                 if "--workspace" in args and args[-1] != "--workspace"
                 else str(DEFAULT_WORKSPACE))
    out["command"] = str(entry["command"])
    out["workspace"] = workspace
    if not _same_file(out["command"], want["command"]):
        out["state"] = "elsewhere"
    elif not _same_file(workspace, str(WORKSPACE)):
        out["state"] = "other-workspace"
    else:
        out["state"] = "connected"
    return out


def ai_clients() -> list[dict]:
    seen = mcp_activity().get("seen") or {}
    return [ai_status(client, seen) for client in AI_CLIENTS]


def ai_connect(client: str) -> dict:
    """Add this workspace to a client's config, leaving the rest alone.

    People keep other servers in these files, so each is read, merged and
    written back rather than replaced -- and backed up first. A file that is
    already broken is refused rather than rewritten: overwriting it would
    silently throw away whatever else was configured. And because the TOML edit
    is made in the text, the result is read back and checked before it is
    allowed to stand.
    """
    if client not in AI_CLIENTS:
        raise ValueError(f"unknown client: {client}")
    spec = AI_CLIENTS[client]
    path = ai_config_path(client)
    want = ai_entry(client)

    before, backup = None, None
    if path.is_file():
        try:
            before = spec["read"](path)
        except Exception as exc:
            raise ValueError(
                f"{spec['label']}'s config file cannot be read, so writing to "
                "it would lose whatever else is configured there. Fix it "
                f"first: {path} ({exc})")
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
    if before == want:
        return {"ok": True, "action": "unchanged", **ai_status(client)}

    path.parent.mkdir(parents=True, exist_ok=True)
    spec["write"](path, want)

    try:
        wrote = spec["read"](path)
    except Exception:
        wrote = None
    if wrote != want:
        if backup is not None:
            shutil.copy2(backup, path)
        raise ValueError(
            f"Writing to {path} did not produce the entry it should have, so "
            "the file has been put back as it was. Add it by hand instead.")
    return {"ok": True, "action": "updated" if before else "added",
            **ai_status(client)}


# --------------------------------------------------------------------------
# Skills
#
# Claude Code reads skills off the filesystem; Claude Desktop does not -- there
# they are uploaded to the account as a zip and synced back down. So the most
# this app can honestly do is hand over archives that are ready to upload.
#
# The split that matters: a skill of pure judgement travels as it is, while one
# that shells out to a local script cannot work in Desktop's sandbox at all.
# Those get an appended note pointing at the MCP tools, which are how the same
# work gets done over there.
# --------------------------------------------------------------------------

SKILLS_DIR = Path.home() / ".claude" / "skills"
SKILL_PREFIX = "cv-studio"
SKILL_JUNK = ("__pycache__", ".pyc", ".pyo", ".DS_Store")

DESKTOP_NOTE = """

---

## Running inside the Claude Desktop app

This copy was packaged by CV Studio. In the desktop app a skill has no local
filesystem, no Python and no localhost, so any command above that runs a script
or opens `127.0.0.1` cannot work here. **Use the `cv-studio` MCP tools instead**
-- they do the same work in the user's real workspace:

| Instead of | Use |
|---|---|
| running a render script | `render_cv` -- renders and returns the page as an image to look at |
| reading or writing a YAML file | `read_cv`, `edit_cv_fields` (keeps comments), `write_cv` |
| creating or duplicating a document | `create_cv` |
| listing the workspace | `list_cvs`, `workspace_info` |
| checking available themes or fonts | `design_options` |

If those tools are not present, say so rather than guessing: the user needs to
connect CV Studio under Settings, Developer, Edit config.
"""


def _front_matter(text: str) -> dict:
    """name and description out of a SKILL.md header."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out, key = {}, None
    for line in text[3:end].splitlines():
        m = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if m:
            key = m.group(1)
            out[key] = m.group(2).strip().lstrip(">|").strip()
        elif key and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


# A skill that runs something local cannot do that inside Desktop's sandbox.
LOCAL_DEP = re.compile(r"127\.0\.0\.1|localhost|~/\.claude/skills|python .*scripts/|uv run")


def skill_folders() -> list[Path]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(d for d in SKILLS_DIR.iterdir()
                  if d.is_dir() and d.name.startswith(SKILL_PREFIX)
                  and (d / "SKILL.md").is_file())


def skills_list() -> dict:
    out = []
    for d in skill_folders():
        text = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        fm = _front_matter(text)
        zipped = WORKSPACE / "assets" / "skills" / f"{d.name}.zip"
        out.append({
            "name": fm.get("name") or d.name,
            "description": (fm.get("description") or "")[:220],
            "needs_mcp": bool(LOCAL_DEP.search(text)),
            "packaged": zipped.is_file(),
            "path": rel(zipped) if zipped.is_file() else None,
        })
    return {"skills": out, "source": str(SKILLS_DIR),
            "out_dir": rel(WORKSPACE / "assets" / "skills")}


def package_skills() -> dict:
    """Write one upload-ready zip per skill into the workspace."""
    folders = skill_folders()
    if not folders:
        raise ValueError(
            f"No CV Studio skills found in {SKILLS_DIR}. They ship with the "
            "Claude Code setup; there is nothing to package without them.")
    dest = WORKSPACE / "assets" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    made = []
    for d in folders:
        text = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        needs = bool(LOCAL_DEP.search(text))
        target = dest / f"{d.name}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(d.rglob("*")):
                if not f.is_file() or any(j in str(f) for j in SKILL_JUNK):
                    continue
                arc = Path(d.name) / f.relative_to(d)
                if f.name == "SKILL.md" and needs:
                    z.writestr(str(arc).replace("\\", "/"), text + DESKTOP_NOTE)
                else:
                    z.write(f, str(arc).replace("\\", "/"))
        made.append({"name": d.name, "path": rel(target), "needs_mcp": needs,
                     "kb": round(target.stat().st_size / 1024, 1)})
    return {"ok": True, "dir": rel(dest), "skills": made}


def note_mcp_activity(tool: str, path: str | None = None, ok: bool = True,
                      error: str | None = None) -> None:
    """Record what the AI just did, so the app can say so.

    Called from the MCP process, read by the app's. Bookkeeping must never be
    the thing that breaks a tool call, so every failure here is swallowed.

    `by` and `agent` are what let the app name the client rather than saying
    "an AI client". Until they existed the app could only name one when exactly
    one was configured, which stopped being true the moment anybody set up two.
    """
    try:
        f = WORKSPACE / ACTIVITY_FILE
        try:
            log = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log = []
        if not isinstance(log, list):
            log = []
        entry = {"tool": tool, "path": path, "at": time.time()}
        entry["by"] = CLIENT_ID or "ai"
        if CLIENT_AGENT:
            entry["agent"] = CLIENT_AGENT
        if not ok:
            entry["ok"] = False
            entry["error"] = error
        log.append(entry)
        f.write_text(json.dumps(log[-ACTIVITY_KEEP:]), encoding="utf-8")
    except OSError:
        pass


def mcp_activity() -> dict:
    """The recent tool calls, plus when each client was last heard from.

    `seen` is the only honest evidence that a client is actually wired up:
    ai_status() reads a config file, which says a client has been *told* where
    the server is, not that it ever started it. A tool call is the handshake
    having happened.
    """
    try:
        log = json.loads((WORKSPACE / ACTIVITY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log = []
    if not isinstance(log, list) or not log:
        return {"last": None, "recent": [], "seen": {}}
    seen: dict[str, dict] = {}
    for entry in log:
        who = entry.get("by")
        if who:
            seen[who] = {"at": entry.get("at"), "agent": entry.get("agent"),
                         "tool": entry.get("tool")}
    return {"last": log[-1], "recent": log[-8:][::-1], "seen": seen}



# --------------------------------------------------------------------------
# Provenance
#
# Two different questions, which look the same on screen and are not:
#
#   "is this line different from the base CV?"  -- computed live from the two
#       documents, correct no matter who changed it or when, and needs nothing
#       stored but a pointer to the base.
#   "who last wrote this line?"                 -- history, and nothing but a
#       record of the writes can answer it.
#
# The second is the one that needs care. A field address like
# ["cv","sections","experience",2,"highlights",0] is positional, so it is wrong
# the moment an entry is inserted above it. Every edit therefore also stores an
# anchor (section, entry title, key) and a hash of the value it wrote. On the
# way back out, an edit is kept only if it can be re-found *and* the value is
# still the one it wrote. Anything else is dropped, because a mark on the wrong
# line is worse than no mark at all.
# --------------------------------------------------------------------------


def _edits_read() -> dict:
    try:
        data = json.loads((WORKSPACE / EDITS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "docs": {}}
    if not isinstance(data, dict) or not isinstance(data.get("docs"), dict):
        return {"version": 1, "docs": {}}
    return data


def _edits_write(data: dict) -> None:
    """Bookkeeping must never be what breaks a save, so failures are swallowed.

    Both halves write this file, so two saves landing together can lose one
    side's entry to the read-modify-write. The cost of that is a missing mark,
    not missing work, which is why it is not worth a lock file: the documents
    themselves are the record, and this only annotates them.
    """
    try:
        (WORKSPACE / EDITS_FILE).write_text(
            json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


def _clip(value) -> object:
    """A value small enough to keep a few hundred of without thinking about it."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = value if isinstance(value, str) else json.dumps(value)
    else:
        text = json.dumps(value, separators=(",", ":"), default=str)
    return text if len(text) <= EDITS_VALUE_CHARS else text[:EDITS_VALUE_CHARS] + "\u2026"


def _hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def field_key(path: list) -> str:
    return ".".join(str(k) for k in path)


def get_at(data, path: list):
    node = data
    for k in path:
        try:
            node = node[int(k)] if isinstance(node, list) else node[k]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return node


def entry_title(entry, i: int) -> str:
    """What the outline calls an entry. Kept in step with entryTitle() in the UI.

    Every RenderCV entry type keeps its headline under a different key, so this
    is a preference order rather than one lookup.
    """
    if not isinstance(entry, dict):
        return " ".join(str(entry or "").split()[:4]) or f"entry {i + 1}"
    for k in ("company", "institution", "name", "title", "label", "position",
              "bullet"):
        if entry.get(k):
            return str(entry[k])
    return f"entry {i + 1}"


def _anchor(path: list, data: dict) -> dict | None:
    """Enough to re-find a field after the entries around it have moved."""
    if len(path) < 4 or path[0] != "cv" or path[1] != "sections":
        return None
    name = path[2]
    try:
        i = int(path[3])
    except (TypeError, ValueError):
        return None
    entry = get_at(data, ["cv", "sections", name, i])
    out = {"section": name, "entry": entry_title(entry, i), "i": i}
    if len(path) > 4:
        out["key"] = path[4]
        if len(path) > 5:
            out["at"] = path[5]
    return out


def _refind(edit: dict, data: dict) -> list | None:
    """Where this edit's field lives now, or None if it cannot be trusted.

    The recorded path first, since nothing has usually moved. Failing that the
    anchor, which survives a reorder. Either way the value has to still hash to
    what was written, otherwise somebody has edited it since and the edit is
    no longer the last word on that field.
    """
    want = edit.get("to_hash")
    path = edit.get("field") or []
    if want and _hash(get_at(data, path)) == want:
        return path
    anchor = edit.get("anchor")
    if not anchor or not want:
        return None
    section = get_at(data, ["cv", "sections", anchor.get("section")])
    if not isinstance(section, list):
        return None
    for i, entry in enumerate(section):
        if entry_title(entry, i) != anchor.get("entry"):
            continue
        found = ["cv", "sections", anchor["section"], i]
        if anchor.get("key") is not None:
            found.append(anchor["key"])
        if anchor.get("at") is not None:
            found.append(anchor["at"])
        if _hash(get_at(data, found)) == want:
            return found
    return None


def changed_fields(before, after, prefix: list | None = None) -> list[list]:
    """The leaf paths at which two parsed documents differ.

    A list is compared whole rather than walked: a bullet list that gained an
    item has shifted every index after it, and reporting that as five changed
    bullets would be true and useless.
    """
    out: list[list] = []
    prefix = prefix or []
    keys = list(dict.fromkeys(list((before or {}).keys()) + list((after or {}).keys())))
    for k in keys:
        a = (before or {}).get(k)
        b = (after or {}).get(k)
        here = prefix + [k]
        if isinstance(a, dict) and isinstance(b, dict):
            out += changed_fields(a, b, here)
        elif isinstance(a, list) and isinstance(b, list):
            for i in range(max(len(a), len(b))):
                av = a[i] if i < len(a) else None
                bv = b[i] if i < len(b) else None
                if isinstance(av, dict) and isinstance(bv, dict):
                    out += changed_fields(av, bv, here + [i])
                elif av != bv:
                    out.append(here + [i])
        elif a != b:
            out.append(here)
    return out


def record_edits(path: Path, before: dict | None, after: dict | None,
                 tool: str, by: str | None = None,
                 agent: str | None = None) -> list[list]:
    """Note who changed which fields. Returns the paths, for the caller to report.

    `by` defaults to whoever this process is: an AI client when the MCP server
    is running, and the user otherwise. That is the whole distinction the marks
    are drawing.
    """
    fields = changed_fields((before or {}).get("cv"), (after or {}).get("cv"),
                            ["cv"])
    if not fields:
        return []
    who = by or CLIENT_ID or ("ai" if IS_MCP else "you")
    now = time.time()
    data = _edits_read()
    doc = data["docs"].setdefault(rel(path), {})
    log = doc.get("edits")
    if not isinstance(log, list):
        log = []
    for field in fields:
        value = get_at(after, field)
        log.append({
            "at": now, "by": who, "tool": tool,
            **({"agent": agent or CLIENT_AGENT} if (agent or CLIENT_AGENT) else {}),
            "field": field,
            **({"anchor": _anchor(field, after)} if _anchor(field, after) else {}),
            "from": _clip(get_at(before, field)),
            "to": _clip(value),
            "to_hash": _hash(value),
        })
    # Only the newest edit per field is ever shown, so only the newest is kept.
    # Without this a file saved fifty times carries fifty records of the same
    # headline, and the sidecar grows with how often you work rather than with
    # how much there is to say. What it costs is the value a field held two
    # edits ago, which nothing asks for.
    cutoff = now - EDITS_MAX_AGE
    newest: dict[str, dict] = {}
    for edit in log:
        if (edit.get("at") or 0) < cutoff:
            continue
        key = field_key(edit.get("field") or [])
        held = newest.get(key)
        if not held or (edit.get("at") or 0) >= (held.get("at") or 0):
            # Keep the value this field held before anyone started editing it,
            # rather than before the most recent keystroke: "was: Your Role" is
            # the useful answer, "was: Solutions Enginee" is not.
            if held:
                edit = {**edit, "from": held.get("from")}
            newest[key] = edit
    doc["edits"] = sorted(newest.values(),
                          key=lambda e: e.get("at") or 0)[-EDITS_KEEP:]
    _edits_write(data)
    return fields


def _forget_deleted(data: dict) -> bool:
    """Drop documents that are no longer in the workspace. Returns True if any went.

    Otherwise a workspace churned through fifty tailored copies keeps the marks
    for all fifty, and the file grows with everything you have ever deleted.
    """
    gone = [p for p in data["docs"] if not (WORKSPACE / p).is_file()]
    for p in gone:
        del data["docs"][p]
    return bool(gone)


def backup_dir() -> Path:
    return cjkfonts.cache_dir().parent


def start_backups() -> None:
    """The day's backup of the workspace you work in, checked each hour.
    Never of sample data, which is made fresh every time anyway."""
    def loop() -> None:
        time.sleep(20)                  # out of the way of the first render
        while True:
            try:
                if not in_sample() and WORKSPACE.is_dir() and backups.due(backup_dir(), WORKSPACE):
                    backups.make(backup_dir(), WORKSPACE)
            except Exception as exc:    # a failed backup must never take the app down
                print(f"backup failed: {exc}", file=sys.stderr)
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True).start()


def _rewrite_refs(old: str, new: str | None) -> None:
    """Point everything that named `old` at `new` (or at nothing): the
    bookkeeping beside the documents, the applications, and the letters that
    say which CV they look like."""
    data = _edits_read()
    docs = data.get("docs") or {}
    entry = docs.pop(old, None)
    if entry is not None and new:
        docs[new] = entry

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return new if x == old else x
    _edits_write(walk(data))
    if jobstore is not None:
        for j in jobstore.list_jobs(WORKSPACE):
            for key in ("cv_path", "letter_path"):
                if j.get(key) == old:
                    jobstore.update_job(WORKSPACE, j["id"], {key: new})
    for f in (WORKSPACE / "letters").glob("*" + letters.EXT):
        try:
            meta, body = letters.parse(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if any(v == old for v in meta.values()):
            meta = {k: (new if v == old else v) for k, v in meta.items()}
            f.write_text(letters.dump(meta, body), encoding="utf-8")


def _clean_name(raw: str) -> str:
    return "".join(c for c in (raw or "") if c.isalnum() or c in "-_ ").strip()


def rename_document(path: str, name: str) -> dict:
    """Rename a CV or a letter where it is, keeping a translation's language
    suffix (my-cv.fr.yaml stays .fr), and repoint everything that named it."""
    src = safe_path(path)
    if not src.is_file():
        raise FileNotFoundError(path)
    clean = _clean_name(name)
    if not clean:
        raise ValueError("Please give it a name.")
    ext = src.suffix
    stem = src.name[: -len(ext)] if ext else src.name
    link = ((_edits_read().get("docs") or {}).get(rel(src)) or {}).get("translation") or {}
    suffix = ""
    m = re.match(r"^.+(\.[a-z]{2}(?:-[a-z]{2})?)$", stem, re.I)
    if link and m:
        suffix = m.group(1)
    dest = src.with_name(clean + suffix + ext)
    if dest == src:
        return {"ok": True, "path": rel(src)}
    if dest.exists():
        raise ValueError(f"Something called {dest.name} already exists.")
    src.rename(dest)
    _rewrite_refs(rel(src), rel(dest))
    return {"ok": True, "path": rel(dest), "was": rel(src)}


def delete_document(path: str) -> dict:
    """Move a CV or a letter to the workspace's .trash folder and detach it
    from its application. The base CV, and a document other languages are
    translated from, are refused: both would leave something pointing at
    nothing."""
    src = safe_path(path)
    if not src.is_file():
        raise FileNotFoundError(path)
    rp = rel(src)
    base = base_cv()
    if base and base.get("path") == rp:
        raise ValueError("This is the base CV, which every tailored CV is copied from. "
                         "Choose another base first, on Documents.")
    kids = [d for d in list_documents() if d.get("translation_of") == rp]
    if kids:
        raise ValueError("Other languages are translated from it ("
                         + ", ".join(sorted(str(d.get("lang")) for d in kids))
                         + "). Delete those first.")
    trash = WORKSPACE / ".trash"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{src.name}"
    shutil.move(str(src), str(dest))
    _rewrite_refs(rp, None)
    return {"ok": True, "trashed": dest.relative_to(WORKSPACE).as_posix()}


def note_lineage(path: Path, base: str | None) -> None:
    """Remember which document this one was copied from.

    Duplicating is how a CV gets tailored, and until this was stored the copy
    had no idea what it was a copy *of* -- so "what did this change from the
    base" was unanswerable for both halves of the app.
    """
    if not base:
        return
    data = _edits_read()
    _forget_deleted(data)
    doc = data["docs"].setdefault(rel(path), {})
    doc["base"] = {"path": base, "at": time.time()}
    _edits_write(data)


def base_cv() -> dict | None:
    """The CV every tailored copy starts from, as {path, at, missing}.

    Kept in the workspace beside the edits rather than in the browser's
    preferences, because it is a fact about this set of documents and not about
    this machine -- and because a model working in here has to be able to read
    it. Which document you tailor from is the first thing it needs to know.

    Distinct from the per-document `base` that note_lineage records. That one
    says what a particular copy came from and stays true even when this moves;
    this one only says where the next copy should come from.

    Pure: a missing file is reported, never quietly repointed or cleared.
    """
    chosen = _edits_read().get("base") or {}
    if chosen.get("path"):
        return {"path": chosen["path"], "at": chosen.get("at"),
                "missing": not safe_path(chosen["path"]).exists()}
    # Nothing chosen. One CV is not a choice, it is the answer -- but two are,
    # and picking for you would put a document you never nominated at the top
    # of the screen and copy every tailored CV from it.
    mine = [d for d in list_documents() if d["group"] == "My CVs"]
    if len(mine) == 1:
        return {"path": mine[0]["path"], "at": None, "missing": False}
    return None


def set_base_cv(path: str | None) -> None:
    """Nominate a document as the base, or clear the nomination with None."""
    data = _edits_read()
    if path is None:
        data.pop("base", None)
    else:
        target = safe_path(path)
        if not target.exists():
            raise FileNotFoundError(path)
        if not is_cv_yaml(target):
            raise ValueError(f"{path} is not a CV.")
        if rel(target).startswith("letters/"):
            raise ValueError("A cover letter cannot be the base CV.")
        data["base"] = {"path": rel(target), "at": time.time()}
    _edits_write(data)
    # _edits_write swallows write failures on purpose -- a lost mark is not
    # worth failing a save over. A choice the user just made is different: it
    # would come back as the old base with nothing said, which is the kind of
    # bug nobody ever diagnoses. So read it back and complain.
    if (_edits_read().get("base") or {}).get("path") != (data.get("base") or {}).get("path"):
        raise OSError(f"Could not record the base CV in {EDITS_FILE}.")


def provenance(path: Path, data: dict | None) -> dict:
    """Who last wrote each field of this document, and how it differs from its base.

    `fields` is keyed by a dotted path so the UI can look one up without
    walking. Only the newest surviving edit per field is returned: the rest are
    history nobody is asking to see.
    """
    # `last` is the newest edit by anyone; `last_ai` the newest by anyone who
    # is not the user. They differ the moment you touch a document a model
    # worked on, and it is the second that the summary wants: "Claude, an hour
    # ago" stays true and stays interesting after you have typed since.
    out = {"fields": {}, "base": None, "from_base": [],
           "last": None, "last_ai": None}
    if not data:
        return out
    doc = (_edits_read()["docs"].get(rel(path)) or {})

    for edit in reversed(doc.get("edits") or []):
        found = _refind(edit, data)
        if found is None:
            continue
        key = field_key(found)
        if key in out["fields"]:
            continue
        out["fields"][key] = {"by": edit.get("by"), "at": edit.get("at"),
                              "agent": edit.get("agent"), "tool": edit.get("tool"),
                              "from": edit.get("from")}
        who = {"by": edit.get("by"), "at": edit.get("at"),
               "agent": edit.get("agent")}
        for slot in ("last", "last_ai"):
            if slot == "last_ai" and edit.get("by") == "you":
                continue
            held = out[slot]
            if not held or (edit.get("at") or 0) > (held.get("at") or 0):
                out[slot] = who

    base = doc.get("base") or {}
    base_path = base.get("path")
    if base_path:
        out["base"] = {"path": base_path, "at": base.get("at"), "missing": True}
        try:
            other = yaml_rt.load(safe_path(base_path).read_text(encoding="utf-8"))
            out["base"]["missing"] = False
            out["from_base"] = [field_key(f) for f in changed_fields(
                to_plain(other).get("cv"), data.get("cv"), ["cv"])]
        except Exception:
            # A base that has been renamed or deleted is a missing comparison,
            # not a broken document. Say so and show the rest.
            pass
    return out


def _where_changed(path: list, mine: dict, theirs: dict) -> str:
    """Where a changed field is, in the words the outline uses."""
    if path[:2] != ["cv", "sections"]:
        return "Header \u00b7 " + " \u203a ".join(str(k) for k in path[1:])
    bits = [str(path[2]).replace("_", " ").capitalize()] if len(path) > 2 else []
    if len(path) > 3:
        i = int(path[3])
        entry = get_at(mine, path[:4])
        if entry is None:
            entry = get_at(theirs, path[:4])
        if isinstance(entry, dict):
            bits.append(entry_title(entry, i))
    rest = path[4:]
    if rest:
        key = str(rest[0])
        if len(rest) > 1 and key == "highlights":
            bits.append(f"bullet {int(rest[1]) + 1}")
        else:
            bits.append(key.replace("_", " "))
    return " \u203a ".join(bits)


def _brief(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return entry_title(v, 0)
    if isinstance(v, list):
        return f"{len(v)} item{'' if len(v) == 1 else 's'}"
    text = " ".join(str(v).split())
    return text if len(text) <= 160 else text[:157] + "\u2026"


def base_diff(path: Path) -> dict:
    """What a tailored CV changed from the one it was copied from, line by line.

    Against the copy's own recorded base rather than today's base CV: a CV
    written for one job should keep saying what it changed from what it was
    made from, even after another document becomes the base.
    """
    doc = (_edits_read()["docs"].get(rel(path)) or {})
    base_path = (doc.get("base") or {}).get("path")
    out = {"base": base_path, "missing": False, "changes": [], "design": []}
    if not base_path:
        return out
    try:
        mine = to_plain(yaml_rt.load(path.read_text(encoding="utf-8"))) or {}
        theirs = to_plain(yaml_rt.load(
            safe_path(base_path).read_text(encoding="utf-8"))) or {}
    except (OSError, ValueError):
        out["missing"] = True
        return out
    except Exception:
        # Unparseable mid-edit: say nothing rather than something wrong.
        return out
    for f in changed_fields(theirs.get("cv"), mine.get("cv"), ["cv"]):
        before, after = get_at(theirs, f), get_at(mine, f)
        out["changes"].append({
            "key": field_key(f), "where": _where_changed(f, mine, theirs),
            "kind": "added" if before is None else "removed" if after is None
                    else "changed",
            "before": _brief(before), "after": _brief(after)})
    out["design"] = sorted({str(f[1]) for f in changed_fields(
        theirs.get("design"), mine.get("design"), ["design"]) if len(f) > 1})
    return out


# --------------------------------------------------------------------------
# Languages
#
# A CV's language is RenderCV's `locale.language`. A translation is a second
# document, saved beside its source as <stem>.<code>.yaml and linked to it in
# the sidecar with a copy of what the source said when it was translated. That
# copy is what lets the translation say what it is missing when the source
# moves on: the source now against the source then, never a guess about the
# translated text itself. CV Studio writes the scaffold -- the locale, the
# section titles, everything untouched that should stay untouched -- and the
# prose is translated by the user or by their AI client, through MCP.
# --------------------------------------------------------------------------


def translation_link(path: Path) -> dict | None:
    """What this document is a translation of, as recorded, or None."""
    t = ((_edits_read()["docs"].get(rel(path)) or {}).get("translation")) or None
    return t if t and t.get("of") else None


def translations_of(path: Path) -> list[dict]:
    """Every translation of this document, as [{path, lang}]."""
    me = rel(path)
    out = []
    for p, doc in _edits_read()["docs"].items():
        t = doc.get("translation") or {}
        if t.get("of") == me and safe_path(p).exists():
            out.append({"path": p, "lang": t.get("lang")})
    return sorted(out, key=lambda t: t["path"])


def language_family(path: Path) -> dict:
    """This document, its source if it is a translation, and the source's other
    translations: the set a language switch moves between."""
    link = translation_link(path)
    root = safe_path(link["of"]) if link else path
    members = [{"path": rel(root), "lang": _doc_lang(root), "source": True}]
    members += [{**t, "source": False} for t in translations_of(root)]
    return {"source": rel(root), "members": members if len(members) > 1 or link
            else members}


def _doc_lang(path: Path) -> str:
    try:
        return languages.file_language(path.read_text(encoding="utf-8"))
    except OSError:
        return "en"


def add_language(path: Path, code: str) -> dict:
    """Write the translation scaffold of `path` in language `code`.

    The copy is the source with its locale set, its section titles translated
    where CV Studio knows them, and nothing else changed: names, contact
    details, links, dates, company names and the design stay exactly as they
    are. The text is left in the source language for the user, or their AI
    client, to translate. Returns what was written and what is left to do.
    """
    if code not in languages.LANGS:
        raise ValueError(f"CV Studio cannot print a CV in {code!r}.")
    link = translation_link(path)
    if link:  # translate from the source, not from another translation
        path = safe_path(link["of"])
    src_lang = _doc_lang(path)
    if code == src_lang:
        raise ValueError(f"{rel(path)} is already in {languages.LANGS[code][2]}.")
    for t in translations_of(path):
        if t["lang"] == code:
            raise ValueError(f"{rel(path)} already has a {languages.LANGS[code][2]} "
                             f"version: {t['path']}.")
    stem = re.sub(r"\.[a-z]{2}$", "", path.stem)
    dest = path.with_name(f"{stem}.{code}{path.suffix}")
    if dest.exists():
        raise ValueError(f"{rel(dest)} already exists.")
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    cv = data.get("cv") or {}
    renamed, kept = {}, []
    secs = cv.get("sections")
    if isinstance(secs, dict):
        fresh = CommentedMap()
        for key, val in secs.items():
            title = languages.section_title(str(key), code)
            if title and title != key:
                renamed[str(key)] = title
                fresh[title] = val
            else:
                if not title:
                    kept.append(str(key))
                fresh[key] = val
        cv["sections"] = fresh
    loc = CommentedMap()
    loc["language"] = languages.LANGS[code][0]
    if code in languages.PRESENT:
        loc["present"] = languages.PRESENT[code]
    data["locale"] = loc
    import io
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    dest.write_text(buf.getvalue(), encoding="utf-8")
    source_cv = to_plain(yaml_rt.load(path.read_text(encoding="utf-8"))).get("cv")
    record = _edits_read()
    record["docs"].setdefault(rel(dest), {})["translation"] = {
        "of": rel(path), "lang": code, "from_lang": src_lang, "at": time.time(),
        "keys": renamed, "synced": source_cv, "synced_at": time.time()}
    _edits_write(record)
    out = {"path": rel(dest), "of": rel(path), "lang": code, "from_lang": src_lang,
           "sections_titled": renamed, "sections_to_title": kept}
    # Fetched now rather than at the first render, so it is usually there by
    # the time the copy is translated.
    if cjkfonts.fetch([code]):
        _, name, mb = cjkfonts.CUTS[code]
        out["font"] = {"language": name, "mb": mb}
    return out


def set_cv_language(path: Path, code: str) -> dict:
    """Print a CV in another language: its locale, and nothing else.

    What Settings writes when the app is used in one language. Dates, month
    names and "present" follow; the text stays as written.
    """
    if code not in languages.LANGS:
        raise ValueError(f"CV Studio cannot print a CV in {code!r}.")
    import io
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    loc = data.get("locale")
    if not isinstance(loc, dict):
        loc = CommentedMap()
        data["locale"] = loc
    loc["language"] = languages.LANGS[code][0]
    if code in languages.PRESENT:
        loc["present"] = languages.PRESENT[code]
    else:
        # Left behind from another language it would print in that one, and
        # empty RenderCV refuses the file.
        loc.pop("present", None)
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")
    return {"path": rel(path), "lang": code}


def _map_to_translation(path_: list, keys: dict) -> list:
    """A field's path in the source, as the same field's path in a
    translation whose section keys were renamed."""
    if len(path_) > 2 and path_[:2] == ["cv", "sections"]:
        return path_[:2] + [keys.get(str(path_[2]), path_[2])] + path_[3:]
    return path_


def translation_drift(path: Path) -> dict | None:
    """What the source changed since this translation was last brought up to
    date, field by field, with what the translation says there now."""
    link = translation_link(path)
    if not link:
        return None
    out = {"of": link["of"], "lang": link.get("lang"), "from_lang": link.get("from_lang"),
           "synced_at": link.get("synced_at"), "missing": False, "changes": []}
    try:
        src = to_plain(yaml_rt.load(safe_path(link["of"]).read_text(encoding="utf-8"))) or {}
        mine = to_plain(yaml_rt.load(path.read_text(encoding="utf-8"))) or {}
    except (OSError, ValueError):
        out["missing"] = True
        return out
    except Exception:
        return out
    then = {"cv": link.get("synced")}
    keys = link.get("keys") or {}
    for f in changed_fields(then.get("cv"), src.get("cv"), ["cv"]):
        before, after = get_at(then, f), get_at(src, f)
        here = _map_to_translation(f, keys)
        out["changes"].append({
            "key": field_key(here), "source_key": field_key(f),
            "where": _where_changed(f, src, then),
            "kind": "added" if before is None else "removed" if after is None
                    else "changed",
            "before": _brief(before), "after": _brief(after),
            "translation": _brief(get_at(mine, here))})
    return out


def mark_translation_current(path: Path) -> dict:
    """Say this translation now covers everything its source says."""
    link = translation_link(path)
    if not link:
        raise ValueError(f"{rel(path)} is not a translation.")
    src = to_plain(yaml_rt.load(safe_path(link["of"]).read_text(encoding="utf-8"))) or {}
    record = _edits_read()
    t = record["docs"].setdefault(rel(path), {}).setdefault("translation", link)
    t["synced"] = src.get("cv")
    t["synced_at"] = time.time()
    _edits_write(record)
    return {"ok": True, "path": rel(path), "of": link["of"]}


def sync_design(path: Path) -> list[str]:
    """Give every translation of `path` the same design block, so the versions
    of one CV print alike. The locale is not design and is left alone."""
    touched = []
    trans = translations_of(path)
    if not trans:
        return touched
    import copy
    import io
    src = yaml_rt.load(path.read_text(encoding="utf-8"))
    design = src.get("design")
    for t in trans:
        tp = safe_path(t["path"])
        try:
            data = yaml_rt.load(tp.read_text(encoding="utf-8"))
            if to_plain(data.get("design")) == to_plain(design):
                continue
            data["design"] = copy.deepcopy(design)
            buf = io.StringIO()
            yaml_rt.dump(data, buf)
            tp.write_text(buf.getvalue(), encoding="utf-8")
            touched.append(t["path"])
        except Exception:
            continue
    return touched


# --------------------------------------------------------------------------
# Photo
#
# One photo per workspace, photo.jpg at its root, already cropped square and
# shrunk by the app before it arrives. RenderCV prints cv.photo from a path
# relative to the YAML, so each CV that shows it points at it relatively
# (../photo.jpg from profile/): tailored copies land beside their source and
# keep working, and the folder still moves as one.
# --------------------------------------------------------------------------

PHOTO_FILE = "photo.jpg"
PHOTO_MAX = 3_000_000


def photo_info() -> dict | None:
    f = WORKSPACE / PHOTO_FILE
    if not f.is_file():
        return None
    st = f.stat()
    return {"path": PHOTO_FILE, "bytes": st.st_size,
            "url": f"/api/asset?path={PHOTO_FILE}&v={int(st.st_mtime * 1000)}"}


def photo_ref(doc: Path) -> str:
    """The workspace photo's path as this document has to write it."""
    return Path(os.path.relpath(WORKSPACE / PHOTO_FILE, doc.parent)).as_posix()


def save_photo(data: bytes) -> dict:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("The photo has to arrive as a JPEG.")
    if len(data) > PHOTO_MAX:
        raise ValueError("That photo is too large. Crop it again.")
    (WORKSPACE / PHOTO_FILE).write_bytes(data)
    return photo_info()


def remove_photo() -> dict:
    """Delete the workspace photo, and take it off every CV that showed it:
    RenderCV refuses a photo path that does not exist, so leaving the
    references would break every one of those CVs."""
    cleared = []
    for f, _, _ in document_files():
        try:
            if "photo:" not in f.read_text(encoding="utf-8"):
                continue
            data = to_plain(yaml_rt.load(f.read_text(encoding="utf-8"))) or {}
            ref = (data.get("cv") or {}).get("photo")
            if ref and (f.parent / str(ref)).resolve() == (WORKSPACE / PHOTO_FILE).resolve():
                apply_patches(f, [{"path": ["cv", "photo"], "value": None}], "photo")
                cleared.append(rel(f))
        except Exception:
            continue
    try:
        (WORKSPACE / PHOTO_FILE).unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "cleared": cleared}


# --------------------------------------------------------------------------
# Cover letters
#
# A letter is a Markdown file under letters/, laid out by letters.py with
# Typst in the look of the CV it names. Everything here is the workspace side:
# which CV that is, where the render goes, the application it belongs to, and
# turning the letters written before as RenderCV documents into files of the
# new kind, once, keeping the old file beside the new one.
# --------------------------------------------------------------------------

def is_letter(p: Path) -> bool:
    return p.suffix == letters.EXT


_DEFAULTS: dict = {}


def design_defaults(theme: str) -> dict:
    """A theme's own value for every design setting, keyed "colors.name"."""
    if theme not in _DEFAULTS:
        out = {}
        for g in design_schema(theme).get("groups", []):
            for f in g["fields"]:
                out[".".join(map(str, f["path"]))] = f.get("default")
        _DEFAULTS[theme] = out
    return _DEFAULTS[theme]


def letter_cv(meta: dict) -> Path | None:
    """The CV a letter looks like: the one it names, else the base CV."""
    for cand in (meta.get("looks_like"), (base_cv() or {}).get("path")):
        if cand:
            try:
                f = safe_path(str(cand))
                if f.exists():
                    return f
            except (PermissionError, ValueError):
                continue
    return None


def letter_head(meta: dict) -> dict:
    cvp = letter_cv(meta)
    data = {}
    if cvp:
        try:
            data = to_plain(yaml_rt.load(cvp.read_text(encoding="utf-8"))) or {}
        except Exception:
            data = {}
    theme = str(((data.get("design") or {}).get("theme")) or "classic")
    head = letters.letterhead(data, design_defaults(theme))
    head["cv"] = rel(cvp) if cvp else None
    return head


def load_letter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    meta, body = letters.parse(text)
    head = letter_head(meta)
    return {"letter": True, "path": rel(path), "text": text, "meta": meta,
            "body": body, "head": head, "date_line": letters.date_line(dated(meta, path)),
            "sent_on": letter_sent_on(path),
            "words": letters.word_count(body), "target": letters.WORD_TARGET,
            "mtime": path.stat().st_mtime}


def save_letter(path: Path, payload: dict, tool: str = "save") -> dict:
    if "text" in payload:
        text = str(payload["text"])
    else:
        meta, body = letters.parse(path.read_text(encoding="utf-8")) if path.exists() else ({}, "")
        meta.update({k: v for k, v in (payload.get("meta") or {}).items()})
        text = letters.dump(meta, payload.get("body", body))
    path.write_text(text, encoding="utf-8")
    return load_letter(path)


def letter_sent_on(path: Path) -> str | None:
    """The day the letter's application was sent, if it was."""
    try:
        job = next((j for j in jobstore.list_jobs(WORKSPACE) if j.get("letter_path") == rel(path)), None)
        sent = next((h["at"] for h in (job or {}).get("status_history") or [] if h.get("status") == "applied"), None)
        return str(sent)[:10] if sent else None
    except Exception:
        return None


def dated(meta: dict, path: Path) -> dict:
    """"today" on a letter already sent would redate it each export: once its
    application went out, the letter carries the day it was sent."""
    if meta.get("date") in (None, "", "today"):
        sent = letter_sent_on(path)
        if sent:
            return {**meta, "date": sent}
    return meta


def render_letter(path: Path) -> dict:
    meta, body = letters.parse(path.read_text(encoding="utf-8"))
    meta = dated(meta, path)
    head = letter_head(meta)
    cvp = letter_cv(meta)
    cjkfonts.ensure(path.read_text(encoding="utf-8") + head.get("name", ""))
    r = letters.render(meta, body, head, output_dir(path), letters.file_stem(head["name"]),
                       [p for p in ([cvp.parent / "fonts"] if cvp else []) + [path.parent / "fonts"]])
    if not r.get("ok"):
        return {"ok": False, "error": r.get("log"), "hint": "The letter did not lay out."}
    stamp = int(time.time() * 1000)
    return {"ok": True, "pages": r["pages"], "words": r["words"], "pdf": rel(Path(r["pdf"])),
            "pngs": [f"/api/asset?path={rel(Path(f))}&v={stamp}" for f in r["png_pages"]]}


def _slug_name(*parts: str) -> str:
    """"Alex Moreau", "Mistral AI", "Senior Platform Engineer" ->
    Alex-Moreau-Mistral-AI-Senior-Platform-Engineer: what a recruiter's
    download folder can tell apart."""
    words = []
    for p in parts:
        words += re.findall(r"[\w]+", unicodedata.normalize("NFKD", str(p or ""))
                            .encode("ascii", "ignore").decode())
    return "-".join(words)[:120] or "application"


def pack_info(job_id: str) -> dict:
    """What Export both would put together for an application."""
    job = next((j for j in jobstore.list_jobs(WORKSPACE) if j["id"] == job_id), None) \
        if jobstore is not None else None
    if job is None:
        raise ValueError("No such job.")
    have = {}
    for key in ("cv_path", "letter_path"):
        p = job.get(key)
        if p:
            try:
                f = safe_path(p)
            except (ValueError, PermissionError):
                continue
            if f.is_file():
                have[key] = f
    person = ""
    if "cv_path" in have:
        data = to_plain(yaml_rt.load(have["cv_path"].read_text(encoding="utf-8"))) or {}
        person = str(((data.get("cv") or {}).get("name")) or "")
    if not person and "letter_path" in have:
        meta, _ = letters.parse(have["letter_path"].read_text(encoding="utf-8"))
        person = str(letter_head(meta).get("name") or "")
    return {"job": job, "files": have,
            "name": _slug_name(person, job.get("company"), job.get("title")),
            "person": _slug_name(person) if person else "",
            "cv": "cv_path" in have, "letter": "letter_path" in have,
            "posting": bool((job.get("description") or "").strip())}


# Save to CV Studio, the bookmark: it has to find the app at an address that
# does not change between launches, and the desktop app picks a new port every
# time. So the server also listens here, for the bookmark's window.
CLIP_PORT = int(os.environ.get("CVSTUDIO_CLIP_PORT") or 47811)
CLIP_STATE = {"ok": False, "why": "not started"}
# Who may talk to the server: requests addressed to this machine, and writes
# from the app's own pages (on its port or the bookmark's). main() widens the
# names when it is told to bind beyond loopback.
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
OWN_PORTS = {CLIP_PORT}
OPEN_NETWORK = False

# Runs on the job page when the bookmark is clicked. It reads the job the page
# describes for search engines (schema.org JobPosting), or the text selected,
# and hands it to CV Studio's own window, which it opens. The page never sees
# the app's key: the window is served by the app and saves from there.
BOOKMARKLET = r"""(()=>{const O="http://127.0.0.1:__PORT__";
const F=o=>{if(!o||typeof o!=="object")return null;if(Array.isArray(o)){for(const x of o){const r=F(x);if(r)return r}return null}
if(o["@graph"])return F(o["@graph"]);return [].concat(o["@type"]||[]).includes("JobPosting")?o:null};
let J=null;for(const s of document.querySelectorAll('script[type="application/ld+json"]')){try{J=F(JSON.parse(s.textContent));if(J)break}catch(e){}}
const q=l=>{for(const x of l.split("|")){const e=document.querySelector(x);if(e&&e.innerText.trim())return e}return null};
const tx=l=>{const e=q(l);return e?e.innerText.trim().split("\n")[0].trim():""};
const P=new URLSearchParams(location.search);
const B=[[/indeed\./,'[data-testid="jobsearch-JobInfoHeader-title"]|.jobsearch-JobInfoHeader-title','[data-testid="inlineHeader-companyName"]|[data-company-name]','[data-testid="inlineHeader-companyLocation"]|[data-testid="job-location"]|#jobLocationText','#jobDescriptionText',()=>{const k=P.get("vjk")||P.get("jk");return k?location.origin+"/viewjob?jk="+k:""}],
[/linkedin\./,'.job-details-jobs-unified-top-card__job-title|.jobs-unified-top-card__job-title|.top-card-layout__title','.job-details-jobs-unified-top-card__company-name|.jobs-unified-top-card__company-name|.topcard__org-name-link','.job-details-jobs-unified-top-card__primary-description-container .tvm__text|.job-details-jobs-unified-top-card__bullet|.topcard__flavor--bullet','#job-details|.jobs-description__content|.show-more-less-html__markup',()=>{const k=P.get("currentJobId");return k?"https://www.linkedin.com/jobs/view/"+k+"/":""}],
[/glassdoor\./,'[data-test="job-title"]|[id^="jd-job-title"]','[data-test="employer-name"]|[class*="EmployerProfile_employerName"]','[data-test="location"]','[class*="JobDetails_jobDescription"]|#JobDescriptionContainer',()=>""]];
for(const [re,t,c,l,ds,u] of B){if(!re.test(location.hostname))continue;const T=tx(t).replace(/\s+-\s+(job post|offre d.emploi|emploi)$/i,""),D=q(ds);if(!T)break;
J={"@type":"JobPosting",title:T,hiringOrganization:{name:tx(c)},jobLocation:{address:tx(l)},description:D?D.innerHTML:"",url:u()||location.href};break}
const m=n=>{const e=document.querySelector('meta[property="'+n+'"],meta[name="'+n+'"]');return e?e.content:""};
const d={url:location.href,host:location.hostname,title:document.title,site:m("og:site_name"),job:J,
selection:String(getSelection()||"").slice(0,40000)};
const w=window.open(O+"/clip","cvstudio_clip","width=500,height=760");
if(!w){alert("CV Studio: allow pop-ups for this site, then click again.");return}
const h=e=>{if(e.origin===O&&e.data==="cvstudio-clip-ready"){w.postMessage(d,O);removeEventListener("message",h)}};
addEventListener("message",h)})();"""


def bookmarklet() -> str:
    code = re.sub(r"\n", "", BOOKMARKLET.replace("__PORT__", str(CLIP_PORT)))
    # Some browsers end a bookmark's address at "#", and read "%" as the
    # start of an escape: both are written escaped, which the browser undoes
    # before running it.
    return "javascript:" + code.replace("%", "%25").replace("#", "%23")


def start_clip_listener() -> None:
    """Listen on CLIP_PORT as well, for the bookmark. Taken (another copy of
    the app, or something else): the bookmark will not work, and Settings
    says so, but nothing else changes."""
    try:
        srv = Server(("127.0.0.1", CLIP_PORT), Handler)
    except OSError as exc:
        CLIP_STATE.update(ok=False, why=f"port {CLIP_PORT} is in use ({exc.strerror or exc})")
        return
    CLIP_STATE.update(ok=True, why="")
    threading.Thread(target=srv.serve_forever, daemon=True, name="clip").start()


def draft_context(job_id: str) -> dict:
    """What an email about an application can say without asking: whose name
    to sign, when you applied and met, and the posting's first duties."""
    job = next((j for j in jobstore.list_jobs(WORKSPACE) if j["id"] == job_id), None) \
        if jobstore is not None else None
    if job is None:
        raise ValueError("No such job.")
    you = ""
    for p in (job.get("cv_path"), (base_cv() or {}).get("path")):
        if not p:
            continue
        try:
            data = to_plain(yaml_rt.load(safe_path(p).read_text(encoding="utf-8"))) or {}
        except Exception:
            continue
        you = str(((data.get("cv") or {}).get("name")) or "")
        if you:
            break
    when = {}
    for h in job.get("status_history") or []:
        when.setdefault(h.get("status"), str(h.get("at") or "")[:10])
    duties = [re.sub(r"[*_`]", "", m).strip().rstrip(".")
              for m in re.findall(r"(?m)^\s*[-*•]\s+(.+)$", job.get("description") or "")][:2]
    return {"you": you, "applied": when.get("applied"), "interviewed": when.get("interviewing"),
            "duties": duties}


def next_round(job: dict) -> dict | None:
    """The round being prepared for: the first one without an outcome."""
    return next((r for r in job.get("rounds") or [] if not r.get("outcome")), None)


def interview_prep(job_id: str) -> dict:
    """The prep for an application's next round: what was saved (by you or an
    AI client), else made here from the posting, the CV sent and the round."""
    import prep as prepmod
    job = next((j for j in jobstore.list_jobs(WORKSPACE) if j["id"] == job_id), None) \
        if jobstore is not None else None
    if job is None:
        raise ValueError("No such job.")
    rnd = next_round(job) or {}
    stored = job.get("prep")
    if stored and stored.get("questions"):
        return {**stored, "local": False, "round": rnd}
    cv = None
    for p in (job.get("cv_path"), (base_cv() or {}).get("path")):
        if not p:
            continue
        try:
            cv = (to_plain(yaml_rt.load(safe_path(p).read_text(encoding="utf-8"))) or {}).get("cv")
            break
        except Exception:
            continue
    who = next((x for x in job.get("people") or [] if x.get("name") and x.get("name") == rnd.get("with")), {})
    made = prepmod.build(job, cv, rnd.get("kind") or "", who.get("role") or "")
    # What you already did with it (notes, the CV re-read) survives a rebuild.
    if stored:
        made = prepmod.merge(stored, {k: made[k] for k in ("questions", "stories", "asks")})
    return {**made, "local": True, "round": rnd}


def save_interview_prep(job_id: str, prep_data: dict, by: str = "") -> dict:
    """Store the prep whole: the app saving your notes, or an AI client its
    questions (merged so your notes stay)."""
    import prep as prepmod
    if by:
        cur = interview_prep(job_id)
        prep_data = prepmod.merge({k: v for k, v in cur.items() if k not in ("local", "round")},
                                  {**prep_data, "by": by, "at": time.strftime("%Y-%m-%d")})
    jobstore.update_job(WORKSPACE, job_id, {"prep": prep_data})
    return interview_prep(job_id)


def application_pack(job_id: str, fmt: str = "pdf", name: str | None = None,
                     posting: bool = False) -> tuple[bytes, str, str]:
    """The CV and the letter for one application as one PDF (CV first) or a
    zip of separate files, each rendered from what is saved now."""
    info = pack_info(job_id)
    if not (info["cv"] or info["letter"]):
        raise ValueError("This application has no CV or letter yet.")
    parts = []
    if info["cv"]:
        pdf, failed = current_pdf(info["files"]["cv_path"])
        if pdf is None:
            raise ValueError((failed or {}).get("hint") or "The CV did not render.")
        parts.append(("CV", pdf.read_bytes()))
    if info["letter"]:
        data, _, _ = letter_export(info["files"]["letter_path"], "pdf")
        parts.append(("Cover-letter", data))
    stem = _slug_name(re.sub(r"\.(pdf|zip)\s*$", "", name.strip(), flags=re.I)) if name and name.strip() \
        else info["name"]
    if fmt == "zip":
        buf = io.BytesIO()
        who = info["person"] or "Application"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for label, data in parts:
                z.writestr(f"{who}-{label}.pdf", data)
            if posting and info["posting"]:
                z.writestr("posting.md", info["job"]["description"])
        return buf.getvalue(), "application/zip", stem + ".zip"
    if len(parts) == 1:
        return parts[0][1], "application/pdf", stem + ".pdf"
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for _, data in parts:
        w.append(PdfReader(io.BytesIO(data)))
    out = io.BytesIO()
    w.write(out)
    return out.getvalue(), "application/pdf", stem + ".pdf"


def letter_export(path: Path, fmt: str) -> tuple[bytes, str, str]:
    """(bytes, content type, file name) for a letter in another form."""
    meta, body = letters.parse(path.read_text(encoding="utf-8"))
    head = letter_head(meta)
    base = letters.file_stem(head["name"])
    if fmt == "docx":
        return (letters.docx(meta, body, head),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                base + ".docx")
    if fmt == "txt":
        return letters.plain_text(meta, body, head).encode("utf-8"), \
            "text/plain; charset=utf-8", base + ".txt"
    pdf = last_pdf(path)
    if not pdf or pdf.stat().st_mtime < path.stat().st_mtime:
        r = render_letter(path)
        if not r.get("ok"):
            raise ValueError(r.get("error") or "The letter did not lay out.")
        pdf = WORKSPACE / r["pdf"]
    return pdf.read_bytes(), "application/pdf", base + ".pdf"


def new_letter(job_id: str | None = None, name: str | None = None) -> dict:
    """A letter with everything filled in but the words: from the application,
    its company, role and language; from its CV (else the base CV), the look
    and the place it is written from. Linked to the application."""
    job = None
    if job_id and jobstore is not None:
        job = next((j for j in jobstore.list_jobs(WORKSPACE) if j["id"] == job_id), None)
    company = (job or {}).get("company") or ""
    role = (job or {}).get("title") or ""
    lang = (job or {}).get("language") or languages.detect(
        f"{role} {(job or {}).get('description') or ''}") or "en"
    looks = (job or {}).get("cv_path") or (base_cv() or {}).get("path")
    place = ""
    try:
        data = to_plain(yaml_rt.load(safe_path(looks).read_text(encoding="utf-8"))) if looks else {}
        place = str(((data or {}).get("cv") or {}).get("location") or "").split(",")[0].strip()
    except Exception:
        pass
    stem = re.sub(r"[^a-z0-9]+", "-", (name or f"cover-{company or 'letter'}").lower()).strip("-") or "cover-letter"
    folder = WORKSPACE / "letters"
    folder.mkdir(parents=True, exist_ok=True)
    dest, n = folder / f"{stem}{letters.EXT}", 2
    while dest.exists():
        dest, n = folder / f"{stem}-{n}{letters.EXT}", n + 1
    dest.write_text(letters.scaffold(company=company, role=role, lang=lang, place=place,
                                     looks_like=looks, application=job_id if job else None),
                    encoding="utf-8")
    if job and jobstore is not None:
        jobstore.update_job(WORKSPACE, job["id"], {"letter_path": rel(dest)})
    return {"ok": True, "path": rel(dest)}


def convert_legacy_letters() -> list[str]:
    """Every letter still written as a RenderCV document, as a Markdown letter.
    The old file is renamed to .yaml.bak beside it, and an application that
    pointed at it points at the new one."""
    done = []
    folder = WORKSPACE / "letters"
    if not folder.is_dir():
        return done
    jobs = jobstore.list_jobs(WORKSPACE) if jobstore is not None else []
    for f in sorted(folder.glob("*.y*ml")):
        if f.name.startswith("."):
            continue
        try:
            data = to_plain(yaml_rt.load(f.read_text(encoding="utf-8"))) or {}
            if not isinstance(data.get("cv"), dict):
                continue
            meta, body = letters.from_legacy(data)
            dest = f.with_suffix(letters.EXT)
            if dest.exists():
                continue
            job = next((j for j in jobs if j.get("letter_path") == rel(f)), None)
            if job:
                meta.update({"application": job["id"], "company": job.get("company"),
                             "looks_like": job.get("cv_path") or (base_cv() or {}).get("path")})
            else:
                meta["looks_like"] = (base_cv() or {}).get("path")
            dest.write_text(letters.dump(meta, body), encoding="utf-8")
            f.rename(f.with_name(f.name + ".bak"))
            if job:
                jobstore.update_job(WORKSPACE, job["id"], {"letter_path": rel(dest)})
            done.append(rel(dest))
        except Exception:
            continue
    return done


def edits_stamp() -> float | None:
    """When the provenance file last changed, so the poll can spot a new mark."""
    try:
        return (WORKSPACE / EDITS_FILE).stat().st_mtime
    except OSError:
        return None


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
    # RenderCV prints "Last updated in <month> <year>" at the top right of
    # page one. It dates the document rather than the work, and a CV that
    # announces it was last touched four months ago is answering a question
    # nobody asked.
    show_top_note: false
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


# The workspace to come back to from the sample data. Held in memory only: a
# restart always opens your own, whatever was on screen when it quit.
REAL_WORKSPACE: Path | None = None


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------
# Theme, language, time zone, notifications: per machine, not per workspace,
# so a workspace copied elsewhere carries documents and not window settings.
# They lived in the page's localStorage, which the desktop app loses at every
# launch: it serves the page on a fresh port each time, and storage belongs
# to the origin, port included. So they are a file in the app's data folder,
# written into the page when it is served, so the theme is right before the
# first paint.

def prefs_path() -> Path:
    return cjkfonts.cache_dir().parent / "prefs.json"


def log_path() -> Path:
    """Where the server writes what it would print, when there is no terminal
    to print it to: the desktop app. Beside the preferences, never in the
    workspace."""
    return cjkfonts.cache_dir().parent / "logs" / "server.log"


def log_to_file() -> None:
    """Send stdout and stderr to the log. Kept under a megabyte: past that,
    the old one becomes server.1.log and a new one starts."""
    p = log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 1_000_000:
            p.replace(p.with_name("server.1.log"))
        f = open(p, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return
    f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} CV Studio {VERSION} ---\n")
    sys.stdout = sys.stderr = f


def load_prefs() -> dict | None:
    """The saved preferences, or None before anything was ever saved."""
    try:
        data = json.loads(prefs_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


_prefs_lock = threading.Lock()


def save_prefs(changes: dict, replace: bool = False) -> dict:
    """Merge `changes` into the saved preferences (or replace them all).
    Two changes a moment apart arrive on two threads; one at a time, or the
    second writes over the first."""
    with _prefs_lock:
        return _save_prefs(changes, replace)


def _save_prefs(changes: dict, replace: bool) -> dict:
    data = {} if replace else (load_prefs() or {})
    for k, v in changes.items():
        if not isinstance(k, str) or len(k) > 64:
            continue
        data[k] = v
    raw = json.dumps(data, ensure_ascii=False, indent=1)
    if len(raw) > 256 * 1024:
        raise ValueError("Preferences are too large.")
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.replace(path)
    return data


def in_sample() -> bool:
    return WORKSPACE.resolve() == sample.folder().resolve()


def open_sample(applications: int = 64) -> dict:
    """Rebuild the sample folder and switch the app to it."""
    global WORKSPACE, REAL_WORKSPACE
    if not in_sample():
        REAL_WORKSPACE = WORKSPACE
    WORKSPACE = sample.folder()
    try:
        return sample.build(sys.modules[__name__], applications)
    except Exception:
        WORKSPACE = REAL_WORKSPACE or WORKSPACE
        raise


def close_sample() -> None:
    global WORKSPACE
    if in_sample() and REAL_WORKSPACE is not None:
        WORKSPACE = REAL_WORKSPACE


def bootstrap(workspace: Path) -> bool:
    """Create and seed the workspace. Returns True when this was a first run."""
    created = not workspace.exists()
    (workspace / "profile").mkdir(parents=True, exist_ok=True)
    (workspace / "applications").mkdir(exist_ok=True)
    (workspace / "letters").mkdir(exist_ok=True)
    (workspace / "assets").mkdir(exist_ok=True)
    if not any((workspace / "profile").glob("*.y*ml")):
        (workspace / "profile" / "my-cv.yaml").write_text(STARTER_CV, encoding="utf-8")
        # The one moment the base is not a guess: this is the only CV there is,
        # and the app put it there. Nominating it now means a new workspace has
        # something to tailor from before anybody has been asked anything.
        set_base_cv("profile/my-cv.yaml")
        created = True
    return created


def starter_untouched() -> bool:
    """Whether the base CV is still the placeholder the app wrote.

    First run is one launch; setup is not. Quit halfway through it and the next
    launch is no longer a first run, but the CV still says Your Name, and that
    is the fact the welcome is really about.
    """
    base = base_cv()
    if not base or base.get("missing"):
        return False
    try:
        return safe_path(base["path"]).read_text(encoding="utf-8") == STARTER_CV
    except OSError:
        return False


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


# --------------------------------------------------------------------------
# Company logos
#
# Local files only, as far as the interface is concerned. Pointing an <img> at
# a remote logo would tell someone else's server which companies you are
# applying to, every time the table drew. A logo is fetched once, by the MCP
# server when a model asks, from the company's own site (fetch_logo below).
#
# So a logo is a file in the workspace, and a job names it. A company with no
# logo gets a monogram instead, which is most of them and has to look
# deliberate rather than broken.
# --------------------------------------------------------------------------

LOGO_DIR = "assets/logos"
LOGO_TYPES = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico"}


def logo_dir() -> Path:
    return WORKSPACE / LOGO_DIR


def list_logos() -> list[str]:
    d = logo_dir()
    if not d.is_dir():
        return []
    return sorted(f.name for f in d.iterdir()
                  if f.is_file() and f.suffix.lower() in LOGO_TYPES)


def logo_url(name: str | None) -> str | None:
    """The asset URL for a stored logo, or None if it is not a real one."""
    if not name:
        return None
    f = logo_dir() / Path(name).name
    if not f.is_file() or f.suffix.lower() not in LOGO_TYPES:
        return None
    return f"/api/asset?path={rel(f)}"


def save_logo(company: str, source: str) -> dict:
    """Copy an image into the workspace and hand back the name to store.

    `source` is a path on this machine. Copying rather than referencing means
    the workspace stays self-contained: back it up, move it, and the logos come
    with it.
    """
    src = Path(source).expanduser()
    if not src.is_file():
        raise ValueError(f"No file at {src}")
    if src.suffix.lower() not in LOGO_TYPES:
        raise ValueError(
            f"{src.suffix or 'That'} is not an image type this can show. "
            f"Use one of: {', '.join(sorted(LOGO_TYPES))}")
    return _store_logo(company, src.read_bytes(), src.suffix.lower())


def _logo_stem(company: str) -> str:
    return "".join(c for c in company.lower() if c.isalnum() or c in "-_") or "logo"


def _store_logo(company: str, data: bytes, ext: str) -> dict:
    dest = logo_dir() / f"{_logo_stem(company)}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"ok": True, "logo": dest.name, "path": rel(dest),
            "kb": round(dest.stat().st_size / 1024, 1)}


def stored_logo(company: str) -> str | None:
    """A logo already saved for this company, by the name save_logo gives it."""
    stem = _logo_stem(company)
    for name in list_logos():
        if Path(name).stem == stem:
            return name
    return None


# Fetching one. This is the only network request anything in CV Studio makes,
# and it is shaped so that it tells nobody anything new: it goes to the
# company's own website, which the model was given, and to wherever that page
# says its icon lives (often the company's own CDN) -- no logo service, no
# search engine, nothing that would learn the list of places you are applying
# to. The app's interface never calls it; only the MCP server
# does, when a model asks.
LOGO_MAX = 512 * 1024
PAGE_MAX = 1536 * 1024
UA = "Mozilla/5.0 (compatible; CV Studio logo fetch)"


def _site(website: str) -> str:
    raw = (website or "").strip()
    if not raw:
        raise ValueError("No website given.")
    u = urlparse(raw if "://" in raw else "https://" + raw)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or "." not in host:
        raise ValueError(f"{website!r} is not a website.")
    return f"{u.scheme}://{host}{f':{u.port}' if u.port else ''}/"


def _fetch(url: str, limit: int) -> bytes:
    import urllib.request
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("not http")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 -- scheme checked
        body = r.read(limit + 1)
    if len(body) > limit:
        raise ValueError("too large")
    return body


def _image_ext(data: bytes) -> str | None:
    """What an image is, from its bytes. A server's Content-Type is a guess."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return ".ico"
    head = data[:2048].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        # Drawn in an <img>, where a script cannot run, but also reachable as
        # a page in its own right. Refuse anything that could do something.
        low = data.lower()
        if re.search(rb"<script|<foreignobject|\son[a-z]+\s*=|javascript:", low):
            return None
        return ".svg"
    return None


def _icon_links(html: str, root: str) -> list[tuple[int, str]]:
    """Every icon the page declares, scored so the best logo comes first."""
    from html.parser import HTMLParser
    from urllib.parse import urljoin
    found: list[tuple[int, str]] = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag != "link":
                return
            a = {k: (v or "") for k, v in attrs}
            rel_ = a.get("rel", "").lower()
            href = a.get("href")
            if not href or "icon" not in rel_ or "mask" in rel_:
                return
            sizes = [int(x) for x in re.findall(r"(\d+)x\d+", a.get("sizes", ""))]
            if href.lower().split("?")[0].endswith(".svg") or "svg" in a.get("type", ""):
                score = 1000
            elif "apple-touch-icon" in rel_:
                score = max(sizes or [180])
            else:
                score = max(sizes or [32])
            found.append((score, urljoin(root, href)))

    try:
        P().feed(html)
    except Exception:
        pass
    return found


def fetch_logo(company: str, website: str) -> dict:
    """Find the company's icon on its own website and store it as its logo."""
    root = _site(website)
    tried: list[str] = []
    candidates: list[tuple[int, str]] = []
    try:
        page = _fetch(root, PAGE_MAX).decode("utf-8", errors="replace")
        candidates += _icon_links(page, root)
    except Exception as exc:
        tried.append(f"{root} ({type(exc).__name__})")
    candidates += [(150, root + "apple-touch-icon.png"), (16, root + "favicon.ico")]
    seen: set[str] = set()
    for _, url in sorted(candidates, key=lambda c: -c[0]):
        if url in seen:
            continue
        seen.add(url)
        try:
            data = _fetch(url, LOGO_MAX)
        except Exception as exc:
            tried.append(f"{url} ({type(exc).__name__})")
            continue
        ext = _image_ext(data)
        if ext:
            saved = _store_logo(company, data, ext)
            saved["from"] = url
            return saved
        tried.append(f"{url} (not an image)")
    raise ValueError(f"No usable logo on {urlparse(root).hostname}. Tried: " +
                     "; ".join(tried[:6]))


def document_files() -> list[tuple[Path, str, str]]:
    """Every candidate document file, as (path, label, group).

    Kept separate from list_documents because the pulse the editor polls only
    wants names and timestamps: deciding what is really a CV means reading the
    head of every file, which is too much to do every couple of seconds.
    """
    found: list[tuple[Path, str, str]] = []
    for folder, group in ((WORKSPACE / "profile", "My CVs"),
                          (WORKSPACE / "letters", "Cover letters")):
        if folder.is_dir():
            found += [(f, f.stem, group) for f in sorted(folder.glob("*.y*ml"))
                      if not f.name.startswith(".")]
            if group == "Cover letters":
                found += [(f, f.stem, group) for f in sorted(folder.glob("*" + letters.EXT))
                          if not f.name.startswith(".")]
    apps = WORKSPACE / "applications"
    if apps.is_dir():
        for app_dir in sorted(apps.iterdir(), reverse=True):
            if app_dir.is_dir():
                found += [(f, app_dir.name, "Applications")
                          for f in sorted(app_dir.glob("*.y*ml"))
                          if not f.name.startswith(".")]
    return found


def _fold(text: str) -> str:
    """Lower case without accents, one character for one, so an index in the
    folded text is an index in the original: "résumé" finds "resume"."""
    return "".join((unicodedata.normalize("NFD", c)[:1] or c).lower() for c in text)


def search_documents(q: str, limit: int = 8) -> list[dict]:
    """Documents whose text holds every word of q, with the line around the
    first word found. For the search box: the page has the documents' names,
    not what is in them."""
    words = [w for w in _fold(q).split() if w]
    if not words:
        return []
    out = []
    for d in list_documents():
        try:
            text = safe_path(d["path"]).read_text(encoding="utf-8")
        except (OSError, ValueError, PermissionError):
            continue
        folded = _fold(text)
        if not all(w in folded for w in words):
            continue
        at = folded.find(words[0])
        start = text.rfind("\n", 0, at) + 1
        end = text.find("\n", at)
        # A YAML list item or a Markdown bullet: the words, not the dash.
        line = re.sub(r"^[-*]\s+", "", text[start:end if end >= 0 else len(text)].strip())
        if len(line) > 140:
            # Keep the match in view when the line is long.
            cut = max(0, at - start - 60)
            line = ("…" if cut else "") + line[cut:cut + 130].strip() + "…"
        out.append({"path": d["path"], "label": d.get("label") or d["path"],
                    "group": d.get("group"), "line": line})
        if len(out) >= limit:
            break
    return out


def list_documents() -> list[dict]:
    """Every CV in the workspace, with who last worked on it.

    `by` is read off the sidecar rather than by parsing each document, because
    the rail wants a mark on a dozen files and none of them is open. It is the
    recorded author of the newest edit, which is one lookup; whether that edit
    still stands is a question only the open document can answer, and the marks
    inside it do.
    """
    docs = _edits_read()["docs"]

    def last_ai(path: str) -> dict | None:
        best = None
        for edit in (docs.get(path, {}).get("edits") or []):
            if edit.get("by") in (None, "you"):
                continue
            if not best or (edit.get("at") or 0) > (best.get("at") or 0):
                best = edit
        return ({"by": best["by"], "at": best.get("at"),
                 "agent": best.get("agent")} if best else None)

    out = []
    for f, label, group in document_files():
        if is_letter(f):
            path = rel(f)
            try:
                meta, _ = letters.parse(f.read_text(encoding="utf-8"))
            except OSError:
                meta = {}
            out.append({"path": path, "label": label, "group": group, "letter": True,
                        "mtime": f.stat().st_mtime, "ai": last_ai(path), "base": None,
                        "lang": languages.code_of(meta.get("language")),
                        "translation_of": None, "looks_like": meta.get("looks_like")})
            continue
        if not is_cv_yaml(f):
            continue
        path = rel(f)
        try:
            lang = languages.file_language(f.read_text(encoding="utf-8"))
        except OSError:
            lang = "en"
        trans = (docs.get(path, {}).get("translation") or {})
        if trans.get("of") and label.endswith("." + str(trans.get("lang"))):
            label = label[: -len(str(trans.get("lang"))) - 1]
        out.append({"path": path, "label": label, "group": group,
                    "mtime": f.stat().st_mtime, "ai": last_ai(path),
                    "base": (docs.get(path, {}).get("base") or {}).get("path"),
                    "lang": lang,
                    "translation_of": trans.get("of")})
    return out


def pulse() -> dict:
    """What changed in the workspace, cheaply enough to ask about repeatedly.

    This is how the app notices that Claude rewrote the file it is showing.
    """
    stamps = {}
    for f, _, _ in document_files():
        try:
            stamps[rel(f)] = f.stat().st_mtime
        except OSError:
            pass
    return {"docs": stamps, "mcp": mcp_activity(), "jobs": jobs_stamp(),
            "edits": edits_stamp()}


def jobs_stamp() -> str | None:
    """A fingerprint of the applications table, for the same reason as `docs`.

    An AI client changing a status is another process writing the workspace,
    and until this existed the open Jobs table had no way to find out: it
    reloaded on boot, on a view switch, and after its own edits, so a status
    moved from a chat sat there stale until the user happened to navigate.

    The newest timestamp and the row count together catch every change that
    matters, including a delete, which a timestamp alone would miss. Cheap
    enough to ask for every couple of seconds.
    """
    if jobstore is None:
        return None
    try:
        con = jobstore.connect(WORKSPACE)
        try:
            row = con.execute(
                "SELECT COUNT(*) n, MAX(updated_at) m FROM jobs").fetchone()
            return f"{row['n']}:{row['m'] or ''}"
        finally:
            con.close()
    except Exception:
        # Never let the poll fail because of the job store: the rest of the
        # payload is what keeps the open document in step.
        return None


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


def write_doc(path: Path, text: str, tool: str = "write") -> dict:
    """Replace a whole document, recording which fields that turned out to move.

    A whole-file write says nothing about what changed, so the difference has
    to be measured: parse what is there, write, parse what is now there. That
    keeps the marks the same whether a field was set through a patch or a file
    was replaced wholesale.
    """
    if is_letter(path):
        path.write_text(text, encoding="utf-8")
        return {"changed": []}
    before = None
    try:
        before = to_plain(yaml_rt.load(path.read_text(encoding="utf-8")))
    except Exception:
        pass
    path.write_text(text, encoding="utf-8")
    try:
        after = to_plain(yaml_rt.load(text))
    except Exception:
        return {"changed": []}
    if (before or {}).get("design") != (after or {}).get("design"):
        sync_design(path)
    return {"changed": record_edits(path, before, after, tool)}


def load_doc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        data, err = yaml_rt.load(text), None
    except Exception as exc:
        data, err = None, str(exc)
    # The mtime is what lets the editor tell its own writes apart from someone
    # else's -- Claude's, usually -- and reload rather than overwrite.
    plain = to_plain(data) if data else None
    return {"yaml": text, "data": plain,
            "parse_error": err, "mtime": path.stat().st_mtime,
            # Where each block lives in the source, so the YAML tab can show
            # the same selection the page and the form do.
            "lines": line_map(data, text) if data else {},
            # Who last wrote each field, and how it differs from the CV it was
            # tailored from. Never written back into the file.
            "prov": provenance(path, plain),
            # Which language this is in, the other languages of the same CV,
            # and what its source changed since it was translated.
            "family": language_family(path),
            "drift": translation_drift(path),
            # How this document would point at the workspace photo, when
            # there is one to point at.
            "photo_ref": photo_ref(path) if photo_info() else None}


def line_map(doc, text: str) -> dict:
    """Where each block of the document sits in the YAML source.

    The selection is the thread through every view, and the YAML tab was the one
    place it could not follow because nothing knew which lines an entry occupied.
    ruamel keeps the position of every node it parsed, so this is exact rather
    than a search for a matching string.

    Returns {"header": [start, end], "<section>": [...], "<section>/<i>": [...]}
    with 0-based, end-exclusive line numbers.
    """
    try:
        cv = doc["cv"]
    except Exception:
        return {}
    total = len(text.splitlines())
    out: dict[str, list[int]] = {}

    sections = cv.get("sections") if hasattr(cv, "get") else None
    # The header is everything in `cv` before the sections block begins.
    try:
        head_start = cv.lc.line
        head_end = sections.lc.line - 1 if sections is not None else total
        out["header"] = [head_start, max(head_start + 1, head_end)]
    except Exception:
        pass
    if sections is None:
        return out

    names = list(sections)
    for n, name in enumerate(names):
        try:
            key_line = sections.lc.key(name)[0]
        except Exception:
            continue
        nxt = sections.lc.key(names[n + 1])[0] if n + 1 < len(names) else total
        out[name] = [key_line, nxt]
        entries = sections[name] or []
        # An entry runs to the start of the next one, or to the end of the section.
        starts = []
        for i in range(len(entries)):
            try:
                starts.append(entries.lc.item(i)[0])
            except Exception:
                starts.append(None)
        for i, s in enumerate(starts):
            if s is None:
                continue
            follow = next((x for x in starts[i + 1:] if x is not None), None)
            out[f"{name}/{i}"] = [s, follow if follow is not None else nxt]
    return out


def apply_ops(path: Path, ops: list[dict], tool: str = "edit") -> dict:
    """Add and remove whole entries and sections.

    apply_patches deliberately refuses a path that is not already in the
    document -- that refusal is what tells a model it mis-indexed an entry
    instead of quietly writing a new one somewhere. Structure is the other
    half of that bargain: adding an entry IS creating a path that did not
    exist, so it has to be asked for in as many words rather than inferred
    from a patch that happened to land past the end of a list.

    RenderCV types a section by the entries in it -- every section is a
    list[OneLineEntry] or a list[ExperienceEntry], never a mixture -- so a
    section with nothing in it cannot be typed and fails validation. Removing
    the last entry therefore removes the section, which is what "remove" meant
    anyway; leaving an empty one behind would break the render of a document
    that was fine a moment ago.

    Returns the same {"applied", "missed", "changed"} shape as apply_patches.
    """
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    before = to_plain(data)
    applied: list[dict] = []
    missed: list[dict] = []
    cv = data.get("cv") if hasattr(data, "get") else None
    if cv is None:
        return {"applied": [], "missed": [{"op": o, "why": "no cv in this file"}
                                          for o in ops], "changed": []}
    for op in ops:
        kind = op.get("op")
        if cv.get("sections") is None:
            cv["sections"] = {}
        sections = cv["sections"]
        try:
            if kind == "add_section":
                name = op["name"]
                if name in sections:
                    missed.append({"op": op, "why": "that section already exists"})
                    continue
                sections[name] = op["value"]
            elif kind == "remove_section":
                del sections[op["name"]]
            elif kind == "add_entry":
                seq = sections[op["section"]]
                at = op.get("at")
                at = len(seq) if at is None else max(0, min(int(at), len(seq)))
                seq.insert(at, op["value"])
            elif kind == "remove_entry":
                name = op["section"]
                seq = sections[name]
                seq.pop(int(op["at"]))
                if not len(seq):
                    del sections[name]
            else:
                missed.append({"op": op, "why": "unknown operation"})
                continue
            applied.append(op)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            missed.append({"op": op, "why": f"{type(exc).__name__}: {exc}"})
    import io
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")
    return {"applied": applied, "missed": missed,
            "changed": record_edits(path, before, to_plain(data), tool)}


def apply_patches(path: Path, patches: list[dict], tool: str = "edit") -> dict:
    """Set individual fields, keeping the rest of the file and its comments.

    Returns {"applied": [...], "missed": [...], "changed": [...]}. `missed`
    matters: a patch whose path does not exist in the document is skipped, and
    reporting that as a success is how a model mis-indexes an entry, is told it
    worked, and moves on. The paths that actually changed value are recorded as
    provenance on the way past.
    """
    data = yaml_rt.load(path.read_text(encoding="utf-8"))
    before = to_plain(data)
    applied: list[list] = []
    missed: list[dict] = []
    for patch in patches:
        keys, value = patch.get("path") or [], patch.get("value")
        if not keys:
            missed.append({"path": keys, "why": "no path given"})
            continue
        node, ok = data, True
        for k in keys[:-1]:
            try:
                node = node[int(k)] if isinstance(node, list) else node[k]
            except (KeyError, IndexError, ValueError, TypeError):
                # A design setting belongs to a group the document may never
                # have written -- a starter CV has no `design.header` at all --
                # and the Design screen offers every one. Those groups are made
                # on the way down. Everywhere else a missing parent still means
                # a wrong path, and is reported rather than invented.
                if keys[0] == "design" and isinstance(node, dict) and isinstance(k, str):
                    node[k] = CommentedMap()
                    node = node[k]
                    continue
                ok = False
                break
        if not ok or node is None:
            missed.append({"path": keys, "why": "no such field"})
            continue
        last = keys[-1]
        try:
            if isinstance(node, list):
                node[int(last)] = value
            else:
                node[last] = value
        except (KeyError, IndexError, ValueError, TypeError):
            missed.append({"path": keys, "why": "no such field"})
            continue
        applied.append(keys)
    import io
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")
    after = to_plain(data)
    if (before or {}).get("design") != (after or {}).get("design"):
        sync_design(path)
    return {"applied": applied, "missed": missed,
            "changed": record_edits(path, before, after, tool)}


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


def outline_of(data: dict | None) -> list[tuple[str, int]]:
    """A document's sections as (key, entry count), in the order they render."""
    cv = (data or {}).get("cv") or {}
    sections = cv.get("sections") or {}
    return [(k, len(v or [])) for k, v in sections.items()]


def block_map(result: dict, source: Path) -> dict | None:
    """Where each section and entry of a render landed on the page.

    Best effort by design: this only makes the preview clickable, so anything
    that goes wrong -- no Typst, a source shaped unexpectedly by some theme --
    costs the click targets and nothing else. The render itself has already
    succeeded by the time this runs.
    """
    if cv_map is None:
        return None, "Typst mapping is unavailable in this build"
    if not result.get("typ"):
        return None, "the render produced no Typst source to read"
    reasons: list = []
    try:
        # Parsed here rather than through load_doc: this wants the section
        # names and nothing else, and load_doc also computes provenance and a
        # line map, which is work nobody asked for on every render -- and one
        # more thing that can throw where the only consequence is the click
        # targets silently vanishing.
        outline = outline_of(to_plain(yaml_rt.load(
            source.read_text(encoding="utf-8"))))
        if not outline:
            return None, "the document has no sections to map"
        built = cv_map.build_map(Path(result["typ"]), outline, source.parent,
                                 reasons)
        if built:
            return built, None
        return None, reasons[0] if reasons else "the render could not be mapped"
    except Exception as exc:
        # Still best effort -- this costs the click targets and nothing else --
        # but it says so now rather than leaving a dead page and no trace.
        return None, f"{type(exc).__name__}: {exc}"


def _shape(result: dict, source: Path) -> dict:
    if not result.get("ok"):
        log = (result.get("log") or "render failed")[-3000:]
        return {"ok": False, "error": log, "hint": friendly(log)}
    stamp = int(time.time() * 1000)
    shaped = {
        "ok": True,
        "pages": result.get("pages"),
        "ats_words": result.get("ats_word_count"),
        "pdf": rel(Path(result["pdf"])) if result.get("pdf") else None,
        "pngs": [f"/api/asset?path={rel(Path(p))}&v={stamp}"
                 for p in result.get("png_pages", [])],
    }
    blocks, why = block_map(result, source)
    if why:
        # The page is still shown, it just cannot be clicked. Saying so beats
        # a preview that quietly stops responding.
        shaped["map_why"] = why
    if blocks and blocks.get("bands"):
        shaped["map"] = blocks["bands"]
        # The column the text sits in, so a click target can hug the writing
        # rather than stretch across the sheet.
        if blocks.get("box"):
            shaped["map_box"] = blocks["box"]
    return shaped


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
    return _shape(render_file(path, output_dir(path)), path)


def last_pdf(path: Path) -> Path | None:
    """The newest PDF in this document's output folder, whatever its age."""
    out = output_dir(path)
    pdfs = sorted(out.glob("*.pdf"), key=lambda f: f.stat().st_mtime,
                  reverse=True) if out.is_dir() else []
    return pdfs[0] if pdfs else None


def current_pdf(path: Path) -> tuple[Path | None, dict | None]:
    """The PDF for this document as it now stands, rendering it if the one on
    disk is older than the YAML. Returns (pdf, None) or (None, failed render)."""
    pdf = last_pdf(path)
    if pdf and pdf.stat().st_mtime >= path.stat().st_mtime:
        return pdf, None
    r = render(path)
    if not r.get("ok"):
        return None, r
    return WORKSPACE / r["pdf"], None


def job_for(path: Path, job_id: str | None = None) -> dict | None:
    """The application to check a CV against: the one named, else the one this
    CV is attached to."""
    if jobstore is None:
        return None
    jobs = jobstore.list_jobs(WORKSPACE)
    if job_id:
        return next((j for j in jobs if j["id"] == job_id), None)
    return next((j for j in jobs if j.get("cv_path") == rel(path)), None)


def ats_report(path: Path, job_id: str | None = None,
               posting: str | None = None) -> dict:
    """What an ATS reads from this CV's PDF, and how it meets a posting.

    The posting is the one pasted, else the named application's, else the
    application this CV is attached to. No posting is not an error: the
    parsing checks stand on their own.
    """
    pdf, failed = current_pdf(path)
    if pdf is None:
        return {"ok": False, "error": (failed or {}).get("hint") or
                "The CV did not render, so there is no PDF to read."}
    try:
        pages = ats.pdf_text(pdf)
    except Exception as exc:
        return {"ok": False, "error": f"Could not read the PDF: {exc}"}
    data = to_plain(yaml_rt.load(path.read_text(encoding="utf-8"))) or {}
    text = "\n".join(pages)
    out = {"ok": True, "pdf": rel(pdf), "pages": len(pages),
           "words": len(text.split()), "text": text[:8000],
           "checks": ats.parse_checks(pages, data), "against": None,
           "keywords": None}
    # An empty posting is a choice ("check it against nothing"), not an
    # absence, so only None falls back to the attached application.
    job = None if posting is not None else job_for(path, job_id)
    source = posting.strip() if posting is not None else (job or {}).get("description")
    if job:
        out["against"] = {"job_id": job["id"], "company": job.get("company"),
                          "title": job.get("title"),
                          "has_posting": bool(job.get("description"))}
    elif source:
        out["against"] = {"pasted": True, "has_posting": True}
    if source:
        terms = ats.keywords(source, (job or {}).get("company"))
        out["keywords"] = ats.match(terms, text)
    # Where the job is does not depend on which posting it is checked against.
    where = job or job_for(path, job_id)
    if where and (data.get("cv") or {}).get("photo") and photo_unusual(where):
        out["checks"].append({
            "id": "photo", "level": "warn",
            "title": "A photo, for a posting where CVs usually have none",
            "detail": "This application is in the UK, the US or Ireland, where "
                      "recruiters usually ask for CVs without a photo, and some "
                      "systems set aside CVs that have one. Turn it off for this "
                      "CV with the Photo chip in the editor."})
    return out


# Where recruiters usually ask for CVs without a photo, read from where the
# job is. Only the application's own location fields: a posting that merely
# mentions London is not evidence.
PHOTO_UNUSUAL = re.compile(
    r"(?i)\b(united kingdom|uk|u\.k\.|england|scotland|wales|northern ireland|"
    r"london|manchester|edinburgh|glasgow|bristol|cambridge, uk|united states|usa|"
    r"u\.s\.a?\.?|us|new york|san francisco|seattle|boston|austin|chicago|"
    r"los angeles|ireland|dublin|cork)\b")


def photo_unusual(job: dict) -> bool:
    where = " ".join(str(job.get(k) or "") for k in ("country", "location"))
    return bool(PHOTO_UNUSUAL.search(where))


# The parsing problems RenderCV's own design options solve, keyed by the
# id parse_checks gives them.
ATS_FIXES = {
    "icons": (["design", "header", "connections", "show_icons"], False),
    "urls": (["design", "header", "connections",
              "display_urls_instead_of_usernames"], True),
    "hyphens": (["design", "typography", "alignment"], "justified-with-no-hyphenation"),
}


def ats_fix(path: Path, fix: str) -> dict:
    if fix not in ATS_FIXES:
        raise ValueError(f"Unknown fix: {fix}")
    keys, value = ATS_FIXES[fix]
    from ruamel.yaml.comments import CommentedMap
    doc = yaml_rt.load(path.read_text(encoding="utf-8"))
    node = doc
    for k in keys[:-1]:
        # apply_patches will not create a missing branch, and the starter CV
        # has no design.header at all, so this builds it.
        if not isinstance(node.get(k), dict):
            node[k] = CommentedMap()
        node = node[k]
    node[keys[-1]] = value
    import io
    buf = io.StringIO()
    yaml_rt.dump(doc, buf)
    return write_doc(path, buf.getvalue(), "save")


def thumb(path: Path) -> dict:
    """The first page as it was last rendered, and whether that is still true.

    Read off disk rather than rendered, because the base card asks on every
    boot and a render is seconds of work. A page older than the YAML is still
    returned -- it is the right shape while the new one is made -- but marked,
    so the caller knows to render rather than show last week's CV as today's.
    """
    pdf = last_pdf(path)
    first = pdf.with_name(f"{pdf.stem}_1.png") if pdf else None
    if not first or not first.is_file():
        return {"png": None, "fresh": False}
    made = first.stat().st_mtime
    return {"png": f"/api/asset?path={rel(first)}&v={int(made * 1000)}",
            "fresh": made >= path.stat().st_mtime}


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
        # Shaped against the scratch copy, not the saved file: the block map has
        # to describe the document as it was just rendered, unsaved edits and
        # all. `tmp` is still on disk here -- the unlink below runs after.
        # Every document previews into this one folder, so last time's output
        # is still sitting in it. Clear it: otherwise the folder grows without
        # bound and stale pages linger for anything that looks at it.
        scratch = WORKSPACE / "assets" / ".preview"
        if scratch.is_dir():
            for stale in scratch.iterdir():
                if stale.is_file():
                    try:
                        stale.unlink()
                    except OSError:
                        pass
        return _shape(render_file(tmp, scratch), tmp)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def theme_preview(path: Path, theme: str, patches: list[dict] | None = None) -> dict:
    """Page one of this document in another theme, for the Design screen.

    The theme picker shows the document you are designing rather than a sample,
    so each tile is a real render. It gets its own folder per theme: the live
    preview clears its scratch folder on every run, and a tile that shared it
    would take the page you are looking at down with it.
    """
    if theme not in available_themes():
        raise ValueError(f"Unknown theme: {theme}")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", path.stem) or "doc"
    out = WORKSPACE / "assets" / ".themes" / stem / theme
    if out.is_dir():
        for stale in out.iterdir():
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass
    tmp = path.parent / (".cvstudio-theme" + path.suffix)
    try:
        tmp.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        apply_patches(tmp, list(patches or []) +
                      [{"path": ["design", "theme"], "value": theme}])
        result = render_file(tmp, out)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    pngs = result.get("png_pages") or []
    if not result.get("ok") or not pngs:
        return {"ok": False, "theme": theme}
    stamp = int(time.time() * 1000)
    return {"ok": True, "theme": theme, "pages": result.get("pages"),
            "png": f"/api/asset?path={rel(Path(pngs[0]))}&v={stamp}"}


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
        # Forced off at render time (cv_render.FORCED), so a switch for it
        # would be a control that does nothing.
        fields = [f for f in fields if f["path"] != ["page", "show_top_note"]]
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
            "/api/render": {"post": {"summary":
                "Render to PDF and PNG, with a map of where each block landed",
                "requestBody": body({"path": {"type": "string"}}), "responses": ok}},
            "/api/preview": {"post": {"summary":
                "Render unsaved content without writing the file",
                "requestBody": body({"path": {"type": "string"},
                                     "yaml": {"type": "string"},
                                     "patches": {"type": "array", "items": {}}}),
                "responses": ok}},
            "/api/letter/new": {"post": {"summary":
                "Write a cover letter scaffold for an application, linked to it",
                "requestBody": body({"job_id": {"type": "string"}, "name": {"type": "string"}}),
                "responses": ok}},
            "/api/letter/export": {"get": {"summary":
                "A cover letter as PDF, Word (.docx) or plain text",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}},
                               {"name": "format", "in": "query",
                                "schema": {"type": "string", "enum": ["pdf", "docx", "txt"]}}],
                "responses": ok}},
            "/api/photo": {"post": {"summary":
                "Save the workspace photo, a square JPEG the app has already cropped",
                "requestBody": body({"data": {"type": "string", "description": "base64 JPEG"}}),
                "responses": ok}},
            "/api/photo/remove": {"post": {"summary":
                "Delete the workspace photo and take it off every CV that showed it",
                "responses": ok}},
            "/api/language/add": {"post": {"summary":
                "Write a translation scaffold of a CV in another language",
                "requestBody": body({"path": {"type": "string"},
                                     "language": {"type": "string",
                                                  "description": "code, e.g. fr"}}),
                "responses": ok}},
            "/api/language/done": {"post": {"summary":
                "Mark a translation as covering everything its source says",
                "requestBody": body({"path": {"type": "string"}}),
                "responses": ok}},
            "/api/import": {"post": {"summary":
                "Read a CV from a PDF or a LinkedIn data archive, without writing anything",
                "requestBody": body({"name": {"type": "string"},
                                     "data": {"type": "string", "description": "base64"}}),
                "responses": ok}},
            "/api/theme-preview": {"post": {"summary":
                "Render page one of a document in another theme",
                "requestBody": body({"path": {"type": "string"},
                                     "theme": {"type": "string"},
                                     "patches": {"type": "array", "items": {}}}),
                "responses": ok}},
            "/api/new": {"post": {"summary": "Create a CV, blank or duplicated",
                "requestBody": body({"name": {"type": "string"},
                                     "kind": {"type": "string"},
                                     "from": {"type": "string"}}), "responses": ok}},
            "/api/language/set": {"post": {"summary":
                "Set the language a CV prints in (its locale); the text is not changed",
                "requestBody": body({"path": {"type": "string"}, "language": {"type": "string"}}),
                "responses": ok}},
            "/api/backups": {"get": {"summary":
                "This workspace's backups, newest first, and the folder they are in",
                "responses": ok}},
            "/api/backups/new": {"post": {"summary": "Back the workspace up now",
                "responses": ok}},
            "/api/backups/restore": {"post": {"summary":
                "Put a backup's files back into the workspace (what is there is backed up first)",
                "requestBody": body({"name": {"type": "string"}}), "responses": ok}},
            "/api/sample": {"post": {"summary":
                "Switch to freshly made sample data (on: true) or back to your own "
                "workspace (on: false). Your workspace is never written to. "
                "applications sets how many to make (default 64, up to 2000)",
                "requestBody": body({"on": {"type": "boolean"}, "applications": {"type": "integer"}}),
                "responses": ok}},
            "/api/prefs": {"post": {"summary":
                "Save the interface's preferences (theme, language, time zone, notifications) "
                "on this machine. set merges; replace swaps them all",
                "requestBody": body({"set": {"type": "object"}, "replace": {"type": "object"}}),
                "responses": ok}},
            "/api/base": {"post": {"summary":
                "Nominate the CV that tailored copies start from; null clears it",
                "requestBody": body({"path": {"type": "string"}}),
                "responses": ok}},
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
            "/api/clip": {"get": {"summary":
                "Save to CV Studio, the bookmark: the fixed port its window is "
                "served on, whether it is listening, and the bookmarklet itself",
                "responses": ok}},
            "/api/jobs/prep": {"get": {"summary":
                "Interview prep for an application's next round: likely questions "
                "(each with its source and your notes), stories that back the "
                "posting's asks, and questions to ask them; made here from the "
                "posting and the CV sent until an AI client writes better ones",
                "parameters": [{"name": "id", "in": "query", "schema": {"type": "string"}}],
                "responses": ok},
                "post": {"summary": "Save the prep whole: {id, prep}", "responses": ok}},
            "/api/jobs/draft": {"get": {"summary":
                "What an email about an application can be written from: your "
                "name (from its CV, else the base CV), the dates you applied and "
                "were interviewed, and the posting's first two duties",
                "parameters": [{"name": "id", "in": "query", "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/pack": {"get": {"summary":
                "An application's CV and cover letter as one PDF (format=pdf, CV "
                "first) or a zip of separate files (format=zip, with posting=1 to "
                "add the posting as Markdown). name sets the file name",
                "parameters": [{"name": n, "in": "query", "schema": {"type": "string"}}
                               for n in ("job", "format", "name", "posting")],
                "responses": {"200": {"description": "The file"}}}},
            "/api/pack/info": {"get": {"summary":
                "What /api/pack would put together: the suggested file name and "
                "whether there is a CV, a letter and a posting",
                "parameters": [{"name": "job", "in": "query", "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/search": {"get": {"summary":
                "Documents whose text holds every word of q (accents and case "
                "ignored), with the line where the first word was found",
                "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/alerts": {"get": {"summary":
                "Applications needing attention: interviews due, follow-ups "
                "due, interviews with no outcome, and silence since applying",
                "responses": ok}},
            "/api/calendar.ics": {"get": {"summary":
                "Interviews and follow-ups as an iCalendar file; ?id= for one application",
                "responses": ok}},
            "/api/jobs/trash": {"get": {"summary":
                "Deleted applications still in the trash, newest first; each can be restored",
                "responses": ok}},
            "/api/jobs/export": {"get": {"summary": "Export every job as JSON or CSV",
                "parameters": [{"name": "format", "in": "query",
                                "schema": {"type": "string", "enum": ["json", "csv"]}}],
                "responses": ok}},
            "/api/basediff": {"get": {"summary":
                "What a tailored CV changed from the document it was copied from",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/ats": {"post": {"summary":
                "What an ATS reads from a CV's PDF, and which of a posting's keywords it uses",
                "requestBody": body({"path": {"type": "string"},
                                     "job_id": {"type": "string"},
                                     "posting": {"type": "string"}}),
                "responses": ok}},
            "/api/ats/fix": {"post": {"summary":
                "Apply one of the design changes an ATS check offers, then check again",
                "requestBody": body({"path": {"type": "string"},
                                     "fix": {"type": "string",
                                             "enum": ["icons", "urls"]}}),
                "responses": ok}},
            "/api/open": {"post": {"summary":
                "Open a web link in the system browser",
                "requestBody": body({"url": {"type": "string"}}),
                "responses": ok}},
            "/api/thumb": {"get": {"summary":
                "A document's first page as last rendered, and whether it is current",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/ai": {"get": {"summary":
                "Whether each AI client is wired up to this build and workspace",
                "responses": ok}},
            "/api/ai/connect": {"post": {"summary":
                "Add this workspace to one AI client's MCP config",
                "requestBody": body({"client": {"type": "string",
                                                "enum": list(AI_CLIENTS)}}),
                "responses": ok}},
            "/api/skills": {"get": {"summary":
                "The CV Studio skills on this machine, and whether they need the MCP",
                "responses": ok}},
            "/api/skills/package": {"post": {"summary":
                "Zip each skill for upload to the Claude Desktop app",
                "responses": ok}},
            "/api/pulse": {"get": {"summary":
                "Document timestamps and recent AI activity, cheap to poll",
                "responses": ok}},
            "/api/asset": {"get": {"summary": "Fetch a rendered PDF or PNG",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}}], "responses": ok}},
            "/api/jobs/delete": {"post": {"summary":
                "Move one job application to the trash (kept 30 days)",
                "requestBody": body({"id": {"type": "string"}}), "responses": ok}},
            "/api/jobs/restore": {"post": {"summary":
                "Put a deleted application back, with its id and history",
                "requestBody": body({"id": {"type": "string"}}), "responses": ok}},
            "/api/doc/rename": {"post": {"summary":
                "Rename a CV or a letter in place; applications, the base CV and "
                "translation links follow it",
                "requestBody": body({"path": {"type": "string"}, "name": {"type": "string"}}),
                "responses": ok}},
            "/api/doc/delete": {"post": {"summary":
                "Move a CV or a letter to the workspace's .trash folder. Refuses the base CV "
                "and a document others are translated from",
                "requestBody": body({"path": {"type": "string"}}), "responses": ok}},
            "/api/reveal": {"post": {"summary":
                "Open the workspace, or one path inside it, in the file manager",
                "requestBody": body({"path": {"type": "string"}}), "responses": ok}},
            "/api/docs": {"get": {"summary": "This reference, as a page",
                "responses": ok}},
        },
        "components": {"securitySchemes": {"apiKey": {
            "type": "apiKey", "in": "header", "name": "X-API-Key"}}},
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

CLIP_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Save to CV Studio</title>
<style>
@font-face{font-family:'IBM Plex Sans';font-style:normal;font-weight:400 600;font-display:swap;
  src:url(/static/fonts/ibm-plex-sans-latin-var.woff2) format('woff2');
  unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,
  U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'IBM Plex Sans';font-style:normal;font-weight:400 600;font-display:swap;
  src:url(/static/fonts/ibm-plex-sans-latin-ext-var.woff2) format('woff2');
  unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,
  U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,
  U+2C60-2C7F,U+A720-A7FF}
:root{--app:#f7f6f3;--field:#fff;--t900:#1b1a17;--t700:#4a463d;--t600:#5b574d;--rule:#ddd8cc;
  --bd:#cfcabd;--acc:#c08a3e;--acc-text:#8a5316;--wash:#fbf4e8;--wash-line:#ecd9b8;--ok:#2f7a63;--bad:#a83519;--warn:#a8761f;
  --chrome:#1b1a17;--chrome-t:#f5f2ea}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--app:#22211d;--field:#26241f;--t900:#f4f2ef;
  --t700:#c6c0b0;--t600:#b6af9b;--rule:#38352e;--bd:#4a473e;--acc-text:#e8bc7c;--wash:#2e2820;--wash-line:#5a4a30;
  --ok:#6cc4a6;--bad:#ea8466;--chrome:#161513}}
:root[data-theme=dark]{--app:#22211d;--field:#26241f;--t900:#f4f2ef;--t700:#c6c0b0;--t600:#b6af9b;--rule:#38352e;
  --bd:#4a473e;--acc-text:#e8bc7c;--wash:#2e2820;--wash-line:#5a4a30;--ok:#6cc4a6;--bad:#ea8466;--chrome:#161513}
*{box-sizing:border-box}
body{margin:0;background:var(--app);color:var(--t900);font:14px/1.45 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
header{height:44px;display:flex;align-items:center;gap:9px;padding:0 16px;background:var(--chrome);color:var(--chrome-t);font-weight:600;font-size:13.5px}
header img{width:20px;height:20px}
main{display:flex;flex-direction:column;gap:14px;padding:18px;max-width:560px;margin:0 auto}
.who{display:flex;align-items:center;gap:12px}
.who b{font-size:15.5px;display:block}
.who small{font-size:12.5px;color:var(--t600)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 12px}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;font-weight:600;color:var(--t600)}
label.wide{grid-column:1/3}
input,select{height:36px;padding:0 10px;border:1px solid var(--bd);border-radius:8px;background:var(--field);
  color:var(--t900);font:inherit;font-size:13.5px;font-weight:400}
select{appearance:none;-webkit-appearance:none;padding-right:30px;cursor:pointer;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238b8578' stroke-width='2.4' stroke-linecap='round'><path d='M6 9l6 6 6-6'/></svg>");
  background-repeat:no-repeat;background-position:right 10px center}
.card{display:flex;flex-direction:column;gap:6px;padding:12px 14px;border:1px solid var(--rule);border-radius:10px;background:var(--field)}
.card.warn{background:var(--wash);border-color:var(--wash-line)}
.line{display:flex;align-items:center;gap:8px;font-size:13px}
.line svg{flex:none}
.muted{color:var(--t600);font-size:12.5px}
.acts{display:flex;gap:8px}
button{font:inherit;cursor:pointer}
.go{flex:1;height:40px;border:0;border-radius:8px;background:var(--acc);color:#1b1a17;font-size:14px;font-weight:600}
.alt{height:40px;padding:0 14px;border:1px solid var(--bd);border-radius:8px;background:var(--field);color:var(--t900);font-size:13.5px}
.link{align-self:flex-start;border:0;background:none;padding:0;color:var(--acc-text);font-size:12.5px;font-weight:500}
.err{color:var(--bad);font-size:12.5px}
[hidden]{display:none!important}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
</style></head><body>
<header><img src="/static/brand-mark.png" alt="">Save to CV Studio</header>
<main id="wait"><p class="muted" id="wait-say">Waiting for the job page…</p></main>
<main id="form" hidden>
  <div class="who"><div><b id="f-head">A new application</b><small id="f-sub">Read from the page: check it, then save</small></div></div>
  <div id="known" class="card" hidden>
    <b id="k-name" data-noi18n></b>
    <span class="muted" id="k-line"></span>
  </div>
  <div class="grid" id="fields">
    <label class="wide">Company<input id="f-company" autocomplete="off"></label>
    <label class="wide">Role<input id="f-title" autocomplete="off"></label>
    <label>Location<input id="f-location" autocomplete="off"></label>
    <label>Found on<input id="f-source" autocomplete="off"></label>
    <label class="wide">Status<select id="f-status"><option value="pending">Draft</option>
      <option value="applied">Applied today</option></select></label>
  </div>
  <div class="card" id="post-card">
    <div class="line" id="post-line"></div>
    <span class="muted" id="post-heads" data-noi18n></span>
    <div class="line"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--ok)" stroke-width="2.4"
      aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg><span>The link, so you can open it again</span></div>
  </div>
  <p class="err" id="f-err" hidden></p>
  <div class="acts"><button class="go" id="f-save">Save application</button><button class="alt" id="f-cancel">Cancel</button></div>
  <button class="link" id="f-new" hidden>It is a different role: save it as a new application</button>
</main>
<main id="done" hidden>
  <div class="card"><div class="line"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--ok)"
    stroke-width="2.4" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg><b id="d-head">Saved to CV Studio</b></div>
    <span class="muted" id="d-say"></span></div>
  <div class="acts"><button class="alt" id="d-close">Close</button></div>
</main>
<script>
var PREFS=__PREFS__||{};
const API_TOKEN=__API_TOKEN__;
if(PREFS.appearance==="dark"||PREFS.appearance==="light") document.documentElement.dataset.theme=PREFS.appearance;
</script>
<script src="/static/i18n.js"></script>
<script>
/* CV Studio's own window for the bookmark: the page it was opened from sends
   what it read, this shows it, and nothing is saved until you say so. */
const $=s=>document.querySelector(s);
const LANG=(()=>{ const l=PREFS.ui_lang||(navigator.language||"en").slice(0,2); return ["fr","es","pt"].includes(l)?l:"en" })();
document.documentElement.lang=LANG;
const D=(window.I18N||{})[LANG]||{};
const t=(s,v)=>{ let r=D[s]||s; if(v) r=r.replace(/\{(\w+)\}/g,(m,k)=>v[k]??m);
  return r.replace(/(\d+)([^\d()]*?)\(s\)/g,(m,n,mid)=>n+mid+((LANG==="fr"?+n<2:+n===1)?"":"s")) };
/* The interface's own words, in its language. */
document.querySelectorAll("header,#wait-say,#f-head,#f-sub,label,option,#fields label,.line span,#f-save,#f-cancel,#f-new,#d-head,#d-close")
  .forEach(el=>{ for(const n of el.childNodes) if(n.nodeType===3&&n.nodeValue.trim()){
    const k=n.nodeValue.trim(); if(D[k]) n.nodeValue=n.nodeValue.replace(k,D[k]) } });
document.title=t("Save to CV Studio");
const api=async(u,o)=>{ o=o||{}; o.headers=Object.assign({"Content-Type":"application/json"},API_TOKEN?{"X-API-Key":API_TOKEN}:{},o.headers);
  const r=await fetch(u,o); const j=await r.json().catch(()=>({error:"No answer from CV Studio."}));
  if(!r.ok||j.error) throw new Error(j.error||("HTTP "+r.status)); return j };

const BOARDS=[[/linkedin\./,"LinkedIn"],[/indeed\./,"Indeed"],[/welcometothejungle\./,"Welcome to the Jungle"],
  [/glassdoor\./,"Glassdoor"],[/wellfound\.|angel\.co/,"Wellfound"],[/xing\./,"XING"],[/hellowork\./,"HelloWork"],
  [/apec\.fr/,"APEC"],[/francetravail\.|pole-emploi\./,"France Travail"],[/jobteaser\./,"JobTeaser"],
  [/greenhouse\.io/,"Greenhouse"],[/lever\.co/,"Lever"],[/workable\.com/,"Workable"],[/ashbyhq\.com/,"Ashby"]];
const sourceOf=d=>{ for(const [re,n] of BOARDS) if(re.test(d.host)) return n; return d.site||d.host.replace(/^www\./,"") };
/* A posting's HTML as the Markdown the app keeps: headings, lists, bold, paragraphs. */
function toMd(html){
  if(!html) return "";
  if(/&lt;\/?[a-z]/i.test(html)&&!/<\/?[a-z]/i.test(html)){ const x=document.createElement("textarea"); x.innerHTML=html; html=x.value }
  const doc=new DOMParser().parseFromString("<div>"+html+"</div>","text/html");
  const walk=n=>{
    if(n.nodeType===3) return n.nodeValue.replace(/\s+/g," ");
    if(n.nodeType!==1) return "";
    const tag=n.tagName.toLowerCase(), inner=()=>[...n.childNodes].map(walk).join("");
    if(/^(script|style)$/.test(tag)) return "";
    if(/^h[1-6]$/.test(tag)) return "\n\n## "+inner().trim()+"\n\n";
    if(tag==="li") return "\n- "+inner().trim();
    if(tag==="br") return "\n";
    if(/^(p|div|section|ul|ol)$/.test(tag)) return "\n\n"+inner().trim()+"\n\n";
    if(/^(b|strong)$/.test(tag)){ const s=inner().trim(); return s?" **"+s+"** ":"" }
    return inner();
  };
  return walk(doc.body.firstChild).replace(/[ \t]+\n/g,"\n").replace(/\n[ \t]+/g,"\n").replace(/\n{3,}/g,"\n\n")
    .replace(/ +/g," ").replace(/\*\* ([,.;:])/g,"**$1").trim();
}
const placeOf=j=>{ const L=[].concat(j.jobLocation||[])[0]; const a=L&&L.address||{};
  const where=(typeof a==="string"?a:a.addressLocality||a.addressRegion||a.addressCountry&&(a.addressCountry.name||a.addressCountry))||"";
  return where||(/TELECOMMUTE/i.test(j.jobLocationType||"")?"Remote":"") };
const payOf=j=>{ const b=j.baseSalary; if(!b||!b.value) return ""; const v=b.value, c=b.currency||"";
  const n=x=>x==null?"":Number(x).toLocaleString("en"); const lo=v.minValue??v.value, hi=v.maxValue;
  return (hi&&hi!==lo?n(lo)+"–"+n(hi):n(lo))+(c?" "+c:"")+(v.unitText?" per "+String(v.unitText).toLowerCase():"") };
/* Links are kept, and matched, without the tracking noise boards add. */
const clean=u=>{ try{ const x=new URL(u); [...x.searchParams.keys()].forEach(k=>{ if(/^(utm_|ref|refId|trk|tracking|src|source|from)/i.test(k)) x.searchParams.delete(k) });
  x.hash=""; return x.toString() }catch(e){ return String(u||"") } };
const norm=u=>{ try{ const x=new URL(clean(u));
  return (x.host.replace(/^www\./,"")+x.pathname.replace(/\/+$/,"")+(x.search||"")).toLowerCase() }catch(e){ return String(u||"").toLowerCase() } };

let PAGE=null, KNOWN=null, POST="";
function show(id){ ["wait","form","done"].forEach(k=>$("#"+k).hidden=k!==id) }
/* The window is as tall as what it shows, not the size it was opened at. */
if(window.ResizeObserver) new ResizeObserver(()=>{
  const h=Math.ceil(document.body.getBoundingClientRect().height)+(outerHeight-innerHeight);
  if(Math.abs(h-outerHeight)>12) try{ resizeTo(outerWidth,Math.min(h,screen.availHeight)) }catch(e){}
}).observe(document.body);
async function receive(d){
  PAGE=d; const j=d.job||{};
  const org=j.hiringOrganization; const company=typeof org==="string"?org:(org&&org.name)||"";
  /* "(H/F)", "m/w/d" and the like say who may apply, not what the job is. */
  $("#f-title").value=(j.title||(!d.job&&d.title?d.title.split(/\s+[|–-]\s+/)[0]:"")||"")
    .replace(/\s*[([]?\b(?:[hfmwdx](?:\s*\/\s*[hfmwdx]){1,2}|all genders)\b[)\]]?/gi,"").replace(/\s*[-–|,]\s*$/,"").trim();
  $("#f-company").value=company.trim();
  $("#f-location").value=placeOf(j);
  $("#f-source").value=sourceOf(d);
  const pay=payOf(j);
  POST=d.job?toMd(j.description||""):(d.selection||"").trim();
  if(POST&&pay&&!POST.includes(pay)) POST="**Salary:** "+pay+"\n\n"+POST;
  const words=POST?POST.split(/\s+/).filter(Boolean).length:0;
  const heads=(POST.match(/^## .+$/gm)||[]).map(h=>h.slice(3)).slice(0,4);
  /* A real posting runs to a few hundred words; a few dozen is a page that
     only showed its first lines, and saying "done" would be wrong. */
  const thin=words>0&&words<120;
  $("#post-line").innerHTML=words&&!thin
    ?'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--ok)" stroke-width="2.4" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg><b></b><span class="muted" style="margin-left:auto"></span>'
    :thin?'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--warn)" stroke-width="2.4" aria-hidden="true"><path d="M12 8v5M12 17h.01"/><circle cx="12" cy="12" r="9"/></svg><b></b>'
    :'<b></b>';
  $("#post-card").classList.toggle("warn",thin);
  $("#post-line b").textContent=thin?t("Only {n} words of the posting",{n:words})
    :words?t("The posting, {n} words",{n:words}):t("No posting found on this page");
  if(words&&!thin) $("#post-line .muted").textContent=d.job?t("headings and lists kept"):t("the text you selected");
  $("#post-heads").textContent=words&&!thin?[...heads,pay].filter(Boolean).join(" · ")
    :t("Select the posting's text on the page, then click the button again.");
  show("form");
  try{
    const r=await api("/api/jobs");
    KNOWN=(r.jobs||[]).find(x=>x.url&&norm(x.url)===norm(j.url||d.url))||null;
  }catch(e){ KNOWN=null }
  if(KNOWN){
    $("#f-head").textContent=t("Already in CV Studio");
    $("#f-sub").textContent=t("Same link as an application you track");
    $("#known").hidden=false; $("#fields").hidden=true;
    $("#k-name").textContent=KNOWN.company+" · "+KNOWN.title;
    const has=(KNOWN.description||"").trim();
    $("#k-line").textContent=has?t("Its posting is saved already."):t("Its posting was never saved: this page has it.");
    $("#f-save").textContent=has?t("Replace the saved posting"):t("Save the posting to it");
    $("#f-save").disabled=!words;
    $("#f-new").hidden=false;
  }
}
$("#f-new").onclick=()=>{ KNOWN=null; $("#known").hidden=true; $("#fields").hidden=false; $("#f-new").hidden=true;
  $("#f-head").textContent=t("A new application"); $("#f-sub").textContent=t("Read from the page: check it, then save");
  $("#f-save").textContent=t("Save application"); $("#f-save").disabled=false };
$("#f-cancel").onclick=()=>window.close();
$("#d-close").onclick=()=>window.close();
$("#f-save").onclick=async()=>{
  const err=$("#f-err"); err.hidden=true; $("#f-save").disabled=true;
  try{
    const url=clean((PAGE.job&&PAGE.job.url)||PAGE.url);
    if(KNOWN){
      await api("/api/jobs/update",{method:"POST",body:JSON.stringify({id:KNOWN.id,description:POST,url:KNOWN.url||url})});
      $("#d-say").textContent=t("The posting is saved with {co}, {role}.",{co:KNOWN.company,role:KNOWN.title});
    }else{
      const title=$("#f-title").value.trim(), company=$("#f-company").value.trim();
      if(!title||!company) throw new Error(t("A job needs at least a title and a company."));
      const status=$("#f-status").value;
      const j=await api("/api/jobs",{method:"POST",body:JSON.stringify({title,company,
        location:$("#f-location").value.trim()||null,source:$("#f-source").value.trim()||null,url,
        description:POST||null,status:"pending"})});
      if(status==="applied") await api("/api/jobs/update",{method:"POST",body:JSON.stringify({id:j.id,status:"applied"})});
      $("#d-say").textContent=t("{co}, {role}: at the top of your applications.",{co:company,role:title});
    }
    show("done");
    setTimeout(()=>window.close(),4000);
  }catch(e){ err.textContent=(D[e.message]||e.message); err.hidden=false; $("#f-save").disabled=false }
};
addEventListener("message",e=>{
  if(!window.opener||e.source!==window.opener||!e.data||typeof e.data!=="object"||!e.data.url) return;
  receive(e.data);
});
if(window.opener) window.opener.postMessage("cvstudio-clip-ready","*");
else $("#wait-say").textContent=t("Open a job page and click Save to CV Studio in your bookmarks bar.");
</script></body></html>"""


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

    def _addressed_here(self) -> bool:
        """The request names this machine. A page on some other site can
        point its own domain at 127.0.0.1 (DNS rebinding) and then read and
        call this server as if it were its own origin; that request still
        carries the other domain in Host, so it stops here."""
        if OPEN_NETWORK:
            return True                   # served to the network: the token guards it
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
        return name in ALLOWED_HOSTS

    def _from_here(self) -> bool:
        """A write comes from one of this app's own pages, or from no page at
        all (a script, curl). Another site's page may send a request here but
        it announces itself in Origin; and one that is not JSON, the only kind
        a page can send across sites without asking first, is refused."""
        origin = self.headers.get("Origin")
        if origin is not None and not OPEN_NETWORK:
            o = urlparse(origin)
            if o.scheme != "http" or (o.hostname or "") not in LOOPBACK_NAMES \
                    or o.port not in OWN_PORTS:
                return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json" or int(self.headers.get("Content-Length") or 0) == 0

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
        if not self._addressed_here():
            return self._json({"error": "not found"}, 404)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/") and u.path != "/api/docs" and not self._authed():
            return self._json({"error": "unauthorised: supply X-API-Key"}, 401)
        try:
            if u.path == "/":
                page = INDEX_HTML.replace(
                    "__API_TOKEN__", json.dumps(API_TOKEN)).replace(
                    "__PREFS__", json.dumps(load_prefs()).replace("</", "<\\/"))
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/state":
                convert_legacy_letters()
                return self._json({
                    "documents": list_documents(), "base": base_cv(),
                    "languages": languages.catalogue(),
                    "photo": photo_info(),
                    "themes": available_themes(),
                    "page_sizes": PAGE_SIZES,
                    "fonts": font_families(),
                    "workspace": str(WORKSPACE),
                    "sample": in_sample(),
                    "first_run": FIRST_RUN and not in_sample(),
                    "starter": starter_untouched(),
                    "version": VERSION,
                    "platform": sys.platform,
                    "server_launch": server_launch(),
                    "api_token": API_TOKEN,
                    "port": self.server.server_address[1],
                })
            if u.path.startswith("/cvfont/"):
                # The fonts a CV prints in, for the letter editor's page to be
                # set in the same face as the PDF. Only RenderCV's bundled
                # families, named exactly: a family is a folder name here, and
                # anything else is not a font this serves.
                return self._cv_font(unquote(u.path[len("/cvfont/"):]))
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
                if f.suffix == ".png":
                    return self._send(200, f.read_bytes(), "image/png")
                ctype = {".js": "application/javascript", ".css": "text/css"}.get(f.suffix, "text/plain")
                return self._send(200, f.read_bytes(), ctype + "; charset=utf-8")
            if u.path == "/api/jobs":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                jobs_out = jobstore.list_jobs(
                    WORKSPACE, q.get("status", [None])[0], q.get("q", [None])[0],
                    q.get("node", [None])[0])
                # An application from before languages were recorded gets a
                # guess from its posting, offered rather than stored.
                for j in jobs_out:
                    if not j.get("language"):
                        j["language_guess"] = languages.detect(
                            f"{j.get('title') or ''} {j.get('description') or ''}")
                # A stored logo is a filename; the interface needs a URL it
                # can put in an <img>, and None when the file has gone.
                for j in jobs_out:
                    j["logo_url"] = logo_url(j.get("logo"))
                return self._json({"jobs": jobs_out,
                                   "statuses": jobstore.STATUSES,
                                   "nodes": jobstore.NODE_STATUSES,
                                   "labels": jobstore.LABELS,
                                   "logos": list_logos()})
            if u.path == "/api/funnel":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json(jobstore.funnel(
                    WORKSPACE, q.get("since", [None])[0]))
            if u.path == "/api/search":
                return self._json({"documents": search_documents(q.get("q", [""])[0])})
            if u.path == "/api/alerts":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json(jobstore.alerts(WORKSPACE))
            if u.path == "/api/calendar.ics":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                body = jobstore.ics(WORKSPACE, q.get("id", [None])[0]).encode("utf-8")
                return self._send(200, body, "text/calendar; charset=utf-8")
            if u.path == "/api/backups":
                return self._json({"backups": backups.listing(backup_dir(), WORKSPACE),
                                   "folder": str(backups.folder(backup_dir(), WORKSPACE)),
                                   "keep": backups.KEEP, "sample": in_sample()})
            if u.path == "/api/jobs/trash":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json({"trash": jobstore.list_trash(WORKSPACE),
                                   "days": jobstore.TRASH_DAYS})
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
                p = safe_path(q["path"][0])
                return self._json(load_letter(p) if is_letter(p) else load_doc(p))
            if u.path == "/clip":
                page = CLIP_HTML.replace(
                    "__API_TOKEN__", json.dumps(API_TOKEN)).replace(
                    "__PREFS__", json.dumps(load_prefs()).replace("</", "<\\/"))
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/clip":
                return self._json({"port": CLIP_PORT, "ok": CLIP_STATE["ok"],
                                   "why": CLIP_STATE["why"], "bookmarklet": bookmarklet()})
            if u.path == "/api/jobs/prep":
                try:
                    return self._json(interview_prep((q.get("id") or [""])[0]))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 404)
            if u.path == "/api/jobs/draft":
                try:
                    return self._json(draft_context((q.get("id") or [""])[0]))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 404)
            if u.path == "/api/pack" or u.path == "/api/pack/info":
                job_id = (q.get("job") or [""])[0]
                try:
                    if u.path == "/api/pack/info":
                        i = pack_info(job_id)
                        return self._json({k: i[k] for k in ("name", "cv", "letter", "posting")})
                    data, ctype, fname = application_pack(
                        job_id, (q.get("format") or ["pdf"])[0], (q.get("name") or [None])[0],
                        (q.get("posting") or [""])[0] in ("1", "true"))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 422)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/letter/export":
                p = safe_path(q["path"][0])
                try:
                    data, ctype, fname = letter_export(p, (q.get("format") or ["pdf"])[0])
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 422)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/ai":
                return self._json({"clients": ai_clients()})
            if u.path == "/api/pulse":
                return self._json(pulse())
            if u.path == "/api/basediff":
                return self._json(base_diff(safe_path(q["path"][0])))
            if u.path == "/api/thumb":
                return self._json(thumb(safe_path(q["path"][0])))
            if u.path == "/api/skills":
                return self._json(skills_list())
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
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

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
                weight = 700 if "bold" in n and "semi" not in n else 600 if "semibold" in n \
                    else 500 if "medium" in n else 300 if "light" in n else 400
                style = "italic" if "italic" in n else "normal"
                rules.append(f"@font-face{{font-family:'{fam.name}';src:url('/cvfont/"
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
            if u.path == "/api/save" and is_letter(safe_path(payload["path"])):
                return self._json({"ok": True, **save_letter(safe_path(payload["path"]), payload)})
            if u.path == "/api/save":
                p = safe_path(payload["path"])
                wrote = {}
                if "yaml" in payload:
                    wrote = write_doc(p, payload["yaml"], "save")
                elif "patches" in payload:
                    wrote = apply_patches(p, payload["patches"], "save")
                # Fields first, then structure. Adding an entry has to carry
                # whatever was already typed into the form with it, or the
                # reload that follows would hand back the file as it was
                # before those edits and quietly lose them.
                if payload.get("ops"):
                    did = apply_ops(p, payload["ops"], "save")
                    wrote = {"applied": (wrote.get("applied") or []) + did["applied"],
                             "missed": (wrote.get("missed") or []) + did["missed"],
                             "changed": (wrote.get("changed") or []) + did["changed"]}
                return self._json({"ok": True, **wrote, **load_doc(p)})
            if u.path == "/api/skills/package":
                try:
                    return self._json(package_skills())
                except (ValueError, OSError) as exc:
                    return self._json({"error": str(exc)}, 400)
            if u.path == "/api/ai/connect":
                try:
                    return self._json(ai_connect(payload.get("client", "")))
                except (ValueError, OSError) as exc:
                    return self._json({"error": str(exc)}, 400)
            if u.path == "/api/render":
                p = safe_path(payload["path"])
                return self._json(render_letter(p) if is_letter(p) else render(p))
            if u.path == "/api/letter/new":
                return self._json(new_letter(payload.get("job_id"), payload.get("name")))
            if u.path == "/api/preview":
                return self._json(preview(safe_path(payload["path"]),
                                          payload.get("yaml"),
                                          payload.get("patches")))
            if u.path == "/api/photo":
                import base64
                try:
                    saved = save_photo(
                        base64.b64decode(str(payload.get("data") or ""), validate=False))
                    ref = photo_ref(safe_path(payload["path"])) if payload.get("path") else None
                    return self._json({"ok": True, "photo": saved, "ref": ref})
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)})
            if u.path == "/api/photo/remove":
                return self._json(remove_photo())
            if u.path == "/api/language/add":
                try:
                    return self._json({"ok": True, **add_language(
                        safe_path(payload["path"]), str(payload.get("language") or ""))})
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)})
            if u.path == "/api/language/done":
                return self._json(mark_translation_current(safe_path(payload["path"])))
            if u.path == "/api/import":
                # The file arrives as base64 in the JSON body, read here and
                # never stored: the caller previews the result and writes it
                # with /api/save once the person has looked at it.
                import base64
                raw = str(payload.get("data") or "")
                if len(raw) > 56_000_000:
                    return self._json({"ok": False, "error": "That file is over 40 MB. LinkedIn's "
                                       "basic archive is much smaller: choose Profile, Positions, "
                                       "Education and Skills when you request it."})
                try:
                    data = base64.b64decode(raw, validate=False)
                    return self._json(importer.import_file(str(payload.get("name") or ""), data))
                except importer.ImportError_ as exc:
                    return self._json({"ok": False, "error": str(exc)})
            if u.path == "/api/theme-preview":
                return self._json(theme_preview(safe_path(payload["path"]),
                                                str(payload.get("theme") or ""),
                                                payload.get("patches")))
            if u.path == "/api/jobs":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                if not payload.get("logo") and payload.get("company"):
                    payload["logo"] = stored_logo(payload["company"])
                return self._json(jobstore.add_job(WORKSPACE, payload))
            if u.path == "/api/jobs/prep":
                try:
                    return self._json(save_interview_prep(payload.get("id", ""), payload.get("prep") or {}))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 404)
            if u.path == "/api/jobs/update":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json(jobstore.update_job(
                    WORKSPACE, payload.pop("id", ""), payload))
            if u.path == "/api/ats":
                return self._json(ats_report(safe_path(payload["path"]),
                                             payload.get("job_id"),
                                             payload.get("posting")))
            if u.path == "/api/ats/fix":
                p = safe_path(payload["path"])
                ats_fix(p, payload.get("fix", ""))
                return self._json({"ok": True, **ats_report(p, payload.get("job_id"),
                                                             payload.get("posting"))})
            if u.path == "/api/open":
                # A link out of the app. The desktop webview drops target=_blank
                # on the floor, so the page asks for it here and the system
                # browser opens it. Web links, and an email to write in the mail
                # app: this is not a way to launch whatever a stored URL names.
                link = str(payload.get("url") or "")
                if urlparse(link).scheme not in ("http", "https", "mailto"):
                    return self._json({"error": "Only web links can be opened."}, 400)
                webbrowser.open(link)
                return self._json({"ok": True})
            if u.path == "/api/reveal":
                target = WORKSPACE
                sub_ = payload.get("path")
                if payload.get("logs"):
                    target = log_path().parent
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
                    return self._json({"error": str(exc)}, 500)
                return self._json({"ok": True})
            if u.path == "/api/jobs/delete":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                jobstore.delete_job(WORKSPACE, payload.get("id", ""))
                return self._json({"ok": True})
            if u.path == "/api/jobs/restore":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                try:
                    return self._json(jobstore.restore_job(WORKSPACE, payload.get("id", "")))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 404)
            if u.path == "/api/doc/rename":
                try:
                    return self._json(rename_document(payload.get("path", ""), payload.get("name", "")))
                except FileNotFoundError:
                    return self._json({"error": "That document no longer exists."}, 404)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 409)
            if u.path == "/api/doc/delete":
                try:
                    return self._json(delete_document(payload.get("path", "")))
                except FileNotFoundError:
                    return self._json({"error": "That document no longer exists."}, 404)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 409)
            if u.path == "/api/new":
                raw = payload.get("name") or "untitled"
                name = "".join(c for c in raw if c.isalnum() or c in "-_ ").strip()
                if not name:
                    return self._json({"error": "Please give it a name."}, 400)
                kind = payload.get("kind") or "cv"
                if kind == "letter" and not payload.get("from"):
                    return self._json(new_letter(payload.get("job_id"), name))
                folder = "letters" if kind == "letter" else "profile"
                src_ext = Path(str(payload.get("from") or "")).suffix
                dest = safe_path(f"{folder}/{name}{src_ext if src_ext == letters.EXT else '.yaml'}")
                if dest.exists():
                    return self._json({"error": "Something with that name already exists."}, 409)
                src = payload.get("from")
                if src:
                    body = safe_path(src).read_text(encoding="utf-8")
                else:
                    body = STARTER_LETTER if kind == "letter" else STARTER_CV
                dest.write_text(body, encoding="utf-8")
                # Which CV this was tailored from is the whole basis of "what
                # did this change from the base", and the Base on picker knew
                # it all along -- it was simply thrown away on write.
                note_lineage(dest, rel(safe_path(src)) if src else None)
                return self._json({"ok": True, "path": rel(dest)})
            if u.path == "/api/language/set":
                return self._json({"ok": True, **set_cv_language(
                    safe_path(payload.get("path", "")), payload.get("language", ""))})
            if u.path == "/api/backups/new":
                if in_sample():
                    return self._json({"error": "Sample data is not backed up; it is made fresh each time."}, 409)
                return self._json({"ok": True, **backups.make(backup_dir(), WORKSPACE, "manual")})
            if u.path == "/api/backups/restore":
                if in_sample():
                    return self._json({"error": "Go back to your workspace to restore it."}, 409)
                try:
                    return self._json({"ok": True, **backups.restore(backup_dir(), WORKSPACE,
                                                                     payload.get("name", ""))})
                except FileNotFoundError:
                    return self._json({"error": "That backup no longer exists."}, 404)
            if u.path == "/api/prefs":
                # {"set": {key: value}} merges; {"replace": {...}} is the one
                # move from the page's old storage into the file.
                if isinstance(payload.get("replace"), dict):
                    return self._json({"ok": True, "prefs": save_prefs(payload["replace"], replace=True)})
                return self._json({"ok": True, "prefs": save_prefs(payload.get("set") or {})})
            if u.path == "/api/sample":
                # Sample data lives in its own folder; switching never writes
                # to the workspace you came from.
                if payload.get("on"):
                    try:
                        n = int(payload.get("applications") or 64)
                    except (TypeError, ValueError):
                        n = 64
                    return self._json({"ok": True, **open_sample(n)})
                close_sample()
                return self._json({"ok": True})
            if u.path == "/api/base":
                # {"path": null} clears the nomination rather than deleting
                # anything: the document is untouched either way.
                set_base_cv(payload.get("path") or None)
                return self._json({"ok": True, "base": base_cv()})
            return self._json({"error": "not found"}, 404)
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 403)
        except Exception as exc:
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


INDEX_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CV Studio</title>
<link rel="stylesheet" href="/static/app.css">
</head><body>
<script>
/* Ahead of everything else: a chosen theme should not flash the other one
   first. Guarded because storage throws outright in some privacy modes. */
/* Preferences come with the page, from a file the server keeps; before there
   is one, from what this browser stored, which is then moved into it. */
var _p=__PREFS__;
if(!_p||typeof _p!=="object"){ try{_p=JSON.parse(localStorage.getItem("cvstudio.prefs")||"{}")}catch(e){_p={}}
  window.CVS_PREFS_MOVE=true }
window.CVS_PREFS=_p;
if(_p.appearance==="dark"||_p.appearance==="light") document.documentElement.dataset.theme=_p.appearance;
const API_TOKEN=__API_TOKEN__;
</script>
<script src="/static/i18n.js"></script>
<script src="/static/worldmap.js"></script>

<!-- Marks for the AI clients. The Claude one is as published by Anthropic, and
     identifies that integration and nothing else: see THIRD-PARTY-NOTICES.md. -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
  <!-- Job boards, for where a posting was found. Simple Icons (CC0), except
       LinkedIn, which asked Simple Icons to remove it; that one is Font
       Awesome's (CC BY 4.0). See THIRD-PARTY-NOTICES.md. -->
  <symbol id="board-linkedin" viewBox="0 0 448 512"><path fill="currentColor" d="M416 32H31.9C14.3 32 0 46.5 0 64.3v383.4C0 465.5 14.3 480 31.9 480H416c17.6 0 32-14.5 32-32.3V64.3c0-17.8-14.4-32.3-32-32.3zM135.4 416H69V202.2h66.5V416zm-33.2-243c-21.3 0-38.5-17.3-38.5-38.5S80.9 96 102.2 96c21.2 0 38.5 17.3 38.5 38.5 0 21.3-17.2 38.5-38.5 38.5zm282.1 243h-66.4V312c0-24.8-.5-56.7-34.5-56.7-34.6 0-39.9 27-39.9 54.9V416h-66.4V202.2h63.7v29.2h.9c8.9-16.8 30.6-34.5 62.9-34.5 67.2 0 79.7 44.3 79.7 101.9V416z"/></symbol>
  <symbol id="board-indeed" viewBox="0 0 24 24"><path fill="currentColor" d="M11.566 21.5633v-8.762c.2553.0231.5009.0346.758.0346 1.2225 0 2.3739-.3206 3.3506-.8928v9.6182c0 .8219-.1957 1.4287-.5757 1.8338-.378.4033-.8808.6049-1.491.6049-.6007 0-1.0766-.2016-1.468-.6183-.3781-.4032-.5739-1.01-.5739-1.8184zM11.589.5659c2.5447-.8929 5.4424-.8449 7.6186.987.405.3687.8673.8334 1.0515 1.3806.2207.6913-.7695-.073-.9057-.167-.71-.4532-1.4182-.8334-2.2127-1.0946C12.8614.3873 8.8122 2.709 6.2945 6.315c-1.0516 1.5939-1.7367 3.2721-2.299 5.1174-.0614.2017-.1094.4647-.2207.6413-.1113.2036-.048-.5453-.048-.5702.0845-.7623.2438-1.4997.4414-2.237C5.3292 5.3375 7.897 2.0655 11.5891.5658zm4.9281 7.0587c0 1.6686-1.353 3.0224-3.0205 3.0224-1.6677 0-3.0186-1.3538-3.0186-3.0224 0-1.6687 1.351-3.0224 3.0186-3.0224 1.6676 0 3.0205 1.3518 3.0205 3.0224Z"/></symbol>
  <symbol id="board-glassdoor" viewBox="0 0 24 24"><path fill="currentColor" d="M14.1093.0006c-.0749-.0074-.1348.0522-.1348.127v3.451c0 .0673.0537.1194.121.127 2.619.172 4.6092.9501 4.6092 3.6814H13.086a.1343.1343 0 0 0-.1348.1347v8.9644c0 .0748.06.1347.1348.1347h10.0034c.0748 0 .1347-.0599.1347-.1347V7.342c0-2.2374-.7996-4.0558-2.4159-5.3279C19.3191.8469 17.0874.1428 14.1093.0006ZM.9107 7.387a.1342.1342 0 0 0-.1347.1347v8.9566c0 .0748.06.1347.1347.1347h5.6189c0 2.7313-1.9902 3.5094-4.6091 3.6815-.0674.0075-.1192.0596-.1192.127v3.451c0 .0747.06.1343.1348.1269 2.9781-.1422 5.2078-.8463 6.6969-2.0136 1.6163-1.272 2.4159-3.0905 2.4159-5.3278V7.5217a.1343.1343 0 0 0-.1348-.1347z"/></symbol>
  <symbol id="board-greenhouse" viewBox="0 0 24 24"><path fill="currentColor" d="M16.279 7.13c0 1.16-.49 2.185-1.293 2.987-.891.891-2.184 1.114-2.184 1.872 0 1.025 1.65.713 3.231 2.295 1.048 1.047 1.694 2.43 1.694 4.034C17.727 21.482 15.187 24 12 24c-3.187 0-5.727-2.518-5.727-5.68 0-1.607.646-2.989 1.694-4.036 1.582-1.582 3.23-1.27 3.23-2.295 0-.758-1.292-.98-2.183-1.872-.802-.802-1.293-1.827-1.293-3.03 0-2.318 1.895-4.19 4.212-4.19.446 0 .847.067 1.181.067.602 0 .914-.268.914-.691 0-.245-.112-.557-.112-.891 0-.758.647-1.382 1.427-1.382s1.404.646 1.404 1.426c0 .825-.647 1.204-1.137 1.382-.401.134-.713.312-.713.713 0 .758 1.382 1.493 1.382 3.61zm-.446 11.19c0-2.206-1.627-3.99-3.833-3.99-2.206 0-3.833 1.784-3.833 3.99 0 2.184 1.627 3.989 3.833 3.989 2.206 0 3.833-1.808 3.833-3.99zM14.518 7.086c0-1.404-1.136-2.562-2.518-2.562S9.482 5.682 9.482 7.086 10.618 9.65 12 9.65s2.518-1.159 2.518-2.563z"/></symbol>
  <symbol id="board-wellfound" viewBox="0 0 24 24"><path fill="currentColor" d="M23.998 8.128c.063-1.379-1.612-2.376-2.795-1.664-1.23.598-1.322 2.52-.156 3.234 1.2.862 2.995-.09 2.951-1.57zm0 7.748c.063-1.38-1.612-2.377-2.795-1.665-1.23.598-1.322 2.52-.156 3.234 1.2.863 2.995-.09 2.951-1.57zm-20.5 1.762L0 6.364h3.257l2.066 8.106 2.245-8.106h3.267l2.244 8.106 2.065-8.106h3.257l-3.54 11.274H11.39c-.73-2.713-1.46-5.426-2.188-8.14l-2.233 8.14H3.5z"/></symbol>
  <symbol id="board-welcometothejungle" viewBox="0 0 24 24"><path fill="currentColor" d="M22.62 3.783c-1.115-1.811-4.355-2.604-6.713-.265-.132.135-.306.548.218 1.104 1.097 1.149 6.819 7.046 4.702 12.196-1.028 2.504-3.953 2.073-5.052-2.076a23.184 23.184 0 0 1-.473-9.367s.105-.394-.065-.52c-.117-.087-.305-.05-.547.33-.06.096-.048.076-.106.178l-.003.002c-1.622 2.688-3.272 5.874-4.049 7.07.38-1.803-.101-4.283-.85-6.359l-.142-.375c-.692-1.776-1.524-2.974-1.776-3.245-.03-.033-.105-.094-.353-.094H.398c-.49 0-.448.412-.293.561 1.862 2.178 7.289 10.343 4.773 18.355-.194.619.11.944.612.305 2.206-2.81 4.942-7.598 6.925-11.187-.437 1.245-.822 2.63-1.028 4.083-.435 3.064.487 5.37 1.162 6.58.345.619.803.998 1.988.824 6.045-.885 8.06-6.117 8.805-8.77 1.357-4.839.363-7.568-.722-9.33"/></symbol>
  <symbol id="board-xing" viewBox="0 0 24 24"><path fill="currentColor" d="M18.188 0c-.517 0-.741.325-.927.66 0 0-7.455 13.224-7.702 13.657.015.024 4.919 9.023 4.919 9.023.17.308.436.66.967.66h3.454c.211 0 .375-.078.463-.22.089-.151.089-.346-.009-.536l-4.879-8.916c-.004-.006-.004-.016 0-.022L22.139.756c.095-.191.097-.387.006-.535C22.056.078 21.894 0 21.686 0h-3.498zM3.648 4.74c-.211 0-.385.074-.473.216-.09.149-.078.339.02.531l2.34 4.05c.004.01.004.016 0 .021L1.86 16.051c-.099.188-.093.381 0 .529.085.142.239.234.45.234h3.461c.518 0 .766-.348.945-.667l3.734-6.609-2.378-4.155c-.172-.315-.434-.659-.962-.659H3.648v.016z"/></symbol>
  <symbol id="board-monster" viewBox="0 0 24 24"><path fill="currentColor" d="M0 0V24H5.42V12.39L12 18.19L18.58 12.39V24H24V0L12 11.23L0 0Z"/></symbol>
  <symbol id="board-ycombinator" viewBox="0 0 24 24"><path fill="currentColor" d="M0 24V0h24v24H0zM6.951 5.896l4.112 7.708v5.064h1.583v-4.972l4.148-7.799h-1.749l-2.457 4.875c-.372.745-.688 1.434-.688 1.434s-.297-.708-.651-1.434L8.831 5.896h-1.88z"/></symbol>
  <symbol id="claude-mark" viewBox="0 0 24 24"><path fill="currentColor"
    fill-rule="nonzero" d="M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z"/></symbol>

  <symbol id="openai-mark" viewBox="0 0 24 24"><path fill="currentColor"
    fill-rule="evenodd" d="M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z"/></symbol>
  <!-- Nous Research, who build Hermes Agent. There is no separate mark
       published for the agent itself -- the only artwork in its repo is a
       wordmark banner -- so the row is identified the same way the OpenAI
       row is: by the company's own mark, from the same source, drawn
       exactly as published rather than redrawn or simplified. It is a
       detailed illustration, so it reads as a silhouette at the 13px the
       title bar draws it at and only resolves in the Settings badge. -->
  <symbol id="hermes-mark" viewBox="0 0 24 24" fill="currentColor"
    fill-rule="evenodd"><path d="M5.938 12.835c.127-.039.285.02.373.143.028.038.036.092.046.14.003.014-.02.033-.04.05-.124-.098-.24-.194-.354-.291-.011-.01-.016-.027-.025-.042zM8.396 9.412c.195-.032.39-.06.588-.05a.54.54 0 01.148.026c.202.071.402.147.601.224.028.01.05.036.075.055l-.013.027a9.203 9.203 0 01-.26-.089c-.115-.038-.213-.077-.315-.098-.25-.05-.25-.046-.292-.014l.574.144c.275.139.55.276.823.417.042.022.09.057.107.098.026.06.063.076.117.072.066-.006.132-.017.213-.027l-.04.086c.051.08.142.02.216.064-.074.13-.247.09-.334.199l.061.074-.12.087c0 .106-.038.168-.306.243l.026.085-.196.042.07.124h-.25l-.007.137c-.081-.01-.161-.018-.244-.027l-.053.123c-.027-.008-.052-.011-.073-.023-.067-.038-.128-.056-.195.006-.019.017-.063.014-.093.008-.026-.006-.05-.029-.07-.042-.11.095-.11.095-.208.003-.057.046-.12.074-.186.011-.063.027-.123-.02-.178-.014-.07.007-.097-.035-.133-.07l-.13.033c-.013-.236-.194-.19-.34-.203.005-.072.05-.092.095-.094a.474.474 0 01.159.022c.164.05.32.12.496.138.203.021.405.029.601-.015.265-.059.52-.149.707-.365.049-.056.083-.127.117-.195.019-.038.02-.084-.02-.116a1.397 1.397 0 00-.382-.217c.024.12-.031.182-.115.221 0 .014-.004.025 0 .03.08.115.084.16-.007.267a1.39 1.39 0 01-.218.211.477.477 0 01-.641-.05 1.36 1.36 0 01-.133-.152c-.078-.107-.076-.108-.033-.236-.165-.08-.128-.226-.104-.364.008-.05.028-.096.049-.163-.04.014-.067.017-.087.032a.897.897 0 00-.316.357c-.007.016-.01.034-.02.047-.012.015-.034.038-.045.035-.02-.006-.037-.027-.05-.045-.008-.012-.007-.032-.012-.057h-.126l.053-.172a14.82 14.82 0 00-.039-.049l.11-.284c-.06.026-.091.044-.124.051-.03.007-.064 0-.095 0 0-.031-.01-.07.004-.092.149-.22.305-.428.593-.476z"></path><path d="M8.06 10.788c-.003-.038-.004-.075.037-.062.016.006.034.048.028.067-.01.04-.038.032-.064-.005z"></path><path clip-rule="evenodd" d="M11.981.009c.226-.012.453-.011.679 0 .247.01.495.024.74.062.401.064.798.157 1.19.273.463.138.92.299 1.356.511a7.31 7.31 0 012.948 2.642c.292.469.536.963.739 1.479.219.556.446 1.11.623 1.683.204.654.329 1.326.458 1.997.097.504.182 1.01.29 1.511.156.722.329 1.44.494 2.16.186.812.4 1.615.63 2.415.102.355.193.713.282 1.072.11.436.202.876.254 1.323.031.278.066.557.073.837a7.56 7.56 0 01-.017.88c-.037.413-.1.818-.226 1.212a5.017 5.017 0 01-.915 1.649l-.13.156.018.023c.043-.023.088-.041.127-.068.2-.138.373-.307.531-.49.4-.46.721-.973.975-1.529a3.59 3.59 0 00.325-1.72c-.024-.424-.097-.834-.3-1.213-.013-.027-.015-.06-.03-.121.05.035.082.048.101.072.107.13.22.258.315.398.33.494.46 1.052.486 1.64a3.75 3.75 0 01-.47 1.97c-.36.655-.887 1.14-1.526 1.506-.193.111-.394.21-.595.308-.157.078-.248.211-.318.365a.522.522 0 00-.033.406.359.359 0 01.013.139c-.005.077-.077.155-.14.162-.054.006-.125-.043-.15-.116a1.206 1.206 0 01-.06-.233c-.04-.314-.155-.6-.308-.87a3.906 3.906 0 00-.73-.91 2.129 2.129 0 00-.897-.524 4.093 4.093 0 00-.692-.131c-.075-.008-.15-.04-.22.01.18.06.363.11.538.18.434.173.82.43 1.18.728.308.255.58.543.794.884.098.155.186.315.227.496.027.123.042.25.067.375.013.062-.002.109-.053.144-.047.033-.122.034-.163-.01a.455.455 0 01-.08-.14c-.03-.073-.038-.159-.078-.225a7.314 7.314 0 00-1.423-1.664c-.16-.137-.329-.26-.537-.323-.376-.114-.753-.203-1.15-.154-.213.025-.427.032-.64.053a1.6 1.6 0 00-.736.278 5.14 5.14 0 00-.834.72c-.329.342-.642.699-.955 1.055-.136.155-.264.319-.314.531a5.227 5.227 0 00-.012.051.096.096 0 01-.09.076h-.31c-.046 0-.082-.048-.072-.094.023-.108.045-.216.07-.324.075-.325.19-.635.368-.917.024-.039.04-.088.104-.08l.01.049.027.077c.28-.435.571-.834.996-1.135.283-.204.584-.378.89-.55a.196.196 0 00-.098-.002c-.162.043-.325.084-.485.134-.402.124-.764.33-1.11.566-.147.1-.298.193-.414.333a7.314 7.314 0 00-1.07 1.767.845.845 0 00-.04.12.075.075 0 01-.072.056h-.494c-.04 0-.062-.051-.036-.082.123-.14.246-.282.377-.415.275-.281.58-.532.777-.884.027-.048.063-.09.095-.135.238-.333.54-.607.818-.902.082-.086.175-.16.26-.24.029-.027.053-.057.079-.085l-.018-.025-.135.041c-.034.017-.07.031-.102.05-.248.144-.494.292-.743.433-.408.23-.825.439-1.209.711-.281.2-.591.358-.889.533-.02.012-.044.015-.08.028-.015-.135.143-.201.108-.336-.033.014-.064.02-.085.038-.111.096-.227.19-.328.296-.148.157-.284.325-.425.488-.125.143-.25.286-.373.431A.153.153 0 019.89 24H8.762a.316.316 0 00.016-.042c.028-.09.085-.172.083-.28-.091-.018-.162.001-.212.077a4.45 4.45 0 00-.136.215c-.01.016-.024.03-.042.03h-.093c-.019 0-.029-.022-.017-.037.071-.088.14-.178.209-.268.001-.002-.006-.012-.012-.024-.014.004-.03.006-.045.013-.176.09-.352.181-.527.274a.363.363 0 01-.168.042H5.202c-.026 0-.039-.036-.019-.053.21-.178.402-.374.558-.605.335-.496.538-1.047.667-1.629.004-.02-.003-.043-.006-.091-.037.048-.059.072-.076.1a1.943 1.943 0 01-.334.415c-.28.258-.59.448-.983.464-.297.012-.588 0-.865-.127-.46-.21-.722-.57-.794-1.072-.025-.17-.017-.171-.182-.219A3.513 3.513 0 011.97 20.6a2.286 2.286 0 01-.808-1.13 3.569 3.569 0 01-.16-1.245c.002-.034.016-.067.024-.1.032.023.046.043.05.066.033.153.059.308.096.46.086.355.257.664.516.92.258.256.571.419.91.532.358.118.717.138 1.07-.016a1.89 1.89 0 00.621-.452c.328-.348.533-.76.648-1.223.009-.034.005-.071.007-.11-.015.006-.026.006-.03.011-.031.05-.064.1-.093.152-.284.502-.679.887-1.196 1.135-.351.17-.718.255-1.11.159a1.607 1.607 0 01-.971-.64 2.006 2.006 0 01-.368-.924 2.903 2.903 0 01.02-.886c.05-.439.466-1.17.742-1.271-.02.063-.035.112-.053.16-.043.116-.097.227-.13.345a1.901 1.901 0 00-.05.82c.033.212.09.416.204.6.147.236.346.407.62.465.11.023.225.014.338.018a.576.576 0 00.386-.131c.164-.128.282-.292.366-.481.168-.375.24-.777.309-1.179.05-.296.093-.594.133-.893.039-.281.071-.563.104-.845.026-.232.048-.464.074-.696.024-.228.052-.455.076-.683.024-.227.047-.455.069-.683.013-.14.022-.28.034-.42l.037-.417c.022-.25.041-.5.065-.748.008-.082-.02-.132-.09-.177a2.46 2.46 0 01-.492-.418c-.1-.109-.188-.228-.282-.342-.035-.042-.056-.097-.116-.118a2.084 2.084 0 00.275.597c.06.092.131.176.196.265.063.086.182.115.234.226-.028.003-.046.01-.06.006a4.74 4.74 0 01-.22-.057 2.71 2.71 0 01-1.287-.819c-.435-.487-.656-1.076-.71-1.723a5.206 5.206 0 01.014-1.06c.072-.602.22-1.186.45-1.745.155-.376.338-.741.526-1.102.205-.393.466-.75.765-1.076.512-.559 1.104-1.024 1.726-1.448.717-.49 1.478-.898 2.277-1.233C8.244.828 8.767.632 9.31.494c.655-.166 1.31-.33 1.982-.415.229-.03.458-.058.688-.07zm-1.847 22.82c-.07.06-.147.111-.207.18-.238.27-.464.549-.668.869l-.044.108a.177.177 0 00.093-.057c.174-.19.351-.378.519-.574.104-.122.195-.255.288-.386.024-.034.03-.08.046-.12l-.027-.02zm1.65-3.695a5.51 5.51 0 00-.653.593l-.37.386a.963.963 0 01-.377.25 1.372 1.372 0 01-.467.09c-.044 0-.087.006-.151.012.028.058.043.097.064.131.15.242.301.482.45.724.136.22.276.438.399.666.068.125.105.267.156.404.077.027.14-.018.202-.048.29-.135.579-.274.867-.412.213-.101.437-.186.636-.31.347-.215.68-.455 1.018-.685.015-.01.026-.028.042-.046-.023-.019-.038-.037-.056-.044-.287-.111-.527-.3-.77-.482a5.319 5.319 0 01-.506-.42 1.757 1.757 0 01-.41-.653c-.019-.049-.045-.095-.075-.156zm-5.847.264c-.06.096-.097.194-.132.293a3.38 3.38 0 01-.555 1.01c-.2.25-.455.412-.762.493-.23.06-.464.076-.7.07-.048-.002-.097.002-.158.005.016.04.021.066.035.085.1.145.23.246.4.295.157.046.316.034.498.023.181-.037.343-.115.485-.234.238-.199.402-.454.536-.732.175-.363.264-.751.342-1.144.01-.053.008-.11.011-.164zm14.945-4.586c.008.029.016.057.027.107.024.155.051.31.072.464.03.219.067.437.078.657.017.344.027.689-.014 1.033-.037.315-.063.633-.116.946a6.153 6.153 0 01-.46 1.518c-.008.018-.01.039-.02.082.047-.03.077-.042.098-.064.085-.083.17-.167.248-.255.271-.305.458-.66.596-1.043.18-.498.228-1.011.145-1.531-.103-.65-.33-1.263-.597-1.881a9.055 9.055 0 00-.024-.055l-.033.022zM5.797 8.29a.26.26 0 00.018.153c.124.251.25.501.379.75.025.049.066.09.03.163-.284.06-.578.119-.88.255.059.038.097.06.132.087.042.032.112.058.09.12-.01.033-.075.048-.117.072.017.01.043.021.067.036.166.102.33.207.447.368.138.192.229.404.188.644-.079.469-.306.85-.69 1.132-.054.04-.106.083-.161.122a.243.243 0 00-.103.245.77.77 0 00.055.195c.083.196.22.35.375.492.083.076.159.164.222.257a.37.37 0 01.025.377c-.023.05-.05.099-.076.148-.03.06-.028.111.022.162.041.042.08.089.112.138.038.058.078.079.147.05a.486.486 0 01.333-.006c.16.046.302.126.444.21.13.077.264.149.4.219.067.035.14.05.219.026.071-.022.124.01.145.076.02.064-.003.108-.074.139-.07.03-.137.063-.209.088-.1.035-.201.073-.314.077-.013-.107.11-.088.127-.159-.206-.126-.643-.145-.801-.034.063.112.035.21-.096.313-.13-.1-.025-.202.002-.3a.209.209 0 00-.249.17c-.015.101.067.216.178.224.108.007.218-.005.326-.012.06-.005.12-.027.199 0-.103.123-.248.127-.357.19.002.05.07.086.019.131-.053.048-.095-.001-.132-.03-.08-.063-.16-.126-.231-.197a.474.474 0 01-.157-.311.52.52 0 00-.043-.172c-.032-.074-.032-.137.033-.19-.018-.03-.028-.053-.045-.072a1.222 1.222 0 01-.196-.369c-.053-.137-.046-.264.048-.381.024-.03.05-.06.064-.095a.664.664 0 00.047-.168c.017-.165-.064-.287-.182-.387-.186-.156-.36-.322-.46-.551-.005-.011-.024-.017-.037-.026-.011.017-.024.027-.025.038-.019.185-.045.37-.052.557-.014.377.058.743.162 1.104.118.41.289.798.488 1.173.267.502.537 1.002.812 1.5.055.098.13.189.208.27.198.202.452.272.724.273.202 0 .404-.006.605-.026.295-.03.59-.073.884-.113.183-.025.365-.057.548-.08.21-.026.38.073.522.21.16.156.305.327.447.5.22.265.397.56.554.867.05.098.07.1.147.03.13-.121.26-.242.394-.36.067-.059.088-.12.067-.213a3.535 3.535 0 01-.085-.796c.002-.157.006-.314.018-.471.015-.224.03-.45.06-.672a59.114 59.114 0 01.362-2.298c.087-.493.182-.984.268-1.477.06-.347.118-.694.162-1.043.034-.273.055-.55.063-.825.011-.332.003-.665.002-.998 0-.077.004-.155-.01-.23-.028-.142-.01-.155-.162-.19a5.826 5.826 0 00-.607-.107c-.146-.018-.207-.053-.221-.19-.006-.049-.025-.098-.041-.146-.009-.025-.024-.048-.046-.09l-.025.264c-.009.096-.029.116-.127.115-.055 0-.11-.008-.164-.008-.476 0-.952-.008-1.426.032-.095.008-.173-.015-.226-.103-.04-.066-.088-.126-.134-.186-.063-.084-.086-.093-.182-.06-.195.068-.388.138-.582.21a2.71 2.71 0 00-.675.394.986.986 0 01-.323.168c-.033.01-.07.008-.127.013.02-.066.024-.114.047-.15.064-.105.135-.205.205-.306.023-.033.049-.063.073-.095l-.015-.023-.201.037c-.146.04-.296.07-.437.122-.148.053-.266.023-.386-.072a3.623 3.623 0 01-.733-.786l-.093-.132zm8.592 8.963l-.147.09c-.22.134-.44.266-.659.402-.093.058-.184.12-.27.188-.085.07-.124.161-.072.272.047.1.093.2.147.294.047.08.124.138.213.147.11.01.228.012.336-.012.217-.05.372-.205.528-.357a.291.291 0 00.087-.308c-.046-.18-.079-.365-.118-.547-.011-.052-.027-.103-.045-.169zm-.257-2.409c-.12.291-.205.597-.325.91-.151.433-.294.87-.435 1.323.036-.01.054-.01.067-.018.261-.16.522-.324.785-.484.054-.033.071-.078.065-.138-.012-.13-.024-.262-.034-.393l-.068-.886c-.008-.103-.02-.206-.029-.31-.009 0-.017-.002-.026-.004zm3.081-8.13l.099.285c.08.231.159.463.24.714l.58 1.952c.187.63.372 1.262.558 1.893.114.382.235.762.343 1.146.072.257.126.519.186.799.044.206.087.413.127.64.034.106.023.226.077.325l.025-.006-.068-.362c-.038-.206-.077-.412-.113-.638-.015-.07-.029-.141-.046-.211-.095-.396-.177-.796-.29-1.187-.196-.685-.413-1.364-.618-2.046-.165-.549-.322-1.1-.488-1.648-.069-.227-.15-.45-.226-.695l-.117-.336c-.037-.107-.075-.216-.115-.322-.04-.106-.084-.21-.127-.314a7.558 7.558 0 01-.027.01zM6.225 14.304c-.063-.001-.115.014-.134.083a.35.35 0 00.41.012 4.533 4.533 0 00-.276-.095zM5.23 11.98c-.026-.027-.057-.048-.075.002-.012.032-.007.07-.01.113.082-.037.082-.037.085-.115zm.062-1.189a.135.135 0 00-.088.056.197.197 0 00-.025.11c.005.152.01.306.026.457a.751.751 0 00.066.218c.061.136.157.167.288.101.055-.027.06-.054.025-.11a4.52 4.52 0 01-.129-.211c-.015-.068-.066-.131-.033-.207.04-.09-.076-.116-.074-.19V10.874c-.003-.038-.006-.087-.056-.083zm-.017-.968a.867.867 0 00-.467.127c-.076.045-.084.07-.05.158.034.087.07.173.115.254.064.117.09.125.21.077a.657.657 0 01.336-.053c.202.022.357.136.504.264l.092.077c.007-.006.014-.013.022-.018-.019-.105-.035-.226-.149-.264-.157-.053-.324-.075-.508-.117l-.24-.005c.24-.169.452-.044.687.009-.063-.115-.153-.147-.23-.193-.082-.05-.17-.092-.25-.144-.06-.037-.12-.08-.072-.172zm10.233.325c-.23-.01-.427.08-.608.211-.034.026-.06.065-.105.117.087.026.15.046.232.065.044-.015.088-.03.13-.046.306-.114.61-.115.904.031.126.063.237.04.366-.005-.02-.031-.03-.054-.045-.071a.986.986 0 00-.448-.273c-.14-.044-.284-.024-.426-.03zM7.99 6.483a.308.308 0 00.002.133c.08.321.156.643.242.962.104.387.27.75.456 1.103.02.037.061.08.098.087a.404.404 0 00.253-.051l-.472-.84c-.23-.448-.405-.92-.579-1.394zM10.397.497c-.2-.008-.405.004-.603.034-.236.035-.47.087-.7.152-.287.08-.569.18-.852.273-.04.013-.074.038-.11.058.028.014.05.018.07.014.287-.068.58-.085.873-.09.134-.002.269.009.402.025.19.024.382.048.57.09.456.104.874.3 1.265.556.464.306.888.66 1.257 1.078.205.232.395.475.56.739.17.274.315.561.449.856.273.601.456 1.232.6 1.876.04.173.07.348.1.524.017.104.065.167.17.19.122.028.2.105.22.251-.003.102-.06.174-.129.24a1.065 1.065 0 00-.268.358.164.164 0 00.083-.039c.08-.086.162-.172.235-.265a.56.56 0 00.13-.333c.009-.05.022-.1.024-.15.007-.124-.017-.15-.143-.168-.025-.004-.049-.014-.073-.015-.082-.007-.125-.063-.137-.131-.033-.198-.004-.355.247-.408.086-.018.174-.03.26-.042.158-.023.315-.053.473-.067.14-.012.19.033.226.167.008.029.018.057.021.087.019.179-.008.225-.141.288-.027.013-.055.024-.078.042a.148.148 0 00-.051.067c-.039.144.073.382.206.445l.673.32c.023.011.05.015.075.023l.018-.026c-.015-.008-.032-.013-.044-.024a2.27 2.27 0 00-.544-.32 4.898 4.898 0 00-.173-.075.203.203 0 01-.126-.191c-.003-.085.045-.154.128-.187l.059-.025c.099-.044.118-.076.112-.187a.384.384 0 00-.008-.063c-.067-.294-.123-.59-.205-.88a9.478 9.478 0 00-.826-2.036 7.465 7.465 0 00-1.39-1.805 4.536 4.536 0 00-1.177-.824 3.656 3.656 0 00-1.016-.328 6.155 6.155 0 00-.712-.074zm6.719 5.955c.01.014.018.028.038.034l-.022-.044-.016.01zM4.103 3.917a.062.062 0 01-.03.012.455.455 0 01-.04.039c-.01.01-.02.02-.045.04l-.363.354c-.088.085-.17.178-.266.253-.284.22-.425.53-.544.855a.132.132 0 00-.007.071c.013.055.033.108.052.168l.074.026c-.017.056-.03.105-.047.152-.058.164-.118.327-.175.491-.005.015.008.036.019.077.08-.175.158-.33.225-.489.228-.544.484-1.074.819-1.561.09-.133.182-.266.283-.401.004-.006.007-.013.022-.03.001-.016.003-.032.015-.04l.008-.017zm12.976 2.408a.023.023 0 01.009.019.073.073 0 00-.006.01.188.188 0 00.007.02l.018.022c.002-.007.007-.016.005-.021-.003-.01-.012-.018-.02-.038a1.331 1.331 0 01-.013-.012zM4.199 4.48c-.003.004-.008.008-.027.014-.005.013-.011.025-.031.047a2.085 2.085 0 01-.124.167c-.048.07-.116.055-.181.041-.134-.028-.228.016-.287.143-.089.187-.187.37-.273.56-.049.108-.11.216-.118.36.081.003.154.007.228.008h.228a2.563 2.563 0 01-.079.264c-.01.052-.022.103-.033.155l.02.004c.018-.046.037-.092.067-.153.066-.142.13-.285.2-.426.02-.04.034-.1.116-.092 0 .043.004.084 0 .124-.005.045-.017.09-.028.143.141.043.086.174.115.269.102-.022.104-.195.248-.144v.205l.017.002.439-1.059c-.13 0-.246-.02-.358.033-.024.011-.058-.001-.108-.004.075-.15.139-.278.211-.417a.128.128 0 01.025-.036c0-.015-.001-.03.008-.038l.006-.02c-.005.006-.01.011-.028.017-.004.012-.009.024-.026.045a.085.085 0 01-.032.033c-.123.157-.09.164-.258.106-.079-.027-.078-.028-.047-.144.028-.046.056-.093.098-.15 0-.016-.001-.032.007-.042L4.2 4.48zm2.073-.67c-.003.006-.007.011-.027.016-.094.125-.194.246-.28.377-.155.238-.301.481-.451.723-.14.224-.345.368-.575.481-.017.008-.04.006-.079.011.012-.059.016-.109.033-.153a6.076 6.076 0 01.229-.518l-.007-.02a.138.138 0 01-.035.025c-.028.05-.055.1-.093.164-.26.424-.443.817-.442.95.024.004.048.011.073.013.177.013.188.007.26-.165.03-.07.077-.12.147-.15l.175-.07c.044-.018.085-.057.146-.032.003.05-.01.11.014.145.042.062.044.125.047.193.002.049.017.098.026.147.029-.034.039-.065.05-.097.142-.39.277-.782.428-1.17.1-.256.22-.504.33-.756.013-.03.013-.067.03-.092V3.81zm3.987-.34c0 .045.01.084.021.123.042.16.094.318.124.48.024.133.023.27.028.406 0 .033-.019.067-.032.11-.094-.058-.047-.158-.106-.215h-.125c-.015.072-.01.152-.046.2-.066.085-.155.154-.236.227-.043.038-.078.018-.103-.025l-.046-.087c-.065.035-.117.069-.172.093-.116.051-.235.095-.35.147-.085.038-.09.053-.07.147.014.075.034.148.047.223.013.072.05.109.123.124.233.05.462.115.657.265.058-.102.058-.102.168-.151.03-.014.06-.03.092-.042.08-.03.115-.017.15.06.023.048.041.098.066.158.06-.14-.042-.267.017-.416.157.18.24.39.375.567a.235.235 0 00.022-.098c.002-.124 0-.247.002-.371 0-.034.013-.067.02-.1l.032-.003c.11.155.13.354.226.52a3.036 3.036 0 00-.01-.392c-.004-.045 0-.074.05-.088.08.036.116.14.215.158-.03-.275-.423-1.137-.798-1.635-.114-.127-.2-.28-.34-.386zm-2.667.696c-.019.034-.03.05-.037.067-.061.185-.125.37-.18.556-.031.105-.087.169-.195.19-.09.019-.178.052-.268.073-.038.009-.089.015-.118-.003-.024-.016-.025-.069-.036-.106-.064.076-.082.087-.17.047-.133-.062-.262-.135-.393-.201-.048-.025-.093-.063-.17-.03-.043.12-.091.25-.137.382-.099.28-.087.242.095.453.046.048.102.03.154.023.054-.009.106-.03.16-.036.13-.013.26-.08.367-.015.204-.064.387-.122.571-.178.05-.015.089.005.114.054.022.042.034.093.082.121.038-.056-.013-.128.063-.178l.14.241-.042-1.46zm.278.358c-.096-.01-.107.01-.11.108-.002.038-.003.078.002.115.03.2.099.386.174.57.002.006.012.01.022.015l.078-.05c.052.036.081.088.153.088.205-.002.41.014.616.012.099-.001.158.042.205.12.018.03.024.077.088.066l-.08-.394c-.05-.195-.085-.395-.172-.589-.057.057-.114.068-.18.046a.72.72 0 00-.135-.028c-.22-.028-.44-.059-.66-.08zm10.254-1.727c.089.163.155.316.139.491-.016.168.026.342-.044.516-.047-.033-.088-.082-.112-.075-.117.035-.164-.057-.227-.115a4.772 4.772 0 01-.286-.29l-.104-.113a4.856 4.856 0 01-.023.019c.035.046.07.093.11.156.04.064.084.127.122.193.034.058.065.118.031.205-.082-.01-.164-.019-.246-.032-.06-.01-.101 0-.124.07-.031.098-.037.096-.15.09.02.042.036.08.057.116.041.074.03.138-.03.196-.06.06-.118.122-.178.181a.175.175 0 01-.185.046c-.222-.061-.447-.113-.67-.174-.032-.009-.063-.04-.086-.068-.03-.04-.052-.087-.08-.13-.044-.07-.09-.138-.136-.207a.18.18 0 00-.014.105c.012.127.03.253.035.38.005.1-.024.12-.121.104-.104-.017-.206-.04-.31-.058-.064-.012-.131-.028-.202.03l.081.208c.09 0 .166-.01.237.002a.819.819 0 01.458.251c.078.083.154.168.241.26l.018-.005c-.004-.006-.008-.013-.01-.04.014-.056-.062-.118.018-.178.031.03.064.057.088.09.058.078.111.159.169.257l.089.141.024-.013a2093.819 2093.819 0 01-.427-.934c.055.007.083.007.108.016.193.07.385.142.577.216.074.028.147.06.219.094.062.028.112.018.157-.033.05-.056.102-.112.154-.167.05-.051.095-.046.132.014.016.025.026.053.04.08.071.138.143.277.217.433l.159.308.025-.011c-.044-.106-.07-.218-.138-.334-.057-.182-.168-.346-.206-.545.136.034.362.326.567.732l.057.074.018-.011a1.563 1.563 0 01-.052-.127c-.046-.145-.097-.29-.136-.436-.022-.083-.036-.173.022-.26l.109.058-.026-.207.027-.016c.022.02.05.036.065.06.073.108.143.22.215.33.01.016.029.029.043.043-.036-.217-.2-.38-.229-.626l.155.112c.014-.166.012-.319.042-.465.032-.158-.023-.297-.063-.445.024.004.036.006.055.025.092.124.183.249.277.371.02.027.05.047.069.087l.04.063.019-.015a.293.293 0 01-.053-.082 27.922 27.922 0 01-.332-.49c-.221-.311-.363-.467-.485-.521zm-6.57.327c-.003.161.092.275.069.415l-.368.087c.09.139.032.237-.052.331-.05.057-.092.122-.143.178-.037.04-.046.078-.018.126l.16.275c.029.048.072.066.128.064.076-.003.152 0 .228-.001.116-.003.216.022.275.137.006.014.02.024.044.052.004-.059-.003-.098.01-.13.016-.04.04-.099.072-.108.084-.023.173-.024.26-.03.013-.001.027.018.04.029l.071.065c.019-.11-.082-.198-.024-.31l.126.04c-.026-.123-.07-.245-.071-.366 0-.123.051-.243.115-.36.107.062.16.156.234.253.183.265.36.533.494.834.165-.078.27.068.407.088-.003-.106-.133-.441-.197-.492a.142.142 0 00-.102-.028c-.06.011-.119.039-.191.063-.025-.039-.056-.078-.077-.122a3.936 3.936 0 00-.473-.783c-.076-.094-.16-.182-.228-.26l-.391.285c-.049.035-.094.03-.132-.017l-.169-.207c-.025-.03-.053-.059-.097-.108z"></path></symbol>

  <symbol id="mistral-mark" viewBox="44 124 559 399">
    <image x="0" y="0" width="648" height="648"
      href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAogAAAKICAYAAADzSQu6AAAACXBIWXMAACxLAAAsSwGlPZapAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAOdEVYdFNvZnR3YXJlAEZpZ21hnrGWYwAACd5JREFUeAHt2LFtEFEQANE9sEgJCBGdUAQ0ROCGoAg6cAmI0IFTy9J36gl958Cne6+G3dVoZwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4JhtuJT1e1vDbtvPZWfgpNy/Y9y/a/kwAADwgkAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQN8O13H0c9lu3s4bdtl9P27Dbur0xf0fcDYc8DdfhgwgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAsQ2X8vjj8xoAeKVPfx40w4X4IAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABAbMOl/P8ya9jt672dOcL8HWP+jjF/x5i/a/FBBAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIDY5mT+zrc1wCl9n3+nuznvifsH53W2++eDCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAhEAEACIEIAEAIRAAAQiACABACEQCAEIgAAIRABAAgBCIAACEQAQAIgQgAQAhEAABCIAIAEAIRAIAQiAAAhEAEACAEIgAAIRABAAiBCABACEQAAEIgAgAQAhEAgBCIAACEQAQAIAQiAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABv6RnMvidEXB5HYAAAAABJRU5ErkJggg=="/>
  </symbol>
</svg>

<header id="chrome" data-tauri-drag-region>
  <div class="lights" id="lights" hidden>
    <button id="w-close" title="Close" aria-label="Close"></button>
    <button id="w-min" title="Minimise" aria-label="Minimise"></button>
    <button id="w-max" title="Maximise" aria-label="Maximise"></button>
  </div>
  <div class="brand"><img src="/static/brand-mark.png" width="24" height="24"
    alt=""><span>CV Studio</span></div>
  <!-- Three screens, and the tabs stay put on all of them, the editor
       included. The editor is where opening a document takes you rather than
       a peer of these, so it has no tab of its own: the tab it came from stays
       lit, and the crumb in the editor's own bar is the way back. -->
  <div class="tabs" id="nav" role="tablist" aria-label="View">
    <button role="tab" data-view="jobs" aria-selected="true">Applications</button>
    <button role="tab" data-view="docs" aria-selected="false">Documents</button>
    <button role="tab" data-view="funnel" aria-selected="false">Funnel</button>
    <button role="tab" data-view="cal" aria-selected="false">Calendar</button>
  </div>
  <button class="tsearch" id="btn-search" title="Search everything (Ctrl K)"
    aria-label="Search" aria-keyshortcuts="Control+K Meta+K"><svg width="14" height="14"
    viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true">
    <circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg><span>Search</span>
    <kbd id="tsearch-kbd">Ctrl K</kbd></button>
  <div class="grow"></div>
  <!-- Only what is true on every screen lives up here -- the AI clients and
       the gear -- so the bar never changes shape. What belongs to a screen
       sits in that screen's own header. -->
  <!-- Says, on every screen, that none of what is showing is yours. -->
  <button class="samp-pill" id="samp-pill" hidden title="Back to your own workspace">
    <i aria-hidden="true"></i>Sample data<b>Back to my workspace</b></button>
  <button class="cbtn ai" id="btn-ai" title="AI clients" aria-label="AI clients">
    <span class="aic" data-client="claude" data-state="unknown"><svg width="13"
      height="13" viewBox="0 0 24 24" aria-hidden="true"
      ><use href="#claude-mark"/></svg><i class="dot"></i></span>
    <span class="aic" data-client="openai" data-state="unknown"><svg width="13"
      height="13" viewBox="0 0 24 24" aria-hidden="true"
      ><use href="#openai-mark"/></svg><i class="dot"></i></span>
    <span class="aic" data-client="hermes" data-state="unknown"><svg width="13"
      height="13" viewBox="0 0 24 24" aria-hidden="true"
      ><use href="#hermes-mark"/></svg><i class="dot"></i></span>
    <span class="aic" data-client="mistral" data-state="unknown"><svg width="13"
      height="13" viewBox="0 0 24 24" aria-hidden="true"
      ><use href="#mistral-mark"/></svg><i class="dot"></i></span>
  </button>
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
</header>

<main>
  <!-- ---------------------------------------------------------------- CVs -->
  <section class="view" id="v-cvs" hidden>
    <aside class="rail rail-cvs">
      <div class="rail-label">Documents</div>
      <div class="rail-list" id="doclist"></div>
      <div class="rail-label" id="outline-label">Outline</div>
      <div class="rail-list" id="outline"></div>
      <div class="grow"></div>
      <div class="budget" id="budget" hidden>
        <div class="brow"><span class="pp"></span><span class="ww mono"></span></div>
        <div class="bar"><i></i><i></i><i></i><i></i><i></i><i></i></div>
        <div class="cap"></div>
      </div>
    </aside>

    <div class="centre">
      <!-- Where you are and what you can do with it. The crumb names the list
           you came from and takes you back to it; the title is the document. -->
      <div class="docbar">
        <button class="crumb" id="back">Applications</button>
        <span class="crumb-sep" aria-hidden="true">/</span>
        <div class="doctitle" id="doctitle"><span class="t"></span><span class="f mono"></span></div>
        <button class="more" id="doc-more" title="Rename or delete this document"
          aria-label="Rename or delete this document">&#8943;</button>
        <div class="langsw" id="langsw" role="group" aria-label="Language" hidden></div>
        <div class="grow"></div>
        <span class="acts" id="act-doc">
          <button class="obtn" id="btn-ats" title="What an applicant tracking system reads from this CV">ATS check</button>
          <button class="obtn" id="btn-design" title="Theme, typeface and page size">Design</button>
          <button class="obtn" id="btn-pdf" disabled>Export PDF&#8230;</button>
          <button class="pbtn" id="btn-render" title="Save and lay out the page again (Ctrl S)">Save</button>
        </span>
      </div>
      <!-- A translation whose source moved on since it was translated. -->
      <div class="extbar" id="driftbar" hidden>
        <span id="driftbar-msg"></span>
        <div class="grow"></div>
        <button class="obtn" id="drift-show">What changed</button>
        <button class="obtn" id="drift-done"
          title="The translation now says everything the source says">Mark as done</button>
      </div>
      <div class="extbar" id="extbar" hidden>
        <svg width="13" height="13" viewBox="0 0 24 24" aria-hidden="true"
          ><use href="#claude-mark"/></svg>
        <span id="extbar-msg"></span>
        <div class="grow"></div>
        <button class="obtn" id="ext-theirs"
          title="Load their version. Your unsaved edits are lost.">Take theirs</button>
        <button class="obtn primary" id="ext-keep"
          title="Keep your unsaved edits. Their version stays on disk.">Keep mine</button>
      </div>
      <div class="subbar">
        <div class="seg light" id="edtabs" role="tablist" aria-label="What edits the page">
          <button role="tab" data-tab="page" aria-selected="true"
            title="The page on its own. Click a block to edit it there">Page</button>
          <button role="tab" data-tab="form" aria-selected="false"
            title="Fields on the left, the page on the right">Form</button>
          <button role="tab" data-tab="yaml" aria-selected="false"
            title="The source on the left, the page on the right">YAML</button>
        </div>
        <button class="prov" id="basechip" hidden></button>
        <button class="prov" id="provchip" hidden></button>
        <button class="prov linkchip" id="linkchip" hidden></button>
        <button class="prov photochip" id="photochip" hidden aria-haspopup="dialog"></button>
        <span class="nomap" id="nomap" hidden></span>
        <div class="grow"></div>
        <div class="meta mono" id="pmeta">
          <button id="pg-prev" title="Previous page" aria-label="Previous page">&#8249;</button>
          <span id="pg-idx">&#8211;</span>
          <button id="pg-next" title="Next page" aria-label="Next page">&#8250;</button>
          <span>&#183;</span>
          <button id="z-out" title="Zoom out" aria-label="Zoom out">&#8722;</button>
          <button id="z-lvl" title="Fit the page to the window">100%</button>
          <button id="z-in" title="Zoom in" aria-label="Zoom in">+</button>
        </div>
      </div>
      <!-- Source on the left, the page on the right. Form and YAML used to
           replace the page rather than stand beside it, so the one moment you
           most want to see the render -- while you are changing the words --
           was the one moment it was not on screen. The page pane is never
           hidden now; the tabs decide what stands beside it. -->
      <div class="panes" id="panes">
        <div class="pane pane-form" id="pane-form" hidden></div>
        <div class="pane pane-yaml" id="pane-yaml" hidden>
          <div class="yamlerr" id="yamlerr" hidden><span></span>
            <button type="button">Go to line</button></div>
          <div class="edwrap"><pre id="hl" aria-hidden="true"></pre>
            <textarea id="yaml" spellcheck="false" aria-label="CV source"></textarea></div>
        </div>
        <div class="split" id="split" role="separator" aria-orientation="vertical"
          aria-label="Resize the editor" aria-valuemin="26" aria-valuemax="68"
          tabindex="0" hidden></div>
        <div class="pane pane-page" id="pane-page"></div>
      </div>
    </div>
  </section>

  <!-- The editor for whatever is selected. It used to be a 312px column
       standing beside the page at all times, holding a form for one entry
       whether or not you wanted one -- and in the Form tab, holding the same
       fields the form beside it was already showing. It is a popover now,
       opened by the block you clicked and anchored beside it, so the page
       gets the whole pane and you edit at the thing you are looking at. -->
  <div class="ed" id="ed" hidden role="dialog" aria-label="Edit block">
    <div class="ed-head">
      <div class="ed-who">
        <b id="insp-title"></b>
        <span id="insp-meta"></span>
      </div>
      <div class="grow"></div>
      <button id="ed-prev" title="Previous block (Up)" aria-label="Previous block"
        >&#8593;</button>
      <button id="ed-next" title="Next block (Down)" aria-label="Next block"
        >&#8595;</button>
      <button id="ed-close" title="Close (Esc)" aria-label="Close">&#10005;</button>
    </div>
    <div class="ed-body" id="insp-body"></div>
  </div>

  <!-- --------------------------------------------------------------- Jobs -->
  <!-- Home. The work is applying for jobs; a CV is something an application
       either has or has not got yet, which is what the list is for. -->
  <section class="view" id="v-jobs">
    <aside class="rail rail-jobs">
      <div id="attentionwrap" hidden>
        <div class="rail-label attn">Attention</div>
        <div id="attentionlist"></div>
      </div>
      <div class="rail-label">Status</div>
      <div id="statuslist"></div>
      <div class="rail-label">Saved views</div>
      <div id="savedlist"></div>
      <div class="grow"></div>
      <!-- The one document every tailored CV is copied from. It sits with the
           filters rather than above the table: it is not a row of the list,
           and as a band across the top it pushed every application down. -->
      <div class="baserow" id="baserow"></div>
    </aside>
    <div class="tablewrap">
      <div class="phead">
        <h1 id="jtitle">All applications</h1><span class="pcount" id="jcount"></span>
        <div class="grow"></div>
        <label class="search" id="search"><svg width="13" height="13" viewBox="0 0 24 24"
            fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"
            style="flex:none"><circle cx="11" cy="11" r="7"/>
            <path d="M20 20l-4-4"/></svg>
          <input id="jobq" type="search" placeholder="Search applications"
            aria-label="Search applications"></label>
        <button class="pbtn" id="btn-newjob">New application&#8230;</button>
      </div>
      <!-- What is next, above the list: the next interview, the follow-ups
           that are late, and the week. Only there when something is. -->
      <section class="na" id="nextup" aria-label="Next actions" hidden></section>
      <div class="tcard">
        <div class="thead"><button type="button" data-sort="company">Company</button><button type="button" data-sort="title">Role</button>
          <span>Documents</span><button type="button" data-sort="status">Status</button>
          <button type="button" data-sort="applied">Applied</button><button type="button" data-sort="next">Next step</button></div>
        <div class="tbody" id="jobrows"></div>
      </div>
    </div>
    <!-- A peek rather than a rail. The old 284px column was fixed at every
         window size, so a thirteen-field record was stacked into a sliver
         while the table beside it had a thousand pixels it did not need. This
         opens over the table at a width that fits the record, leaves the rows
         that identify the application visible to its left, and moves between
         them with the arrow keys without ever closing. -->
    <aside class="peek" id="jpeek" hidden aria-label="Application">
      <div class="peek-head">
        <span class="peek-logo" id="jinsp-logo" aria-hidden="true"></span>
        <div class="peek-who" aria-live="polite"><b id="jinsp-title"></b><span id="jinsp-sub"></span></div>
        <span class="ap-pill" id="jinsp-status"></span>
        <div class="peek-nav">
          <button id="jpk-prev" title="Previous application (Up)"
            aria-label="Previous application">&#8593;</button>
          <span class="mono" id="jpk-idx"></span>
          <button id="jpk-next" title="Next application (Down)"
            aria-label="Next application">&#8595;</button>
          <button id="jpk-close" title="Close (Esc)" aria-label="Close">&#10005;</button>
        </div>
      </div>
      <div class="peek-body" id="jinsp-body"></div>
    </aside>
  </section>

  <!-- ---------------------------------------------------------- Documents -->
  <!-- The app's second noun. Applications are the work; documents are what the
       work is done with -- and until this screen the only list of them lived
       inside the editor, so finding a document meant already having one open.
       A CV written for an application is reachable from that application's
       row; one written for nothing was reachable from nowhere. -->
  <section class="view" id="v-docs" hidden>
    <div class="docpane">
      <div class="docwrap">
        <div class="phead">
          <h1>Documents</h1><span class="pcount" id="dcount"></span>
          <div class="grow"></div>
          <div class="dfilter" id="dfilter" role="group" aria-label="Language" hidden></div>
          <button class="obtn" id="btn-importdoc" title="A PDF of a CV, or your LinkedIn profile or data archive">Import&#8230;</button>
          <input type="file" id="importdoc-file" accept=".pdf,.zip,application/pdf,application/zip" hidden>
          <button class="pbtn" id="btn-newdoc">New document&#8230;</button>
        </div>
        <div id="docbase"></div>
        <div id="doclanes"></div>
      </div>
    </div>
  </section>

  <!-- ------------------------------------------------------------- Funnel -->
  <!-- ------------------------------------------------------ Cover letter -->
  <section class="view" id="v-letter" hidden>
    <div class="docbar">
      <button class="crumb" id="lt-back">Applications</button>
      <span class="crumb-sep" aria-hidden="true">/</span>
      <div class="doctitle"><span class="t" id="lt-title"></span><span class="f mono" id="lt-file"></span></div>
      <div class="seg light" id="lt-tabs" role="tablist" aria-label="View" style="margin-left:6px">
        <button role="tab" data-lt="write" aria-selected="true">Write</button>
        <button role="tab" data-lt="pdf" aria-selected="false">PDF</button>
        <button role="tab" data-lt="md" aria-selected="false">Markdown</button>
      </div>
      <div class="grow"></div>
      <span class="lt-export"><button class="obtn" id="lt-export" aria-haspopup="menu">Export &#9662;</button></span>
      <button class="pbtn" id="lt-save">Save</button>
    </div>
    <div class="lt-body">
      <div class="lt-stage" id="lt-stage"></div>
      <aside class="lt-panel" id="lt-panel" aria-label="This letter"></aside>
    </div>
  </section>
  <section class="view" id="v-cal" hidden>
    <div class="cal-page" id="cal-page">
      <div class="phead cal-bar"><div class="fn-head"><h1>Calendar</h1><span id="cal-sub"></span></div>
        <div class="grow"></div>
        <div class="cal-nav" id="cal-nav" hidden>
          <button class="obtn cal-arrow" data-step="-1" aria-label="Previous">&#8249;</button>
          <b id="cal-title"></b>
          <button class="obtn cal-arrow" data-step="1" aria-label="Next">&#8250;</button>
          <button class="obtn" id="cal-today">Today</button>
        </div>
        <div class="seg light" id="cal-views" role="tablist" aria-label="View">
          <button role="tab" data-cv="overview" aria-selected="true">Overview</button>
          <button role="tab" data-cv="month" aria-selected="false">Month</button>
          <button role="tab" data-cv="week" aria-selected="false">Week</button>
        </div>
        <button class="obtn" id="cal-ics" title="A file your calendar app imports">Export .ics</button></div>
      <div id="cal-body"></div>
    </div>
  </section>
  <section class="view" id="v-funnel" hidden>
    <div class="fn-page" id="fn-page">
      <div class="phead fn-bar"><div class="fn-head"><h1>Funnel</h1><span id="fn-sub"></span></div>
        <div class="grow"></div>
        <div class="seg light" id="range" role="tablist" aria-label="Date range">
          <button role="tab" data-since="" aria-selected="true">All time</button>
          <button role="tab" data-since="6m" aria-selected="false">6 months</button>
          <button role="tab" data-since="30d" aria-selected="false">30 days</button>
        </div>
        <button class="obtn" id="ex-csv">Export CSV</button>
</div>
      <div id="fn-body">
        <!-- Sent to accepted, as five numbers and the rate between each pair. -->
        <section class="fn-card fn-journey" id="fn-journey" aria-label="From sent to accepted"></section>
        <div class="fn-main">
          <!-- White in light mode like every card; a dark stage in dark mode. -->
          <section class="fn-stage" aria-labelledby="fn-stage-h">
            <div class="fn-shead"><h2 id="fn-stage-h">Where they went</h2><span id="fn-hint"></span>
              <div class="grow"></div>
              <span class="fn-legend" id="fn-legend"><i></i>Moving dots: applications still open</span></div>
            <div id="chart"></div>
          </section>
          <!-- What is behind the stage you clicked goes beside the chart, in
               place of what is still in play, so the chart never moves. -->
          <aside class="fn-side">
            <section class="fn-card" id="fn-momentum" aria-label="Momentum"></section>
            <section class="fn-card fn-list" id="fn-jobs"></section>
          </aside>
        </div>
        <div class="fn-row3">
          <section class="fn-card" id="fn-sources" aria-label="Where interviews come from"></section>
          <section class="fn-card" id="fn-reply" aria-label="How long they take to answer"></section>
          <section class="fn-card" id="fn-rates" aria-label="What it says"></section>
        </div>
      </div>
    </div>
  </section>
</main>

<footer id="status"><span id="st-left"></span><div class="grow"></div>
  <span id="st-right" class="mono"></span></footer>

<div id="toasts" aria-live="polite"></div>
<div class="scrim" id="scrim" hidden></div>
<div class="pal-scrim" id="pal-scrim" hidden></div>
<div class="pal" id="pal" hidden role="dialog" aria-modal="true" aria-label="Search">
  <label class="pal-q"><svg width="18" height="18" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="2.2" aria-hidden="true"><circle cx="11" cy="11" r="7"/>
    <path d="M20 20l-4-4"/></svg>
    <input id="pal-in" type="search" autocomplete="off" spellcheck="false" role="combobox"
      aria-expanded="true" aria-controls="pal-list" aria-autocomplete="list"
      placeholder="Search applications, documents, postings, or go to…" aria-label="Search">
    <kbd>Esc</kbd></label>
  <div class="pal-list" id="pal-list" role="listbox" aria-label="Results"></div>
  <div class="pal-foot"><span><kbd>↑↓</kbd> move</span><span><kbd>Enter</kbd> open</span>
    <span class="grow"></span><span id="pal-count"></span></div>
</div>
<div class="sheet" id="sheet" hidden role="dialog" aria-modal="true"
  aria-labelledby="sheet-title"></div>
<!-- The first launch, over the whole window: drawn by onboardingSheet(). -->
<div class="onb" id="onb" hidden role="dialog" aria-modal="true" aria-labelledby="onb-title"></div>

<!-- ------------------------------------------------------------- Design -->
<!-- A screen of its own under the title bar, not a panel over the page: every
     setting RenderCV has, grouped the way RenderCV groups them, beside the
     page they change. -->
<div class="ovl dz" id="ovl-design" hidden>
  <div class="docbar">
    <button class="crumb" id="dz-list">Documents</button>
    <span class="crumb-sep" aria-hidden="true">/</span>
    <button class="crumb" id="dz-doc" data-close-ovl></button>
    <span class="crumb-sep" aria-hidden="true">/</span>
    <h1 class="dz-title">Design</h1>
    <span class="dz-badge" id="dz-base" hidden>Base CV</span>
    <span class="dz-note" id="dz-note"></span>
    <div class="grow"></div>
    <button class="obtn" id="dz-pdf">Export PDF&#8230;</button>
    <button class="pbtn" data-close-ovl>Done</button>
  </div>
  <div class="dz-body">
    <nav class="dz-nav" id="dz-nav" aria-label="Design settings"></nav>
    <section class="dz-pane" aria-labelledby="dz-h">
      <div class="dz-head">
        <div><h2 id="dz-h"></h2><p id="dz-desc"></p></div>
        <button class="obtn" id="dz-reset" hidden>Reset section</button>
      </div>
      <div class="dz-scroll">
        <div class="themegrid" id="themegrid" role="radiogroup" aria-label="Theme"></div>
        <div id="dz-photo" hidden></div>
        <div id="dz-advanced"></div>
      </div>
    </section>
    <div class="dz-preview">
      <div class="dz-effect" id="dz-effect"></div>
      <div class="dz-pages" id="dz-pages"></div>
    </div>
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
      <button data-s="region" aria-selected="false">Language &amp; region</button>
      <button data-s="notify" aria-selected="false">Notifications</button>
      <button data-s="browser" aria-selected="false">Save from the browser</button>
      <button data-s="ai" aria-selected="false">AI clients</button>
      <button data-s="api" aria-selected="false">API</button>
      <button data-s="updates" aria-selected="false">Updates</button>
      <button data-s="about" aria-selected="false">About</button>
    </nav>
    <div>
      <section class="sp" id="sp-workspace">
        <h3>Workspace</h3>
        <p class="sp-lede">Everything lives in one folder you own. CVs are plain YAML,
          letters plain Markdown, and applications a single SQLite file. Copy the folder
          and you have copied everything.</p>
        <div class="srow"><div><b>Setup</b><span>The welcome and the steps from the
          first launch: import a CV from a PDF or LinkedIn, your name at the top of the
          base CV, how it prints, and an AI client.</span></div>
          <button class="obtn" id="s-setup">Run setup again</button></div>
        <div class="srow"><div><b>Backups</b><span id="s-bk-say">A copy of this workspace every day,
          beside the app, the last fourteen kept.</span></div>
          <div class="acts"><button class="obtn" id="s-bk-list">Restore…</button>
          <button class="obtn" id="s-bk-now">Back up now</button></div></div>
        <div class="srow"><div><b>Sample data</b><span id="s-sample-say">See the app in use:
          about sixty applications in every state over five months, tailored CVs, cover
          letters, and the base CV in French, Spanish and Brazilian Portuguese. It opens in
          a folder of its own, made fresh each time; your workspace is not touched, and a
          restart brings you back to it.</span></div>
          <button class="obtn" id="s-sample">Open sample data</button></div>
        <div class="srow"><div><b>Folder</b><span id="s-ws" class="mono"></span></div>
          <button class="obtn" id="s-open">Open folder</button></div>
        <div class="srow"><div><b>Applications</b><span>Exported as CSV for a spreadsheet, or JSON
          for another program: the list is never locked in here.</span></div>
          <span class="acts"><button class="obtn" id="s-exp-csv">Export CSV</button>
          <button class="obtn" id="s-exp">Export JSON</button></span></div>
      </section>

      <section class="sp" id="sp-notify" hidden>
        <h3>Notifications</h3>
        <p class="sp-lede">Reminders from CV Studio, as your system's notifications: before an
          interview, and on the morning a follow-up is due. Off until you turn them on.</p>
        <div class="srow"><div><b>Notifications</b><span id="s-notify-state">Off.</span></div>
          <label class="tgl"><input type="checkbox" id="s-notify"><i></i></label></div>
        <div class="srow nf-sub"><div><b>Interviews</b><span>With the time in yours and in theirs.</span></div>
          <select id="s-notify-iv">
            <option value="off">Off</option>
            <option value="10">10 minutes before</option>
            <option value="60">1 hour before</option>
            <option value="60,10">1 hour before, and 10 minutes before</option>
            <option value="1440,60">The day before, and 1 hour before</option>
          </select></div>
        <div class="srow nf-sub"><div><b>Follow-ups</b><span>One notification for all the ones due that day,
          and the ones already late.</span></div>
          <select id="s-notify-fu">
            <option value="off">Off</option>
            <option value="8">At 08:00</option>
            <option value="9">At 09:00</option>
            <option value="10">At 10:00</option>
            <option value="14">At 14:00</option>
          </select></div>
        <div class="srow nf-sub" data-desk hidden><div><b>Keep running when the window is closed</b><span>So
          reminders still come. CV Studio stays in the tray (the menu bar on a Mac); quit it from there.</span></div>
          <label class="tgl"><input type="checkbox" id="s-keep"><i></i></label></div>
        <div class="srow" data-desk hidden><div><b>Open at login</b><span>Starts CV Studio, in the tray, when you
          log in, so a reminder is not missed because the app was never opened.</span></div>
          <label class="tgl"><input type="checkbox" id="s-autostart"><i></i></label></div>
        <div class="srow nf-sub"><div><b>Try it</b><span>Sends one now, so you can see where they appear.</span></div>
          <button class="obtn" id="s-notify-test">Send a test</button></div>
      </section>

      <section class="sp" id="sp-region" hidden>
        <h3>Language &amp; region</h3>
        <p class="sp-lede">The language the app itself speaks, and the time zone it shows
          times in. Neither changes your CVs or letters: each of those has its own
          language.</p>
        <div class="srow"><div><b>Interface language</b><span>Follows your computer unless
          you choose one.</span></div>
          <select id="s-uilang"><option value="">Match system</option>
            <option value="en">English</option><option value="fr">Français</option>
            <option value="es">Español</option><option value="pt">Português (Brasil)</option></select></div>
        <div class="srow"><div><b>CVs in more than one language</b><span>For applying in more
          than one country: a base CV per language, kept in step with the first one. Off, the
          app never mentions languages.</span></div>
          <label class="tgl"><input type="checkbox" id="s-multilang"><i></i></label></div>
        <div class="srow" id="s-cvlang-row"><div><b>Language of your CVs</b><span>Dates, month
          names and “present” print in it, and new letters are written in it.</span></div>
          <select id="s-cvlang"></select></div>
        <div class="srow"><div><b>Time zone</b><span>Interviews somewhere else show in
          your time, with theirs beside it.</span></div>
          <select id="s-tz"></select></div>
        <div class="tzmap" id="tzmap" aria-hidden="true"></div>
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
        <div class="srow"><div><b>Accent</b>
          <span>The colour the interface marks things with. The rendered CV page is
            never tinted by it.</span></div>
          <div class="swatches" id="s-accent"></div></div>
        <div class="srow"><div><b>Appearance</b>
          <span>Follows your system unless you choose one. It changes the surfaces
            you work <em>on</em>: the panels, the forms, the tables. The window
            chrome stays dark and the rendered CV page stays white in both, because
            one frames the work and the other <em>is</em> the work.</span></div>
          <select id="s-appearance">
            <option value="system">Match system</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option></select></div>
      </section>

      <section class="sp" id="sp-ai" hidden>
        <h3>AI clients</h3>
        <p class="sp-lede">Connect an AI client and ask it, in plain words, to tailor your CV to
          a posting, write the cover letter or prepare an interview. With your mail connected
          to it, it also moves your applications along as replies come in. It works on the
          same files as this app: nothing to sync, nothing to upload.</p>

        <div class="clients" id="s-ai-clients"></div>

        <p class="sp-sub">What a connected model can do</p>
        <div class="tools">
          <div class="tool lead"><span class="n">render_cv</span>
            <p>Renders the CV <b>and looks at the page</b>. A bullet stranded alone on
              page two, a heading orphaned at a break, a lopsided last page. None of it
              is visible in the source, all of it is obvious in the picture. This is the
              point of the whole thing.</p></div>
          <div class="tool"><span class="n">edit_cv_fields</span>
            <p>Changes single fields, and keeps the comments you wrote</p></div>
          <div class="tool"><span class="n">create_cv</span>
            <p>A new CV or cover letter, or a copy to tailor for one application</p></div>
          <div class="tool"><span class="n">read_cv &nbsp;list_cvs</span>
            <p>Reads the YAML, lists what is in the workspace</p></div>
          <div class="tool"><span class="n">write_cv</span>
            <p>Replaces a whole file. Blunt, and it drops comments</p></div>
          <div class="tool"><span class="n">ats_check</span>
            <p>Reads the PDF the way an ATS does, and which of the posting's keywords it uses</p></div>
          <div class="tool"><span class="n">design_options</span>
            <p>Themes, typefaces and page sizes it may choose from</p></div>
          <div class="tool"><span class="n">workspace_info</span>
            <p>Where the workspace is and what is in it</p></div>
        </div>

        <p class="sp-sub">And your applications</p>
        <div class="tools">
          <div class="tool lead"><span class="n">set_job_status</span>
            <p>Moves an application along. Paired with a mail or calendar connector
              on the model's side, this is what keeps the tracker honest without you
              typing anything: it reads the reply, works out which application it
              belongs to, and asks you before it moves.</p></div>
          <div class="tool"><span class="n">update_job_tracking</span>
            <p>Interview times, follow-up dates, who is writing to you</p></div>
          <div class="tool"><span class="n">find_job &nbsp;list_jobs &nbsp;read_job</span>
            <p>Reads applications, and works out which one a message is about</p></div>
          <div class="tool"><span class="n">job_alerts</span>
            <p>The same list as Attention in the Applications view, read out loud</p></div>
          <div class="tool"><span class="n">calendar</span>
            <p>Interviews and follow-ups ahead, and an .ics file, so it can put them in your calendar</p></div>
          <div class="tool"><span class="n">add_job</span>
            <p>Adds one from a posting you paste, and refuses likely duplicates</p></div>
          <div class="tool"><span class="n">set_company_logo</span>
            <p>Points every application at one company to the same logo</p></div>
        </div>
        <div class="caveat"><svg width="15" height="15" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" stroke-width="2" style="flex:none;margin-top:1px"
          aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.5"/>
          </svg><div>
          <p><b>These write to your files the moment they are called</b>, and there is no
            undo in this app. They are plain YAML, so keeping the workspace in git gives
            you a real history.</p>
          <p><b>Your applications are part of this too.</b> A model can read them, move
            a status, record an interview and add one you never got round to logging.
            It cannot delete an application, rename the company or the role, or paint
            over notes you typed: it only ever appends a dated line. Those are not
            promises, they are missing parameters.</p>
          <p><b>A status change is permanent.</b> It appends to the history the funnel
            is drawn from, and there is no undo here either. The model is told to show
            you every change and wait, but that one is a rule in prose rather than a
            lock in the code.</p>
          <p><b>Nothing here reaches your mail or your calendar.</b> This app makes no
            network calls at all. Those come through your AI client's own connectors,
            they are only ever read, and nothing is written back to them, which is also
            why an interview time recorded here is only as fresh as the last time you
            asked.</p></div></div>

        <p class="sp-sub">Recent activity</p>
        <div id="s-cl-log" class="mcplog"></div>

        <p class="sp-sub">Skills</p>
        <p class="sp-note" style="margin-top:0">Skills are the judgement around the
          documents: reading a posting, tailoring from a master profile, letters,
          interview prep. Claude Code reads them off disk and already has them. The
          desktop app does not: there they are uploaded to your account, so the most
          this app can do is hand you archives that are ready to upload.</p>
        <div class="skills" id="s-skills"></div>
        <div class="skillcta">
          <button class="obtn primary" id="s-skill-pack">Package for Claude Desktop</button>
          <button class="obtn" id="s-skill-show" hidden>Show the folder</button>
        </div>
        <ol class="steps" id="s-skill-steps" hidden>
          <li>Open the Claude Desktop app, then Customize, then Skills.</li>
          <li>Press <b>+</b> and upload each <code>.zip</code> from that folder.</li>
        </ol>
        <p class="sp-sub">On the command line</p>
        <p class="sp-note">The same server works with Claude Code and the Codex CLI.
          Beyond the tools above, the skills in <code>~/.claude/skills/</code> cover the
          judgement around the documents: reading a posting, tailoring from a master
          profile, letters, tracking applications and interview prep.</p>

        <details class="fold"><summary>Set them up by hand instead</summary>
          <div id="s-ai-manual"></div>
        </details>
      </section>

      <section class="sp" id="sp-api" hidden>
        <h3>API</h3>
        <p class="sp-lede">The same server answers a small HTTP API, so scripts and other
          tools can drive it.</p>
        <div class="srow"><div><b>Base URL</b><span id="s-base" class="mono"></span></div>
          <a class="obtn btnlink" id="s-spec" target="_blank" rel="noreferrer">Open reference</a></div>
        <div class="srow"><div><b>Authentication</b><span id="s-auth"></span></div></div>
        <pre class="code" id="s-curl"></pre>
        <div class="skillcta"><button class="obtn" data-copy="s-curl">Copy</button>
        <button class="obtn" id="s-key" hidden>Show the key</button></div>
      </section>

      <section class="sp" id="sp-browser" hidden>
        <h3>Save from the browser</h3>
        <p class="sp-lede">One button in your bookmarks bar. On a job page it opens a small CV Studio
          window with the role, the company and the posting already filled in, and saves them as an
          application.</p>
        <div class="clip-steps">
          <div class="clip-step"><span class="clip-n">1</span><div><b>Drag this button to your bookmarks bar</b>
            <a class="clip-bm" id="s-clip-bm" href="#" draggable="true"><img src="/static/brand-mark.png"
              alt="" width="18" height="18"><span><span class="vh" data-noi18n>💼 </span>Save to CV Studio</span></a>
            <span class="clip-hint">No bookmarks bar? Show it with Ctrl+Shift+B (⌘⇧B on a Mac).</span>
            <span class="clip-hint">Dragging does not work? <button class="linkbtn" id="s-clip-copy">Copy it</button>,
              add a bookmark to any page, name it 💼 Save to CV Studio and paste it in place of the address.</span></div></div>
          <div class="clip-step"><span class="clip-n">2</span><div><b>On a job page, click it</b></div></div>
        </div>
        <div class="srow"><div><b>Works where the page describes the job</b><span>Most job boards and
          careers pages mark up the job for search engines. Elsewhere, select the posting's text first and it takes
          that.</span></div></div>
        <div class="srow"><div><b>CV Studio needs to be running</b><span id="s-clip-say">With the window
          closed it waits in the tray. Nothing leaves your computer: the page goes straight to the
          app.</span></div><span class="clip-state" id="s-clip-state"></span></div>
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
        <p class="sp-lede">A CV tailored to every application, every follow-up on time and every
          interview in your calendar, with an AI client doing the writing beside you if you
          connect one. Built on RenderCV and Typst.</p>
        <p class="sp-lede">Everything runs on your machine. No account, no server, no
          telemetry, which matters more, not less, once an AI is editing the files:
          your CVs and letters stay plain text in a folder you own, and the app and
          your AI client only ever touch that folder.</p>
        <p class="sp-note">MIT licensed. Bundles RenderCV (MIT), Typst (Apache-2.0), the
          RenderCV font set and IBM Plex (SIL Open Font License), and d3-sankey (ISC).
          The Claude mark is a trademark of Anthropic, used here only to identify the
          Claude Desktop integration.</p>
        <div class="srow" style="margin-top:8px"><div><b>Log</b><span>What the app noted while running, errors included. Useful
          to attach if you report a problem; nothing in it leaves your computer on its own.</span></div>
          <button class="sbtn" id="s-logs">Open the log folder</button></div>
      </section>
    </div>
  </div></div>
</div>

<script src="/static/app.js"></script>
</body></html>"""


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    global WORKSPACE, FIRST_RUN, API_TOKEN, VERSION, OPEN_NETWORK
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
        # Run by the desktop app, which shows no terminal: what would be
        # printed goes to the log, so there is something to look at when a
        # user reports a problem.
        log_to_file()
        watch_parent(args.parent_pid)

    WORKSPACE = Path(args.workspace).resolve() if args.workspace else DEFAULT_WORKSPACE
    FIRST_RUN = bootstrap(WORKSPACE)
    cjkfonts.register()
    start_backups()

    API_TOKEN = args.token
    if args.host not in ("127.0.0.1", "localhost", "::1") and not API_TOKEN:
        # Reachable from the network without a token would mean anyone on it can
        # read and rewrite the user's CVs, so generate one rather than allow it.
        API_TOKEN = secrets.token_urlsafe(24)
        print(f"Generated API token: {API_TOKEN}")

    url = f"http://{args.host}:{args.port}/"
    OWN_PORTS.add(args.port)
    if args.host not in LOOPBACK_NAMES:
        # Deliberately served beyond loopback, where it is reached by names
        # this cannot know: the token (made mandatory above) guards it.
        OPEN_NETWORK = True
    if args.host in ("127.0.0.1", "localhost", "::1"):
        start_clip_listener()
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
