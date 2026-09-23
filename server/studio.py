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
VERSION = "0.21.0"

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
        "label": "OpenAI",
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
            "body": body, "head": head, "date_line": letters.date_line(meta),
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


def render_letter(path: Path) -> dict:
    meta, body = letters.parse(path.read_text(encoding="utf-8"))
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


def open_sample() -> dict:
    """Rebuild the sample folder and switch the app to it."""
    global WORKSPACE, REAL_WORKSPACE
    if not in_sample():
        REAL_WORKSPACE = WORKSPACE
    WORKSPACE = sample.folder()
    try:
        return sample.build(sys.modules[__name__])
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


# The two parsing problems RenderCV's own design options solve, keyed by the
# id parse_checks gives them.
ATS_FIXES = {
    "icons": (["design", "header", "connections", "show_icons"], False),
    "urls": (["design", "header", "connections",
              "display_urls_instead_of_usernames"], True),
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
            "/api/sample": {"post": {"summary":
                "Switch to freshly made sample data (on: true) or back to your own "
                "workspace (on: false). Your workspace is never written to",
                "requestBody": body({"on": {"type": "boolean"}}),
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
            "/api/alerts": {"get": {"summary":
                "Applications needing attention: interviews due, follow-ups "
                "due, interviews with no outcome, and silence since applying",
                "responses": ok}},
            "/api/calendar.ics": {"get": {"summary":
                "Interviews and follow-ups as an iCalendar file; ?id= for one application",
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
            "/api/jobs/delete": {"post": {"summary": "Delete one job application",
                "requestBody": body({"id": {"type": "string"}}), "responses": ok}},
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
                ctype = "application/javascript" if f.suffix == ".js" else "text/plain"
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
            if u.path == "/api/alerts":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                return self._json(jobstore.alerts(WORKSPACE))
            if u.path == "/api/calendar.ics":
                if jobstore is None:
                    return self._json({"error": "job store unavailable"}, 501)
                body = jobstore.ics(WORKSPACE, q.get("id", [None])[0]).encode("utf-8")
                return self._send(200, body, "text/calendar; charset=utf-8")
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
                # browser opens it. Web links only: this is not a way to launch
                # whatever a stored URL happens to name.
                link = str(payload.get("url") or "")
                if urlparse(link).scheme not in ("http", "https"):
                    return self._json({"error": "Only web links can be opened."}, 400)
                webbrowser.open(link)
                return self._json({"ok": True})
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
                    return self._json({"ok": True, **open_sample()})
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
  --c550:#343229; --c500:#3a3833; --c450:#3d3b34; --c400:#4a4842; --c300:#9d998b;
  --c200:#a5a091; --c100:#cfcabd; --c050:#f0ede5; --cw:#f5f2ea; --c-hover:#5c594f;
  /* paper */
  --app:#f7f6f3; --panel:#f2efe8; --bar:#efece4; --canvas:#e4e1d8; --row-alt:#fbfaf7;
  --field:#ffffff; --page:#fffefc;
  --rule:#ddd8cc; --rule-strong:#d3cfc3; --bd-field:#cfcabd; --bd-inner:#e6e2d8;
  --t900:#1b1a17; --t800:#33302a; --t700:#4a463d; --t600:#5b574d; --t500:#686459;
  --t400:#70695b; --dot-idle:#8f8878; --dot-dead:#b3ad9d; --paper-hover:#e9e5da;
  /* accent -- used sparingly */
  --acc:#c08a3e; --acc-hover:#d09a4c; --acc-text:#8a5316; --acc-text-dark:#e8bc7c;
  --acc-wash:rgba(192,138,62,.10); --acc-ring:rgba(192,138,62,.18);
  --acc-line:rgba(192,138,62,.6);
  /* the funnel, which is drawn rather than styled inline so it follows the theme */
  --fn-total:#33312b; --fn-neutral:#7d7767; --fn-positive:#a8761f; --fn-label:#33312b;
  --fn-offer:#3f8f76; --fn-lost-late:#7d2f3f;
  --co-1:#6f6a60; --co-2:#7d766a; --co-3:#63605c; --co-4:#77706a;
  --co-5:#6a6660; --co-6:#807a70;
  --fn-won:#007a5e; --fn-lost:#a83519; --fn-wait:#3a6ea5; --fn-closed:#7a5cb8;
  --fn-band:.34;
  --seg-track:#dedbd0; --seg-on:#ffffff; --knob:#ffffff;
  --row-hover:#f1eee6; --spine:#c8c2b3; --bad-line:#e6cfc5;
  /* YAML syntax: the same muted ramp, retuned for a warm ground. Deliberately
     not ochre -- the accent already means "selected" everywhere else. */
  --tk-key:#33506b; --tk-str:#3d5f42; --tk-num:#5d3f6d; --tk-bool:#8f3f21;
  --tk-com:#70695b; --tk-punc:#8b8578; --tk-blk:#7a4f19; --tk-sel:rgba(192,138,62,.22);
  --bad:#8f3119; --bad-bg:#f7ece7;
}

/* ---------------------------------------------------------------------------
   Dark.

   The chrome barely moves -- it was already dark. What inverts is the content:
   paper becomes a warm near-black and the text ramp climbs instead of falling.
   Each rung is solved against the *lightest* content surface, so a value that
   passes on the panel still passes on an input.

   The rendered CV page stays white. It is a document, not a surface: darkening
   it would misrepresent what the PDF actually looks like.
--------------------------------------------------------------------------- */
:root[data-theme=dark]{
  --app:#22211d; --panel:#1d1c19; --bar:#282621; --canvas:#141310; --row-alt:#26241f;
  --field:#2b2924; --page:#fffefc;
  --rule:#38352e; --rule-strong:#454239; --bd-field:#4a473e; --bd-inner:#322f2a;
  --t900:#f4f2ef; --t800:#dad6cc; --t700:#c6c0b0; --t600:#b6af9b; --t500:#a79e86;
  --t400:#9b9175; --dot-idle:#8a8371; --dot-dead:#5c574b; --paper-hover:#302e28;
  --acc-text:#e8bc7c;
  --acc-wash:rgba(192,138,62,.16); --acc-ring:rgba(192,138,62,.32);
  --fn-total:#8f8877; --fn-neutral:#6e685a; --fn-positive:#b8832f; --fn-label:#c6c0b0;
  --fn-offer:#4fae90; --fn-lost-late:#a8415a;
  --co-1:#8b857a; --co-2:#98907f; --co-3:#7e7a74; --co-4:#928a82;
  --co-5:#857f78; --co-6:#9c958a;
  --fn-won:#189072; --fn-lost:#cf5a39; --fn-wait:#5b8fc9; --fn-closed:#9b7ad6;
  --fn-band:.42;
  --tk-key:#8fb4d9; --tk-str:#9ac4a4; --tk-num:#c3a4dc; --tk-bool:#e09070;
  --tk-com:#9b9175; --tk-punc:#7a7364; --tk-blk:#d0a468; --tk-sel:rgba(192,138,62,.3);
  --bad:#f0a189; --bad-bg:#2e1c15;
  --seg-track:#141310; --seg-on:#413d34; --knob:#e8e4da;
  --row-hover:#2c2a24; --spine:#5c574b; --bad-line:#4a2a1e;
  --c-hover:#6f6b60;
}
@media(prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --app:#22211d; --panel:#1d1c19; --bar:#282621; --canvas:#141310; --row-alt:#26241f;
    --field:#2b2924; --page:#fffefc;
    --rule:#38352e; --rule-strong:#454239; --bd-field:#4a473e; --bd-inner:#322f2a;
    --t900:#f4f2ef; --t800:#dad6cc; --t700:#c6c0b0; --t600:#b6af9b; --t500:#a79e86;
    --t400:#9b9175; --dot-idle:#8a8371; --dot-dead:#5c574b; --paper-hover:#302e28;
    --acc-text:#e8bc7c;
    --acc-wash:rgba(192,138,62,.16); --acc-ring:rgba(192,138,62,.32);
    --fn-total:#8f8877; --fn-neutral:#6e685a; --fn-positive:#b8832f; --fn-label:#c6c0b0;
  --fn-offer:#4fae90; --fn-lost-late:#a8415a;
  --co-1:#8b857a; --co-2:#98907f; --co-3:#7e7a74; --co-4:#928a82;
  --co-5:#857f78; --co-6:#9c958a;
  --fn-won:#189072; --fn-lost:#cf5a39; --fn-wait:#5b8fc9; --fn-closed:#9b7ad6;
    --fn-band:.42;
    --tk-key:#8fb4d9; --tk-str:#9ac4a4; --tk-num:#c3a4dc; --tk-bool:#e09070;
    --tk-com:#9b9175; --tk-punc:#7a7364; --tk-blk:#d0a468; --tk-sel:rgba(192,138,62,.3);
    --bad:#f0a189; --bad-bg:#2e1c15;
    --seg-track:#141310; --seg-on:#413d34; --knob:#e8e4da;
    --row-hover:#2c2a24; --spine:#5c574b; --bad-line:#4a2a1e;
    --c-hover:#6f6b60;
  }
}
:root{color-scheme:light}
:root[data-theme=dark]{color-scheme:dark}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark}}
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
/* A filled accent button at 40% fades its label and its fill together and
   lands around 1.7:1 -- unreadable, while still shaped like the app's primary
   action, so it reads as broken rather than as unavailable. Disabled means
   "not now", so it drops to a plain outline instead. */
.obtn.primary:disabled,.sbtn.primary:disabled,.pbtn:disabled{opacity:1;
  background:var(--field);border:1px solid var(--bd-field);color:var(--t500);
  font-weight:400}
:focus{outline:none}
:focus-visible{outline:2px solid var(--acc);outline-offset:1px;border-radius:3px}
.grow{flex:1}
[hidden]{display:none!important}

/* ---------- title bar (52px) -------------------------------------------
   The one dark surface left. It frames the window; everything under it is
   paper. It holds only what is true on every screen -- the mark, the three
   tabs, the AI clients and the gear -- so its shape never changes. */
#chrome{
  position:relative;
  height:52px; flex:none; display:flex; align-items:center; gap:24px; padding:0 16px;
  background:var(--c800); border-bottom:1px solid #000;
  -webkit-app-region:drag; user-select:none;
}
#chrome button,#chrome input,#chrome .tabs{-webkit-app-region:no-drag}
.lights{display:flex;gap:7px;padding-right:5px}
.lights button{width:11px;height:11px;border-radius:50%;background:var(--c400);
  transition:background .12s}
.lights button:hover{background:#6c6960}
.lights #w-close:hover{background:#c0392b}
.brand{display:flex;align-items:center;gap:9px;flex:none;font-size:14px;
  font-weight:600;color:var(--cw)}
.brand img{display:block}
/* Tabs, underlined. A pill track here read as one more control among the
   buttons; an underline reads as where you are. */
.tabs{display:flex;align-self:stretch;gap:2px}
.tabs button{padding:0 12px;font-size:13.5px;color:var(--c200);
  box-shadow:inset 0 -2px 0 transparent}
.tabs button:hover{color:var(--c050)}
.tabs button[aria-selected=true]{color:var(--cw);font-weight:500;
  box-shadow:inset 0 -2px 0 var(--acc)}

/* segmented control, dark */
.seg{display:flex;gap:1px;background:var(--c900);border-radius:5px;padding:2px}
.seg button{padding:4px 13px;border-radius:4px;color:var(--c200);font-size:12.5px;
  white-space:nowrap}
.seg button:hover{color:var(--c050)}
.seg button[aria-selected=true]{background:var(--c500);color:var(--c050);font-weight:500}

/* A screen's own header: what it is, how many, and what you can do there. */
.phead{flex:none;display:flex;align-items:center;gap:12px;padding:20px 24px 14px}
/* ---- Next up, on the applications list ---- */
.nu{flex:none;margin:0 24px 14px;display:grid;border:1px solid var(--rule);border-radius:14px;
  background:var(--field);overflow:hidden;box-shadow:0 1px 2px rgba(27,26,23,.05),0 6px 18px rgba(27,26,23,.05);
  animation:nu-rise .45s cubic-bezier(.2,.8,.2,1) both}
.nu>div{display:flex;align-items:center;gap:16px;padding:14px 20px;min-width:0}
.nu>div+div{border-left:1px solid var(--bd-inner)}
.nu .nu-tx{display:flex;flex-direction:column;gap:3px;min-width:0;flex:1}
.nu .ey{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:600;letter-spacing:.06em;
  text-transform:uppercase;color:var(--acc-text)}
.nu .ey i{width:7px;height:7px;border-radius:50%;background:var(--acc);flex:none;animation:nu-live 1.6s ease-in-out infinite}
.nu .ey.late{color:var(--bad)}
.nu .ey.late i{background:var(--bad);animation:nu-late 1.8s ease-out infinite}
.nu .ey.due{color:var(--fn-wait)}
.nu .ey.due i{background:var(--fn-wait);animation:none}
.nu .who{font-size:15px;font-weight:600;color:var(--t900);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nu .who span{font-weight:400;color:var(--t600)}
.nu .sub{font-size:12.5px;color:var(--t600);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nu .colog{width:44px;height:44px;border-radius:11px;font-size:14px}
.nu .end{display:flex;flex-direction:column;align-items:flex-end;gap:8px;flex:none}
.nu .cd{display:flex;align-items:baseline;gap:3px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--t900)}
.nu .cd b{font-size:22px;font-weight:600}
.nu .cd u{text-decoration:none;font-size:11.5px;color:var(--t500);margin-right:6px}
.nu .cd b.s{color:var(--acc)}
.nu .acts{display:flex;gap:8px}
.nu .acts .obtn{height:30px;padding:0 12px;border-radius:7px;font-size:12.5px;font-weight:500;white-space:nowrap}
.nu .obtn.dark{background:var(--t900);color:var(--app);border-color:var(--t900)}
.nu .stack{display:flex;flex:none}
.nu .stack .colog{width:30px;height:30px;border-radius:8px;font-size:10px;box-shadow:0 0 0 2px var(--field)}
.nu .stack .colog+.colog{margin-left:-8px}
.nu-wk{flex-direction:column;align-items:stretch!important;gap:8px!important;background:var(--row-alt)}
.nu-wk .hd{display:flex;align-items:baseline;justify-content:space-between;font-size:12.5px;font-weight:600;color:var(--t900)}
.nu-wk .hd button{border:0;background:none;padding:0;font-size:12.5px;font-weight:500;color:var(--acc-text)}
.nu-wk .hd button:hover{text-decoration:underline}
.nu-wk .days{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.nu-wk .days button{display:flex;flex-direction:column;align-items:center;gap:3px;padding:4px 0 5px;border:0;
  border-radius:7px;background:transparent;color:var(--t700);font-size:12.5px}
.nu-wk .days button:hover{background:var(--paper-hover)}
.nu-wk .days button.today{background:var(--field);box-shadow:0 0 0 1.5px var(--t900);color:var(--t900);font-weight:600}
.nu-wk .days small{font-size:10px;color:var(--t500)}
.nu-wk .days .mk{height:7px;display:flex;gap:3px;align-items:center}
.nu-wk .days .mk .d{width:6px;height:6px;background:var(--acc);transform:rotate(45deg)}
.nu-wk .days .mk .r{width:5px;height:5px;border-radius:50%;border:1.5px solid var(--fn-wait)}
.nu-wk .days .mk .r.late{border-color:var(--bad)}
/* An interview in under three hours takes the strip. */
.nu.soon{border-color:color-mix(in srgb,var(--acc) 55%,var(--rule));
  box-shadow:0 1px 2px rgba(27,26,23,.05),0 8px 24px color-mix(in srgb,var(--acc) 16%,transparent)}
.nu.soon .ey i{animation:nu-soon 1.6s ease-out infinite}
.nu .left{display:flex;flex-direction:column;align-items:center;padding:0 22px;border-left:1px solid var(--bd-inner);
  border-right:1px solid var(--bd-inner);flex:none}
.nu .left small{font-size:12px;color:var(--t500)}
.nu .left b{font-size:26px;font-weight:700;color:var(--acc-text);font-variant-numeric:tabular-nums;white-space:nowrap}
.nu .ready{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px;font-size:12.5px;color:var(--t700);flex:1;min-width:0}
.nu .ready li{display:flex;align-items:center;gap:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nu .ready i{width:14px;height:14px;border-radius:50%;flex:none;display:grid;place-items:center;font-style:normal;
  font-size:9px;font-weight:700;color:#fff;background:var(--good,#007a5e)}
.nu .ready i.no{background:none;border:1.5px solid var(--bd-field)}
.nu .pbtn{flex:none}
.nu .nu-fu .who{font-size:14px;font-weight:500}
.nu .ey{white-space:nowrap}
.nu .lnk{border:0;background:none;padding:0;font-size:12.5px;font-weight:500;color:var(--acc-text)}
.nu .lnk:hover{text-decoration:underline}
.nu .cd b{font-size:20px}
.nu.soon .nu-iv .nu-tx{flex:0 1 auto;max-width:38%}
/* Only follow-ups: one slim line. */
.nu.slim>div{padding:10px 18px}
.nu.slim .who{font-size:14px}
@keyframes nu-rise{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
@keyframes nu-live{0%,100%{opacity:.45}50%{opacity:1}}
@keyframes nu-late{0%,100%{box-shadow:0 0 0 0 color-mix(in srgb,var(--bad) 45%,transparent)}50%{box-shadow:0 0 0 5px transparent}}
@keyframes nu-soon{0%,100%{box-shadow:0 0 0 0 color-mix(in srgb,var(--acc) 50%,transparent)}50%{box-shadow:0 0 0 7px transparent}}
@media (prefers-reduced-motion:reduce){.nu,.nu .ey i{animation:none!important}}
@media (max-width:1240px){ .nu .nu-wk{display:none} .nu{grid-template-columns:minmax(0,1.4fr) minmax(0,1fr)!important} }
.phead h1{margin:0;font-size:20px;font-weight:600;letter-spacing:-.01em;
  color:var(--t900);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pcount{font-size:14px;color:var(--t500);font-variant-numeric:tabular-nums}

/* The editor's header: the crumb back to the list, the document, its actions. */
.docbar{height:52px;flex:none;display:flex;align-items:center;gap:8px;padding:0 16px;
  background:var(--app);border-bottom:1px solid var(--rule)}
.crumb{font-size:14px;color:var(--t600);flex:none}
.crumb:hover{color:var(--t900);text-decoration:underline}
.crumb-sep{color:var(--t400);font-size:14px}
.doctitle{display:flex;align-items:baseline;gap:9px;min-width:0;overflow:hidden}
.doctitle .t{font-size:14px;font-weight:600;color:var(--t900);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.doctitle .f{font-size:11.5px;color:var(--t500);white-space:nowrap;flex:none}
.acts{display:flex;align-items:center;gap:8px;flex:none}
.acts button{white-space:nowrap}
.doctitle{min-width:70px}
/* On a narrower window the language switch keeps its flags and drops the
   names, so the title and the buttons keep their room. */
@media (max-width:1380px){ #langsw .ln{display:none} #langsw button{padding:0 6px} }
@media (max-width:1180px){ .doctitle .f{display:none} }
/* Every screen header's secondary buttons, one height with its primary. */
.docbar .obtn,.phead .obtn{height:34px;padding:0 13px;font-size:13px;font-weight:500;
  color:var(--t900);border-radius:8px}

.search{display:flex;align-items:center;gap:8px;width:240px;height:34px;padding:0 11px;
  border:1px solid var(--bd-field);border-radius:8px;background:var(--field);color:var(--t500)}
.search input{border:0;background:none;font-size:13px;color:var(--t900);width:100%;padding:0}
.search input::placeholder{color:var(--t500)}
.search:focus-within{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-ring)}

.cbtn{font-size:12px;color:var(--c100);padding:4px 11px;border:1px solid var(--c500);
  border-radius:5px;white-space:nowrap}
.cbtn:hover:not(:disabled){border-color:var(--c-hover);color:#fff}
.cbtn.icon{width:32px;height:32px;padding:0;border:0;border-radius:8px;
  display:grid;place-items:center}
.cbtn.icon:hover:not(:disabled){background:var(--c600)}

/* The AI clients sit in the chrome because whether they are connected is a
   running state of the app, not a setting you visit once. One mark each, and
   the state rides on the mark: a green dot on its corner when connected, a
   red one when something is wrong, and a faded mark with no dot when it is
   not set up. Claude's mark keeps its own colour so it reads as Claude's. */
.cbtn.ai{display:flex;align-items:center;gap:13px;height:32px;padding:0 13px;
  border-radius:16px}
.aic{position:relative;display:flex}
.aic svg{flex:none;width:16px;height:16px}
.aic[data-client=claude] svg{color:#D97757}
/* The same tile as in the settings badge, at dot size. See the note there. */
.aic[data-client=hermes] svg{padding:2px;box-sizing:border-box;background:#fff;
  color:#000;border-radius:3px}
.aic .dot{position:absolute;right:-3px;bottom:-3px;width:7px;height:7px;
  border-radius:50%;background:var(--dot-idle);box-shadow:0 0 0 2px var(--c800);
  transition:background .15s}
.aic[data-state=connected] .dot{background:var(--fn-won)}
.aic[data-state=elsewhere] .dot,
.aic[data-state=other-workspace] .dot,
.aic[data-state=unreadable] .dot{background:var(--bad)}
.aic[data-state=absent] svg,.aic[data-state=unknown] svg{opacity:.45}
.aic[data-state=absent] .dot,.aic[data-state=unknown] .dot{display:none}
.pbtn{display:inline-flex;align-items:center;height:34px;padding:0 15px;border-radius:8px;
  font-size:13px;font-weight:600;color:var(--c800);background:var(--acc);white-space:nowrap}
.pbtn:hover:not(:disabled){background:var(--acc-hover)}

/* ---------- shell ------------------------------------------------------ */
main{flex:1;min-height:0;display:flex;background:var(--app)}
.view{flex:1;display:flex;min-height:0;min-width:0;position:relative}
.rail{flex:none;background:var(--panel);border-right:1px solid var(--rule);display:flex;
  flex-direction:column;min-height:0;overflow-y:auto}
.rail-cvs{width:232px;padding-bottom:10px} .rail-jobs{width:220px;padding:14px 10px 12px;gap:1px}
.rail-label{padding:16px 17px 6px;font-size:12px;font-weight:600;color:var(--t500)}
.rail-jobs .rail-label{padding:4px 10px 6px}
.rail-jobs .rail-label+.rail-label,.rail-jobs .rail-label:not(:first-child){padding-top:20px}
.rail-list{display:flex;flex-direction:column;padding:0 7px}
/* group headings inside a rail list: quieter than the rail's own label, so
   the documents stay the thing you read and the kinds just separate them */
.rail-sub{padding:12px 10px 4px;font-size:11.5px;color:var(--t500)}
.rail-list>.rail-sub:first-child{padding-top:2px}
/* a document that belongs to an application wears a small ochre tie */
.row .tie{width:5px;height:5px;border-radius:50%;background:var(--acc);
  flex:none;opacity:.75}


/* A sidebar row. Selected lifts onto the field colour, the way a chosen card
   sits on a desk, in both rails alike. */
.row{display:flex;align-items:center;gap:9px;padding:7px 10px;border-radius:7px;
  text-align:left;width:100%}
/* A translation of the base CV, hung under it. */
#doclist .row.tr{padding-left:24px;position:relative}
#doclist .row.tr::before{content:"";position:absolute;left:13px;top:-2px;bottom:50%;width:7px;
  border-left:1px solid var(--rule);border-bottom:1px solid var(--rule);border-bottom-left-radius:4px}
#doclist .row.tr .lbl{font-size:13px;color:var(--t500)}
.flg-only{display:flex;flex:none;opacity:.85}
.row:hover{background:var(--paper-hover)}
.row .mark{display:none}
.row .lbl{font-size:13.5px;color:var(--t700);flex:1;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.row .ct{font-size:12px;color:var(--t500);flex:none;font-variant-numeric:tabular-nums}
/* Attention borrows the ochre already used for an overdue follow-up rather
   than introducing a second warning colour. The section hides itself when
   every bucket is empty, so its presence is the signal and it does not need
   to shout. The count carries the colour; the labels stay ordinary text. */
.rail-label.attn{color:var(--acc-text)}
#attentionlist .row .ct{color:var(--acc-text);font-variant-numeric:tabular-nums}
.row.sel,.row.sel:hover{background:var(--field);
  box-shadow:0 1px 2px rgba(27,26,23,.08),0 0 0 1px var(--bd-inner)}
.row.sel .lbl{color:var(--t900);font-weight:600}
.row.sel .ct{color:var(--t600)}

/* outline rows sit one level in; the active section shows its entries */
.orow{display:flex;justify-content:space-between;gap:8px;padding:6px 10px 6px 20px;
  border-radius:7px;font-size:13px;color:var(--t700);text-align:left;width:100%}
.orow:hover{background:var(--paper-hover)}
.orow.sel{background:var(--field);color:var(--t900);font-weight:600;
  box-shadow:0 1px 2px rgba(27,26,23,.08),0 0 0 1px var(--bd-inner)}
.orow .ct{font-size:11.5px;color:var(--t500);flex:none;font-weight:400}
.orow.sel .ct{color:var(--t600)}
.okids{display:flex;flex-direction:column;padding:2px 0}
.okid{padding:5px 8px 5px 32px;font-size:12.5px;color:var(--t600);text-align:left;width:100%;
  border-radius:7px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.okid:hover{background:var(--paper-hover)}
.okid.sel{color:var(--acc-text);font-weight:500}

/* page budget */
.budget{padding:13px 17px;border-top:1px solid var(--rule);display:flex;flex-direction:column;
  gap:7px;flex:none}
.budget .brow{display:flex;justify-content:space-between;align-items:baseline}
.budget .pp{font-size:13px;font-weight:500;color:var(--t900)}
.budget .ww{font-size:11px;color:var(--t500)}
.budget .bar{display:flex;gap:2px}
.budget .bar i{height:5px;flex:1;background:var(--bd-field);border-radius:1px}
.budget .bar i.on{background:var(--acc)}
.budget .cap{font-size:12px;color:var(--t600);line-height:1.4}

/* ---------- centre column ---------------------------------------------- */
.centre{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0;
  background:var(--canvas)}
.subbar{height:33px;flex:none;display:flex;align-items:center;gap:11px;padding:0 12px;
  background:var(--bar);border-bottom:1px solid var(--rule-strong)}
.seg.light{background:var(--seg-track)}
.seg.light button{color:var(--t600);padding:3px 12px;font-size:12px}
.seg.light button:hover{color:var(--t900)}
.seg.light button[aria-selected=true]{background:var(--seg-on);color:var(--t900);font-weight:500}
.meta{font-size:11px;color:var(--t500);display:flex;align-items:center;gap:6px}
.meta button{font-size:12px;color:var(--t500);padding:0 3px;line-height:1}
.meta button:hover:not(:disabled){color:var(--t900)}

/* ---------- provenance ---------------------------------------------------
   Two questions, two marks, because they are not the same thing and reading
   them as one is how you end up trusting the wrong line:

     a client's own mark   -- who last wrote this field
     a plain ochre rule    -- this field no longer says what the base CV says

   None of this exists in the YAML. It is drawn from a sidecar into the app's
   own DOM, so there is no path by which a mark could reach RenderCV, Typst or
   the PDF. The page you export is the page you would have exported without it.
*/
.pmark{flex:none;display:inline-flex;align-items:center;justify-content:center;
  width:13px;height:13px;vertical-align:-2px;opacity:.92}
.pmark svg{width:11px;height:11px;display:block}
.pmark[data-by=claude]{color:#D97757}
.pmark[data-by=openai]{color:var(--t700)}
.pmark[data-by=mistral]{color:#FA520F}
.pmark[data-by=hermes]{color:var(--t700)}
.pmark[data-by=ai]{color:var(--t600)}
.pmark[data-by=you]{display:none}       /* your own edits are the default */
/* Stands for something underneath rather than for the line it sits on. */
.pmark.rolled{opacity:.45}
/* The vs-base mark is deliberately not a logo: it is a property of the line,
   not an author, and giving it a face would say somebody did it.

   It is also deliberately NOT ochre. A short rounded ochre bar is already this
   app's selection idiom -- the doc rail's .mark, the page band's inset rule,
   the form's selected card -- so drawing divergence the same way put two
   unrelated meanings on one glyph, in one viewport, 200px apart. That does not
   produce confusion, it produces a confident wrong reading. A dashed neutral
   rule shares the position but not the shape or the colour. */
.fromb{flex:none;width:2px;height:11px;border-radius:0;background:var(--t500);
  opacity:.75;vertical-align:-2px}
.fg>label .pmark,.fg>label .fromb{margin-left:5px}
.blabel .pmark,.blabel .fromb{margin-left:5px}
.crow .pmark{margin-left:3px}
.orow .pmark,.okid .pmark{margin-left:auto;margin-right:2px}

/* The chip in the subbar: the one place the whole document's provenance is
   summarised, on every tab, because it is a fact about the file rather than
   about the view you happen to be in. */
/* Sits in the page's left margin, pulled out of the text column rather than
   laid over it. Pointer-events off: the band underneath is the click target,
   and a mark that swallowed the click would break editing from the page. */
.pgmark{position:absolute;transform:translate(-136%,-2px);pointer-events:none;
  width:12px;height:12px;display:flex;align-items:center;justify-content:center;
  opacity:.85}
.pgmark svg{width:11px;height:11px;display:block}
.pgmark[data-by=claude]{color:#D97757}
.pgmark[data-by=openai]{color:#5b5750}
.pgmark[data-by=mistral]{color:#FA520F}
.pgmark[data-by=hermes]{color:#5b5750}
.pgmark[data-by=ai]{color:#8a877f;font-size:9px}
.pgmark[data-by=you]{display:none}

/* Clicking the page is a headline feature, and when the map cannot be built
   it simply is not there -- no error, no cursor change, a page that ignores
   you. This is the difference between a missing feature and a broken one. */
.nomap{display:inline-flex;align-items:center;gap:6px;height:21px;padding:0 9px;
  border:1px solid var(--rule-strong);border-radius:11px;background:var(--field);
  font-size:11.5px;color:var(--t500);cursor:help}
.nomap::before{content:"";width:5px;height:5px;border-radius:50%;
  background:var(--t500);flex:none}

.prov{display:inline-flex;align-items:center;gap:7px;height:21px;padding:0 9px;
  border:1px solid var(--rule-strong);border-radius:11px;background:var(--field);
  font-size:11.5px;color:var(--t600);cursor:pointer}
.prov:hover{color:var(--t900);border-color:var(--bd-field)}
.prov b{font-weight:500;color:var(--t900)}
.prov .dot{width:4px;height:4px;border-radius:50%;background:var(--t500);flex:none}
.provlist{display:flex;flex-direction:column;max-height:46vh;overflow:auto;
  border:1px solid var(--bd-field);border-radius:8px;font-size:12px}
.provlist .r{display:flex;align-items:flex-start;gap:9px;padding:8px 11px;
  background:var(--field);width:100%;text-align:left;cursor:pointer;
  border:0;border-radius:0;font:inherit;color:inherit}
.provlist .r:hover{background:var(--row-hover)}
.provlist .r:focus-visible{outline:2px solid var(--acc);outline-offset:-2px}
.provlist .go{flex:none;color:var(--t500);font-size:15px;line-height:1.1}
.provlist .r:hover .go{color:var(--t900)}
.provlist .w .mine{color:var(--t500)}
.provlist .r+.r{border-top:1px solid var(--rule)}
.provlist .w{flex:none;display:flex;align-items:center;gap:5px;min-width:112px;
  color:var(--t600);font-size:11px}
.provlist .f{flex:1;min-width:0}
.provlist .f b{display:block;font-weight:500;color:var(--t900);margin-bottom:2px}
/* What it says now, then what it used to say. The order is the answer to the
   question the sheet is titled after. */
.provlist .f em{display:block;font-style:normal;color:var(--t900);
  overflow-wrap:anywhere}
.provlist .f s{display:block;color:var(--t500);text-decoration:line-through;
  overflow-wrap:anywhere;margin-top:2px}
.provlist .none{padding:10px 12px;color:var(--t500);background:var(--field)}

/* Shown only when the file changed underneath you and you have edits that
   would overwrite it. Above the tabs, because it is about the document rather
   than about whichever view of it you happen to be in. */
.extbar{flex:none;display:flex;align-items:center;gap:9px;padding:7px 12px;
  font-size:12.5px;color:var(--t900);background:var(--acc-wash);
  border-bottom:1px solid var(--acc-line)}
.extbar svg{flex:none;color:var(--acc)}
.extbar .obtn{padding:3px 10px;font-size:12px}

/* Source beside the page, with a divider you can drag. The page takes what is
   left rather than a share of its own: it is the thing being made, and the
   form only needs to be wide enough to type in. */
.panes{flex:1;min-width:0;min-height:0;display:flex;--split:46%}
.pane{min-height:0;overflow:auto}
.pane-page{flex:1;min-width:0;display:grid;justify-items:center;align-content:start;
  padding:26px;--ed-room:406px}
.pane-form,.pane-yaml{flex:none;width:var(--split);min-width:260px}
/* A hairline you can actually hit: 1px of rule inside 7px of grab area, which
   is the difference between a divider that drags and one you chase. */
.split{flex:none;width:7px;cursor:col-resize;touch-action:none;
  border-left:3px solid transparent;border-right:3px solid transparent;
  background:var(--rule-strong);background-clip:content-box;
  transition:background-color .12s}
.split:hover,.split.on{background-color:var(--acc)}
.split:focus-visible{outline:2px solid var(--acc);outline-offset:-1px}
body.dragging{cursor:col-resize;user-select:none}
/* Room for the editor, made the way a word processor makes room for its
   comment rail: the sheet shifts left and the card sits clear of it, rather
   than landing on top of the paragraph you opened it to read. Padding rather
   than a margin, so the pane still scrolls over the whole sheet. Set from
   script, and only when the sheet can spare the width. */
.pane-page.ed-open{padding-right:var(--ed-room)}
#z-lvl{min-width:42px}
#z-lvl.auto{color:var(--t900)}
.pgwrap{position:relative;display:block;line-height:0}
.pg{display:block;background:var(--page);box-shadow:0 1px 2px rgba(0,0,0,.28),
  0 10px 34px rgba(0,0,0,.45)}

/* Click targets over the rendered page, one per block. Invisible until you
   point at one; the selected one keeps a bar down its left edge, the same way
   the outline rail marks the same state. The tints are fixed rather than
   themed because the page underneath is always white -- it is a document.
   Drawn with an inset shadow rather than a pseudo-element: a bar outside the
   button does not paint reliably, and it belongs inside the band anyway. */
.hit{position:absolute;padding:0;border:0;border-radius:3px;background:transparent;
  cursor:pointer;transition:background .1s,box-shadow .1s}
.hit:hover{background:rgba(192,138,62,.13)}
.hit.sel{background:rgba(192,138,62,.15);box-shadow:inset 3px 0 0 var(--acc)}
.hit.sel:hover{background:rgba(192,138,62,.21)}
.hit:focus-visible{outline:2px solid var(--acc);outline-offset:-2px}
.pane-form{background:var(--app);padding:16px 20px 40px}
/* The pane is the whole width of the app now that the panel beside it is gone,
   and a start date stretched across a thousand pixels reads as a bug. Hold the
   form to a measure you can scan, the way the page tab holds a sheet. */
#pane-form>*{max-width:880px;margin-inline:auto}
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
.entry{border-left:1px solid var(--rule);padding:2px 0 2px 14px;
  margin:12px 0 12px -14px}
/* the same mark the page and the outline use, in the form */
/* The padding is on the block at rest, not only when selected: the inset bar
   that marks a selection is drawn *over* the first three pixels of content,
   so without a gutter it sliced the leading character off the first label
   ("text 1" arriving as "ext 1"), and adding the room only on select would
   shift every field sideways as you moved through the form. */
.formblock{border-radius:4px;padding-left:9px;margin-left:-9px;
  transition:background .12s,box-shadow .12s}
.formblock.on{background:var(--acc-wash);box-shadow:inset 3px 0 0 var(--acc)}
.entry.formblock.on{border-left-color:transparent}
.entry-hd{font-size:12.5px;font-weight:600;margin-bottom:8px;display:flex;gap:8px;
  align-items:baseline}
/* The remove sits at the far end of the head, away from the fields: it is the
   one control here that cannot be undone by typing something else. */
.entry-hd .rm{margin-left:auto;color:var(--t500)}
.entry-hd .rm:hover{color:var(--bad);border-color:var(--bad)}
/* Adding is the last thing in a section and the last thing in the document,
   in that order, so the form reads as a list you can add to rather than a
   fixed shape someone else decided on. */
.addrow{padding:2px 0 14px}
.addrow.end{padding:4px 0 0;border-top:1px solid var(--rule)}
/* The same second line the block editor carries, for the same reason: two
   jobs at one employer are two identical headings without it. */
.entry-hd .sub{font-weight:400;font-size:11px;color:var(--t500);min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.entry-hd .sub:empty{display:none}

.fg{display:grid;grid-template-columns:70px 1fr;gap:8px 10px;align-items:center}
.fg.wide{grid-template-columns:120px 1fr;align-items:start}
.fg.w88{grid-template-columns:88px 1fr;gap:10px 12px}
.fg>label{font-size:12px;font-weight:500;color:var(--t600);text-align:left;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fg.wide>label{padding-top:6px}
/* background-COLOR, not the shorthand: the shorthand resets background-image
   and silently strips the chevron off every select. */
.fg input,.fg select,.fg textarea{background-color:var(--field);
  border:1px solid var(--bd-field);
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

/* ---------- the block editor -------------------------------------------
   Anchored beside the block it edits rather than parked in a column, so the
   page gets the whole pane and you edit at the thing you are looking at.

   Fixed to the window, placed from script. Living inside .pgwrap would have
   made it travel with the sheet for free, but the pane scrolls and clips, and
   a block near the left edge produced a negative offset that cut the label
   column off against the rail. The scroll handler walks it instead. */
.ed{position:fixed;z-index:30;width:380px;max-width:calc(100vw - 48px);
  display:flex;flex-direction:column;max-height:min(560px,calc(100vh - 140px));
  background:var(--panel);border:1px solid var(--bd-field);border-radius:10px;
  box-shadow:0 18px 44px -16px rgba(20,17,10,.42),0 2px 8px -3px rgba(20,17,10,.3);
  animation:edin .1s ease-out}
@keyframes edin{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}
.ed-head{flex:none;display:flex;align-items:flex-start;gap:8px;padding:7px 6px 8px 13px;
  background:var(--bar);border-bottom:1px solid var(--rule);
  border-radius:9px 9px 0 0}
/* Two lines: what the entry is called, and what tells it apart from the one
   above it. Two jobs at the same employer share a title, so the role and the
   years carry the difference. */
.ed-who{min-width:0;display:flex;flex-direction:column;gap:1px;padding-top:2px}
.ed-who b{font-size:12.5px;font-weight:600;color:var(--t900);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ed-who span{font-size:10.5px;color:var(--t500);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ed-who span:empty{display:none}
.ed-head button{width:24px;height:24px;border-radius:5px;color:var(--t500);
  font-size:12px;line-height:1;flex:none}
.ed-head button:hover:not(:disabled){background:var(--paper-hover);color:var(--t900)}
.ed-head button:disabled{opacity:.3}
.ed-head #ed-close{margin-left:4px}
.ed-body{flex:1;min-height:0;overflow-y:auto;padding:13px;
  display:flex;flex-direction:column;gap:13px}

/* Points at the block. Without it the card reads as floating over the page
   rather than as belonging to the paragraph beside it. */
.ed::before{content:"";position:absolute;top:var(--ptr,13px);width:8px;height:8px;
  background:var(--bar);border-left:1px solid var(--bd-field);
  border-bottom:1px solid var(--bd-field);transform:rotate(45deg)}
.ed[data-side=right]::before{left:-5px}
.ed[data-side=left]::before{right:-5px;transform:rotate(225deg)}

/* A document-level fact, so it sits with the other one rather than being
   redrawn inside every block's editor. */
.linkchip .dot{width:6px;height:6px}
/* "Base" is a role the document plays, so it reads as a tag rather than as
   another sentence in the chip. Neutral: the accent is spoken for. */
.btag{font-size:10.5px;font-weight:600;color:var(--t800);background:var(--bar);
  border-radius:4px;padding:1px 6px;line-height:1.5;flex:none;
  box-shadow:inset 0 0 0 1px var(--bd-field)}
.row .btag{font-weight:500;padding:0 5px;margin-left:2px}

/* ---------- the application peek ----------------------------------------
   Sized to the record rather than to a habit: wide enough for two columns of
   fields, capped so it never swallows the list it belongs to. It is not modal
   -- the table underneath stays live, and clicking another row moves the peek
   to it rather than stacking a second one. */
/* Open, the record gets the room: the table narrows to a list of who and
   where, just wide enough to arrow through, and the record takes the rest. It
   used to be the other way round -- the record at 46% and the table squeezed
   beside it into "M.", "D.", "No C...". */
#v-jobs{--peek-w:max(460px,calc(100% - 600px))}
.peek{position:absolute;top:0;right:0;bottom:0;z-index:40;
  width:var(--peek-w);display:flex;flex-direction:column;
  background:var(--app);border-left:1px solid var(--rule);
  animation:peekin .12s ease-out}
@keyframes peekin{from{transform:translateX(10px);opacity:.4}to{transform:none;opacity:1}}
.peek-head{flex:none;display:flex;align-items:center;gap:14px;padding:0 12px 0 24px;
  height:64px;border-bottom:1px solid var(--rule)}
.peek-who{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}
.peek-who b{font-size:18px;font-weight:600;letter-spacing:-.01em;color:var(--t900);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.peek-who span{font-size:13px;color:var(--t600);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.peek-nav{flex:none;display:flex;align-items:center;gap:4px}
.peek-nav button{width:34px;height:34px;border-radius:8px;color:var(--t600);
  font-size:14px;line-height:1;border:1px solid var(--bd-field);background:var(--field)}
.peek-nav button:hover:not(:disabled){background:var(--paper-hover);color:var(--t900)}
.peek-nav #jpk-idx{font-size:11px;color:var(--t500);padding:0 5px;min-width:44px;
  text-align:center}
.peek-nav #jpk-close{margin-left:6px}
.peek-body{flex:1;min-height:0;overflow-y:auto;padding:22px 24px;
  display:flex;flex-direction:column;gap:20px}
/* The peek is absolutely positioned, so the table has to be told to stop
   underneath it. Giving way rather than being covered means the row you are
   arrowing through stays readable beside the record it opened. */
.peeking .tablewrap{margin-right:var(--peek-w)}
@media(max-width:1100px){ .peeking .tablewrap{margin-right:0} }
/* The compact list: company over role, and the status as its dot alone. */
.peeking .phead .search,.peeking #btn-newjob,.peeking .thead{display:none}
.peeking .trow{grid-template-columns:minmax(0,1fr) 28px;grid-template-rows:auto auto;
  height:auto;padding:9px 0;row-gap:1px}
.peeking .trow>:nth-child(1){grid-column:1;grid-row:1}
.peeking .trow>:nth-child(2){grid-column:1;grid-row:2;padding-left:49px;
  font-size:12px;color:var(--t500)}
.peeking .trow>:nth-child(2) b{font-weight:400}
.peeking .trow>:nth-child(3),.peeking .trow>:nth-child(5),
.peeking .trow>:nth-child(6){display:none}
.peeking .trow>:nth-child(4){grid-column:2;grid-row:1/span 2;font-size:0;padding:0}

/* Two columns once there is room for two. Below that it stacks, which is the
   old behaviour and still correct on a small window. */
.peek .fg2{display:grid;grid-template-columns:88px minmax(0,1fr);gap:10px 12px;
  align-items:center}
.peek .fg2 input,.peek .fg2 select,.peek .statusctl select{min-height:36px;
  border-radius:8px;font-size:13px}
.peek-grid{flex:1;min-height:0;display:grid;grid-template-columns:1fr;gap:16px}
@media(min-width:1280px){
  .peek-grid{grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px 22px}
}
.peek-grid .col{display:flex;flex-direction:column;gap:16px;min-width:0}
/* Notes gets real room -- it used to be three rows -- but not the whole column:
   stretched to 700px it reads as a mistake rather than as generosity. */
.peek-grid textarea.notes{min-height:150px;max-height:280px;resize:vertical}
/* The posting is the long one, so it takes what is left and scrolls. */
.peek-grid .block.grow{flex:1;min-height:0}
.peek .posting{flex:1;min-height:80px;overflow-y:auto;white-space:pre-wrap;
  font-size:12px;line-height:1.6;color:var(--t600);background:var(--field);
  border:1px solid var(--bd-field);border-radius:7px;padding:10px 12px;
  overflow-wrap:anywhere}
/* Destructive, so it is last and separated -- but not pinned to the bottom of
   the column, where it floated alone over a half-screen of nothing and drew
   the eye to the one control that should never attract it. */
.peek-grid .foot-del{margin-top:4px}
.peek-grid .foot-del .sbtn{align-self:flex-start}
/* The posting and the documents read as one list, so they share a card. */
.peek .tl{max-height:none}
.insp-head{height:33px;flex:none;display:flex;align-items:center;justify-content:space-between;
  gap:10px;padding:0 13px;background:var(--bar);border-bottom:1px solid var(--rule-strong)}
.insp-head b{font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.insp-head .mono{font-size:10.5px;color:var(--t500);flex:none}
.insp-body{flex:1;min-height:0;overflow-y:auto;padding:14px;display:flex;
  flex-direction:column;gap:14px}
.block{display:flex;flex-direction:column;gap:7px}
.block.ruled{padding-top:12px;border-top:1px solid var(--rule)}
/* Section labels read as headings, in the interface face, rather than as
   spaced-out capitals in the code face. */
.blabel{font-size:13px;color:var(--t900);font-weight:600}

.card{border:1px solid var(--bd-field);border-radius:10px;background:var(--field);overflow:hidden}
.card>*+*{border-top:1px solid var(--bd-inner)}
.crow{display:flex;gap:9px;padding:8px 10px;align-items:flex-start}
.crow .cidx{font-size:10.5px;color:var(--t400);padding-top:2px;flex:none;
  display:flex;align-items:center;gap:3px;white-space:nowrap}
.crow .cidx>span:first-child{min-width:11px}
.crow.on{background:var(--acc-wash)}
.crow.on .cidx{color:var(--acc-text)}
.crow textarea{flex:1;border:0;background:none;resize:none;overflow:hidden;padding:0;
  font:12.5px/1.45 inherit;color:var(--t900);min-width:0}
.drow{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 10px}
.drow>span{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drow select{border:0;background:none;font-size:12.5px;color:var(--t900);flex:1;min-width:0;
  padding:0;cursor:pointer}
.drow select.empty{color:var(--t400)}
.alink{font-size:12px;color:var(--acc-text);flex:none;cursor:pointer}
/* A job board's mark: small, square, in its own colours, because it is
   identifying somebody else's product. Lettered tiles stay neutral. */
.board{width:16px;height:16px;border-radius:4px;flex:none;display:inline-grid;
  place-items:center;vertical-align:middle}
.board svg{width:10px;height:10px;display:block}
.board.lettered{background:var(--bar);color:var(--t700);font-size:8px;font-weight:600;
  box-shadow:inset 0 0 0 1px var(--bd-field)}
.trow .role .via{flex:none;display:inline-flex;align-self:center}
.posting-row{justify-content:flex-start}
.posting-row>span:not(.board){flex:1;min-width:0;text-align:left}
.posting-row .alink{white-space:nowrap}
.posting-row>span i{font-style:normal;font-size:11.5px;margin-left:4px}
/* What the CV changed from its base, under the CV it describes. */
.bdiff{font-size:12px;color:var(--t700);line-height:1.45}
.bd-head{margin:0;display:flex;flex-wrap:wrap;gap:2px 8px;align-items:baseline}
.bd-head b{color:var(--t900);font-weight:600}
.bd-design{font-size:11.5px;color:var(--t500)}
.bd-list{list-style:none;margin:7px 0 0;padding:0;display:flex;flex-direction:column;gap:7px}
.bd-list li{display:flex;flex-direction:column;gap:2px;padding-left:9px;
  border-left:2px solid var(--rule)}
.bd-where{font-size:11.5px;color:var(--t500)}
.bd-kind{font-style:normal;font-size:10.5px;color:var(--t600);background:var(--bar);
  border-radius:3px;padding:0 4px;margin-left:4px}
.bdiff del{color:var(--t500);text-decoration:line-through;text-decoration-color:var(--t400)}
.bdiff ins{text-decoration:none;color:var(--t900)}
.bd-more{margin-top:7px}
.bd-more summary{cursor:pointer;font-size:11.5px;color:var(--acc-text)}
.alink:hover{text-decoration:underline}
.muted{color:var(--t400)}

.mini{width:24px;height:21px;display:grid;place-items:center;border:1px solid var(--bd-field);
  border-radius:4px;background:var(--field);font-size:13px;color:var(--t700);flex:none}
.mini:hover:not(:disabled){background:var(--paper-hover)}
.obtn{font-size:12px;padding:5px 11px;border:1px solid var(--bd-field);border-radius:6px;
  background:var(--field);color:var(--t700)}
.obtn:hover:not(:disabled){background:var(--paper-hover)}

/* five bars for a 0-5 fit score */
.fit{display:flex;gap:3px;align-items:center}
.statusctl{display:flex;align-items:center;gap:8px;flex:1;min-width:0}
.statusctl select{flex:1;min-width:0}
/* An unrated bar is an empty track, not a filled one. It used to be painted
   --c400 -- a token off the dark-chrome ramp -- so five near-black filled bars
   sat next to the words "not rated" and read as five out of five. The only
   difference between "best possible fit" and "no opinion recorded" was hue.
   The hit area is padded out to something a finger or a hurried cursor can
   land on; the bar itself stays 6px. */
.fit button{width:16px;height:20px;background:none;border-radius:0;padding:7px 0;
  background-clip:content-box;box-shadow:inset 0 0 0 1px var(--bd-inner);
  -webkit-background-clip:content-box}
.fit button:hover{background:var(--t500);background-clip:content-box}
.fitv{margin-left:8px;font-size:11.5px;color:var(--t500);white-space:nowrap}
.fit button.on{background:var(--acc);background-clip:content-box;
  box-shadow:inset 0 0 0 1px transparent}
.dot{width:6px;height:6px;border-radius:50%;flex:none;background:var(--dot-idle)}
.dot.live{background:var(--acc)}
.dot.won{background:var(--fn-won)} .dot.lost{background:var(--fn-lost)}
.dot.waiting{background:var(--fn-wait)} .dot.closed{background:var(--fn-closed)}
/* An offer is a live conversation at its peak -- same family, higher
   stakes -- so it keeps the amber and earns a ring rather than a sixth
   hue. The palette is at its useful hue budget; shape is the encoding
   with room left in it. */
.dot.offer{background:var(--acc);outline:1.5px solid var(--acc);
  outline-offset:1.5px}
/* Draft had the lowest contrast of any dot in the app at 2.2:1 -- the one
   state you genuinely could not see. */
.dot.draft{background:var(--t500)}

/* history timeline */
.tl{display:flex;flex-direction:column}
.tli{display:flex;gap:10px}
.tli .spine{display:flex;flex-direction:column;align-items:center;width:9px;flex:none}
.tli .spine i{width:7px;height:7px;border-radius:50%;background:var(--spine);margin-top:4px;flex:none}
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
/* On paper, under a hairline: it reports on the work, so it sits with it
   rather than closing a dark frame around it. */
#status{height:24px;flex:none;display:flex;align-items:center;gap:8px;padding:0 16px;
  background:var(--panel);border-top:1px solid var(--rule);font-size:11px;
  color:var(--t500);user-select:none}
/* a decision you just took about someone else's edit, not routine chatter */
#st-right.said{color:var(--acc-text);font-weight:500}
/* the MCP boundary having just stopped something */
#st-right.blocked{color:var(--bad);font-weight:500}
#status .warn{color:var(--acc-text)}

/* ---------- jobs table --------------------------------------------------- */
.tablewrap{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0;
  background:var(--app)}
/* The table is a card on the paper, so the rows read as one object with an
   edge rather than as lines running off both sides of the window. */
.tcard{flex:1;min-height:0;display:flex;flex-direction:column;margin:0 24px 20px;
  background:var(--field);border:1px solid var(--rule);border-radius:10px;overflow:hidden}
/* The base CV, as a card at the foot of the filters. It is not a row of the
   table -- it is the thing the table's rows are copies of -- and as a band
   across the top of the list it pushed every application down a row. */
.baserow{flex:none;display:flex;flex-direction:column;gap:6px;margin:14px 0 0;padding:10px;
  background:var(--field);border:1px solid var(--rule);border-radius:14px;
  box-shadow:0 10px 26px -20px rgba(30,26,18,.45)}
.baserow .bl{font-size:12px;font-weight:600;color:var(--t500)}
.baserow .bn{font-size:14.5px;font-weight:600;color:var(--t900);min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.baserow .bsub{font-size:12px;color:var(--t500);padding:0 4px}
.baserow .bsub:empty{display:none}
.baserow.gone .bn{text-decoration:line-through;color:var(--t500)}
.baserow .obtn{flex:none}
/* The page on a little stage: tinted, with the sheet standing on it. */
.bstage{position:relative;display:flex;justify-content:center;padding:14px 12px 0;
  height:150px;overflow:hidden;border-radius:10px;
  background:linear-gradient(160deg,color-mix(in srgb,var(--acc) 14%,var(--bar)),var(--bar) 70%)}
.bstage .btag{position:absolute;left:8px;top:8px;z-index:1;height:20px;display:flex;align-items:center;
  padding:0 8px;border-radius:10px;font-size:11px;font-weight:600;color:var(--acc-text);
  background:var(--field);box-shadow:0 1px 3px rgba(30,26,18,.15)}
.baserow .bthumb{width:132px;height:auto;aspect-ratio:210/297;border:0;border-radius:3px 3px 0 0;
  box-shadow:0 10px 24px -10px rgba(30,26,18,.5),0 0 0 1px rgba(30,26,18,.06);
  transition:transform .2s ease,box-shadow .2s ease}
.baserow .bthumb:hover{transform:translateY(-4px);
  box-shadow:0 16px 30px -12px rgba(30,26,18,.55),0 0 0 1px rgba(30,26,18,.08)}
.baserow .bthumb img{object-fit:cover;object-position:top center}
.bmeta{display:flex;align-items:center;gap:8px;padding:4px 4px 0;min-width:0}
.bmeta .bn{flex:1}
.bflags{display:flex;gap:3px;flex:none}
.bflags .flg{width:16px;height:11px}
.bacts{display:flex;gap:6px;padding:4px 0 0}
.bacts .bopen{flex:1}
.bacts .bico{width:34px;padding:0;display:grid;place-items:center;color:var(--t700)}
.baserow .grow{display:none}
/* The top of the base's first page: the name, the headline and the first
   section, which is what tells two CVs apart at a glance. The page stays white
   in either appearance, as it does in the editor. */
.bthumb{display:block;padding:0;border:1px solid var(--rule);border-radius:6px;
  background:#fff;overflow:hidden;cursor:pointer;flex:none}
.bthumb img{display:block;width:100%;height:100%;object-fit:cover;
  object-position:top center}
.bthumb.empty{display:grid;place-items:center;background:var(--bar)}
.bthumb.empty span{font-size:11px;color:var(--t500)}
.bthumb:hover{border-color:var(--bd-field)}
.bthumb:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
/* ------------------------------------------------------------- Documents -- */
.docpane{flex:1;min-width:0;min-height:0;overflow-y:auto;background:var(--app)}
.docwrap{max-width:1000px;margin:0 auto;padding:12px 24px 64px}
.docwrap>.phead{padding:10px 0 18px}
/* The base as a sheet of paper you can read, beside what it is and what came
   of it: the page is the point of a CV, so the card shows the whole of page
   one rather than a strip of it. */
.bhero{display:flex;flex-direction:column;background:var(--field);border:1px solid var(--rule);
  border-radius:14px;overflow:hidden}
.bmain{display:flex;gap:28px;padding:22px}
/* One tab per language of the base CV. Only there once a second language is,
   apart from the way to add one. */
.btabs{display:flex;align-items:center;gap:4px;padding:7px 9px;background:var(--bar);
  border-bottom:1px solid var(--rule);flex-wrap:wrap}
.btabs button{display:flex;align-items:center;gap:8px;height:34px;padding:0 12px;border:0;
  border-radius:8px;background:transparent;color:var(--t700);font-size:13px}
.btabs button:hover{background:var(--paper-hover)}
.btabs button[aria-selected=true]{background:var(--field);color:var(--t900);font-weight:600;
  box-shadow:0 1px 2px rgba(27,26,23,.12)}
.btabs .src{font-size:11.5px;font-weight:400;color:var(--t500)}
.btabs .behind{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:500;
  color:var(--acc-text)}
.btabs .behind i{width:7px;height:7px;border-radius:50%;background:var(--acc)}
.btabs .add{border:1px dashed var(--bd-field);color:var(--t600)}
.samp-pill{display:flex;align-items:center;gap:8px;height:30px;padding:0 6px 0 12px;margin-right:12px;
  border-radius:15px;background:rgba(192,138,62,.16);box-shadow:inset 0 0 0 1px rgba(232,188,124,.45);
  color:#e8bc7c;font-size:12.5px;font-weight:600}
.samp-pill i{width:7px;height:7px;border-radius:50%;background:#e8bc7c}
.samp-pill b{height:22px;display:flex;align-items:center;padding:0 10px;border-radius:11px;
  background:#e8bc7c;color:#1b1a17;font-weight:600;font-size:12px}
.samp-pill:hover b{background:#f0cb93}
/* A language, as its code: EN, FR. Dark when it is the one you are on. */
.lchip{display:inline-flex;align-items:center;height:19px;padding:0 6px;border-radius:5px;
  font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10.5px;font-weight:600;
  letter-spacing:.04em;background:transparent;box-shadow:inset 0 0 0 1px var(--bd-field);
  color:var(--t700);flex:none}
.lchip.on{background:var(--c800);color:var(--cw);box-shadow:none}
.flg{width:15px;height:10px;border-radius:2px;flex:none;display:inline-block;
  box-shadow:0 0 0 .5px rgba(0,0,0,.25)}
.lchip .flg{margin-right:4px}
#dfilter .flg{margin-right:6px;vertical-align:-1px}
/* One language: no tabs, flags, filters or language fields anywhere. */
html.mono .btabs,html.mono #dfilter,html.mono .bflags,html.mono .ap-fact.lang,
html.mono .langs,html.mono #langsw{display:none!important}
.ap-lang{display:flex;align-items:center;gap:7px;min-width:0}
.ap-lang .flg{width:18px;height:12px;border-radius:2px;flex:none;box-shadow:0 0 0 .5px rgba(0,0,0,.25)}
:root[data-theme=dark] .lchip.on{background:var(--cw);color:var(--c800)}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]) .lchip.on{
  background:var(--cw);color:var(--c800)}}
.bhero .bthumb{width:196px;aspect-ratio:210/297;height:auto;border-radius:3px;
  box-shadow:0 10px 28px -12px rgba(30,26,18,.45),0 1px 2px rgba(30,26,18,.12)}
.bhero .bthumb img{object-fit:contain}
.bhero .bbody{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px;padding-top:4px}
.bhero .bl{font-size:12px;font-weight:600;color:var(--acc-text)}
.bhero .bn{margin:0;font-size:24px;font-weight:700;letter-spacing:-.015em;color:var(--t900);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bhero .bmeta{display:block;padding:0;font-size:13px;color:var(--t500)}
.bhero .bwhy{margin:8px 0 0;font-size:13.5px;line-height:1.55;color:var(--t700);max-width:60ch}
.bstats{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}
.bstats div{min-width:120px;padding:10px 14px;border-radius:10px;background:var(--app);
  border:1px solid var(--bd-inner);display:flex;flex-direction:column;gap:2px}
.bstats b{font-size:20px;font-weight:600;color:var(--t900);font-variant-numeric:tabular-nums}
.bstats span{font-size:12px;color:var(--t500)}
.bhero .bacts{margin-top:auto;padding-top:18px;display:flex;gap:8px;flex-wrap:wrap}
.bhero.gone .bn{text-decoration:line-through;color:var(--t500)}
.bhero.none .bmain{align-items:center}
/* The other documents, as pages. */
.dsec{margin-top:34px}
.dsec h2{margin:0;font-size:15px;font-weight:600;color:var(--t900);display:flex;
  align-items:baseline;gap:8px}
.dsec h2 .n{color:var(--t500);font-weight:400;font-size:13px}
.dsec .why{margin:4px 0 14px;font-size:13px;color:var(--t600);max-width:70ch}
.dgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(184px,1fr));gap:18px}
.dcard{display:flex;flex-direction:column;gap:10px;padding:0;border:0;background:none;
  text-align:left;color:var(--t900);cursor:pointer;font-family:inherit;min-width:0}
.dcard .pg{position:relative;aspect-ratio:210/297;background:#fff;border:1px solid var(--rule);
  border-radius:4px;overflow:hidden;box-shadow:0 6px 18px -12px rgba(30,26,18,.4);
  transition:transform .15s,box-shadow .15s;display:grid;place-items:center}
.dcard .pg img{width:100%;height:100%;object-fit:contain;object-position:top;display:block}
.dcard .pg>span{font-size:11.5px;color:#6b675d}
.dcard:hover .pg{transform:translateY(-2px);box-shadow:0 14px 28px -14px rgba(30,26,18,.5);
  border-color:var(--bd-field)}
.dcard:focus-visible{outline:none}
.dcard:focus-visible .pg{outline:2px solid var(--acc);outline-offset:3px}
.dcard .langs{position:absolute;left:8px;bottom:8px;display:flex;gap:4px}
.dcard .tag{position:absolute;right:8px;bottom:8px;font-size:10.5px;font-weight:600;
  padding:2px 8px;border-radius:9px;background:var(--c800);color:var(--cw)}
.dcard .meta{display:flex;flex-direction:column;align-items:stretch;gap:3px;padding:0 2px;
  min-width:0;text-align:left}
.dcard .meta b{font-size:13.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.dcard .meta span{font-size:12px;color:var(--t600);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;display:flex;align-items:center;justify-content:flex-start;gap:6px}
.dcard .meta em{font-style:normal;color:var(--t500)}
.dfilter{display:flex;gap:2px;padding:3px;border-radius:9px;background:var(--seg-track)}
.dfilter button{height:28px;padding:0 11px;border:0;border-radius:7px;background:transparent;
  color:var(--t700);font-size:12.5px}
.dfilter button[aria-pressed=true]{background:var(--seg-on);color:var(--t900);font-weight:600;
  box-shadow:0 1px 2px rgba(27,26,23,.1)}
/* The language switch in the editor's bar, between the versions of one CV. */
.langsw{display:flex;gap:2px;padding:3px;border-radius:9px;background:var(--seg-track);
  margin-left:6px}
.langsw button{display:flex;align-items:center;gap:6px;height:28px;padding:0 9px;border:0;
  border-radius:7px;background:transparent;color:var(--t700);font-size:12.5px}
.langsw button[aria-current=true]{background:var(--seg-on);color:var(--t900);font-weight:600;
  box-shadow:0 1px 2px rgba(27,26,23,.1)}
/* Add a language, and what to say to your AI client afterwards. */
.al-list{display:flex;flex-direction:column;gap:5px;max-height:300px;overflow-y:auto;padding:1px}
.al-list button{display:flex;align-items:center;gap:10px;padding:8px 11px;border-radius:8px;
  border:1px solid var(--bd-inner);background:var(--row-alt);text-align:left;color:var(--t900);
  font-size:13px}
.al-list button:hover{border-color:var(--bd-field)}
.al-list button[aria-checked=true]{border:2px solid var(--acc);padding:7px 10px;background:var(--field)}
.al-list em{font-style:normal;color:var(--t500);font-size:12px}
.al-does{margin:0;padding:11px 13px;list-style:none;border-radius:9px;background:var(--field);
  border:1px solid var(--bd-inner);display:flex;flex-direction:column;gap:6px;font-size:12.5px;
  line-height:1.45;color:var(--t800)}
.al-does li{display:flex;gap:8px}
.al-does li::before{content:"\2713";color:var(--fn-won);font-weight:700;flex:none}
.say-box{display:flex;flex-direction:column;gap:8px;padding:12px 13px;border-radius:9px;
  background:var(--field);border:1px solid var(--bd-inner)}
.say-box b{font-size:12.5px;font-weight:600;color:var(--t900)}
.say-box .say{font-size:13px;line-height:1.5;color:var(--t900);padding:9px 11px;border-radius:7px;
  background:var(--app);border:1px solid var(--bd-inner);user-select:all}
.say-box .row{display:flex;align-items:center;flex-wrap:wrap;gap:8px 10px;font-size:12px;
  color:var(--t500)}
.lt-panel .say-box .row .grow{flex-basis:100%}
.say-box .row .grow{flex:1}
.drift-list{margin:0;padding:0;list-style:none;max-height:320px;overflow-y:auto;
  border:1px solid var(--rule);border-radius:9px;background:var(--field)}
.drift-list li{display:flex;flex-direction:column;gap:4px;padding:10px 12px;font-size:12.5px;
  line-height:1.45}
.drift-list li+li{border-top:1px solid var(--bd-inner)}
.drift-list .w{font-size:11.5px;font-weight:600;color:var(--t500)}
.drift-list .l{display:flex;gap:8px}
.drift-list .l .lchip{margin-top:1px}
.drift-list .then{color:var(--t500);text-decoration:line-through}
.dempty{padding:26px 20px;border:1.5px dashed var(--rule-strong);border-radius:12px;
  font-size:13px;line-height:1.5;color:var(--t500);text-align:center}
@media (max-width:720px){
  .bhero{flex-direction:column}
  .bhero .bthumb{width:150px}
}
.thead,.trow{display:grid;
  grid-template-columns:minmax(0,1.25fr) minmax(0,1.5fr) minmax(0,1.15fr) 186px 86px 96px;
  align-items:center}
.thead{height:38px;flex:none;background:var(--row-alt);border-bottom:1px solid var(--rule);
  font-size:12px;font-weight:600;color:var(--t500)}
.thead>*,.trow>*{padding:0 14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* the company mark, and the column it leads */
.co{display:flex;align-items:center;gap:9px;min-width:0}
.con{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.colog{width:28px;height:28px;border-radius:7px;flex:none;object-fit:contain;
  background:var(--bar)}
/* A black or near-black tile would melt into the dark surfaces. */
:root[data-theme=dark] img.colog{box-shadow:0 0 0 1px rgba(255,255,255,.12)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) img.colog{box-shadow:0 0 0 1px rgba(255,255,255,.12)}}
span.colog{display:grid;place-items:center;font-size:10.5px;font-weight:600;
  color:#fff;letter-spacing:.02em}
.trow .role b{font-weight:400}

.tbody{flex:1;min-height:0;overflow-y:auto}
.trow{height:50px;font-size:13.5px;border-bottom:1px solid var(--bd-inner);width:100%;
  text-align:left;color:var(--t900)}
.trow:hover{background:var(--row-hover)}
.trow .role{display:flex;gap:8px;align-items:baseline;min-width:0}
.trow .role b{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trow .role i{font-style:normal;font-size:12px;color:var(--t500);flex:none}
.trow .docs{font-size:12px;color:var(--t700)}
/* the most repeated string in the table, so it has to clear AA */
.trow .docs.none{color:var(--t500);font-size:12.5px}
/* An application with no CV is the one row that wants something doing, so it
   offers -- but only once you are on the row. Not a <button>: .trow is itself
   a button and nesting one is invalid, which is why data-open is a span too. */
.trow .docs.make{color:var(--t500);font-size:12.5px;cursor:pointer}
.trow .docs.make .nt{font-style:normal}
.trow .docs.make .tl{display:none;text-decoration:none;font-size:12px;font-weight:500;
  color:var(--t900);background:var(--field);border:1px solid var(--bd-field);
  border-radius:6px;padding:3px 9px}
.trow:hover .docs.make .nt,.trow.sel .docs.make .nt{display:none}
.trow:hover .docs.make .tl,.trow.sel .docs.make .tl{display:inline-block}
.trow .docs.make .tl:hover{border-color:var(--t400)}
.trow .docs.busy{color:var(--t500);font-size:12.5px;cursor:default}
.trow .docs.busy:hover,.trow .docs.make:hover{text-decoration:none}
.trow .docs:hover{text-decoration:underline}
.trow .st{display:flex;align-items:center;gap:8px;font-size:13px}
.trow .money{font-size:12px}
.trow .when{font-size:13px;color:var(--t600);font-variant-numeric:tabular-nums}
.trow.dead{color:var(--t600)}
.trow.dead .money,.trow.dead .when{color:var(--t600)}
.trow .when.none,.trow .money.none{color:var(--t400)}
.trow .when.due{color:var(--acc-text);font-weight:600}
/* Selected is the accent's one job in the table: a wash and an edge, with
   the text left as it was so nothing has to be re-read in a new colour. */
.trow.sel,.trow.sel:hover{background:var(--acc-wash);box-shadow:inset 3px 0 0 var(--acc)}

/* ---------- funnel ------------------------------------------------------- */
.fn-page{flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;
  background:var(--app);overflow-y:auto}
.fn-bar .obtn{height:34px;padding:0 13px;font-size:13px}
.fn-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;min-width:0}
.fn-head span{font-size:13.5px;color:var(--t500)}
#fn-body{display:flex;flex-direction:column;gap:20px;padding:0 24px 28px}
.fn-card{background:var(--field);border:1px solid var(--rule);border-radius:14px;
  padding:18px 20px;min-width:0}
.fn-card h2{margin:0;font-size:14px;font-weight:600;color:var(--t900)}
.fn-ch{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.fn-ch span{font-size:12.5px;color:var(--t500)}
.fn-big{font-size:30px;font-weight:600;letter-spacing:-.02em;color:var(--t900);
  font-variant-numeric:tabular-nums;line-height:1.1}

/* Sent to accepted. */
.fn-journey{display:flex;align-items:flex-start;gap:14px;padding:22px 28px 24px}
.fj-stg{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}
.fj-stg .sl{font-size:13px;font-weight:500;color:var(--t600)}
.fj-stg b{font-size:46px;font-weight:600;letter-spacing:-.03em;line-height:1.05;
  color:var(--t900);font-variant-numeric:tabular-nums}
.fj-stg small{font-size:12.5px;color:var(--t500)}
.fj-bar{margin-top:10px;height:6px;border-radius:3px;background:var(--bd-inner);overflow:hidden}
.fj-bar span{display:block;height:100%;border-radius:3px;background:var(--t800);
  transform-origin:left}
.fj-conv{flex:none;align-self:flex-start;display:flex;flex-direction:column;align-items:center;
  gap:2px;margin-top:14px;padding:10px 12px;border-radius:12px;color:var(--t400)}
.fj-conv b{font-size:17px;font-weight:600;color:var(--t900);font-variant-numeric:tabular-nums}
.fj-conv span{font-size:11.5px;color:var(--t500);white-space:nowrap}
.fj-conv.acc{background:var(--acc-wash);color:var(--acc);box-shadow:inset 0 0 0 1px
  color-mix(in srgb,var(--acc) 35%,transparent)}
.fj-conv.acc b,.fj-conv.acc span{color:var(--acc-text)}
.fj-conv.acc span{font-weight:500}

.fn-main{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:20px;align-items:stretch}
.fn-side{display:flex;flex-direction:column;gap:20px;min-width:0}
.fn-row3{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr) minmax(0,1fr);gap:20px}
@media (max-width:1180px){
  .fn-main{grid-template-columns:minmax(0,1fr)}
  .fn-row3{grid-template-columns:minmax(0,1fr)}
}

/* The chart's stage. Its own palette, the dark theme's funnel colours, in
   both themes: glow and moving light only read on a dark ground. */
.fn-stage{position:relative;display:flex;flex-direction:column;min-width:0;min-height:520px;
  padding:22px 24px 20px;border-radius:16px;overflow:hidden;
  /* Light: a white card like the others, in the funnel's light palette. */
  --sk-total:#8f8878;--sk-draft:#b3ad9d;--sk-waiting:#3a6ea5;--sk-live:#a8761f;--sk-offer:#3f8f76;
  --sk-won:#007a5e;--sk-closed:#7a5cb8;--sk-lost:#a83519;--sk-lost-late:#7d2f3f;--sk-a0:.22;--sk-a1:.5;
  --sk-fg:#1b1a17;--sk-mute:#686459;--sk-chip:rgba(255,255,255,.94);--sk-chip-t:#4a463d;
  --sk-chip-line:rgba(27,26,23,.12);--sk-chip-shadow:rgba(27,26,23,.10);--sk-ring:#1b1a17;--sk-bg-t:#f5f2ea;
  color:var(--sk-fg);background:var(--field);border:1px solid var(--rule);
  box-shadow:0 14px 40px -28px rgba(27,26,23,.35)}
/* Dark: the stage it was designed as. Glow and moving light read best here. */
:root[data-theme=dark] .fn-stage{
  --sk-total:#8f8877;--sk-draft:#6e685a;--sk-waiting:#5b8fc9;--sk-live:#d19a45;--sk-offer:#4fae90;
  --sk-won:#2bb58c;--sk-closed:#9b7ad6;--sk-lost:#cf5a39;--sk-lost-late:#b0485f;--sk-a0:.30;--sk-a1:.62;
  --sk-fg:#f5f2ea;--sk-mute:#a5a091;--sk-chip:rgba(27,26,23,.88);--sk-chip-t:#cfcabd;
  --sk-chip-line:rgba(255,255,255,.08);--sk-chip-shadow:rgba(0,0,0,.35);--sk-ring:#f5f2ea;--sk-bg-t:#1b1a17;
  border-color:transparent;box-shadow:0 18px 50px -24px rgba(0,0,0,.55);
  background:radial-gradient(900px 420px at 30% 0%,#2a2722 0%,#1b1a17 55%,#161513 100%)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .fn-stage{
  --sk-total:#8f8877;--sk-draft:#6e685a;--sk-waiting:#5b8fc9;--sk-live:#d19a45;--sk-offer:#4fae90;
  --sk-won:#2bb58c;--sk-closed:#9b7ad6;--sk-lost:#cf5a39;--sk-lost-late:#b0485f;--sk-a0:.30;--sk-a1:.62;
  --sk-fg:#f5f2ea;--sk-mute:#a5a091;--sk-chip:rgba(27,26,23,.88);--sk-chip-t:#cfcabd;
  --sk-chip-line:rgba(255,255,255,.08);--sk-chip-shadow:rgba(0,0,0,.35);--sk-ring:#f5f2ea;--sk-bg-t:#1b1a17;
  border-color:transparent;box-shadow:0 18px 50px -24px rgba(0,0,0,.55);
  background:radial-gradient(900px 420px at 30% 0%,#2a2722 0%,#1b1a17 55%,#161513 100%)}}
.fn-shead{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.fn-shead h2{margin:0;font-size:15px;font-weight:600;color:var(--sk-fg)}
#fn-hint{font-size:12.5px;color:var(--sk-mute)}
#fn-hint b{color:var(--sk-fg);font-weight:600}
.fn-legend{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--sk-mute)}
.fn-legend i{position:relative;width:8px;height:8px;border-radius:50%;background:var(--sk-live)}
.fn-legend i::after{content:"";position:absolute;inset:0;border-radius:50%;
  border:1.5px solid var(--sk-live);animation:fx-pulse 1.8s ease-out infinite}
#chart{position:relative;flex:1;min-height:0;margin-top:22px}
#chart svg{display:block;overflow:visible}
#chart .empty h3{color:var(--sk-fg)}#chart .empty p{color:var(--sk-mute)}
.sk-link{transition:opacity .25s,filter .2s}
.sk-link.sk-dim{opacity:.07}
.sk-hit{cursor:pointer;outline:none}
.sk-hit:focus-visible .sk-node{stroke:var(--sk-ring);stroke-width:2}
.sk-node{transition:opacity .25s}
.sk-hit.sk-dim .sk-node{opacity:.35}
.sk-node.sk-on{stroke:var(--sk-ring);stroke-width:2;paint-order:stroke}
/* Pulses outward by the same few pixels on every side, whatever the stage's
   height; a scaled ring turned a tall stage into a ghost twice its size. */
.sk-ring{fill:none;animation:fx-ring 1.8s ease-out infinite}
@keyframes fx-ring{0%{stroke-width:1.5;stroke-opacity:.9}100%{stroke-width:9;stroke-opacity:0}}
.sk-chip{position:absolute;display:flex;align-items:center;gap:6px;height:24px;
  padding:0 9px 0 8px;border-radius:12px;background:var(--sk-chip);
  box-shadow:0 0 0 1px var(--sk-chip-line),0 4px 14px var(--sk-chip-shadow);
  font-size:12px;color:var(--sk-chip-t);white-space:nowrap;pointer-events:none;transition:opacity .25s}
.sk-chip i{width:7px;height:7px;border-radius:2px}
.sk-chip b{color:var(--sk-fg);font-weight:600;font-variant-numeric:tabular-nums}
.sk-chip span{color:var(--sk-mute);font-size:11.5px}
.sk-chip em{font-style:normal;font-size:10.5px;font-weight:600;color:#1b1a17;
  background:#e8bc7c;border-radius:8px;padding:1px 6px;animation:fx-breathe 2.2s ease-in-out infinite}
.sk-chip.won{box-shadow:0 0 0 1px var(--sk-won),0 0 22px color-mix(in srgb,var(--sk-won) 40%,transparent)}
.sk-chip.won b{color:var(--sk-won)}
.sk-chip.sk-dim{opacity:.4}
.sk-tip{position:absolute;z-index:3;display:flex;flex-direction:column;gap:3px;min-width:210px;
  max-width:300px;padding:11px 13px;border-radius:10px;background:var(--sk-fg);color:var(--sk-bg-t);
  font-size:12.5px;box-shadow:0 12px 30px rgba(0,0,0,.3);pointer-events:none}
/* The opposite of the stage it sits on, so it stands off it either way. */
.sk-tip b{font-size:13.5px}.sk-tip span{opacity:.75}.sk-tip .who{opacity:.9}
.sk-tip .go{color:var(--sk-live);font-weight:500;margin-top:2px;opacity:1}

/* Momentum. */
.fn-weeks{display:flex;align-items:flex-end;gap:5px;height:150px;margin-top:12px;
  padding-bottom:6px;border-bottom:1px solid var(--bd-inner)}
.fn-wk{flex:1;min-width:0;height:100%;display:flex;flex-direction:column;
  justify-content:flex-end;align-items:center;gap:4px}
.fn-wk .bar{width:100%;max-width:12px;border-radius:3px 3px 1px 1px;background:var(--bd-field);
  transform-origin:bottom}
.fn-wk .bar.now{background:var(--acc)}
.fn-wk .iv{display:flex;flex-direction:column;gap:3px}
.fn-wk .iv i{width:6px;height:6px;border-radius:50%;background:var(--fn-offer)}
.fn-wax{display:flex;justify-content:space-between;margin-top:6px;font-size:11.5px;color:var(--t500)}
.fn-mhead{display:flex;align-items:baseline;gap:18px;margin-top:4px}
.fn-mhead .sub{font-size:12.5px;color:var(--t500);line-height:1.45}
.fn-mhead .sub b{color:var(--fn-offer);font-weight:500}

/* Still in play, or what is behind the stage you clicked. */
.fn-list{display:flex;flex-direction:column;padding:0;overflow:hidden}
.fn-list .fn-ch{padding:16px 18px 6px;margin:0;align-items:center}
.fn-list .fn-ch .grow{flex:1}
.fn-list .x{width:28px;height:28px;border-radius:7px;color:var(--t600);font-size:13px}
.fn-list .x:hover{background:var(--paper-hover);color:var(--t900)}
.fn-list .sw{width:10px;height:10px;border-radius:3px;flex:none;align-self:center}
.fn-jrow{display:flex;align-items:center;gap:12px;padding:10px 18px;border-top:1px solid
  var(--bd-inner);text-align:left;width:100%}
.fn-jrow:hover{background:var(--row-hover)}
.fj-who{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}
.fj-who .con{font-weight:500;font-size:13.5px}
.fj-role{font-size:12px;color:var(--t500);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fj-st{display:flex;flex-direction:column;align-items:flex-end;gap:2px;flex:none}
.fn-jrow .st{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--t700);white-space:nowrap}
.fj-st small{font-size:11.5px;color:var(--t500)}
.fn-jlist{max-height:420px;overflow-y:auto}
.fn-list .note{padding:12px 18px;margin:0}
.fn-jfoot{display:flex;justify-content:flex-end;padding:10px 18px 14px;border-top:1px solid var(--bd-inner)}

/* Sources. */
.fn-src{display:grid;grid-template-columns:24px minmax(0,150px) minmax(0,1fr) 92px;
  align-items:center;gap:10px;height:32px}
.fn-src .mk{width:24px;height:24px;border-radius:6px;display:grid;place-items:center;
  font-size:11px;font-weight:600;background:var(--bd-inner);color:var(--t700);overflow:hidden}
.fn-src .mk .board{width:24px;height:24px;border-radius:6px}
.fn-src .mk .board svg{width:14px;height:14px}
.fn-src .sn{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fn-src .track{height:8px;border-radius:4px;background:var(--bd-inner);overflow:hidden}
.fn-src .fill{display:block;height:100%;border-radius:4px;background:var(--bd-field);transform-origin:left}
.fn-src .fill.best{background:linear-gradient(90deg,var(--fn-offer),var(--fn-won))}
.fn-src .sv{font-size:12px;color:var(--t500);text-align:right;font-variant-numeric:tabular-nums}
.fn-src .sv b{color:var(--t900);font-weight:600;margin-right:4px}

/* Reply times. */
.fn-dots{position:relative;height:100px;margin:0 6px}
.fn-dots i{position:absolute;width:9px;height:9px;margin-left:-4.5px;border-radius:50%;
  background:var(--fn-wait)}
.fn-dots i.g{background:transparent;box-shadow:inset 0 0 0 1.5px var(--fn-lost);margin-left:0}
.fn-dots .med{position:absolute;top:-6px;bottom:0;border-left:1.5px dashed var(--acc)}
.fn-dax{display:flex;justify-content:space-between;margin-top:6px;padding-top:6px;
  border-top:1px solid var(--bd-inner);font-size:11.5px;color:var(--t500)}
.fn-reply p{margin:12px 0 0;font-size:12.5px;line-height:1.5;color:var(--t600)}

/* Readings. */
.fn-ins{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-top:1px solid var(--bd-inner)}
.fn-ins:first-of-type{border-top:0;padding-top:4px}
.fn-ins .ic{width:30px;height:30px;flex:none;border-radius:9px;display:grid;place-items:center}
.fn-ins .ic.up{background:color-mix(in srgb,var(--fn-won) 14%,transparent);color:var(--fn-won)}
.fn-ins .ic.leak{background:var(--acc-wash);color:var(--acc-text)}
.fn-ins .ic.lost{background:color-mix(in srgb,var(--fn-lost) 13%,transparent);color:var(--fn-lost)}
.fn-ins .ic.wait{background:color-mix(in srgb,var(--fn-wait) 14%,transparent);color:var(--fn-wait)}
.fn-ins p{margin:0;font-size:13.5px;line-height:1.5;color:var(--t700)}
.fn-ins p b{color:var(--t900)}
.fn-empty{font-size:13px;color:var(--t500);line-height:1.5;margin:4px 0 0}

/* Entrance. Only while .fx-in is on the page: the class comes off once it has
   played, so a resize or a click redraws without replaying it. */
@keyframes fx-rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes fx-fade{from{opacity:0}to{opacity:1}}
@keyframes fx-grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes fx-growy{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes fx-sweep{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}
@keyframes fx-pop{0%{opacity:0;transform:scale(.4)}70%{opacity:1;transform:scale(1.15)}100%{opacity:1;transform:scale(1)}}
@keyframes fx-node{from{opacity:0;transform:scaleY(.2)}to{opacity:1;transform:none}}
@keyframes fx-pulse{0%{opacity:.9;transform:scale(1)}100%{opacity:0;transform:scale(1.9)}}
@keyframes fx-breathe{0%,100%{opacity:.55}50%{opacity:1}}
.fx-in .fx-r{animation:fx-rise .7s cubic-bezier(.2,.8,.2,1) both}
.fx-in .fj-bar span,.fx-in .fn-src .fill{animation:fx-grow 1.1s cubic-bezier(.2,.8,.2,1) both}
.fx-in .fj-conv{animation:fx-pop .6s cubic-bezier(.2,.8,.2,1) both}
.fx-in .fn-wk .bar{animation:fx-growy .7s cubic-bezier(.2,.8,.2,1) both}
.fx-in .fn-wk .iv i,.fx-in .fn-dots i{animation:fx-pop .45s both}
.fx-in .sk-link{animation:fx-sweep 1.1s cubic-bezier(.3,.7,.2,1) both}
.fx-in .sk-hit{transform-box:fill-box;transform-origin:center;animation:fx-node .6s cubic-bezier(.2,.8,.2,1) both}
.fx-in .sk-chip{animation:fx-rise .6s cubic-bezier(.2,.8,.2,1) both}
.fx-in .sk-parts{animation:fx-fade 1s ease 2.2s both}
@media (prefers-reduced-motion:reduce){
  .fn-page *{animation:none!important;transition:none!important}
  .sk-parts{display:none}
}

/* Says how thin the number underneath a rate is, rather than printing a
   percentage off three applications at the same weight as one off fifty. */
.kv.thin{margin-top:-6px}
.kv.thin .v{font-size:10.5px;color:var(--t500)}
.t-total{fill:var(--fn-total)} .t-neutral{fill:var(--fn-neutral)}
.t-positive{fill:var(--fn-positive)}
.t-won{fill:var(--fn-won)} .t-lost{fill:var(--fn-lost)}
.t-live{fill:var(--fn-positive)} .t-draft{fill:var(--fn-neutral)}
.t-offer{fill:var(--fn-offer)} .b-offer{stroke:var(--fn-offer)}
.t-lost-late{fill:var(--fn-lost-late)} .b-lost-late{stroke:var(--fn-lost-late)}
.t-waiting{fill:var(--fn-wait)} .t-closed{fill:var(--fn-closed)}
.b-neutral{stroke:var(--fn-neutral)} .b-positive{stroke:var(--fn-positive)}
.b-won{stroke:var(--fn-won)} .b-lost{stroke:var(--fn-lost)}
.b-live{stroke:var(--fn-positive)} .b-draft{stroke:var(--fn-neutral)}
.b-waiting{stroke:var(--fn-wait)} .b-closed{stroke:var(--fn-closed)}

/* ---------- calendar ----------------------------------------------------- */
.cal-page{flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;background:var(--app);overflow-y:auto}
.cal-bar .obtn{height:34px;padding:0 13px;font-size:13px}
.cal-nav{display:flex;align-items:center;gap:6px}
.cal-nav b{min-width:170px;text-align:center;font-size:15px;font-weight:600;color:var(--t900)}
.cal-nav .cal-arrow{width:34px;padding:0;font-size:17px}
#cal-body{display:flex;flex-direction:column;gap:20px;padding:0 24px 28px;flex:1;min-height:0}
.cal-card{background:var(--field);border:1px solid var(--rule);border-radius:16px;min-width:0}
.cal-card h2{margin:0;font-size:14px;font-weight:600;color:var(--t900)}
.cal-ch{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.cal-ch span{font-size:12.5px;color:var(--t500)}
.cal-top{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:20px}
@media (max-width:1180px){.cal-top{grid-template-columns:minmax(0,1fr)}}

/* Next up: the next interview, counting down. */
.cal-hero{position:relative;overflow:hidden;border-radius:18px;padding:22px 26px;color:var(--t900);
  display:grid;grid-template-columns:minmax(0,1fr) auto;gap:24px;
  background:radial-gradient(520px 260px at 88% 10%,color-mix(in srgb,var(--acc) 38%,transparent),transparent 70%),
    linear-gradient(135deg,color-mix(in srgb,var(--acc) 5%,var(--field)) 0%,color-mix(in srgb,var(--acc) 18%,var(--field)) 100%);
  border:1px solid color-mix(in srgb,var(--acc) 30%,var(--rule));box-shadow:0 20px 50px -30px color-mix(in srgb,var(--acc) 70%,transparent)}
.cal-hero::after{content:"";position:absolute;right:-60px;bottom:-80px;width:260px;height:260px;border-radius:50%;
  border:1px dashed color-mix(in srgb,var(--acc) 40%,transparent);animation:cv-sweep 60s linear infinite;pointer-events:none}
.cal-hero>*{position:relative;z-index:1}
.cal-next{display:flex;align-items:center;gap:10px}
.cal-next .tag{height:24px;display:flex;align-items:center;gap:6px;padding:0 10px;border-radius:12px;
  background:var(--t900);color:var(--app);font-size:11.5px;font-weight:700;letter-spacing:.02em}
.cal-next .tag i{width:7px;height:7px;border-radius:50%;background:var(--acc);animation:cv-now 1.6s ease-in-out infinite}
.cal-next span{font-size:13px;color:var(--acc-text)}
.cal-who{display:flex;align-items:center;gap:14px;margin-top:14px}
.cal-who .colog{width:52px;height:52px;border-radius:12px;font-size:16px}
.cal-who b{display:block;font-size:26px;letter-spacing:-.02em}
.cal-who span{font-size:14px;color:var(--t600)}
.cal-cd{display:flex;align-items:flex-end;gap:18px;margin-top:16px;flex-wrap:wrap}
.cal-cd .n{font-size:44px;font-weight:700;letter-spacing:-.03em;font-variant-numeric:tabular-nums;line-height:1}
.cal-cd .u{font-size:11.5px;color:var(--acc-text);font-weight:600;margin-top:4px;text-transform:uppercase}
.cal-cd .s .n{color:var(--acc)}
.cal-cd .when{padding-bottom:6px;font-size:13.5px;color:var(--t800);line-height:1.45}
.cal-checks{display:flex;flex-direction:column;gap:8px;margin-top:16px}
.cal-check{display:flex;align-items:center;gap:9px;font-size:13px;color:var(--t800)}
.cal-check i{width:18px;height:18px;border-radius:50%;flex:none;display:grid;place-items:center;font-style:normal;
  font-size:11px;font-weight:700}
.cal-check i.ok{background:var(--fn-won);color:#fff}
.cal-check i.no{box-shadow:inset 0 0 0 1.5px var(--bd-field)}
.cal-acts{display:flex;gap:8px;margin-top:16px}
.cal-acts .dark{background:var(--t900);color:var(--app);border-color:var(--t900);font-weight:600}
.cal-clocks{display:flex;flex-direction:column;justify-content:center;align-items:center;gap:12px}
.cal-clocks .row{display:flex;gap:22px}
.cal-clock{display:flex;flex-direction:column;align-items:center;gap:6px}
.cal-clock b{font-size:15px}.cal-clock span{font-size:11.5px;color:var(--t500);margin-top:-4px}
.cal-clock svg .face{fill:var(--field);stroke:color-mix(in srgb,var(--acc) 30%,var(--rule))}
.cal-clock svg .tk{stroke:var(--t400)}.cal-clock svg .tk.big{stroke:var(--t900)}
.cal-clock svg .hh{stroke:var(--t900)}.cal-clock svg .mh{stroke:var(--acc)}.cal-clock svg .pin{fill:var(--t900)}
.cal-hand{transform-origin:50px 50px;transform-box:view-box;transform:rotate(var(--a))}
.cal-in .cal-hand{animation:cv-hand 1.4s cubic-bezier(.2,.8,.2,1) both}
.cal-clocks small{font-size:12px;color:var(--acc-text);text-align:center}
.cal-empty{display:flex;flex-direction:column;gap:6px;justify-content:center}
.cal-empty b{font-size:20px}.cal-empty p{margin:0;color:var(--t600);font-size:13.5px;max-width:520px;line-height:1.5}

/* The month, as how busy each day was. */
.cal-mini{padding:18px 20px;display:flex;flex-direction:column;gap:10px}
.cal-mgrid{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}
.cal-mgrid .dw{font-size:10.5px;font-weight:600;color:var(--t500);text-align:center}
.cal-md{position:relative;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;
  font-size:12px;font-weight:600;color:var(--t900);background:var(--bd-inner);cursor:pointer}
.cal-md.out{background:transparent;color:var(--t400);cursor:default}
.cal-md.l1{background:color-mix(in srgb,var(--acc) 22%,var(--field))}
.cal-md.l2{background:color-mix(in srgb,var(--acc) 45%,var(--field))}
.cal-md.l3{background:color-mix(in srgb,var(--acc) 75%,var(--field))}
.cal-md.today{box-shadow:0 0 0 2px var(--t900)}
.cal-md i{position:absolute;right:4px;top:4px;width:7px;height:7px;border-radius:2px;background:var(--t900);transform:rotate(45deg)}
.cal-legend{display:flex;align-items:center;gap:14px;font-size:11.5px;color:var(--t500)}
.cal-legend .ramp{display:flex;gap:2px}.cal-legend .ramp i{width:10px;height:10px;border-radius:3px}
.cal-legend .dia{width:7px;height:7px;border-radius:2px;background:var(--t900);transform:rotate(45deg)}

/* Journeys: each application from sent to now, and what is ahead. */
.cal-jr{padding:18px 24px 16px}
.jr-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--t600);margin-left:auto}
.jr-legend span{display:flex;align-items:center;gap:6px}
.jr-legend .bar{width:18px;height:8px;border-radius:4px}
.jr-axis{position:relative;height:24px;margin:16px 0 0 var(--jr-lx)}
.jr-tick{position:absolute;top:0;font-size:11px;color:var(--t500);white-space:nowrap}
.jr-body{position:relative}
.jr-bg{position:absolute;top:0;bottom:0;left:var(--jr-lx);right:0;pointer-events:none}
.jr-wkend{position:absolute;top:0;bottom:0;background:color-mix(in srgb,var(--t900) 3%,transparent)}
.jr-now{position:absolute;top:-30px;bottom:-4px;width:2px;background:var(--acc);animation:cv-now 2.2s ease-in-out infinite;z-index:2}
.jr-now::before{content:attr(data-label);position:absolute;top:-2px;left:50%;transform:translateX(-50%);height:20px;
  display:flex;align-items:center;padding:0 8px;border-radius:10px;background:var(--acc);color:#1b1a17;font-size:11px;font-weight:700}
.jr-now::after{content:"";position:absolute;inset:0 -8px;background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--acc) 16%,transparent),transparent)}
.jr-lane{position:relative;height:38px;border-top:1px solid var(--bd-inner);cursor:pointer}
.jr-lane:hover{background:var(--row-hover)}
.jr-lane.closed{opacity:.5}
.jr-who{position:absolute;left:0;top:5px;width:calc(var(--jr-lx) - 16px);display:flex;align-items:center;gap:10px;min-width:0}
.jr-who .colog{width:26px;height:26px;border-radius:7px;font-size:10px;flex:none}
.jr-who b{display:block;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.jr-who small{display:block;font-size:11px;color:var(--t500);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.jr-track{position:absolute;left:var(--jr-lx);right:0;top:0;bottom:0}
.jr-seg{position:absolute;top:13px;height:12px;border-radius:6px}
.jr-seg.wait{background:linear-gradient(90deg,color-mix(in srgb,var(--fn-wait) 18%,transparent),color-mix(in srgb,var(--fn-wait) 45%,transparent));border:1px solid color-mix(in srgb,var(--fn-wait) 35%,transparent)}
.jr-seg.live{background:linear-gradient(90deg,color-mix(in srgb,var(--fn-positive) 20%,transparent),color-mix(in srgb,var(--fn-positive) 60%,transparent));border:1px solid color-mix(in srgb,var(--fn-positive) 35%,transparent)}
.jr-seg.offer{background:linear-gradient(90deg,color-mix(in srgb,var(--fn-offer) 20%,transparent),color-mix(in srgb,var(--fn-offer) 60%,transparent));border:1px solid color-mix(in srgb,var(--fn-offer) 35%,transparent)}
.jr-dash{position:absolute;top:18px;height:2px;background:repeating-linear-gradient(90deg,color-mix(in srgb,var(--acc) 55%,transparent) 0 6px,transparent 6px 12px);animation:cv-dash 1.2s linear infinite}
.jr-iv{position:absolute;top:10px;width:18px;height:18px;margin-left:-9px;border-radius:4px;transform:rotate(45deg);
  background:linear-gradient(135deg,color-mix(in srgb,var(--acc) 80%,#fff),var(--fn-positive));animation:cv-glow 2.4s ease-in-out infinite}
.jr-ring{position:absolute;top:12px;width:14px;height:14px;margin-left:-7px;border-radius:50%;background:var(--field);box-shadow:inset 0 0 0 2.5px var(--fn-wait)}
.jr-ring.late{box-shadow:inset 0 0 0 2.5px var(--fn-lost);animation:cv-late 1.8s ease-out infinite}
.jr-dot{position:absolute;top:13px;width:12px;height:12px;margin-left:-6px;border-radius:50%}
.jr-dot.of{background:var(--fn-offer);box-shadow:0 0 0 3px color-mix(in srgb,var(--fn-offer) 22%,transparent)}
.jr-dot.rp{background:var(--fn-positive);box-shadow:0 0 0 3px color-mix(in srgb,var(--fn-positive) 22%,transparent)}
.jr-x{position:absolute;top:10px;margin-left:-5px;font-size:16px;line-height:16px;color:var(--fn-lost);font-weight:700}
.jr-lab{position:absolute;top:9px;height:20px;display:flex;align-items:center;padding:0 8px;border-radius:10px;font-size:11.5px;
  white-space:nowrap;background:color-mix(in srgb,var(--fn-wait) 12%,var(--field));color:var(--fn-wait);z-index:1}
.jr-lab.iv{background:var(--t900);color:var(--app);font-weight:600;box-shadow:0 6px 16px -6px rgba(0,0,0,.4)}
.jr-lab.late{background:color-mix(in srgb,var(--fn-lost) 12%,var(--field));color:var(--fn-lost);font-weight:600}
.jr-lab.x{background:transparent;color:var(--fn-lost)}
.jr-more{padding:10px 0 0 var(--jr-lx);font-size:12.5px;color:var(--t500)}
.cal-in .jr-seg{animation:cv-grow .8s cubic-bezier(.3,.7,.2,1) both}
.cal-in .jr-iv{animation:cv-pop .6s both,cv-glow 2.4s ease-in-out 2.4s infinite}
.cal-in .jr-ring,.cal-in .jr-dot{animation:cv-popr .5s both}
.cal-in .jr-ring.late{animation:cv-popr .5s both,cv-late 1.8s ease-out 2.2s infinite}
.cal-in .jr-lab,.cal-in .cal-r{animation:fx-rise .6s cubic-bezier(.2,.8,.2,1) both}
.cal-in .cal-md{animation:cv-popr .4s both}

/* Month */
.cal-month{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:20px;flex:1;min-height:560px}
/* Below this the month needs the whole width to be readable; what is coming
   up goes under it. */
@media (max-width:1240px){ .cal-month{grid-template-columns:minmax(0,1fr);flex:none} .cal-month .cal-grid{min-height:660px} .cal-side{max-height:none} }
.cal-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));overflow:hidden;padding:0}
.cal-grid .dw{padding:8px 10px;font-size:11.5px;font-weight:600;color:var(--t500);border-bottom:1px solid var(--rule)}
.cal-cell{display:flex;flex-direction:column;gap:4px;min-width:0;padding:7px 7px 6px;border-right:1px solid var(--bd-inner);
  border-bottom:1px solid var(--bd-inner);background:var(--field)}
.cal-cell.out,.cal-cell.wkend{background:color-mix(in srgb,var(--t900) 2%,var(--field))}
.cal-cell.today{background:color-mix(in srgb,var(--acc) 6%,var(--field));box-shadow:inset 0 0 0 1.5px var(--acc)}
.cal-cell.drop{box-shadow:inset 0 0 0 2px var(--fn-wait);background:color-mix(in srgb,var(--fn-wait) 8%,var(--field))}
.cal-cell .d{height:24px;display:flex;align-items:center;font-size:12.5px;font-weight:600;color:var(--t700)}
.cal-cell.out .d{color:var(--t400)}
.cal-cell.today .d span{width:24px;height:24px;border-radius:12px;background:var(--acc);color:#1b1a17;display:grid;place-items:center;font-weight:700}
.cal-cell .act{margin-top:auto;display:flex;flex-wrap:wrap;gap:2px 8px;font-size:11px;color:var(--t500)}
.cal-cell .act span{display:flex;align-items:center;gap:4px;white-space:nowrap}
.cal-cell .act i{width:6px;height:6px;border-radius:50%}
.cal-chip{display:flex;align-items:center;gap:5px;height:22px;padding:0 7px;border-radius:6px;font-size:11.5px;white-space:nowrap;
  overflow:hidden;cursor:pointer;min-width:0;text-align:left;width:100%}
.cal-chip span{overflow:hidden;text-overflow:ellipsis}
.cal-chip.iv{background:color-mix(in srgb,var(--fn-positive) 18%,var(--field));color:var(--acc-text);font-weight:600}
.cal-chip.iv .pt{width:6px;height:6px;flex:none;border-radius:50%;background:var(--fn-positive)}
.cal-chip.iv .gl{margin-left:auto;display:flex;color:var(--fn-positive)}
.cal-chip.fu{border:1px dashed color-mix(in srgb,var(--fn-wait) 45%,transparent);color:var(--fn-wait);background:var(--field)}
.cal-chip.fu.late{border:1px solid color-mix(in srgb,var(--fn-lost) 40%,transparent);color:var(--fn-lost);
  background:color-mix(in srgb,var(--fn-lost) 7%,var(--field))}
.cal-chip.of{background:color-mix(in srgb,var(--fn-offer) 18%,var(--field));color:var(--fn-offer);font-weight:600}
.cal-chip.rp{box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--fn-positive) 40%,transparent);color:var(--acc-text)}
.cal-chip[draggable=true]{cursor:grab}
.cal-side{display:flex;flex-direction:column;overflow:hidden}
.cal-side .cal-ch{padding:16px 18px 10px}
.cal-late{margin:0 12px 8px;padding:10px 12px;border-radius:10px;background:color-mix(in srgb,var(--fn-lost) 9%,var(--field));
  color:var(--fn-lost);font-size:12.5px;line-height:1.45}
.cal-item{display:flex;gap:12px;padding:11px 18px;border-top:1px solid var(--bd-inner);text-align:left;width:100%}
.cal-item:hover{background:var(--row-hover)}
.cal-item .bar{width:3px;flex:none;border-radius:2px}
.cal-item .tx{display:flex;flex-direction:column;gap:2px;min-width:0}
.cal-item .tx small{font-size:12px;color:var(--t500)}
.cal-item .tx b{font-size:13.5px;font-weight:600}
.cal-item .tx span{font-size:12px;color:var(--t700)}
.cal-item .tx em{font-style:normal;display:flex;align-items:center;gap:5px;margin-top:3px;font-size:11.5px;color:var(--fn-positive)}
.cal-none{padding:14px 18px;font-size:13px;color:var(--t500)}

/* Week */
.cal-week{position:relative;display:flex;flex-direction:column;overflow:hidden;flex:1;min-height:560px}
.cal-wh{display:grid;grid-template-columns:64px repeat(7,minmax(0,1fr));border-bottom:1px solid var(--rule)}
.cal-wh>div{padding:10px 10px 8px;border-left:1px solid var(--bd-inner)}
.cal-wh>div:first-child{border-left:0}
.cal-wh small{display:block;font-size:11.5px;font-weight:600;color:var(--t500)}
.cal-wh b{font-size:20px;font-weight:600;letter-spacing:-.02em}
.cal-wh .today b{color:var(--acc-text)}
.cal-wa{display:grid;grid-template-columns:64px repeat(7,minmax(0,1fr));border-bottom:1px solid var(--rule);
  background:color-mix(in srgb,var(--t900) 2%,var(--field))}
.cal-wa>div{display:flex;flex-direction:column;gap:4px;padding:6px;border-left:1px solid var(--bd-inner);min-height:30px;min-width:0}
.cal-wa>div:first-child{border-left:0;font-size:11px;color:var(--t500);text-align:right;padding:8px 8px 0 0}
.cal-wg{display:grid;grid-template-columns:64px minmax(0,1fr);overflow-y:auto}
.cal-hr{height:56px;border-top:1px solid var(--bd-inner);font-size:11px;color:var(--t500);padding:2px 8px 0 0;text-align:right;box-sizing:border-box}
.cal-wcols{position:relative}
.cal-wcols .ln{height:56px;border-top:1px solid var(--bd-inner);box-sizing:border-box}
.cal-wcols .col{position:absolute;top:0;bottom:0;border-left:1px solid var(--bd-inner)}
.cal-blk{position:absolute;border-radius:8px;padding:4px 8px;line-height:1.25;box-sizing:border-box;display:flex;flex-direction:column;gap:2px;overflow:hidden;
  background:color-mix(in srgb,var(--fn-positive) 18%,var(--field));border-left:3px solid var(--fn-positive);text-align:left;cursor:pointer}
.cal-blk b{font-size:12px;color:var(--acc-text)}.cal-blk span{font-size:11.5px;color:var(--acc-text)}
.cal-blk em{font-style:normal;display:flex;align-items:center;gap:4px;font-size:11px;color:var(--fn-positive)}
.cal-blk.on{box-shadow:0 0 0 2px var(--t900),0 10px 24px -8px rgba(0,0,0,.35)}
.cal-nowline{position:absolute;height:2px;background:var(--fn-lost);z-index:2}
.cal-nowline::before{content:"";position:absolute;left:-5px;top:-4px;width:10px;height:10px;border-radius:50%;background:var(--fn-lost)}
.cal-pop{position:absolute;width:320px;display:flex;flex-direction:column;gap:10px;padding:16px 18px;border-radius:14px;
  background:var(--field);box-shadow:0 0 0 1px var(--rule),0 24px 48px -16px rgba(0,0,0,.35);z-index:5}
.cal-pop .hd{display:flex;align-items:center;gap:10px}
.cal-pop .hd .colog{width:34px;height:34px;border-radius:8px}
.cal-pop .hd b{display:block;font-size:14px}.cal-pop .hd span{font-size:12px;color:var(--t500)}
.cal-pop .tz{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.cal-pop .tz div{padding:9px 10px;border-radius:9px;background:color-mix(in srgb,var(--acc) 7%,var(--field))}
.cal-pop .tz small{display:block;font-size:11px;color:var(--t500)}.cal-pop .tz b{font-size:15px}
.cal-pop p{margin:0;font-size:12.5px;line-height:1.5;color:var(--t800)}
.cal-pop .a{display:flex;gap:8px}.cal-pop .a .pbtn{flex:1}
.cal-pop .x{position:absolute;right:8px;top:8px;width:28px;height:28px;border-radius:7px;color:var(--t600)}

@keyframes cv-grow{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}
@keyframes cv-pop{0%{opacity:0;transform:scale(.3) rotate(45deg)}70%{opacity:1;transform:scale(1.25) rotate(45deg)}100%{opacity:1;transform:scale(1) rotate(45deg)}}
@keyframes cv-popr{0%{opacity:0;transform:scale(.3)}70%{opacity:1;transform:scale(1.2)}100%{opacity:1;transform:scale(1)}}
@keyframes cv-glow{0%,100%{box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 18%,transparent),0 0 14px color-mix(in srgb,var(--acc) 45%,transparent)}
  50%{box-shadow:0 0 0 6px color-mix(in srgb,var(--acc) 10%,transparent),0 0 24px color-mix(in srgb,var(--acc) 70%,transparent)}}
@keyframes cv-late{0%,100%{box-shadow:inset 0 0 0 2.5px var(--fn-lost),0 0 0 0 color-mix(in srgb,var(--fn-lost) 45%,transparent)}
  50%{box-shadow:inset 0 0 0 2.5px var(--fn-lost),0 0 0 6px transparent}}
@keyframes cv-now{0%,100%{opacity:.55}50%{opacity:1}}
@keyframes cv-dash{to{background-position:24px 0}}
@keyframes cv-hand{from{transform:rotate(0deg)}to{transform:rotate(var(--a))}}
@keyframes cv-sweep{from{transform:rotate(0)}to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.cal-page *{animation:none!important}}

/* ---------- sheets and overlays ------------------------------------------ */
/* Above the full-screen overlays too: a sheet opened from Design or Settings
   has to land on top of them. */
.scrim{position:fixed;inset:0;background:rgba(0,0,0,.34);z-index:46}
.sheet{position:fixed;left:50%;top:52px;transform:translateX(-50%);width:620px;
  max-width:calc(100% - 40px);max-height:calc(100% - 66px);overflow-y:auto;
  background:var(--app);border-radius:0 0 9px 9px;box-shadow:0 22px 48px rgba(0,0,0,.45);
  padding:22px 24px;display:flex;flex-direction:column;gap:16px;z-index:47}
.sheet h3{margin:0;font-size:15px;font-weight:600}
.sheet p{margin:4px 0 0;font-size:12.5px;color:var(--t600);line-height:1.45}
.sheet .foot{display:flex;justify-content:flex-end;gap:8px;padding-top:2px;
  align-items:center}
/* What a new version says about itself. Capped and scrolling: a release with
   twenty lines of notes should not push the install button off the screen. */
.relnotes{max-height:230px;overflow-y:auto;font-size:12.5px;line-height:1.55;
  color:var(--t700);border-top:1px solid var(--rule);padding-top:11px}
.relnotes p{margin:0 0 8px}
.relnotes ul{margin:0 0 8px;padding-left:18px}
.relnotes li{margin:0 0 5px}
.relnotes b{color:var(--t900);font-weight:600}
.relnotes code{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Consolas,monospace;
  font-size:11.5px;background:var(--field);
  border:1px solid var(--bd-field);border-radius:3px;padding:1px 4px}
/* Left of the buttons rather than crowded against them. */
#u-say{margin-right:auto;font-size:11px;color:var(--t500)}
.sheet .foot .left{margin-right:auto}
.sheet .foot .sbtn{white-space:nowrap}
/* What an import read, before it is made into a document. */
.imp-found{margin:0;padding:0;list-style:none;border:1px solid var(--rule);border-radius:7px;
  background:var(--field)}
.imp-found li{display:flex;justify-content:space-between;gap:12px;padding:8px 12px;font-size:12.5px}
.imp-found li+li{border-top:1px solid var(--bd-inner)}
.imp-found b{font-weight:500;color:var(--t900)}
.imp-found span{color:var(--t600)}
.imp-notes{font-size:12.5px;line-height:1.5;color:var(--t800);background:var(--acc-wash);
  border:1px solid var(--acc-line);border-radius:7px;padding:9px 12px}
.imp-notes b{font-weight:600}
.imp-notes ul{margin:4px 0 0;padding-left:18px}
.sbtn{font-size:13px;height:34px;padding:0 16px;border:1px solid var(--bd-field);border-radius:8px;
  background:var(--field);color:var(--t900)}
.sbtn:hover:not(:disabled){background:var(--paper-hover)}
.sbtn.primary{background:var(--acc);color:var(--c800);font-weight:500;border-color:var(--acc);
  padding:0 18px}
.sbtn.primary:hover:not(:disabled){background:var(--acc-hover);border-color:var(--acc-hover)}
.sbtn.danger{border-color:transparent;color:var(--bad);background:none}
.sbtn.danger:hover{background:var(--bad-bg)}

/* ---------- setup ---------------------------------------------------------
   The whole window, not a sheet over it: a first launch has nothing behind it
   worth seeing yet. The welcome is dark and moves; the steps after it keep a
   dark rail with where you are, and the paper beside it with your own page. */
.onb{position:fixed;inset:0;z-index:70;display:flex;background:var(--app);color:var(--t900);
  font-size:14px;overflow:hidden}
.onb[hidden]{display:none}
.onb-bar{position:absolute;left:0;right:0;top:0;height:30px;z-index:3;display:flex;
  align-items:center;padding:0 14px;-webkit-app-region:drag}
.onb-bar .lights{-webkit-app-region:no-drag}
.onb button,.onb input,.onb label{-webkit-app-region:no-drag}
.onb .linkish{border:0;background:none;padding:0;color:inherit;cursor:pointer;
  text-decoration:underline;text-underline-offset:2px}

/* The welcome. */
.onb-hero{position:relative;flex:1;background:var(--c900);color:var(--cw);overflow:auto;
  display:flex;flex-direction:column;align-items:center;padding:clamp(40px,11vh,118px) 24px 64px}
.onb-orbs{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.onb-orbs span{position:absolute;left:50%;top:0;border-radius:50%;filter:blur(90px)}
.onb-orbs span:nth-child(1){width:360px;height:360px;margin-left:-250px;top:90px;background:#3b82c4;
  opacity:.22;animation:onb-drift-a 14s ease-in-out infinite alternate}
.onb-orbs span:nth-child(2){width:320px;height:320px;margin-left:-20px;top:150px;background:#8b5cf6;
  opacity:.22;animation:onb-drift-b 16s ease-in-out infinite alternate}
.onb-orbs span:nth-child(3){width:300px;height:300px;margin-left:-160px;top:260px;background:#f97316;
  opacity:.16;filter:blur(100px);animation:onb-drift-b 18s ease-in-out infinite alternate}
.onb-orbs span:nth-child(4){width:260px;height:260px;margin-left:-200px;top:40px;background:#22c55e;
  opacity:.12;animation:onb-drift-a 20s ease-in-out infinite alternate}
.onb-hero>*{position:relative}
.onb-mark{width:148px;height:148px;flex:none;display:grid;place-items:center}
.onb-mark::before{content:"";position:absolute;inset:-34px;border-radius:50%;
  background:radial-gradient(circle,rgba(192,138,62,.35),rgba(139,92,246,.18) 45%,transparent 70%);
  animation:onb-fade .9s ease-out .2s both,onb-glow 5s ease-in-out 1.2s infinite}
.onb-mark img{position:relative;width:132px;height:132px;display:block;
  animation:onb-bloom 1.1s cubic-bezier(.2,.8,.2,1) both}
.onb-word{margin:34px 0 0;display:flex;font-size:64px;font-weight:700;letter-spacing:-.03em;
  line-height:1;color:var(--cw)}
.onb-word span{display:inline-block;white-space:pre;animation:onb-rise .7s cubic-bezier(.2,.8,.2,1) both}
.onb-rule{margin-top:18px;width:64px;height:3px;border-radius:2px;background:var(--acc);
  animation:onb-line .6s ease-out 1.45s both}
.onb-lede{margin:20px 0 0;max-width:560px;text-align:center;font-size:19px;line-height:1.5;
  color:var(--c100);animation:onb-rise .7s ease-out 1.6s both}
.onb-facts{margin-top:48px;display:grid;grid-template-columns:repeat(3,minmax(0,280px));gap:16px;
  max-width:100%}
.onb-fact{display:flex;flex-direction:column;gap:8px;padding:18px 20px;border-radius:14px;
  background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.09);
  animation:onb-rise .7s ease-out both}
.onb-fact .ic{width:32px;height:32px;border-radius:9px;background:rgba(192,138,62,.16);
  color:var(--acc-text-dark);display:grid;place-items:center}
.onb-fact b{font-size:15px;font-weight:600;color:var(--cw)}
.onb-fact span:last-child{font-size:13.5px;line-height:1.5;color:var(--c200)}
.onb-fact .mono{color:var(--c100);word-break:break-all}
.onb-fact .linkish{color:var(--acc-text-dark);font-size:13.5px}
.onb-go{margin-top:44px;display:flex;flex-direction:column;align-items:center;gap:14px;
  animation:onb-rise .7s ease-out 2.4s both}
.onb-cta{display:flex;align-items:center;gap:10px;height:50px;padding:0 28px;border:0;
  border-radius:12px;background:var(--acc);color:var(--c800);font-size:16px;font-weight:600;
  box-shadow:0 8px 24px rgba(192,138,62,.28)}
.onb-cta:hover{background:var(--acc-hover)}
.onb-skip{border:0;background:none;font-size:13.5px;color:var(--c200)}
.onb-skip:hover{color:var(--cw)}
.onb-or{display:flex;align-items:center;gap:12px;width:340px;max-width:80vw;margin-top:6px;
  color:var(--c300);font-size:11.5px;letter-spacing:.14em;text-transform:uppercase}
.onb-or::before,.onb-or::after{content:"";flex:1;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.16),transparent)}
.onb-demo{position:relative;display:flex;align-items:center;gap:14px;max-width:min(460px,90vw);
  padding:12px 16px 12px 12px;border-radius:14px;text-align:left;color:var(--cw);
  background:linear-gradient(rgba(28,26,24,.92),rgba(28,26,24,.92)) padding-box,
    linear-gradient(120deg,rgba(192,138,62,.75),rgba(255,255,255,.12) 40%,rgba(120,160,220,.55) 75%,
    rgba(192,138,62,.75)) border-box;
  background-size:100% 100%,240% 100%;border:1px solid transparent;
  box-shadow:0 10px 30px rgba(0,0,0,.28);animation:onb-sheen 6s linear infinite;
  transition:transform .18s ease,box-shadow .18s ease}
.onb-demo:hover{transform:translateY(-2px);box-shadow:0 14px 36px rgba(192,138,62,.22)}
.onb-demo:disabled{opacity:.75;cursor:progress;transform:none}
.onb-demo .dm-ic{flex:none;width:40px;height:40px;border-radius:11px;display:grid;place-items:center;
  color:var(--acc-text-dark);background:radial-gradient(circle at 30% 25%,rgba(192,138,62,.38),
  rgba(192,138,62,.1))}
.onb-demo .dm-tx{display:flex;flex-direction:column;gap:3px;min-width:0}
.onb-demo b{font-size:14.5px;font-weight:600}
.onb-demo small{font-size:12.5px;line-height:1.4;color:var(--c200)}
.onb-demo .dm-go{flex:none;margin-left:4px;color:var(--c200);transition:transform .18s ease,color .18s}
.onb-demo:hover .dm-go{transform:translateX(3px);color:var(--acc-text-dark)}
.onb-demo.busy .dm-ic svg{animation:onb-spin 1s linear infinite}
@keyframes onb-sheen{from{background-position:0 0,0 0}to{background-position:0 0,240% 0}}
@keyframes onb-spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.onb-demo{animation:none}}
/* With the sample-data card under it, the welcome needs a tighter rhythm to
   fit a laptop screen without scrolling. */
@media (max-height:960px){
  .onb-hero{padding-top:clamp(28px,6vh,64px)}
  .onb-mark{width:116px;height:116px}.onb-mark img{width:104px;height:104px}
  .onb-word{margin-top:20px;font-size:56px}
  .onb-lede{margin-top:14px;font-size:18px}
  .onb-facts{margin-top:30px}.onb-fact{padding:15px 18px}
  .onb-go{margin-top:28px;gap:10px}
}
.onb-pips{position:absolute;left:0;right:0;bottom:22px;display:flex;justify-content:center;gap:8px}
.onb-pips i{width:8px;height:4px;border-radius:2px;background:var(--c400)}
.onb-pips i.on{width:22px;background:var(--acc)}
@keyframes onb-bloom{0%{opacity:0;transform:scale(.55) rotate(-8deg);filter:blur(10px)}
  60%{opacity:1;filter:blur(0)}100%{opacity:1;transform:none}}
@keyframes onb-glow{0%,100%{opacity:.55;transform:scale(1)}50%{opacity:.85;transform:scale(1.08)}}
@keyframes onb-rise{from{opacity:0;transform:translateY(22px)}to{opacity:1;transform:none}}
@keyframes onb-fade{from{opacity:0}to{opacity:1}}
@keyframes onb-drift-a{from{transform:translate(-40px,-20px)}to{transform:translate(60px,40px)}}
@keyframes onb-drift-b{from{transform:translate(50px,30px)}to{transform:translate(-50px,-40px)}}
@keyframes onb-line{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes onb-in{from{opacity:0;transform:translateX(18px)}to{opacity:1;transform:none}}

/* The steps. */
.onb-rail{width:300px;flex:none;display:flex;flex-direction:column;padding:40px 28px 26px;
  background:var(--c900);color:var(--c100);overflow-y:auto}
.onb-brand{display:flex;align-items:center;gap:12px;font-size:19px;font-weight:700;
  letter-spacing:-.02em;color:var(--cw)}
.onb-brand img{width:36px;height:36px;display:block}
.onb-rail>p{margin:14px 0 0;font-size:13.5px;line-height:1.5;color:var(--c200)}
.onb-steps{list-style:none;margin:40px 0 0;padding:0}
.onb-steps li{display:flex;gap:14px}
.onb-steps .num{display:flex;flex-direction:column;align-items:center;width:28px;flex:none}
.onb-steps .num b{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;
  font-size:12.5px;font-weight:600;border:1.5px solid var(--c400);color:var(--c300)}
.onb-steps .num i{width:2px;flex-grow:1;min-height:26px;background:var(--c600)}
.onb-steps li:last-child .num i{background:transparent}
.onb-steps .txt{display:flex;flex-direction:column;gap:2px;padding:3px 0 22px}
.onb-steps .txt b{font-size:14.5px;font-weight:500;color:var(--c300)}
.onb-steps .txt span{font-size:12.5px;color:#8b877c}
.onb-steps li.done .num b{border:0;background:var(--acc);color:var(--c900)}
.onb-steps li.done .num i{background:var(--acc)}
.onb-steps li.done .txt b{color:var(--cw)}
.onb-steps li[aria-current] .num b{border:2px solid var(--acc);color:var(--cw)}
.onb-steps li[aria-current] .txt b{color:var(--cw);font-weight:600}
.onb-later{margin-top:auto;padding-top:24px;display:flex;flex-direction:column;gap:6px}
.onb-later button{align-self:flex-start;border:0;background:none;padding:0;font-size:13px;
  color:var(--c200)}
.onb-later button:hover{color:var(--cw)}
.onb-later span{font-size:12px;color:#8b877c}
.onb-work{flex:1;min-width:0;display:flex;flex-direction:column}
.onb-step{flex:1;min-height:0;display:flex;gap:48px;padding:56px 56px 0;overflow-y:auto;
  animation:onb-in .45s ease-out both}
.onb-main{width:520px;max-width:100%;flex:none;display:flex;flex-direction:column}
.onb-kicker{font-size:13px;font-weight:600;color:var(--acc-text)}
.onb-main h1{margin:8px 0 0;font-size:32px;font-weight:700;letter-spacing:-.02em;line-height:1.15}
.onb-main>p{margin:12px 0 0;font-size:15.5px;line-height:1.55;color:var(--t700)}
.onb-side{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:12px;
  padding-bottom:24px}
.onb-foot{flex:none;display:flex;align-items:center;justify-content:flex-end;gap:10px;
  padding:18px 56px 26px}
.onb-foot .say{margin-right:auto;font-size:13px;color:var(--t500)}
.onb-btn{height:44px;padding:0 20px;border-radius:10px;border:1px solid var(--bd-field);
  background:var(--field);color:var(--t900);font-size:14.5px;font-weight:500}
.onb-btn:hover:not(:disabled){background:var(--paper-hover)}
.onb-btn.primary{border-color:var(--acc);background:var(--acc);color:var(--c800);
  font-weight:600;padding:0 24px}
.onb-btn.primary:hover:not(:disabled){background:var(--acc-hover);border-color:var(--acc-hover)}
.onb-btn:disabled{opacity:.6}

/* Start from. */
.onb-srcs{margin-top:28px;display:flex;flex-direction:column;gap:12px}
.onb-src{display:flex;flex-direction:column;gap:10px;padding:16px 18px;border-radius:12px;
  border:1px solid var(--rule);background:var(--row-alt);text-align:left;color:var(--t900)}
.onb-src:hover{border-color:var(--rule-strong)}
.onb-src[aria-checked=true]{border:2px solid var(--acc);padding:15px 17px;background:var(--field)}
.onb-src .row{display:flex;align-items:center;gap:14px}
.onb-src .tile{width:40px;height:40px;flex:none;border-radius:10px;display:grid;place-items:center;
  font-size:13px;font-weight:700;background:var(--bar);color:var(--t700)}
.onb-src[data-src=pdf] .tile{background:#f3e1e1;color:#a83519}
.onb-src[data-src=linkedin] .tile{background:#0a66c2;color:#fff}
.onb-src .tile svg{width:20px;height:20px}
.onb-src .what{flex:1;display:flex;flex-direction:column;gap:2px}
.onb-src .what b{font-size:15px;font-weight:600}
.onb-src .what span{font-size:13px;line-height:1.45;color:var(--t600)}
.onb-src .radio{width:18px;height:18px;flex:none;border-radius:50%;border:1.5px solid var(--bd-field)}
.onb-src[aria-checked=true] .radio{border:5px solid var(--acc)}
.onb-src .how{margin-left:54px;display:flex;flex-direction:column;gap:8px;font-size:13px;
  line-height:1.5;color:var(--t800)}
.onb-src .how b{font-weight:600}
.onb-src .how .quiet{color:var(--t500)}
.onb-drop{margin-top:36px;width:100%;max-width:480px;min-height:420px;flex:1;max-height:560px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:32px;
  border-radius:18px;border:2px dashed var(--bd-field);background:var(--row-alt);text-align:center;
  transition:border-color .15s,background .15s}
.onb-drop.over{border-color:var(--acc);background:var(--acc-wash)}
.onb-drop .ic{width:64px;height:64px;border-radius:16px;background:var(--bar);color:var(--acc-text);
  display:grid;place-items:center}
.onb-drop b{font-size:18px;font-weight:600}
.onb-drop>span{font-size:14px;line-height:1.5;color:var(--t600);max-width:320px}
.onb-drop small{font-size:12.5px;color:var(--t500)}
.onb-drop .err{font-size:13px;line-height:1.5;color:var(--bad);max-width:340px}
.onb-drop.busy .ic svg{animation:sp 1s linear infinite}
.onb-found{margin-top:26px;border:1px solid var(--rule);border-radius:12px;background:var(--field)}
.onb-found div{display:flex;align-items:center;gap:12px;padding:13px 16px}
.onb-found div+div{border-top:1px solid var(--bd-inner)}
.onb-found i{width:24px;height:24px;flex:none;border-radius:50%;background:rgba(0,122,94,.12);
  color:var(--fn-won);display:grid;place-items:center}
.onb-found b{flex:1;font-size:14.5px;font-weight:500}
.onb-found span{font-size:13.5px;color:var(--t600)}
.onb-note{margin-top:12px;display:flex;gap:12px;padding:13px 16px;border-radius:12px;
  background:var(--acc-wash);border:1px solid var(--acc-line);font-size:13.5px;line-height:1.5;
  color:var(--t800)}
.onb-note>i{width:24px;height:24px;flex:none;border-radius:50%;background:var(--acc);
  color:var(--c800);display:grid;place-items:center;font-style:normal;font-weight:700;font-size:13px}
.onb-note ul{margin:4px 0 0;padding-left:18px}
.onb-small{margin:14px 0 0;font-size:13px;line-height:1.5;color:var(--t500)}
.onb-small .linkish{color:var(--acc-text)}

/* You. */
.onb-fields{margin-top:30px;display:flex;flex-direction:column;gap:16px}
.onb-fields label{display:flex;flex-direction:column;gap:7px;font-size:13px;font-weight:500;
  color:var(--t800)}
.onb-fields em{font-style:normal;font-weight:400;color:var(--t500)}
.onb-fields input{height:44px;padding:0 14px;border-radius:10px;border:1px solid var(--bd-field);
  background:var(--field);color:var(--t900);font-size:15px;font-family:inherit}
.onb-fields input:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-ring)}

/* The page, beside every step that changes it. */
.onb-cap{display:flex;align-items:center;gap:8px;min-height:30px;padding:0 12px;border-radius:15px;
  background:var(--field);border:1px solid var(--rule);font-size:12.5px;color:var(--t700)}
.onb-cap i{width:7px;height:7px;border-radius:50%;background:var(--fn-won)}
.onb-cap.busy i{background:var(--acc);animation:pulse 1s ease-in-out infinite}
.onb-shot{width:100%;max-width:480px;aspect-ratio:210/297;background:#fff;border-radius:2px;
  box-shadow:0 10px 40px rgba(27,26,23,.16),0 1px 3px rgba(27,26,23,.12);overflow:hidden;
  display:grid;place-items:center;transition:opacity .15s}
.onb-shot.busy{opacity:.6}
.onb-shot img{width:100%;height:100%;object-fit:cover;object-position:top center;display:block}
.onb-shot span{font-size:13px;color:#6b675d;padding:16px;text-align:center}

/* The page: themes and paper. */
.onb-lab{margin-top:22px;font-size:13px;font-weight:600;color:var(--t800)}
.onb-themes{margin-top:10px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.onb-themes button{display:flex;flex-direction:column;gap:6px;padding:5px 5px 7px;border-radius:9px;
  border:1px solid var(--bd-inner);background:transparent;text-align:left;color:var(--t900)}
.onb-themes button:hover{background:var(--paper-hover)}
.onb-themes button[aria-checked=true]{border:2px solid var(--acc);padding:4px 4px 6px;
  background:var(--field)}
.onb-themes .pic{aspect-ratio:360/223;border-radius:4px;background:#fff;overflow:hidden;
  box-shadow:0 0 0 1px var(--bd-inner);display:grid}
.onb-themes .pic img{width:100%;height:100%;object-fit:cover;object-position:top;display:block}
.onb-themes .pic .thumb{width:100%;height:100%}
.onb-themes .nm{padding:0 3px;font-size:12.5px;display:flex;justify-content:space-between;gap:6px;
  white-space:nowrap}
.onb-themes .nm>span{overflow:hidden;text-overflow:ellipsis}
.onb-themes .nm em{flex:none}
.onb-themes button[aria-checked=true] .nm{font-weight:600}
.onb-themes .nm em{font-style:normal;color:var(--t500);font-weight:400}
.onb-seg{margin-top:10px;display:flex;align-self:flex-start;padding:3px;border-radius:10px;
  background:var(--seg-track)}
.onb-seg button{height:34px;padding:0 18px;border:0;border-radius:8px;background:transparent;
  color:var(--t700);font-size:13.5px}
.onb-seg button[aria-checked=true]{background:var(--seg-on);color:var(--t900);font-weight:600;
  box-shadow:0 1px 2px rgba(27,26,23,.1)}

/* AI. */
.onb-ai{margin-top:28px;display:flex;flex-direction:column;gap:10px}
.onb-client{display:flex;align-items:center;gap:14px;padding:14px 16px;border-radius:12px;
  border:1px solid var(--rule);background:var(--field)}
.onb-client .badge{width:40px;height:40px;flex:none;border-radius:10px;display:grid;
  place-items:center;background:var(--bar);color:var(--t900)}
.onb-client .badge svg{width:22px;height:22px}
.onb-client[data-client=hermes] .badge svg{width:30px;height:30px}
.onb-client .nm{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.onb-client .nm b{font-size:14.5px;font-weight:600}
.onb-client .nm span{font-size:12.5px;line-height:1.4;color:var(--t600)}
.onb-client .on{display:flex;align-items:center;gap:6px;font-size:13px;font-weight:500;
  color:var(--fn-won)}
.onb-client .on i{width:8px;height:8px;border-radius:50%;background:currentColor}
.onb-can{margin-top:70px;width:100%;max-width:420px;display:flex;flex-direction:column;gap:14px;
  padding:26px 28px;border-radius:16px;background:var(--c900);color:var(--c100);
  border:1px solid rgba(255,255,255,.07)}
.onb-can>b{font-size:13px;font-weight:600;color:var(--acc-text-dark)}
.onb-can div{display:flex;gap:10px;font-size:14px;line-height:1.5;color:var(--c050)}
.onb-can div svg{flex:none;margin-top:3px}
.onb-can>span{font-size:12.5px;color:#8b877c}

@media (max-width:1080px){
  .onb-rail{width:230px;padding:40px 20px 20px}
  .onb-step{gap:28px;padding:44px 32px 0}
  .onb-foot{padding:16px 32px 22px}
  .onb-main{width:440px}
}
@media (max-width:860px){
  .onb-rail{display:none}
  .onb-step{flex-direction:column}
  .onb-main{width:auto}
  .onb-facts{grid-template-columns:minmax(0,1fr)}
  .onb-word{font-size:46px}
}
@media (prefers-reduced-motion:reduce){
  .onb *,.onb *::before{animation:none!important}
}

/* ---------- an application, as a page --------------------------------------- */
.peek-head{height:76px}
.peek-logo{flex:none;display:grid}
.peek-logo .colog{width:44px;height:44px;border-radius:11px;font-size:15px}
.peek-who b{font-size:19px}
.ap-pill{flex:none;display:inline-flex;align-items:center;gap:7px;height:28px;padding:0 12px;
  border-radius:14px;background:var(--bar);font-size:12.5px;font-weight:600;color:var(--t800)}
.ap-pill .dot{width:8px;height:8px}
.ap-facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:12px 14px;
  padding:14px 16px;border:1px solid var(--rule);border-radius:12px;background:var(--field)}
.ap-fact{display:flex;flex-direction:column;gap:5px;min-width:0}
.ap-fact>span{font-size:11.5px;font-weight:600;color:var(--t500)}
.ap-fact select,.ap-fact input{width:100%;min-width:0;height:32px;border-radius:8px;font-size:13px}
.ap-fact .statusctl{gap:6px}
.ap-fact.wide{grid-column:1/-1}
.ap-iv{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px;max-width:520px}
.ap-iv-say{font-size:12.5px;color:var(--t700)}
.ap-src{display:flex;align-items:center;gap:8px;width:100%;height:32px;padding:0 10px;
  border:1px solid var(--bd-field);border-radius:8px;background:var(--field);color:var(--t900);
  font-size:13px;text-align:left;min-width:0}
.ap-src:hover{background:var(--paper-hover)}
.ap-src .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ap-src .caret{color:var(--t500);font-size:10px}
.ap-src .board,.ap-menu .board{width:20px;height:20px;border-radius:5px}
.ap-src .board svg,.ap-menu .board svg{width:12px;height:12px}
.ap-src .board.lettered,.ap-menu .board.lettered{font-size:9px}
.ap-menu{position:fixed;z-index:48;width:250px;max-height:360px;overflow-y:auto;padding:5px;
  border:1px solid var(--rule);border-radius:11px;background:var(--field);
  box-shadow:0 16px 36px -10px rgba(27,26,23,.35);display:flex;flex-direction:column;gap:1px}
.ap-menu button{flex:none;display:flex;align-items:center;gap:10px;height:34px;padding:0 9px;border:0;
  border-radius:7px;background:none;color:var(--t900);font-size:13px;text-align:left}
.ap-menu button:hover{background:var(--paper-hover)}
.ap-menu button[aria-selected=true]{background:var(--acc-wash);font-weight:600}
.ap-menu hr{border:0;border-top:1px solid var(--bd-inner);margin:4px 2px}
.ap-menu .gl{width:20px;height:20px;border-radius:5px;display:grid;place-items:center;
  background:var(--bar);color:var(--t700);font-size:11px;flex:none}
.ap-docs{display:grid;grid-template-columns:repeat(2,minmax(0,210px));gap:18px}
.ap-doc{display:flex;flex-direction:column;gap:8px;min-width:0}
.ap-doc .pg{aspect-ratio:210/297;border:1px solid var(--rule);border-radius:5px;background:#fff;
  overflow:hidden;cursor:pointer;display:grid;place-items:center;
  box-shadow:0 6px 16px -10px rgba(27,26,23,.4)}
.ap-doc .pg img{width:100%;height:100%;object-fit:contain;object-position:top;display:block}
.ap-doc .pg>span{font-size:11px;color:#6b675d}
.ap-doc .pg.none{border:2px dashed var(--bd-field);background:var(--row-alt);cursor:default;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;
  padding:10px;text-align:center;box-shadow:none}
.ap-doc .pg.none span{font-size:12px;color:var(--t600);line-height:1.4}
.ap-doc .pg.post{background:var(--field);cursor:default;display:flex;flex-direction:column;
  align-items:stretch;justify-content:flex-start;gap:8px;padding:12px;box-shadow:none}
.ap-doc .pg.post .who{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;
  color:var(--t900)}
.ap-doc .pg.post .who .board{width:24px;height:24px;border-radius:6px}
.ap-doc .pg.post .who .board svg{width:14px;height:14px}
.ap-doc .pg.post .u{font-size:11px;line-height:1.45;color:var(--t600);overflow-wrap:anywhere}
.ap-doc .pg.post .n{margin-top:auto;font-size:11px;color:var(--t500)}
.ap-doc .t{display:flex;flex-direction:column;gap:1px;min-width:0}
.ap-doc .t b{font-size:12.5px;font-weight:600;color:var(--t900)}
.ap-doc .t span{font-size:11.5px;color:var(--t600);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.ap-doc .a{display:flex;gap:6px;flex-wrap:nowrap}
.ap-doc .a .obtn{height:28px;padding:0 10px;border-radius:7px;font-size:12px}
/* The posting, read as a posting: its headings and lists, with the facts a
   posting buries -- where, how much, travel, sponsorship -- pulled up top. */
.ap-post{font-size:13.5px;line-height:1.6;color:var(--t800);overflow-wrap:anywhere}
.ap-post h4{margin:16px 0 5px;font-size:14px;font-weight:600;color:var(--t900)}
.ap-post h4:first-child{margin-top:0}
.ap-post p{margin:0 0 8px}
.ap-post ul{margin:0 0 8px;padding-left:18px;display:flex;flex-direction:column;gap:2px}
.ap-post a{color:var(--acc-text)}
.ap-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.ap-chips span{height:24px;display:inline-flex;align-items:center;padding:0 10px;border-radius:12px;
  background:var(--bar);font-size:12px;color:var(--t800)}
.ap-pcard{height:100%;min-height:360px;display:flex;flex-direction:column;border:1px solid var(--rule);
  border-radius:14px;background:var(--field);overflow:hidden}
.ap-phead{flex:none;display:flex;align-items:center;gap:8px;padding:11px 14px;border-bottom:1px solid var(--bd-inner);
  background:var(--row-alt)}
.ap-phead .tx{display:flex;flex-direction:column;gap:1px;min-width:0}
.ap-phead .tx b{font-size:14px;font-weight:600;color:var(--t900)}
.ap-phead .tx span{font-size:12px;line-height:1.35;color:var(--t500);display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden}
.ap-phead .tx{flex:1}
.ap-phead .obtn{height:30px;display:inline-flex;align-items:center;gap:7px;padding:0 11px;border-radius:8px;
  font-size:12.5px;font-weight:500;color:var(--t900);text-decoration:none;white-space:nowrap;flex:none}
.ap-phead .obtn .board{width:16px;height:16px;border-radius:4px}
.ap-phead .obtn .board svg{width:10px;height:10px}
.ap-phead .obtn .ext{color:var(--t500)}
.ap-pbody{flex:1;min-height:0;overflow:auto;padding:16px 18px;display:flex;flex-direction:column;gap:10px}
.ap-pbody>.ap-post,.ap-pbody>.ap-chips{flex:none}
.ap-plabel{font-size:13px;font-weight:600;color:var(--t900)}
.ap-pfoot{display:flex;align-items:center;gap:10px;font-size:12.5px;color:var(--t600)}
.ap-pfoot code{font-size:12px;padding:2px 6px;border-radius:5px;background:var(--bar);color:var(--t800)}
.peek-grid textarea.posting-edit{flex:1;min-height:260px;font-size:12.5px;line-height:1.6;
  border:1.5px dashed var(--bd-field);border-radius:10px;background:var(--row-alt);padding:12px 14px;resize:vertical}
@media(max-width:1180px){ .ap-docs{grid-template-columns:repeat(2,minmax(0,200px))} }

/* ---------- cover letters --------------------------------------------------
   The page is the editor: a sheet at the letter's real size, set in the CV's
   font at the CV's margins, with the body editable in place. The letterhead
   comes from the CV and is not edited here. */
#v-letter{flex:1;min-height:0;display:flex;flex-direction:column}
.lt-body{flex:1;min-height:0;display:flex}
.lt-stage{flex:1;min-width:0;overflow:auto;background:var(--canvas);display:flex;
  flex-direction:column;align-items:center;gap:18px;padding:34px 20px 60px}
.lt-paper{position:relative;flex:none;background:#fff;color:#1b1a17;
  box-shadow:0 10px 40px rgba(27,26,23,.16),0 1px 3px rgba(27,26,23,.12)}
.lt-paper .lh{position:relative;cursor:pointer}
.lt-paper .lh .tag{position:absolute;right:0;top:-34px;display:none;align-items:center;gap:6px;
  height:24px;padding:0 10px;border-radius:12px;background:#1b1a17;color:#f5f2ea;
  font:500 11.5px 'IBM Plex Sans',system-ui,sans-serif;white-space:nowrap}
.lt-paper .lh:hover .tag,.lt-paper .lh:focus-visible .tag{display:inline-flex}
.lt-paper .lh:hover{outline:1px dashed #c9c2b3;outline-offset:6px}
.lt-paper [contenteditable]{outline:none;border-radius:2px}
.lt-paper [contenteditable]:hover{box-shadow:0 0 0 1px #e2dccf}
.lt-paper [contenteditable]:focus{box-shadow:0 0 0 1px #d8c29e}
.lt-paper .subj:empty::before{content:attr(data-ph);color:#b3ad9d;font-weight:400}
.lt-paper .ltbody p{margin:0}
.lt-paper .ltbody ul{margin:0;padding-left:1.4em}
.lt-paper .ltbody a{color:inherit;text-decoration:underline;text-decoration-color:#b3ad9d;
  text-underline-offset:3px}
.lt-paper .pgend{position:absolute;left:0;right:0;border-top:1px dashed #d8a86a;
  pointer-events:none}
.lt-paper .pgend span{position:absolute;right:8px;top:-10px;padding:0 6px;background:#fff;
  font:500 10.5px 'IBM Plex Sans',system-ui,sans-serif;color:#a8761f}
.lt-pdf{display:flex;flex-direction:column;gap:18px;align-items:center}
.lt-pdf img{display:block;background:#fff;box-shadow:0 10px 40px rgba(27,26,23,.16)}
.lt-md{width:min(820px,100%);flex:1;min-height:520px;resize:none;padding:18px 20px;
  border:1px solid var(--rule);border-radius:10px;background:var(--field);color:var(--t900);
  font:13px/1.7 'IBM Plex Mono',ui-monospace,monospace}
.lt-bar{position:fixed;z-index:48;display:flex;gap:2px;padding:3px;border-radius:8px;
  background:#1b1a17;box-shadow:0 6px 16px rgba(0,0,0,.25)}
.lt-bar button{min-width:30px;height:28px;padding:0 8px;border:0;border-radius:6px;
  background:transparent;color:#cfcabd;font:500 12.5px 'IBM Plex Sans',system-ui,sans-serif}
.lt-bar button:hover,.lt-bar button[aria-pressed=true]{background:#3a3833;color:#f5f2ea}
.lt-panel{width:300px;flex:none;overflow-y:auto;padding:20px;border-left:1px solid var(--rule);
  background:var(--app);display:flex;flex-direction:column;gap:16px;font-size:13px}
.lt-panel h4{margin:0;font-size:12px;font-weight:600;color:var(--t500)}
.lt-panel .big{font-size:20px;font-weight:600;color:var(--t900)}
.lt-meter{height:6px;border-radius:3px;background:var(--bd-inner);overflow:hidden}
.lt-meter i{display:block;height:100%;background:var(--acc)}
.lt-meter.over i{background:var(--bad)}
.lt-panel .muted2{color:var(--t600);line-height:1.45}
.lt-panel hr{border:0;border-top:1px solid var(--rule);margin:0}
.lt-panel .fg{display:grid;grid-template-columns:96px minmax(0,1fr);gap:8px 10px;align-items:center}
.lt-panel .fg label{font-size:12.5px;color:var(--t600)}
.lt-panel .fg input,.lt-panel .fg select,.lt-panel .fg textarea{width:100%;min-width:0;
  font-size:12.5px}
.lt-panel .fg textarea{min-height:64px;resize:vertical}
.lt-panel .today{display:flex;align-items:center;gap:8px}
.lt-panel .today input[type=date]{flex:1}
.lt-export{position:relative}
.lt-menu{position:absolute;right:0;top:40px;z-index:30;min-width:200px;padding:5px;
  border:1px solid var(--rule);border-radius:10px;background:var(--field);
  box-shadow:0 16px 36px -10px rgba(27,26,23,.35);display:flex;flex-direction:column}
.lt-menu button{display:flex;flex-direction:column;align-items:flex-start;gap:1px;padding:8px 10px;
  border:0;border-radius:7px;background:none;text-align:left;color:var(--t900);font-size:13px}
.lt-menu button:hover{background:var(--paper-hover)}
.lt-menu small{font-size:11.5px;color:var(--t500)}

/* ---------- ATS check ------------------------------------------------------ */
.sheet.ats{width:980px}
.ats-top{display:flex;gap:18px;align-items:flex-start;justify-content:space-between}
.ats-vs{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--t600);
  flex:none;margin-top:2px}
.ats-vs select{max-width:280px;font-size:12.5px;padding:5px 8px;border-radius:5px;
  border:1px solid var(--bd-field);background:var(--field);color:var(--t900)}
.ats-paste{width:100%;min-height:110px;resize:vertical;font:12.5px/1.5 inherit;padding:9px 10px;
  border:1px solid var(--bd-field);border-radius:6px;background:var(--field);color:var(--t900)}
.ats-body{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,.9fr);gap:22px;
  min-height:0;transition:opacity .15s}
.ats-body.busy{opacity:.55}
.ats-body>.sp-note,.ats-body>.note{grid-column:1/-1}
.ats-main{display:flex;flex-direction:column;gap:20px;min-width:0}
.ats-sec{display:flex;flex-direction:column;gap:8px}
.ats-sec .blabel em,.ats-read .blabel em{font-style:normal;font-weight:400;color:var(--t500);
  font-size:12px;margin-left:8px}
.ats-sec .sp-note{margin:4px 0 0}
.ats-score{display:flex;align-items:baseline;gap:8px}
.ats-score b{font-size:26px;font-weight:500;color:var(--t900);line-height:1}
.ats-score span{font-size:12.5px;color:var(--t600)}
/* The live metric, so the one place in the sheet the accent goes. */
.meter{height:5px;border-radius:3px;background:var(--rule);overflow:hidden}
.meter i{display:block;height:100%;background:var(--acc);border-radius:3px}
.kwlabel{font-size:11.5px;color:var(--t500);margin-top:4px}
.kws{display:flex;flex-wrap:wrap;gap:5px}
.kw{font-size:12px;padding:2px 8px;border-radius:10px;border:1px dashed var(--t400);
  color:var(--t800)}
.kw.on{border:1px solid transparent;background:var(--bar);color:var(--t700)}
.kw i{font-style:normal;color:var(--fn-won);margin-right:4px;font-size:11px}
.checks{list-style:none;margin:0;padding:0;border:1px solid var(--bd-field);border-radius:9px;
  background:var(--field);overflow:hidden}
.checks li{display:flex;gap:10px;align-items:flex-start;padding:9px 11px}
.checks li+li{border-top:1px solid var(--bd-inner)}
.checks li>i{flex:none;width:17px;height:17px;border-radius:50%;display:grid;place-items:center;
  font-style:normal;font-size:10.5px;font-weight:700;margin-top:1px}
.checks li.ok>i{background:color-mix(in srgb,var(--fn-won) 18%,transparent);color:var(--fn-won)}
.checks li.warn>i{background:var(--bar);color:var(--t800);box-shadow:inset 0 0 0 1px var(--t400)}
.checks li.bad>i{background:var(--bad-bg);color:var(--bad)}
.checks li>div{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.checks b{font-size:12.5px;font-weight:600;color:var(--t900)}
.checks span{font-size:12px;color:var(--t600);line-height:1.45}
.checks li.ok span{color:var(--t500)}
.checks .obtn{flex:none;align-self:center}
.ats-read{display:flex;flex-direction:column;gap:8px;min-width:0}
.ats-read pre{margin:0;padding:12px 13px;max-height:520px;overflow:auto;white-space:pre-wrap;
  word-break:break-word;font-size:11.5px;line-height:1.55;color:var(--t800);
  background:var(--field);border:1px solid var(--bd-field);border-radius:9px}
.ats-read .pua{font-style:normal;color:var(--bad);background:var(--bad-bg);border-radius:2px;
  padding:0 1px}
.ats-link{margin-top:-6px}
@media (max-width:900px){
  .ats-body{grid-template-columns:1fr}
  .ats-top{flex-direction:column}
}

/* segmented control on paper */
.seg.paper{background:var(--seg-track);width:fit-content}
.seg.paper button{color:var(--t700);padding:4px 15px;font-size:12.5px}
.seg.paper button[aria-selected=true]{background:var(--seg-on);color:var(--t900);font-weight:500}
/* Ochre marks the one action on a screen, so a selected option is the same
   white pill here as everywhere else. */
.seg.paper.acc button[aria-selected=true]{box-shadow:0 1px 2px rgba(27,26,23,.1)}

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
/* ---------- design ------------------------------------------------------- */
/* Under the title bar rather than over it, so the tabs and the AI clients
   stay where they always are. */
.ovl.dz{top:52px}
.dz-title{margin:0;font-size:17px;font-weight:600;color:var(--t900)}
.dz-badge{height:22px;padding:0 9px;display:flex;align-items:center;border-radius:11px;
  background:var(--bar);color:var(--t700);font-size:12px;font-weight:500;flex:none}
.dz-note{font-size:13px;color:var(--t500);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;min-width:0}
.dz-body{flex:1;min-height:0;display:flex}
.dz-nav{width:212px;flex:none;display:flex;flex-direction:column;gap:2px;padding:14px 10px;
  background:var(--panel);border-right:1px solid var(--rule);overflow-y:auto}
.dz-nav button{display:flex;align-items:center;gap:8px;min-height:36px;padding:0 10px;
  border-radius:7px;font-size:13.5px;color:var(--t700);text-align:left}
.dz-nav button:hover{background:var(--paper-hover)}
.dz-nav button[aria-current=true]{background:var(--field);color:var(--t900);font-weight:600;
  box-shadow:0 1px 2px rgba(27,26,23,.08),0 0 0 1px var(--bd-inner)}
.dz-nav .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dz-nav .chg{display:flex;align-items:center;gap:4px;font-size:11.5px;color:var(--acc-text);
  font-weight:500}
.dz-nav .ct{font-size:11.5px;color:var(--t500);font-weight:400}
.dz-dot{width:6px;height:6px;border-radius:50%;background:var(--acc);flex:none}
.dz-key{margin-top:auto;padding:10px;border-top:1px solid var(--rule);font-size:12px;
  color:var(--t500);display:flex;align-items:center;gap:7px}
.dz-pane{width:520px;flex:none;display:flex;flex-direction:column;min-height:0;
  border-right:1px solid var(--rule);background:var(--app)}
.dz-head{flex:none;display:flex;align-items:flex-start;gap:12px;padding:20px 24px 14px}
.dz-head>div{flex:1;min-width:0}
.dz-head h2{margin:0;font-size:18px;font-weight:600}
.dz-head p{margin:4px 0 0;font-size:13px;line-height:1.5;color:var(--t600)}
.dz-scroll{flex:1;min-height:0;overflow-y:auto;padding:0 24px 28px}
.dz-sub{margin:4px 0 6px;font-size:13px;font-weight:600;color:var(--t800)}
/* The photo, above its size and place on the page. */
#dz-photo{display:flex;flex-direction:column;gap:14px;margin-bottom:18px}
.ph-drop{display:flex;flex-direction:column;align-items:center;gap:11px;padding:30px 22px;
  border-radius:12px;border:2px dashed var(--bd-field);background:var(--row-alt);text-align:center}
.ph-drop.over{border-color:var(--acc);background:var(--acc-wash)}
.ph-drop .ic{width:52px;height:52px;border-radius:13px;background:var(--bar);color:var(--acc-text);
  display:grid;place-items:center}
.ph-drop b{font-size:14.5px;font-weight:600}
.ph-drop span{font-size:13px;line-height:1.5;color:var(--t600);max-width:320px}
.ph-card{border:1px solid var(--rule);border-radius:10px;background:var(--field);overflow:hidden}
.ph-top{display:flex;align-items:center;gap:16px;padding:14px}
.ph-top img{width:88px;height:88px;border-radius:7px;display:block;flex:none;
  box-shadow:0 0 0 1px var(--bd-inner);background:#fff}
.ph-top .m{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.ph-top .m b{font-size:13.5px;font-weight:600}
.ph-top .m span{font-size:12px;color:var(--t500)}
.ph-top .acts{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.ph-top .del{border:0;background:none;color:var(--bad);font-size:12.5px;padding:0 6px}
.ph-note{display:flex;gap:10px;padding:10px 12px;border-radius:9px;background:var(--row-alt);
  border:1px solid var(--bd-inner);font-size:12.5px;line-height:1.5;color:var(--t700)}
.ph-note svg{flex:none;margin-top:2px;color:var(--acc-text)}
/* A switch, for a yes/no that takes effect at once. */
.sw{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;cursor:pointer}
.ph-card .sw{border-top:1px solid var(--bd-inner)}
.sw input{position:absolute;opacity:0;width:1px;height:1px}
.sw .tr{position:relative;width:36px;height:20px;flex:none;margin-top:1px;border-radius:10px;
  background:var(--bd-field);transition:background .15s}
.sw .tr::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;
  border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.2);transition:left .15s}
.sw input:checked+.tr{background:var(--acc)}
.sw input:checked+.tr::after{left:18px}
.sw input:focus-visible+.tr{outline:2px solid var(--acc);outline-offset:2px}
.sw .tx{display:flex;flex-direction:column;gap:3px}
.sw .tx b{font-size:13px;font-weight:600;color:var(--t900)}
.sw .tx span{font-size:12px;line-height:1.45;color:var(--t600)}
/* The cropper: the whole photo dimmed, the square that is kept at full strength. */
.crop{position:relative;height:360px;border-radius:10px;overflow:hidden;background:#1b1a17;
  touch-action:none;cursor:grab;user-select:none}
.crop.dragging{cursor:grabbing}
.crop img{position:absolute;left:50%;top:50%;max-width:none;pointer-events:none}
.crop .dim{opacity:.4}
.crop .win{position:absolute;left:50%;top:50%;width:260px;height:260px;margin:-130px 0 0 -130px;
  overflow:hidden;box-shadow:0 0 0 2px #fff;pointer-events:none}
.crop .win img{left:130px;top:130px}
.crop .win.grid{inset:auto;left:50%;top:50%;background-image:linear-gradient(rgba(255,255,255,.35) 1px,
  transparent 1px),linear-gradient(90deg,rgba(255,255,255,.35) 1px,transparent 1px);
  background-size:86.67px 86.67px;background-position:-1px -1px}
.cropzoom{display:flex;align-items:center;gap:12px;font-size:13px;color:var(--t800)}
.cropzoom input{flex:1;accent-color:var(--acc)}
/* The photo chip in the editor's bar, and what it opens. */
.photochip.off{color:var(--t500)}
.photopop{position:fixed;z-index:48;width:320px;background:var(--field);border:1px solid var(--rule);
  border-radius:11px;box-shadow:0 16px 36px -10px rgba(27,26,23,.35);overflow:hidden}
.photopop .why{display:flex;gap:9px;padding:10px 14px 12px;border-top:1px solid var(--bd-inner);
  background:var(--row-alt);font-size:12px;line-height:1.45;color:var(--t700)}
.photopop .why i{width:7px;height:7px;flex:none;margin-top:5px;border-radius:50%;background:var(--acc)}
.dz-card{border:1px solid var(--rule);border-radius:10px;background:var(--field);
  margin-bottom:18px}
.dz-row{display:grid;grid-template-columns:160px minmax(0,1fr) 52px;align-items:center;
  gap:10px;min-height:46px;padding:6px 12px 6px 14px}
.dz-row+.dz-row{border-top:1px solid var(--bd-inner)}
.dz-group>.dz-card:last-child{margin-bottom:0}
.dz-row>label{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--t800);
  min-width:0}
.dz-row>label span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dz-row>label .dz-dot{visibility:hidden}
.dz-row.chg>label .dz-dot{visibility:visible}
.dz-row .dreset{justify-self:end;font-size:12px;font-weight:500;color:var(--acc-text);
  visibility:hidden}
.dz-row.chg .dreset{visibility:visible}
.dz-row .dreset:hover{text-decoration:underline}
.dz-pages{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;
  align-items:center;gap:18px;padding:4px 24px 28px}
.dz-pages img{width:100%;max-width:560px;display:block;background:var(--page);
  box-shadow:0 2px 10px rgba(27,26,23,.14)}
.dz-preview{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;
  background:var(--canvas)}
.dz-effect{flex:none;margin:14px 0 12px;display:flex;align-items:center;gap:10px;
  min-height:32px;padding:4px 14px;border-radius:16px;background:var(--field);
  border:1px solid var(--rule);font-size:12.5px;color:var(--t800);max-width:calc(100% - 48px)}
.dz-effect b{font-weight:600}
.dz-effect .sep{color:var(--t400)}
.dz-effect .why{color:var(--t600);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.themegrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.thumbwrap{display:flex;flex-direction:column;gap:7px;padding:6px 6px 9px;border-radius:9px;
  border:1px solid var(--bd-inner);text-align:left;width:100%}
.thumbwrap:hover{border-color:var(--bd-field)}
.thumbwrap.sel{background:var(--field);border:2px solid var(--acc);padding:5px 5px 8px}
.thumbwrap img{width:100%;aspect-ratio:360/223;object-fit:cover;object-position:top;
  display:block;border-radius:4px;box-shadow:0 0 0 1px var(--bd-inner);background:var(--page)}
/* The drawn sketch stands in while the real render of this document arrives. */
.thumb{width:100%;aspect-ratio:360/223;background:var(--page);border-radius:4px;
  box-shadow:0 0 0 1px var(--bd-inner);padding:10px 9px;display:flex;
  flex-direction:column;gap:4px;opacity:.7}
.thumb i{display:block;background:#e0dcd1;flex:none}
.thumb i.ink{background:#15140f}
.thumbcap{display:flex;align-items:baseline;justify-content:space-between;gap:6px;padding:0 3px}
.thumbcap span{font-size:13px;color:var(--t800)}
.thumbcap em{font-size:11.5px;font-style:normal;color:var(--t500)}
.thumbwrap.sel .thumbcap span{color:var(--t900);font-weight:600}

.dctl{display:flex;align-items:center;gap:7px;min-width:0}
/* background-COLOR: the shorthand would strip the select's chevron. */
.dctl input[type=text],.dctl input[type=number],.dctl select,.dctl textarea{
  background-color:var(--field);border:1px solid var(--bd-field);border-radius:7px;
  padding:0 8px;height:32px;font-size:13px;min-width:0;flex:1}
.dctl select{padding-right:28px}
.dctl textarea{height:auto;padding:6px 8px;line-height:1.45;resize:vertical;
  font-family:'IBM Plex Mono',ui-monospace,Consolas,monospace;font-size:12px}
.dctl input:focus,.dctl select:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-ring)}
.dctl input[type=number]{max-width:84px;flex:none;font-variant-numeric:tabular-nums}
.dctl select.unit{max-width:66px;flex:none}
.dctl input[type=color]{width:34px;height:28px;padding:2px;border:1px solid var(--bd-field);
  border-radius:7px;background:var(--field);cursor:pointer;flex:none}
.dctl input[type=checkbox]{width:17px;height:17px;accent-color:var(--acc);cursor:pointer}
.dctl .hex{font-size:11px;color:var(--t500);flex:none}

/* ---------- settings ----------------------------------------------------- */
/* Settings opens over the app rather than replacing it: you are changing how
   the thing behind it works, so the thing stays in view. The wide shadow is
   the backdrop, which keeps it one element with nothing else to show or hide. */
#ovl-settings{inset:auto;top:50%;left:50%;transform:translate(-50%,-50%);
  width:min(940px,calc(100% - 48px));height:min(680px,calc(100% - 96px));
  border-radius:14px;overflow:hidden;
  box-shadow:0 0 0 100vmax rgba(22,21,19,.45),0 24px 60px rgba(0,0,0,.35)}
#ovl-settings .ovl-bar{height:56px;padding:0 12px 0 24px;background:var(--app);
  border-bottom:1px solid var(--rule);-webkit-app-region:no-drag}
#ovl-settings .ovl-bar .ttl{font-size:16px;font-weight:600;color:var(--t900)}
#ovl-settings .ovl-bar .cbtn{height:34px;padding:0 14px;border-radius:8px;font-size:13px;
  font-weight:500;color:var(--t900);background:var(--field);border-color:var(--bd-field)}
.set-wrap{flex:1;min-height:0;overflow-y:auto}
.set-inner{display:grid;grid-template-columns:200px minmax(0,1fr);min-height:100%}
.set-inner>div:last-child{padding:24px 32px 48px;min-width:0}
.set-rail{display:flex;flex-direction:column;gap:2px;padding:16px 12px;
  background:var(--panel);border-right:1px solid var(--rule)}
.set-rail button{text-align:left;padding:7px 10px;font-size:13.5px;color:var(--t700);
  border-radius:7px}
.set-rail button:hover{background:var(--paper-hover);color:var(--t900)}
.set-rail button[aria-selected=true]{color:var(--t900);font-weight:600;background:var(--field);
  box-shadow:0 1px 2px rgba(27,26,23,.08),0 0 0 1px var(--bd-inner)}
.sp h3{font-size:18px;font-weight:600;margin:0 0 6px}
.sp-lede{color:var(--t600);font-size:12.5px;line-height:1.6;margin:0 0 16px;max-width:62ch}
.sp-note{color:var(--t600);font-size:12px;line-height:1.6;margin:14px 0 0;max-width:62ch}
.srow{display:flex;align-items:center;gap:28px;padding:14px 0;border-top:1px solid var(--rule)}
.srow.nf-sub.off{opacity:.45;pointer-events:none}
/* The time zone on a little world: the land in dots, the night where it is
   night now, your zone's band and your city pinned, the places you have an
   interview linked to it. Clicking picks the nearest city. */
.tzmap{position:relative;max-width:620px;margin:2px auto 6px;padding:14px 14px 10px;border-radius:14px;
  background:var(--field);border:1px solid var(--bd-inner);overflow:hidden}
.tzmap svg{display:block;width:100%;height:auto;cursor:crosshair}
.tzmap .land{fill:none;stroke:var(--t400);stroke-width:2.5;stroke-linecap:round;opacity:.55}
.tzmap .night{fill:var(--t900);opacity:.07}
.tzmap .band{fill:var(--acc);opacity:.13}
.tzmap .band-edge{stroke:var(--acc);stroke-width:1;opacity:.35;stroke-dasharray:2 3}
.tzmap .arc{fill:none;stroke:var(--acc);stroke-width:1.4;stroke-dasharray:3 4;opacity:.7;
  animation:tz-flow 1.6s linear infinite}
.tzmap .iv{fill:var(--card,#fff);stroke:var(--acc);stroke-width:2}
.tzmap .pin{fill:var(--acc)}
.tzmap .ring{fill:none;stroke:var(--acc);stroke-width:2;transform-box:fill-box;transform-origin:center;
  animation:tz-ping 2.4s ease-out infinite}
.tzmap .ghost{fill:none;stroke:var(--t700);stroke-width:1.5;opacity:.8}
.tzmap .cap{display:flex;align-items:center;gap:14px;margin-top:8px;font-size:12px;color:var(--t500)}
.tzmap .cap b{color:var(--t900);font-weight:600}
.tzmap .cap .sw{display:inline-flex;align-items:center;gap:5px}
.tzmap .cap .sw i{width:10px;height:10px;border-radius:3px;display:inline-block}
.tzmap .cap .grow{flex:1}
.tzmap .tip{position:absolute;pointer-events:none;transform:translate(-50%,-130%);padding:4px 8px;
  border-radius:6px;background:var(--c900);color:var(--cw);font-size:11.5px;white-space:nowrap;
  box-shadow:0 4px 12px rgba(0,0,0,.2)}
.tzmap .card{position:absolute;pointer-events:none;padding:7px 10px;border-radius:9px;
  background:var(--c900);color:var(--cw);font-size:12px;line-height:1.35;white-space:nowrap;
  box-shadow:0 6px 18px rgba(0,0,0,.22)}
.tzmap .card .c1{font-size:13px;font-weight:600}
.tzmap .card .c2{color:var(--c200)}
:root[data-theme=dark] .tzmap .night{fill:#000;opacity:.32}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .tzmap .night{fill:#000;opacity:.32}}
@keyframes tz-ping{0%{transform:scale(.6);opacity:.9}100%{transform:scale(2.6);opacity:0}}
@keyframes tz-flow{to{stroke-dashoffset:-14}}
@media (prefers-reduced-motion:reduce){.tzmap .ring,.tzmap .arc{animation:none}}
.srow>div{flex:1;min-width:0;max-width:56ch}
.srow b{display:block;font-size:13px;font-weight:500;margin-bottom:2px}
.srow span{display:block;color:var(--t600);font-size:12px;line-height:1.55;
  overflow-wrap:anywhere}
.srow select,.srow input{border:1px solid var(--bd-field);border-radius:5px;
  padding:6px 10px;background-color:var(--field);font-size:12.5px}
/* The column governs where controls END, not how wide they are: stretching
   a switch to 184px turns it into a progress bar and a button into a box
   with its label jammed right. `:not(:first-child)` matters -- a row with
   only a label is its own last child, and would otherwise be flexed. */
.srow>:last-child:not(:first-child){flex:none;margin-left:auto;display:flex;
  justify-content:flex-end;align-items:center}
.srow select{min-width:184px}
.srow input{min-width:184px}
.btnlink{text-decoration:none;color:var(--t700)}
.sp-sub{display:flex;align-items:center;gap:10px;font-size:13px;font-weight:600;
  color:var(--t900);margin:30px 0 12px}
.sp-sub::after{content:"";flex:1;height:1px;background:var(--rule)}

/* ---------- AI clients ---------------------------------------------------
   One card each, because connecting a client is something you do rather than a
   setting you read: the mark says which, the pill says where it stands, and the
   button is the only thing you have to understand. */
.clients{display:flex;flex-direction:column;gap:10px;margin:18px 0 4px}
.client{display:grid;grid-template-columns:38px 1fr auto;gap:0 14px;
  padding:14px 16px;border:1px solid var(--bd-field);border-radius:9px;
  background:var(--field)}
.client .badge{grid-column:1;grid-row:1/3;align-self:center;width:38px;height:38px;
  border-radius:9px;display:grid;place-items:center;background:var(--bar)}
.client[data-client=claude] .badge,.onb-client[data-client=claude] .badge{
  background:rgba(217,119,87,.14);color:#D97757}
.client[data-client=openai] .badge,.onb-client[data-client=openai] .badge{color:var(--t900)}
/* Nous publish this one as an avatar -- a figure on a tile -- rather than as a
   glyph that takes the colour around it, so it is drawn that way: black on
   white, at the 0.75 of the tile their own icon set declares. Left to take
   currentColor like the other three it inverts on a dark background, and an
   inverted illustration is not the mark. It also needs the extra size: at the
   19px the rest are drawn at, its detail closes up into a blot. */
.client[data-client=hermes] .badge,.onb-client[data-client=hermes] .badge{
  background:#fff;color:#000;
  box-shadow:inset 0 0 0 1px var(--bd-field)}   /* white on white needs an edge */
.client[data-client=hermes] .badge svg{width:28px;height:28px}
.client[data-client=mistral] .badge,.onb-client[data-client=mistral] .badge{
  background:rgba(250,80,15,.12)}
.client .who{grid-column:2;grid-row:1;display:flex;align-items:center;gap:9px;
  min-width:0;flex-wrap:wrap}
.client .who b{font-size:13.5px;font-weight:600;color:var(--t900)}
.client .say{grid-column:2;grid-row:2;font-size:12.5px;color:var(--t600);
  line-height:1.5;margin-top:3px}
.client .go{grid-column:3;grid-row:1/3;align-self:center}
/* The steps sit under the card's own sentence, before the button is pressed
   rather than after, because step 2 is the one that does the connecting and
   the user needs to know it is coming. */
.aisteps{grid-column:2/4;grid-row:3;list-style:none;margin:11px 0 0;padding:0;
  display:flex;flex-direction:column;gap:6px}
.aisteps li{display:flex;align-items:flex-start;gap:8px;font-size:12px;
  color:var(--t600);line-height:1.45}
.aisteps li i{flex:none;width:16px;height:16px;border-radius:50%;
  display:grid;place-items:center;font-style:normal;font-size:10px;
  font-weight:600;background:var(--bar);color:var(--t500);margin-top:1px}
.aisteps li.done{color:var(--t900)}
.aisteps li.done i{background:color-mix(in srgb,var(--fn-won) 18%,transparent);
  color:var(--fn-won)}
.aisteps li.wait i{background:var(--acc-wash);color:var(--acc-text)}
.aisteps li.wait span{color:var(--acc-text)}
.client .path{grid-column:2/4;grid-row:4;margin-top:11px;padding-top:10px;
  border-top:1px solid var(--rule);display:flex;align-items:center;gap:10px}
.client .path span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;color:var(--t500);
  font:11.5px/1.4 'IBM Plex Mono',ui-monospace,Consolas,monospace}
.client .path button{font-size:11.5px;color:var(--t500);padding:1px 4px;flex:none}
.client .path button:hover{color:var(--t900)}

/* Ochre is what this app already uses for live, so connected wears it; a
   misconfigured client is a real problem and wears the error colour. */
.pill{display:inline-flex;align-items:center;gap:5px;flex:none;font-size:11px;
  font-weight:500;padding:2px 9px 2px 7px;border-radius:99px;
  background:var(--bar);color:var(--t600)}
.pill i{width:5px;height:5px;border-radius:50%;background:var(--dot-idle);flex:none}
/* The same teal the funnel uses for a good outcome, so liveness reads as a
   state rather than as a selection. */
.pill[data-state=connected]{background:color-mix(in srgb,var(--fn-won) 15%,transparent);
  color:var(--t900)}
.pill[data-state=connected] i{background:var(--fn-won)}
.pill[data-state=elsewhere],.pill[data-state=other-workspace],
.pill[data-state=unreadable]{background:var(--bad-bg);color:var(--bad)}
/* A client pointed at the wrong workspace is silently editing CVs the user is
   not looking at, which is the worst state in this pane and used to be the
   quietest: its pill sat at the same lightness as "Not set up", so the card
   that needed attention looked like the two that did not. */
.client.wrong{border-color:var(--bad-line)}
.client.wrong .badge{background:var(--bad-bg)}
.pill[data-state=elsewhere] i,.pill[data-state=other-workspace] i,
.pill[data-state=unreadable] i{background:var(--bad)}

/* ---------- what the model can do --------------------------------------- */
.tools{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
.tool{padding:11px 13px;border:1px solid var(--bd-field);border-radius:8px;
  background:var(--field)}
.tool.lead{grid-column:1/-1;border-color:var(--acc-line);background:var(--acc-wash)}
.tool .n{display:block;margin-bottom:6px;color:var(--t900);font-weight:500;
  font:11.5px/1 'IBM Plex Mono',ui-monospace,Consolas,monospace}
.tool p{margin:0;font-size:12px;line-height:1.55;color:var(--t600)}
.tool b{font-weight:600;color:var(--t900)}

.caveat{display:flex;gap:11px;margin-top:14px;padding:12px 14px;border-radius:8px;
  background:var(--bar);font-size:12px;line-height:1.6;color:var(--t600)}
.caveat p{margin:0}.caveat p+p{margin-top:7px}
.caveat b{font-weight:600;color:var(--t900)}
.caveat svg{color:var(--t500)}

.skills{display:flex;flex-direction:column;gap:1px;margin:12px 0 0;
  border:1px solid var(--bd-field);border-radius:8px;overflow:hidden}
.skills div{display:flex;align-items:center;gap:10px;padding:9px 13px;
  background:var(--field);font-size:12.5px}
.skills div+div{border-top:1px solid var(--rule)}
.skills .nm{flex:none;color:var(--t900);font-weight:500}
.skills .ds{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;color:var(--t500);font-size:11.5px}
.skills .tag{flex:none;font-size:10.5px;padding:1px 7px;border-radius:99px;
  background:var(--bar);color:var(--t600)}
.skills .tag.mcp{background:var(--acc-wash);color:var(--t900)}
.skillcta{display:flex;gap:8px;margin-top:12px}
.obtn.primary{background:var(--acc);border-color:transparent;color:var(--c800);
  font-weight:500}
.obtn.primary:hover:not(:disabled){background:var(--acc-hover)}
.mcplog{border:1px solid var(--bd-field);border-radius:8px;overflow:hidden;
  font-size:12px;color:var(--t600)}
.mcplog div{display:flex;align-items:center;gap:10px;padding:8px 13px;
  background:var(--field)}
.mcplog div+div{border-top:1px solid var(--rule)}
.mcplog .t{flex:none;color:var(--t900);
  font:11.5px/1 'IBM Plex Mono',ui-monospace,Consolas,monospace}
.mcplog .p{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;font-size:11.5px;color:var(--t500)}
.mcplog .w{flex:none;color:var(--t500);font-size:11px}
.mcplog .none{color:var(--t500)}
/* A call the server refused. It belongs in the record -- an attempt
   to reach outside the workspace is worth seeing -- but it must not
   read like something the model actually did. */
.mcplog div.no{background:var(--bad-bg)}
/* A read leaves the author column empty rather than borrowing the mark. */
.mcplog .roi{flex:none;width:13px;height:13px}
.mcplog div.no .t{color:var(--bad)}

.fold{margin-top:26px;border-top:1px solid var(--rule);padding-top:14px}
.fold summary{font-size:12.5px;color:var(--t600);cursor:pointer;margin-bottom:12px}
.fold summary:hover{color:var(--t900)}
.fold h4{font-size:12.5px;font-weight:600;color:var(--t900);margin:16px 0 4px}
.steps{margin:0 0 14px;padding-left:18px;font-size:13px;line-height:1.7}
.steps li{margin-bottom:4px}.steps li::marker{color:var(--t500)}
pre.code{background:var(--bar);border:1px solid var(--rule);border-radius:4px;padding:12px 14px;
  font:11.5px/1.7 'IBM Plex Mono',ui-monospace,Consolas,monospace;overflow-x:auto;margin:0 0 8px;
  white-space:pre}
.swatches{display:flex;gap:7px;flex:none}
.swatches button{width:23px;height:23px;border-radius:50%;flex:none;
  border:2px solid transparent;background-clip:padding-box;
  transition:transform .1s,box-shadow .1s}
.swatches button:hover{transform:scale(1.12)}
.swatches button[aria-pressed=true]{box-shadow:0 0 0 2px var(--panel),
  0 0 0 3.5px var(--t600)}
.swatches button:focus-visible{outline:2px solid var(--acc);outline-offset:3px}
/* One select, ours. The OS control brings its own chevron, its own metrics
   and its own idea of a focus ring, none of which match anything here. */
select{appearance:none;-webkit-appearance:none;font:inherit;font-size:12.5px;
  color:var(--t900);background:var(--field);border:1px solid var(--bd-field);
  border-radius:5px;padding:6px 30px 6px 10px;cursor:pointer;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238b8578' stroke-width='2.4' stroke-linecap='round'><path d='M6 9l6 6 6-6'/></svg>");
  background-repeat:no-repeat;background-position:right 9px center}
select:hover{border-color:var(--ink-3,var(--t500))}
select:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
/* The native date control brings its own chrome and its own glyph. The
   displayed format follows the OS locale and cannot be overridden without
   giving up the picker, so match the rest and let the format be. */
input[type=date]{appearance:none;-webkit-appearance:none;
  background-color:var(--field);border:1px solid var(--bd-field);
  border-radius:5px;padding:6px 10px;font:inherit;font-size:12.5px;
  color:var(--t900)}
input[type=date]::-webkit-calendar-picker-indicator{opacity:.55;cursor:pointer;
  filter:grayscale(1)}
input[type=date]::-webkit-calendar-picker-indicator:hover{opacity:1}
textarea{resize:vertical}
.insp textarea,.fg textarea{resize:none}
.tgl{position:relative;display:inline-block;width:34px;height:20px;flex:none;cursor:pointer}
.tgl input{opacity:0;width:0;height:0;position:absolute}
.tgl i{position:absolute;inset:0;background:var(--rule);border-radius:99px;transition:background .16s}
.tgl i::before{content:"";position:absolute;width:14px;height:14px;left:3px;top:3px;
  background:var(--knob);border-radius:50%;transition:transform .16s}
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
.yband{position:absolute;left:0;right:0;pointer-events:none;
  background:var(--acc-wash);box-shadow:inset 3px 0 0 var(--acc);z-index:0}
.t-key{color:var(--tk-key)}.t-str{color:var(--tk-str)}.t-num{color:var(--tk-num)}
.t-bool{color:var(--tk-bool)}.t-com{color:var(--tk-com)}
.t-punc{color:var(--tk-punc)}.t-blk{color:var(--tk-blk)}
.yamlerr{flex:none;background:var(--bad-bg);border-bottom:1px solid var(--bad-line);padding:8px 18px;
  font-size:12px;color:var(--bad);display:flex;gap:10px;align-items:baseline}
.yamlerr button{font-size:12px;color:var(--bad);text-decoration:underline;flex:none;
  margin-left:auto}

/* ---------- states -------------------------------------------------------- */
.empty{padding:56px 26px;color:var(--t600);max-width:54ch}
.empty h3{margin:0 0 6px;font-size:13.5px;color:var(--t900);font-weight:600}
.empty p{margin:0;font-size:13px;line-height:1.7}
.empty p+p{margin-top:11px}
.empty .cta{margin-top:18px}
.err{margin:20px;background:var(--bad-bg);border-left:2px solid var(--bad);padding:14px 16px;
  color:var(--bad);max-width:70ch}
.err h4{margin:0 0 6px;font-size:12.5px;font-weight:600}
.err .hint{color:var(--t900);margin:8px 0 0;font-size:12.5px;line-height:1.65}
.err pre{margin:10px 0 0;white-space:pre-wrap;font:11px/1.55 'IBM Plex Mono',Consolas,monospace;
  max-height:190px;overflow:auto;color:var(--t600)}
.spin{display:inline-block;width:9px;height:9px;border:1.5px solid var(--rule);
  border-top-color:var(--t500);border-radius:50%;animation:sp .8s linear infinite}


/* The swatch colours, named once so the picker and the themes cannot drift. */
:root{--sw-ochre:#c08a3e;--sw-indigo:#7095dc;--sw-teal:#0aaab5;
  --sw-rose:#d27782;--sw-moss:#6aa867}
/* ---------- accent themes ------------------------------------------------
   Each is the default ochre rotated in hue at the same OKLCH lightness and
   chroma, so every contrast pairing the interface already depends on holds
   whichever one is chosen. Only the accent family changes; the warm greys
   the app is built from stay put. */
:root[data-accent="indigo"]{--acc:#7095dc;--acc-hover:#82a8f0;
  --acc-line:rgba(112,149,220,.6);--acc-ring:rgba(112,149,220,.18);
  --acc-wash:rgba(112,149,220,.10);--acc-text:#325293;--acc-text-dark:#a3c4ff}
@media(prefers-color-scheme:dark){:root[data-accent="indigo"]:not([data-theme=light]){--acc-wash:rgba(112,149,220,.16);--acc-ring:rgba(112,149,220,.32);--acc-text:#a3c4ff}}
:root[data-theme=dark][data-accent="indigo"]{--acc-wash:rgba(112,149,220,.16);--acc-ring:rgba(112,149,220,.32);--acc-text:#a3c4ff}
:root[data-accent="teal"]{--acc:#0aaab5;--acc-hover:#34bdc8;
  --acc-line:rgba(10,170,181,.6);--acc-ring:rgba(10,170,181,.18);
  --acc-wash:rgba(10,170,181,.10);--acc-text:#006671;--acc-text-dark:#70d7e0}
@media(prefers-color-scheme:dark){:root[data-accent="teal"]:not([data-theme=light]){--acc-wash:rgba(10,170,181,.16);--acc-ring:rgba(10,170,181,.32);--acc-text:#70d7e0}}
:root[data-theme=dark][data-accent="teal"]{--acc-wash:rgba(10,170,181,.16);--acc-ring:rgba(10,170,181,.32);--acc-text:#70d7e0}
:root[data-accent="rose"]{--acc:#d27782;--acc-hover:#e68a94;
  --acc-line:rgba(210,119,130,.6);--acc-ring:rgba(210,119,130,.18);
  --acc-wash:rgba(210,119,130,.10);--acc-text:#883643;--acc-text-dark:#fcaab2}
@media(prefers-color-scheme:dark){:root[data-accent="rose"]:not([data-theme=light]){--acc-wash:rgba(210,119,130,.16);--acc-ring:rgba(210,119,130,.32);--acc-text:#fcaab2}}
:root[data-theme=dark][data-accent="rose"]{--acc-wash:rgba(210,119,130,.16);--acc-ring:rgba(210,119,130,.32);--acc-text:#fcaab2}
:root[data-accent="moss"]{--acc:#6aa867;--acc-hover:#7dbb79;
  --acc-line:rgba(106,168,103,.6);--acc-ring:rgba(106,168,103,.18);
  --acc-wash:rgba(106,168,103,.10);--acc-text:#286426;--acc-text-dark:#9fd59b}
@media(prefers-color-scheme:dark){:root[data-accent="moss"]:not([data-theme=light]){--acc-wash:rgba(106,168,103,.16);--acc-ring:rgba(106,168,103,.32);--acc-text:#9fd59b}}
:root[data-theme=dark][data-accent="moss"]{--acc-wash:rgba(106,168,103,.16);--acc-ring:rgba(106,168,103,.32);--acc-text:#9fd59b}

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
  .rail-cvs{width:200px}
  .peek{width:auto;left:0}
  .dz-pane{width:440px}
}
/* Phones: the two-up tiles and the card's third column both stop making
   sense well before the app itself does. */
@media(max-width:680px){
  .tools{grid-template-columns:1fr}
  .client{grid-template-columns:34px 1fr}
  .client .badge{width:34px;height:34px}
  .client .go{grid-column:1/3;grid-row:3;margin-top:12px}
  .client .go .obtn{width:100%}
  .client .path{grid-column:1/3;grid-row:4}
  .rail-cvs{width:150px}
}
@media(max-width:880px){
  .insp{display:none}
  .thead,.trow{grid-template-columns:minmax(0,2fr) minmax(0,1.4fr) 170px 90px}
  .thead>*:nth-child(n+5),.trow>*:nth-child(n+5){display:none}
  .set-inner{grid-template-columns:1fr;gap:16px}
  .set-rail{flex-direction:row;flex-wrap:wrap;position:static}
}
</style></head><body>
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
        <div class="langsw" id="langsw" role="group" aria-label="Language" hidden></div>
        <div class="grow"></div>
        <span class="acts" id="act-doc">
          <button class="obtn" id="btn-ats" title="What an applicant tracking system reads from this CV">ATS check</button>
          <button class="obtn" id="btn-design" title="Theme, typeface and page size">Design</button>
          <button class="obtn" id="btn-pdf" disabled>Export PDF&#8230;</button>
          <button class="pbtn" id="btn-render">Render</button>
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
      <section class="nu" id="nextup" aria-label="Next up" hidden></section>
      <div class="tcard">
        <div class="thead"><span>Company</span><span>Role</span>
          <span>Documents</span><span>Status</span>
          <span>Applied</span><span>Follow-up</span></div>
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
        <button class="obtn" id="ex-json">JSON</button></div>
      <div id="fn-body">
        <!-- Sent to accepted, as five numbers and the rate between each pair. -->
        <section class="fn-card fn-journey" id="fn-journey" aria-label="From sent to accepted"></section>
        <div class="fn-main">
          <!-- White in light mode like every card; a dark stage in dark mode. -->
          <section class="fn-stage" aria-labelledby="fn-stage-h">
            <div class="fn-shead"><h2 id="fn-stage-h">Where they went</h2><span id="fn-hint"></span>
              <div class="grow"></div>
              <span class="fn-legend" id="fn-legend"><i></i>Moving: still in play</span></div>
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
      <button data-s="ai" aria-selected="false">AI clients</button>
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
        <div class="srow"><div><b>Setup</b><span>The welcome and the steps from the
          first launch: import a CV from a PDF or LinkedIn, your name at the top of the
          base CV, how it prints, and an AI client.</span></div>
          <button class="obtn" id="s-setup">Run setup again</button></div>
        <div class="srow"><div><b>Sample data</b><span id="s-sample-say">See the app in use:
          about sixty applications in every state over five months, tailored CVs, cover
          letters, and the base CV in French, Spanish and Brazilian Portuguese. It opens in
          a folder of its own, made fresh each time; your workspace is not touched, and a
          restart brings you back to it.</span></div>
          <button class="obtn" id="s-sample">Open sample data</button></div>
        <div class="srow"><div><b>Folder</b><span id="s-ws" class="mono"></span></div>
          <button class="obtn" id="s-open">Open folder</button></div>
        <div class="srow"><div><b>Documents</b><span id="s-count"></span></div></div>
        <div class="srow"><div><b>Applications</b><span>Exported as JSON or CSV so the
          database is never a lock-in.</span></div>
          <button class="obtn" id="s-exp">Export JSON</button></div>
      </section>

      <section class="sp" id="sp-notify" hidden>
        <h3>Notifications</h3>
        <p class="sp-lede">Reminders from CV Studio, as your system's notifications: before an
          interview, and on the morning a follow-up is due. They come while the app is open,
          even in the background. Off until you turn them on.</p>
        <div class="srow"><div><b>Notifications</b><span id="s-notify-state">Off.</span></div>
          <label class="tgl"><input type="checkbox" id="s-notify"><i></i></label></div>
        <div class="srow nf-sub"><div><b>Interviews</b><span>With the time in yours and in theirs.</span></div>
          <select id="s-notify-iv">
            <option value="off">Off</option>
            <option value="10">10 minutes before</option>
            <option value="60">1 hour before</option>
            <option value="60,10">1 hour and 10 minutes before</option>
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
        <p class="sp-lede">This app is one half of a pair. The model writes and tailors
          the CVs, through a server that ships inside this app; here you look at the
          rendered page and fix what it got wrong. It can keep your applications up to
          date too: given a mail or calendar connector of its own, it reads the replies
          and moves the statuses, and nothing about that passes through this app. Both
          halves work on the same workspace, so there is nothing to sync and nothing to
          upload.</p>

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
        <p class="sp-lede">The eyes of a CV written with Claude. The model reads the
          posting and writes the YAML; this renders it, shows you the page and the page
          budget, and lets you fix by hand what is easier pointed at than described.
          Built on RenderCV and Typst.</p>
        <p class="sp-lede">Everything runs on your machine. No account, no server, no
          telemetry, which matters more, not less, once an AI is editing the files:
          your CVs stay plain YAML in a folder you own, and both halves only ever touch
          that folder.</p>
        <p class="sp-note">MIT licensed. Bundles RenderCV (MIT), Typst (Apache-2.0), the
          RenderCV font set and IBM Plex (SIL Open Font License), and d3-sankey (ISC).
          The Claude mark is a trademark of Anthropic, used here only to identify the
          Claude Desktop integration.</p>
      </section>
    </div>
  </div></div>
</div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const API_TOKEN=__API_TOKEN__;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* ---- the interface's language ---------------------------------------------
   English, French, Spanish or Brazilian Portuguese: the computer's language
   unless Settings says otherwise. Two ways in. t("...") for text built in
   code, with {name} holes for what changes. And a watcher that translates
   the interface's text as it lands in the page, so a label written once in
   markup needs no call at all. It never touches what is yours: CVs, letters,
   postings, notes and anything you are typing (I18N_SKIP). */
const UI_LANGS={en:"en-GB",fr:"fr-FR",es:"es-ES",pt:"pt-BR"};
function uiLang(){
  let p=new URLSearchParams(location.search).get("lang");
  if(!p||!UI_LANGS[p]) p=(window.CVS_PREFS||{}).ui_lang;
  if(p&&UI_LANGS[p]) return p;
  const n=String(navigator.language||"en").slice(0,2).toLowerCase();
  return UI_LANGS[n]?n:"en";
}
const UI_LANG=uiLang();
const uiLocale=()=>UI_LANGS[UI_LANG];
const I18N_D=(window.I18N||{})[UI_LANG]||null, I18N_P=(window.I18N_RX||{})[UI_LANG]||[];
/* The catalogue writes a plural as "envoyée(s)"; with the count in front of
   it, it becomes the right form. French counts 0 and 1 as singular. */
const plur=r=>r.indexOf("(s)")<0?r:r.replace(/(\d+)([^\d()]*?)\(s\)/g,
  (m,n,mid)=>n+mid+((UI_LANG==="fr"?+n<2:+n===1)?"":"s"));
function t(s,v){
  let r=(I18N_D&&I18N_D[s])||s;
  if(v) r=r.replace(/\{(\w+)\}/g,(m,k)=>v[k]??m);
  return plur(r);
}
function trString(str){
  if(!I18N_D) return null;
  const key=str.replace(/\s+/g," ").trim();
  if(!key||key.length>600) return null;
  let out=I18N_D[key];
  if(out==null) for(const [re,rep] of I18N_P){ if(re.test(key)){ out=key.replace(re,rep); break } }
  if(out==null||out===key) return null;
  out=plur(out);
  const lead=str.match(/^\s*/)[0], trail=str.match(/\s*$/)[0];
  return lead+out+trail;
}
const I18N_SKIP="[data-noi18n],[contenteditable],textarea,input,script,style,code,pre,.ap-post,"+
  ".ap-notes,.lt-stage,.fj-who,.fn-src .sn,.co,.con,.sk-tip .who,#yaml,.yamlerr,.drift-list .then";
const I18N_ATTRS=["placeholder","title","aria-label","data-ph"];
const I18N_SKIP_FIELD="[data-noi18n],[contenteditable],#yaml,.ap-post,.ap-notes";
function trNode(n){
  if(n.nodeType===3){
    const p=n.parentElement; if(!p||p.closest(I18N_SKIP)) return;
    const r=trString(n.nodeValue); if(r!=null) n.nodeValue=r;
  }else if(n.nodeType===1){
    /* A field's own words (its placeholder, its label) are the app's, even
       though what is typed in it never is. */
    (n.matches("input,textarea")?[n]:[...n.querySelectorAll("input,textarea")]).forEach(f=>{
      if(f.closest(I18N_SKIP_FIELD)) return;
      for(const a of I18N_ATTRS){ const v=f.getAttribute(a); if(v){ const r=trString(v); if(r!=null) f.setAttribute(a,r) } }
    });
    if(n.closest(I18N_SKIP)) return;
    for(const a of I18N_ATTRS){ const v=n.getAttribute(a); if(v){ const r=trString(v); if(r!=null) n.setAttribute(a,r) } }
    const w=document.createTreeWalker(n,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{acceptNode:x=>
      (x.nodeType===1?x:x.parentElement).closest(I18N_SKIP)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
    let x; while((x=w.nextNode())){
      if(x.nodeType===3){ const r=trString(x.nodeValue); if(r!=null) x.nodeValue=r }
      else for(const a of I18N_ATTRS){ const v=x.getAttribute(a); if(v){ const r=trString(v); if(r!=null) x.setAttribute(a,r) } }
    }
  }
}
if(I18N_D){
  /* Native dialogs are outside the page, where the watcher cannot see. */
  for(const k of ["confirm","prompt","alert"]){
    const f=window[k].bind(window);
    window[k]=(m,...a)=>f(m==null?m:(trString(String(m))??String(m)),...a);
  }
  document.documentElement.lang=UI_LANG;
  trNode(document.body);
  new MutationObserver(ms=>{ for(const m of ms){
    if(m.type==="childList") m.addedNodes.forEach(trNode);
    else if(m.type==="characterData") trNode(m.target);
    else if(m.type==="attributes") trNode(m.target);
  } }).observe(document.body,{childList:true,subtree:true,characterData:true,
    attributes:true,attributeFilter:I18N_ATTRS});
}
const tok=()=>API_TOKEN?"&token="+encodeURIComponent(API_TOKEN):"";

/* One object holds everything the three screens share. Selection, filters and
   the last good render all live here so that switching views never throws work
   away: the funnel can hand a status filter to Jobs, and Jobs can hand a
   document to the editor, without either reloading. */
const S={
  view:"jobs", state:null,
  path:null, doc:null, data:null, tab:"page",
  dirty:false, savedAt:null, busy:false,
  pdf:null, render:null, renderMs:null, live:"idle", liveMsg:"",
  page:0, zoom:1, zoomAuto:true, fill:null,
  sel:null, openSection:null,
  docThumbs:{},             /* each document's first page, for Documents */
  baseLang:null,            /* which language of the base CV Documents shows */
  docLang:null,             /* the Documents language filter, null for all */
  driftBy:null,             /* how far behind each translation of the base is */
  ai:null,                  /* which AI clients are wired up to us */
  prov:null,                /* who wrote each field, and what differs from the base */
  pulse:null,               /* last workspace poll: file stamps and AI activity */
  skills:null,              /* the cv-studio skills on this machine */
  keyShown:false,           /* the API key is masked until asked for */
  docMtime:null,            /* the open file as we last read or wrote it */
  extMtime:null,            /* a newer version on disk we have not taken */
  extTheirs:null,           /* their version, so the bar can name the fields */
  resolved:null,            /* how the last conflict was settled, and when */
  pages:{},                 /* path -> page count, learned as things render */
  themePages:{},            /* theme -> page count for the open document */
  jobs:[], statuses:[], nodes:{}, labels:{}, jready:false,
  tailoring:new Set(),      /* applications whose CV is being copied right now */
  jfilter:{kind:"all", value:""}, jsel:null,
  funnel:null, since:"", fnode:null,
  baseThumb:null,           /* {path, png, failed}: the base's first page */
  schema:null, schemaTheme:null,
};
const DZ={theme:null, section:"theme"};

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
const MONTHS=Array.from({length:12},(_,i)=>{ try{ return new Intl.DateTimeFormat(uiLocale(),{month:"short"})
  .format(new Date(2026,i,15)).replace(".","") }catch(e){ return ["Jan","Feb","Mar","Apr","May","Jun","Jul",
  "Aug","Sep","Oct","Nov","Dec"][i] } });
function shortDate(iso){
  if(!iso) return "";
  /* The tracker stores ISO strings; a document's age arrives as an mtime that
     has already been turned into a Date. Both want the same "14 Sep". */
  const d=iso instanceof Date?iso:new Date(String(iso).slice(0,19));
  if(isNaN(d)) return String(iso).slice(0,10);
  return d.getDate()+" "+MONTHS[d.getMonth()]+
    (d.getFullYear()!==new Date().getFullYear()?" "+String(d.getFullYear()).slice(2):"");
}
/* ---- time zones -----------------------------------------------------------
   An interview time is kept as the wall-clock time the invitation gave, in the
   zone it gave it in (interview_tz), so the file says what the email said.
   Showing it, or comparing it with now, goes through these. The zone data is
   the browser's own, so nothing ships for it. */
const machineTz=()=>{ try{ return Intl.DateTimeFormat().resolvedOptions().timeZone||"UTC" }catch(e){ return "UTC" } };
const userTz=()=>prefs().tz||machineTz();
function tzOffset(tz,date){
  const p=new Intl.DateTimeFormat("en-US",{timeZone:tz,hourCycle:"h23",year:"numeric",month:"2-digit",
    day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit"}).formatToParts(date);
  const g=t=>+p.find(x=>x.type===t).value;
  return (Date.UTC(g("year"),g("month")-1,g("day"),g("hour")%24,g("minute"),g("second"))-date.getTime())/60000;
}
/* "2026-09-25T10:00" in a zone, as the moment it names. */
function wallToInstant(wall,tz){
  const [d,t="00:00"]=String(wall).split("T"), [Y,M,D]=d.split("-").map(Number);
  const [h,m]=t.split(":").map(Number), guess=Date.UTC(Y,M-1,D,h||0,m||0);
  const off=tzOffset(tz,new Date(guess)); let at=guess-off*60000;
  const off2=tzOffset(tz,new Date(at)); if(off2!==off) at=guess-off2*60000;
  return new Date(at);
}
function interviewMoment(j){
  if(!j||!j.interview_at) return null;
  return wallToInstant(String(j.interview_at).slice(0,16),j.interview_tz||machineTz());
}
const TZ_NAMES={Sao_Paulo:"São Paulo",Bogota:"Bogotá",Mexico_City:"Mexico City",Zurich:"Zürich"};
const tzCity=tz=>{ const k=String(tz||"").split("/").pop(); return TZ_NAMES[k]||k.replace(/_/g," ") };
function fmtWhen(date,tz,withDay=true){
  const o={timeZone:tz,hour:"2-digit",minute:"2-digit"};
  if(withDay) Object.assign(o,{weekday:"short",day:"numeric",month:"short"});
  try{ return new Intl.DateTimeFormat(uiLocale(),o).format(date) }catch(e){ return date.toISOString().slice(0,16) }
}
/* "Thu 25 Sep, 11:00 your time · 10:00 in London", or just the one when the
   two zones agree at that moment. */
function interviewLine(j){
  const at=interviewMoment(j); if(!at) return "";
  const mine=userTz(), theirs=j.interview_tz;
  const here=fmtWhen(at,mine);
  if(!theirs||tzOffset(theirs,at)===tzOffset(mine,at)) return here;
  return here+" "+t("your time")+" · "+fmtWhen(at,theirs,false)+" "+t("in")+" "+tzCity(theirs);
}
/* The zone an interview is probably in, from where the job is. Remote, or
   anywhere not listed, is your own. */
const TZ_GUESS=[
  [/london|manchester|edinburgh|\buk\b|united kingdom|england|scotland/i,"Europe/London"],
  [/dublin|ireland/i,"Europe/Dublin"],
  [/paris|lyon|marseille|toulouse|bordeaux|lille|nantes|montpellier|france/i,"Europe/Paris"],
  [/madrid|barcelona|valencia|sevilla|seville|spain|españa/i,"Europe/Madrid"],
  [/lisbon|lisboa|porto|portugal/i,"Europe/Lisbon"],
  [/berlin|munich|münchen|hamburg|frankfurt|germany|deutschland/i,"Europe/Berlin"],
  [/amsterdam|rotterdam|netherlands/i,"Europe/Amsterdam"],
  [/brussels|bruxelles|belgium/i,"Europe/Brussels"],
  [/zurich|zürich|geneva|genève|switzerland/i,"Europe/Zurich"],
  [/milan|milano|rome|roma|italy/i,"Europe/Rome"],
  [/stockholm|sweden/i,"Europe/Stockholm"],[/copenhagen|denmark/i,"Europe/Copenhagen"],
  [/oslo|norway/i,"Europe/Oslo"],[/helsinki|finland/i,"Europe/Helsinki"],
  [/warsaw|poland/i,"Europe/Warsaw"],
  [/são paulo|sao paulo|rio de janeiro|belo horizonte|brazil|brasil/i,"America/Sao_Paulo"],
  [/new york|nyc|boston|miami|atlanta|washington|toronto|montreal|montréal/i,"America/New_York"],
  [/chicago|austin|dallas|houston/i,"America/Chicago"],[/denver|boulder/i,"America/Denver"],
  [/san francisco|\bsf\b|bay area|seattle|los angeles|palo alto|mountain view|vancouver|california/i,"America/Los_Angeles"],
  [/mexico/i,"America/Mexico_City"],[/buenos aires|argentina/i,"America/Argentina/Buenos_Aires"],
  [/singapore/i,"Asia/Singapore"],[/tokyo|japan/i,"Asia/Tokyo"],[/sydney|melbourne/i,"Australia/Sydney"],
  [/dubai/i,"Asia/Dubai"],[/bangalore|bengaluru|india/i,"Asia/Kolkata"],
];
function guessTz(j){
  const where=[j.location,j.country].filter(Boolean).join(" ");
  const hit=TZ_GUESS.find(([re])=>re.test(where));
  return hit?hit[1]:null;
}
const COMMON_TZ=["Europe/London","Europe/Dublin","Europe/Lisbon","Europe/Paris","Europe/Madrid",
  "Europe/Berlin","Europe/Amsterdam","Europe/Zurich","Europe/Stockholm","America/New_York",
  "America/Chicago","America/Denver","America/Los_Angeles","America/Sao_Paulo","America/Mexico_City",
  "Asia/Dubai","Asia/Kolkata","Asia/Singapore","Asia/Tokyo","Australia/Sydney","UTC"];
function allTz(){ try{ return Intl.supportedValuesOf("timeZone") }catch(e){ return COMMON_TZ } }
/* ---- the little world on the time zone setting ------------------------ */
const WX=lon=>(lon+180)*2, WY=lat=>(82.5-lat)*2, WW=720, WH=280;
function tzCoords(z){ const W=window.WORLD; return W&&W.zones[z]||null }
/* Where it is night now: the terminator from the sun's declination and the
   meridian it stands over, closed toward the pole that is in the dark. */
function nightPath(now){
  const doy=(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate())-
    Date.UTC(now.getUTCFullYear(),0,0))/864e5;
  let dec=-23.44*Math.cos(2*Math.PI/365*(doy+10));
  if(Math.abs(dec)<.3) dec=dec<0?-.3:.3;
  const sunLon=-((now.getUTCHours()+now.getUTCMinutes()/60)-12)*15, r=Math.PI/180;
  const pts=[];
  for(let lon=-180;lon<=180;lon+=3){
    const lat=Math.atan(-Math.cos((lon-sunLon)*r)/Math.tan(dec*r))/r;
    pts.push(WX(lon).toFixed(1)+","+Math.max(-4,Math.min(WH+4,WY(lat))).toFixed(1));
  }
  const edge=dec>0?WH+4:-4;
  return "M"+pts.join("L")+"L"+WX(180)+","+edge+"L"+WX(-180)+","+edge+"Z";
}
function drawTzMap(sel){
  const host=$("#tzmap"), W=window.WORLD;
  if(!host) return;
  if(!W){ host.hidden=true; return }
  const now=new Date(), zone=userTz(), here=tzCoords(zone);
  const off=tzOffset(zone,now)/60;
  let land="";
  W.rows.forEach((hex,ri)=>{
    const y=WY(W.lat0-ri*W.step);
    for(let c=0;c<hex.length;c++){ const v=parseInt(hex[c],16);
      for(let b=0;b<4;b++) if(v&(8>>b)) land+="M"+WX(W.lon0+(c*4+b)*W.step).toFixed(1)+" "+y.toFixed(1)+"h0" }
  });
  /* The band is the zone's offset as a slice of the globe: fifteen degrees an hour. */
  const bx=WX(Math.max(-180,off*15-7.5)), bw=WX(Math.min(180,off*15+7.5))-bx;
  const ivs=[...new Set((S.jobs||[]).filter(j=>j.interview_tz&&j.interview_at&&
    interviewMoment(j)>=Date.now()-864e5&&j.interview_tz!==zone).map(j=>j.interview_tz))]
    .map(z=>({z,c:tzCoords(z)})).filter(x=>x.c);
  const arc=(a,b)=>{ const x1=WX(a[0]),y1=WY(a[1]),x2=WX(b[0]),y2=WY(b[1]);
    const mx=(x1+x2)/2, my=(y1+y2)/2-Math.hypot(x2-x1,y2-y1)*.28;
    return '<path class="arc" d="M'+x1+' '+y1+'Q'+mx+' '+my+' '+x2+' '+y2+'"/>' };
  const hm=z=>new Intl.DateTimeFormat(uiLocale(),{timeZone:z,hour:"2-digit",minute:"2-digit"}).format(now);
  host.innerHTML='<svg viewBox="0 0 '+WW+' '+WH+'" role="img">'+
    '<rect class="band" x="'+bx+'" y="0" width="'+bw+'" height="'+WH+'"/>'+
    '<line class="band-edge" x1="'+bx+'" x2="'+bx+'" y1="0" y2="'+WH+'"/>'+
    '<line class="band-edge" x1="'+(bx+bw)+'" x2="'+(bx+bw)+'" y1="0" y2="'+WH+'"/>'+
    '<path class="land" d="'+land+'"/>'+
    '<path class="night" d="'+nightPath(now)+'"/>'+
    (here?ivs.map(x=>arc(here,x.c)).join(""):"")+
    ivs.map(x=>'<circle class="iv" cx="'+WX(x.c[0])+'" cy="'+WY(x.c[1])+'" r="4.5"><title>'+
      esc(tzCity(x.z)+" · "+hm(x.z))+'</title></circle>').join("")+
    (here?'<circle class="ring" cx="'+WX(here[0])+'" cy="'+WY(here[1])+'" r="7"/>'+
      '<circle class="pin" cx="'+WX(here[0])+'" cy="'+WY(here[1])+'" r="5.5"/>':"")+
    '<circle class="ghost" r="6" cx="-20" cy="-20"/></svg>'+
    (here?'<div class="card" id="tz-card"><div class="c1">'+esc(tzCity(zone))+'</div><div class="c2">'+esc(hm(zone))+
      ' · '+esc(utcLabel(zone))+'</div></div>':"")+
    '<div class="tip" hidden></div>'+
    '<div class="cap"><span>'+esc(t("Click the map to pick the nearest city."))+'</span><span class="grow"></span>'+
      (ivs.length?'<span class="sw"><i style="border:2px solid var(--acc);border-radius:50%;width:8px;height:8px"></i>'+
        esc(t("Interviews"))+'</span>':"")+
      '<span class="sw"><i style="background:var(--acc);opacity:.35"></i>'+esc(t("Your zone"))+'</span>'+
      '<span class="sw"><i style="background:var(--t900);opacity:.2"></i>'+esc(t("Night now"))+'</span></div>';
  /* The card sits beside the pin, on whichever side has room. */
  const card=$("#tz-card"), svg=host.querySelector("svg");
  const place=()=>{ if(!card||!here) return;
    const k=svg.clientWidth/WW, x=14+WX(here[0])*k, y=14+WY(here[1])*k;
    const left=x>svg.clientWidth*.6;
    card.style.left=(left?x-card.offsetWidth-14:x+14)+"px"; card.style.top=(y-card.offsetHeight/2)+"px" };
  requestAnimationFrame(place);
  const zones=allTz().map(z=>[z,tzCoords(z)]).filter(x=>x[1]);
  const nearest=(lon,lat)=>{ let best=null,bd=1e9;
    for(const [z,c] of zones){ const dl=Math.abs(c[0]-lon), dx=Math.min(dl,360-dl)*Math.cos(lat*Math.PI/180),
      d=dx*dx+(c[1]-lat)**2; if(d<bd){bd=d;best=[z,c]} } return best };
  const at=e=>{ const r=svg.getBoundingClientRect(), k=WW/r.width;
    return [(e.clientX-r.left)*k/2-180, 82.5-(e.clientY-r.top)*k/2] };
  const tip=host.querySelector(".tip"), ghost=host.querySelector(".ghost");
  svg.onmousemove=e=>{ const n=nearest(...at(e)); if(!n) return;
    const k=svg.clientWidth/WW;
    ghost.setAttribute("cx",WX(n[1][0])); ghost.setAttribute("cy",WY(n[1][1]));
    tip.hidden=false; tip.textContent=tzCity(n[0])+" · "+utcLabel(n[0]);
    tip.style.left=(14+WX(n[1][0])*k)+"px"; tip.style.top=(14+WY(n[1][1])*k)+"px" };
  svg.onmouseleave=()=>{ tip.hidden=true; ghost.setAttribute("cx",-20) };
  svg.onclick=e=>{ const n=nearest(...at(e)); if(!n||n[0]===zone) return;
    if(![...sel.options].some(o=>o.value===n[0])) sel.insertAdjacentHTML("beforeend",
      '<option value="'+esc(n[0])+'">'+esc(tzCity(n[0]))+'</option>');
    sel.value=n[0]; sel.onchange() };
}

/* A zone as people read it: "UTC−3", "UTC+5:30". Worked out once per zone. */
const TZ_UTC=new Map();
function utcLabel(z){
  if(TZ_UTC.has(z)) return TZ_UTC.get(z);
  let l="";
  try{ const m=Math.round(tzOffset(z,new Date())), a=Math.abs(m);
    l="UTC"+(m?(m<0?"\u2212":"+")+Math.floor(a/60)+(a%60?":"+String(a%60).padStart(2,"0"):""):"");
  }catch(e){}
  TZ_UTC.set(z,l); return l;
}
function tzOptions(cur,first){
  const top=[...new Set([first,...COMMON_TZ].filter(Boolean))];
  const opt=z=>'<option value="'+esc(z)+'"'+(z===cur?" selected":"")+'>'+esc(tzCity(z))+
    (utcLabel(z)?' · '+utcLabel(z):"")+'</option>';
  return top.map(opt).join("")+'<option disabled>──────────</option>'+
    allTz().filter(z=>!top.includes(z)).map(opt).join("");
}

const prettyStatus=s=>{
  const map={pending:"Draft", applied:"Awaiting reply", interviewing:"Interviewing",
    offer:"Offer", accepted:"Accepted", refused:"Declined", rejected:"Rejected",
    ghosted:"Ghosted", rejected_interviewing:"Rejected after interview",
    ghosted_interviewing:"Ghosted after interview"};
  return map[s]||String(s).replace(/_/g," ");
};
/* Live means an application can still turn into a job; dead means it cannot. */
/* One status vocabulary, read by the Jobs table and the funnel alike. Two
   views of the same data disagreeing about what a colour means is worse than
   having no colour at all: it was telling you an offer you declined and an
   offer you accepted were the same thing.

   draft   nothing sent yet            grey
   waiting sent, their move            blue
   live    a conversation is happening amber
   won     you got it                  teal
   closed  you ended it                violet
   lost    they ended it               red   */
const STATUS_TONE={
  pending:"draft", applied:"waiting", interviewing:"live", offer:"offer",
  accepted:"won", refused:"closed", rejected:"lost", ghosted:"lost",
  rejected_interviewing:"lost", ghosted_interviewing:"lost",
};
const statusTone=st=>STATUS_TONE[st]||"draft";
const DEAD_STATUS=new Set(Object.keys(STATUS_TONE).filter(
  k=>["lost","closed","draft"].includes(STATUS_TONE[k])));

/* A company's mark: its logo if one has been stored, otherwise its initials.
   Most companies will never have a logo, so the fallback is the common case and
   has to look chosen rather than missing. The tint is derived from the name, so
   a company keeps the same colour everywhere without anyone assigning one.

   These used to be the funnel's own hues -- #3a6ea5 was --fn-wait, #007a5e was
   --fn-won, #a83519 was --fn-lost -- so a 20x20 saturated square carrying a
   hash of the company name sat in the same row as a 6px dot carrying the
   status, in the same colours, meaning nothing. Contoso wore the Rejected red
   while its dot said Awaiting reply. The status palette is signal and a hash is
   not, so the marks are neutral now: they separate one row from the next
   without competing for the colour that means something. They were also
   hardcoded hexes, which left them running light-mode values in dark. */
const CO_TINTS=["var(--co-1)","var(--co-2)","var(--co-3)",
                "var(--co-4)","var(--co-5)","var(--co-6)"];
function companyTint(name){
  let h=0;
  for(const ch of String(name||"")) h=(h*31+ch.charCodeAt(0))>>>0;
  return CO_TINTS[h%CO_TINTS.length];
}
function initials(name){
  const words=String(name||"").split(/[^A-Za-z0-9]+/).filter(Boolean);
  if(!words.length) return "?";
  return (words.length>1?words[0][0]+words[1][0]:words[0].slice(0,2)).toUpperCase();
}
function companyMark(j){
  if(j.logo_url) return '<img class="colog" src="'+esc(j.logo_url)+tok()+
    '" alt="" loading="lazy">';
  return '<span class="colog mono" style="background:'+companyTint(j.company)+
    '">'+esc(initials(j.company))+'</span>';
}

/* Where a posting was found. The source is what someone said -- "LinkedIn",
   "via a recruiter on Indeed" -- so it wins over the link, which for a board
   that forwards to the company's own careers page names the wrong place.
   Boards that publish no mark anyone may use get their initials on a neutral
   tile, like a company with no logo, rather than a drawing of their logo. */
const BOARDS=[
  {id:"linkedin",label:"LinkedIn",bg:"#0A66C2",fg:"#fff",
   host:/(^|\.)linkedin\.com$/,word:/linked\s?in/i},
  {id:"indeed",label:"Indeed",bg:"#003A9B",fg:"#fff",host:/(^|\.)indeed\./,word:/indeed/i},
  {id:"glassdoor",label:"Glassdoor",bg:"#00A162",fg:"#fff",
   host:/(^|\.)glassdoor\./,word:/glassdoor/i},
  {id:"greenhouse",label:"Greenhouse",bg:"#24A47F",fg:"#fff",
   host:/(^|\.)greenhouse\.io$/,word:/greenhouse/i},
  {id:"wellfound",label:"Wellfound",bg:"#000",fg:"#fff",
   host:/(^|\.)(wellfound\.com|angel\.co)$/,word:/wellfound|angel\.?list/i},
  {id:"welcometothejungle",label:"Welcome to the Jungle",bg:"#FFCD00",fg:"#000",
   host:/(^|\.)welcometothejungle\.com$/,word:/welcome to the jungle|\bwttj\b/i},
  {id:"xing",label:"XING",bg:"#006567",fg:"#fff",host:/(^|\.)xing\.com$/,word:/\bxing\b/i},
  {id:"monster",label:"Monster",bg:"#6D4C9F",fg:"#fff",host:/(^|\.)monster\./,
   word:/\bmonster\b/i},
  {id:"ycombinator",label:"Work at a Startup",bg:"#F0652F",fg:"#fff",
   host:/(^|\.)(workatastartup|ycombinator)\.com$/,word:/y\s?combinator|work at a startup/i},
  {id:"lever",label:"Lever",letters:"Lv",host:/(^|\.)lever\.co$/,word:/\blever\b/i},
  {id:"workday",label:"Workday",letters:"Wd",
   host:/(^|\.)(myworkdayjobs|workday)\.com$/,word:/workday/i},
  {id:"ashby",label:"Ashby",letters:"As",host:/(^|\.)ashbyhq\.com$/,word:/\bashby/i},
  {id:"smartrecruiters",label:"SmartRecruiters",letters:"SR",
   host:/(^|\.)smartrecruiters\.com$/,word:/smart\s?recruiters/i},
  {id:"francetravail",label:"France Travail",letters:"FT",
   host:/(^|\.)(francetravail|pole-emploi)\.fr$/,word:/france travail|p[o\u00f4]le.emploi/i},
  {id:"apec",label:"Apec",letters:"Ap",host:/(^|\.)apec\.fr$/,word:/\bapec\b/i},
  {id:"hellowork",label:"HelloWork",letters:"HW",host:/(^|\.)hellowork\.com$/,
   word:/hello\s?work/i},
  {id:"jobteaser",label:"JobTeaser",letters:"JT",host:/(^|\.)jobteaser\.com$/,
   word:/job\s?teaser/i},
];
function jobBoard(j){
  const said=String(j.source||"");
  if(said){ const b=BOARDS.find(b=>b.word.test(said)); if(b) return b }
  let host="";
  try{ host=new URL(j.url).hostname.toLowerCase() }catch(e){}
  return host?BOARDS.find(b=>b.host.test(host))||null:null;
}
function boardMark(b){
  if(b.letters) return '<span class="board lettered mono" aria-hidden="true">'+
    esc(b.letters)+'</span>';
  return '<span class="board" aria-hidden="true" style="background:'+b.bg+';color:'+b.fg+
    '"><svg viewBox="0 0 24 24"><use href="#board-'+b.id+'"/></svg></span>';
}

/* What the tailored CV changed from the one it was copied from. The editor
   has been marking these field by field since lineage was recorded; this is
   the same list said once, on the application, where the question "what did
   I send them" gets asked. */
const DIFF_SHOWN=4;
async function fillBaseDiff(el,path){
  let d;
  try{ d=await api("/api/basediff?path="+encodeURIComponent(path)) }
  catch(e){ return }
  if(!el.isConnected||el.dataset.path!==path) return;
  const box=el.closest(".block")||el;
  const base=d.base?d.base.split("/").pop().replace(/\.ya?ml$/,""):null;
  if(!base) return;
  box.hidden=false;
  if(d.missing){
    el.innerHTML='<p class="bd-head">Copied from <b>'+esc(base)+'</b>, which is no longer '+
      'in the workspace, so there is nothing to compare it with.</p>';
    return;
  }
  const n=d.changes.length;
  const item=c=>'<li><span class="bd-where">'+esc(c.where)+
      (c.kind==="changed"?"":' <em class="bd-kind '+c.kind+'">'+c.kind+'</em>')+'</span>'+
    (c.before&&c.kind!=="added"?'<del>'+esc(c.before)+'</del>':'')+
    (c.after&&c.kind!=="removed"?'<ins>'+esc(c.after)+'</ins>':'')+'</li>';
  el.innerHTML=
    '<p class="bd-head">'+(n
      ?'<b>'+n+' change'+(n===1?"":"s")+'</b> from <b>'+esc(base)+'</b>'
      :'Same as <b>'+esc(base)+'</b> so far. Nothing has been tailored yet.')+
    (d.design.length?'<span class="bd-design">Design: '+esc(d.design.join(", "))+'</span>':'')+
    '</p>'+
    (n?'<ul class="bd-list">'+d.changes.slice(0,DIFF_SHOWN).map(item).join("")+'</ul>':'')+
    (n>DIFF_SHOWN?'<details class="bd-more"><summary>'+(n-DIFF_SHOWN)+' more</summary>'+
      '<ul class="bd-list">'+d.changes.slice(DIFF_SHOWN).map(item).join("")+'</ul></details>':'');
}

/* Links out of the app. The desktop webview ignores target=_blank -- the
   click simply went nowhere -- so outside a browser tab the server opens the
   link in the system browser instead. In a browser tab the default works and
   is left alone. */
document.addEventListener("click",e=>{
  const a=e.target.closest&&e.target.closest('a[target="_blank"]');
  if(!a||!window.__TAURI__) return;
  const href=a.href||"";
  if(!/^https?:/i.test(href)) return;
  e.preventDefault();
  post("/api/open",{url:href}).catch(err=>toast(err.message,true));
});

const appliedAt=j=>{
  const h=j.status_history||[];
  for(const e of h) if(e.status==="applied") return e.at;
  return j.status==="pending"?null:j.created_at;
};
const isoToday=()=>new Date().toISOString().slice(0,10);

/* ---- view switching ---------------------------------------------------- */
/* The app has two kinds of screen -- a list you navigate, and one document you
   work on -- so the chrome has two shapes rather than six elements hidden
   independently of each other. The gap between them is what pins the cluster
   to the right edge, and that has to hold in both or the gear moves when you
   switch. */
function setView(v){
  S.view=v;
  const doc=v==="cvs";
  ["cvs","jobs","docs","funnel","cal","letter"].forEach(k=>{ $("#v-"+k).hidden = k!==v });
  if(v!=="cal") clearInterval(S.calTick);
  if(doc) paintBackLabel();
  else if(v==="letter") ltBackLabel();
  else $$("#nav button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.view===v)));
  if(v==="jobs"){ loadJobs(); loadAlerts() }
  /* Which application a document was written for is a fact about the jobs, so
     this screen needs them too -- and you can land on it without ever having
     opened the list. Draw what is known now, fill in the rest when it lands. */
  if(v==="docs"){ S.driftBy=null; drawDocuments(); if(!S.jready) loadJobs(true) }
  if(v==="funnel") loadFunnel();
  if(v==="cal") openCalendar();
  paintStatus();
}
/* Out of the editor, to the application the open document was written for.
   Derived from the link rather than remembered as history: a stack can go
   stale and this cannot, and "this document belongs to Acme" is the relation
   that is actually true. With nothing linked it is the list you left, which
   still has whatever you had selected -- S.jsel survives the trip. */
function goBack(){
  const j=linkedJob();
  if(j){ setView("jobs"); selectJob(j.id); return }
  setView(S.fromList==="docs"?"docs":"jobs");
}
/* The crumb says where it goes, which is not always the applications, and
   the tab for that list stays lit while you are in the editor. */
function paintBackLabel(){
  const to=(!linkedJob()&&S.fromList==="docs")?"docs":"jobs";
  $("#back").textContent = to==="docs" ? "Documents" : "Applications";
  $$("#nav button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.view===to)));
}
$("#back").onclick=goBack;
$$("#nav button").forEach(b=>b.onclick=()=>{ closeOverlays(); setView(b.dataset.view) });

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
    /* The right of the status bar is where the workspace lives, and what Claude
       last did in it belongs in the same place -- it is the other thing acting
       on these files. It gives way to the path once it goes stale. */
    /* A resolved conflict outranks the raw activity line: after you choose,
       the footer has to report your decision, not keep reporting their edit. */
    const done=S.resolved&&(Date.now()-S.resolved.at)<900000?S.resolved:null;
    if(done){
      R.classList.add("said");
      R.textContent=done.kept==="mine"
        ? "You kept your version over "+done.who+"'s change"
        : "You took "+done.who+"'s version";
      R.title="";
      return;
    }
    R.classList.remove("said");
    R.classList.toggle("blocked",!!(S.pulse&&S.pulse.mcp&&
      S.pulse.mcp.last&&S.pulse.mcp.last.ok===false));
    const last=S.pulse&&S.pulse.mcp&&S.pulse.mcp.last;
    R.textContent=last&&(Date.now()/1000-last.at)<900
      ? clientName(last)+" · "+(last.ok===false?"refused ":"")+last.tool+(last.path?" · "+last.path:"")+
        " · "+ago(last.at*1000)+" ago"
      : shortPath((S.state&&S.state.workspace)||"");
    R.title=(S.state&&S.state.workspace)||"";
  }else if(S.view==="jobs"){
    L.className="mono";
    L.textContent=S.jobs.length+" application"+(S.jobs.length===1?"":"s")+
      (S.jsel?" · 1 selected":"");
    R.textContent="applications.db";
  }else if(S.view==="docs"){
    /* The applications view names the store its rows live in; these rows live
       in the workspace folder, so that is what belongs in the same slot. */
    const n=((S.state&&S.state.documents)||[]).length;
    L.className="mono";
    L.textContent=n+" document"+(n===1?"":"s");
    R.textContent=(S.state&&S.state.workspace)||"";
  }else{
    /* The chart says how to use it, in its own header; the footer only says
       which applications it is drawn from. */
    L.className="mono";
    L.textContent=S.funnel?S.funnel.totals.total+" applications":"";
    R.textContent="applications.db";
  }
}
function ago(t){
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<60) return s+"s";
  if(s<3600) return Math.round(s/60)+"m";
  return Math.round(s/3600)+"h";
}
setInterval(()=>{ if(S.view==="cvs"&&!S.dirty&&S.savedAt) paintStatus() },10000);

/* ---- Claude -------------------------------------------------------------
   The MCP server is this same program in another process, started and owned by
   Claude Desktop, so the app cannot talk to it. What the two halves do share is
   the workspace folder and Claude's config file, and those answer the only two
   questions worth asking: is it wired up, and what has it been doing. */
const AI_STATE={
  connected:"Connected to this workspace.",
  absent:"Not set up yet. One click adds it to the config.",
  elsewhere:"Configured, but pointing at a different copy of CV Studio.",
  "other-workspace":"Configured, but pointing at a different workspace.",
  unreadable:"The config file could not be read.",
  unknown:"Checking…",
};
const aiClient=id=>(S.ai||[]).find(c=>c.id===id)||null;
/* Every tool call now says which client made it, so naming one is a lookup
   rather than a deduction. loneClient stays as the answer for a call recorded
   before any of this existed, and for a client that names itself something
   nobody here recognises. */
function loneClient(){
  const on=(S.ai||[]).filter(c=>c.state==="connected");
  return on.length===1?on[0].label:null;
}
function clientName(entry){
  if(entry&&entry.by&&entry.by!=="ai"){
    const c=aiClient(entry.by);
    if(c) return c.label;
  }
  return (entry&&entry.agent)||loneClient()||"An AI client";
}

async function loadAI(){
  try{ S.ai=(await api("/api/ai")).clients }
  catch(e){ S.ai=null }
  paintAI();
}
function paintAI(){
  const bits=[];
  $$("#btn-ai .aic").forEach(el=>{
    const c=aiClient(el.dataset.client), st=(c&&c.state)||"unknown";
    el.dataset.state=st;
    bits.push((c?c.label:el.dataset.client)+": "+
      (c&&st==="connected"&&c.last_seen
        ? "Connected. Last heard from "+ago(c.last_seen*1000)+" ago."
        : c&&st==="connected" ? "Set up, but it has not called in yet."
        : AI_STATE[st]));
  });
  $("#btn-ai").title=bits.join("\n");
  if(!$("#ovl-settings").hidden) fillAIPanel();
}
/* The marks in the title bar are the AI surface's own entry point, so they go
   straight to it rather than opening Settings and then navigating. */
$("#btn-ai").onclick=()=>openSettings("ai");

/* A short label for the pill, and a single line of plain English under the
   name. The long version of any of this belongs in the title, not the card. */
const AI_PILL={
  connected:"Connected", absent:"Not set up", elsewhere:"Another copy",
  "other-workspace":"Another workspace", unreadable:"Unreadable",
  unknown:"Checking",
};
/* "Configured" and "Connected" are different claims and the card should not
   make the second on the strength of the first. */
const aiPill=c=>c.state==="connected"&&!c.last_seen?"Configured":AI_PILL[c.state];
function aiSay(c){
  if(c.state==="connected"){
    /* A config file says a client has been *told* where the server is, not
       that it ever started it. A tool call is the only evidence the handshake
       actually happened, so the card reports that instead of implying it. */
    if(c.last_seen)
      return "Last heard from "+ago(c.last_seen*1000)+" ago"+
        (c.agent&&c.agent!==c.label?" ("+c.agent+")":"")+".";
    return "Set up, but it has not called in yet. Restart it: "+
      c.restart.replace(/^Restart /,"restart ").replace(/^Start /,"start ");
  }
  /* Never "one click". The click writes a config file; the client only picks
     it up when it is restarted, and until then nothing is connected. Promising
     one click and then putting the step that completes it in a toast -- the
     most disposable container in the app -- is most of why this felt clunky. */
  if(c.state==="absent") return "Not set up yet. The steps are below.";
  if(c.state==="elsewhere") return "Pointing at another copy of CV Studio, so "+
    "it is editing CVs you are not looking at.";
  if(c.state==="other-workspace")
    return "Pointing at "+shortPath(c.workspace)+", so it is editing CVs you "+
      "are not looking at.";
  if(c.state==="unreadable") return c.error||"Its config file could not be read.";
  return "Checking…";
}
/* The two steps, on the card, before the first click rather than after it.
   Step 2 is the one that actually connects anything, and it used to exist only
   in a toast that fired once and vanished. */
function aiSteps(c){
  if(c.state==="unreadable") return "";
  const wrote=c.state==="connected";
  const live=wrote&&c.last_seen;
  const step=(n,done,text)=>'<li'+(done?' class="done"':"")+'><i>'+
    (done?"&#10003;":n)+'</i><span>'+text+'</span></li>';
  return '<ol class="aisteps">'+
    step(1,wrote,wrote?"Added to its config":"Add this workspace to its config")+
    step(2,live,esc(c.restart))+
    /* Only a client that has been configured is actually waiting on anything.
       One that was never set up is not pending, it is untouched. */
    '<li'+(live?' class="done"':wrote?' class="wait"':"")+'><i>'+(live?"&#10003;":"3")+
      '</i><span>'+(live
        ? "Heard from it "+ago(c.last_seen*1000)+" ago"
        : wrote ? "Waiting for its first call\u2026"
                : "It calls in, and this turns green")+'</span></li>'+
    '</ol>';
}

/* Paths here are long enough to swallow the card, and the end is the part that
   identifies them, so keep the tail and let CSS trim the head. */
function shortPath(p){
  const bits=String(p||"").split(/[\\/]/).filter(Boolean);
  return bits.length<=2?String(p||""):"…/"+bits.slice(-2).join("/");
}

function fillAIPanel(){
  const clients=S.ai||[];
  $("#s-ai-clients").innerHTML=clients.map(c=>{
    const st=c.state;
    const wrong=st==="elsewhere"||st==="other-workspace"||st==="unreadable";
    return '<div class="client'+(wrong?" wrong":"")+'" data-client="'+c.id+'">'+
      '<span class="badge"><svg width="19" height="19" viewBox="0 0 24 24"'+
        ' aria-hidden="true"><use href="#'+c.id+'-mark"/></svg></span>'+
      '<div class="who"><b>'+esc(c.label)+'</b>'+
        '<span class="pill" data-state="'+(st==="connected"&&!c.last_seen?"unknown":st)+
          '"><i></i>'+aiPill(c)+'</span></div>'+
      '<div class="say">'+esc(aiSay(c))+'</div>'+
      '<div class="go"><button class="obtn" data-connect="'+c.id+'">'+
        (st==="connected"?"Set up again"
          :st==="absent"?"Add to its config":"Point it at this workspace")+
        '</button></div>'+
      aiSteps(c)+
      '<div class="path"><span title="'+esc(c.config_path)+'">'+
        esc(shortPath(c.config_path))+'</span>'+
        '<button data-copy-path="'+esc(c.config_path)+
        '" title="Copy the full path">Copy path</button>'+
      '</div></div>';
  }).join("")||'<p class="sp-note">Checking…</p>';

  $$("#s-ai-clients [data-connect]").forEach(b=>b.onclick=async()=>{
    const c=aiClient(b.dataset.connect);
    b.disabled=true; b.textContent="Setting up…";
    try{
      const r=await post("/api/ai/connect",{client:b.dataset.connect});
      await loadAI();
      toast(r.action==="unchanged" ? "Already set up."
        : (c?c.label:"Done")+" is connected. "+(r.restart||""));
    }catch(e){ toast(e.message,true); await loadAI() }
  });
  /* Not a reveal: these files live outside the workspace, and /api/reveal is
     deliberately confined to it. The path itself is the useful thing. */
  $$("#s-ai-clients [data-copy-path]").forEach(b=>b.onclick=async()=>{
    try{ await navigator.clipboard.writeText(b.dataset.copyPath); toast("Copied") }
    catch(e){ toast("Select the path and copy manually",true) }
  });

  $("#s-ai-manual").innerHTML=clients.map(c=>
    '<h4>'+esc(c.label)+'</h4><p class="sp-note" style="margin:0 0 8px">'+
    esc(c.manual)+' Put this in <code>'+esc(c.config_path)+'</code>, then '+
    esc(c.restart)+'</p><pre class="code">'+esc(c.snippet)+'</pre>').join("");
  paintAILog();
  loadSkills();
}
/* Claude Code already reads these off disk; the desktop app cannot, so the
   button packages them for upload rather than pretending to install them. */
async function loadSkills(){
  try{ S.skills=await api("/api/skills") }catch(e){ S.skills=null }
  paintSkills();
}
function paintSkills(){
  const d=S.skills, list=(d&&d.skills)||[];
  $("#s-skills").innerHTML=list.length
    ? list.map(k=>'<div><span class="nm">'+esc(k.name)+'</span>'+
        '<span class="ds">'+esc(k.description)+'</span>'+
        '<span class="tag'+(k.needs_mcp?" mcp":"")+'">'+
        (k.needs_mcp?"needs the tools":"travels as is")+'</span></div>').join("")
    : '<div><span class="ds">None found'+(d?" in "+esc(d.source):"")+
      '. They come with the Claude Code setup.</span></div>';
  const packed=list.some(k=>k.packaged);
  $("#s-skill-show").hidden=!packed;
  $("#s-skill-steps").hidden=!packed;
  const pack=$("#s-skill-pack");
  pack.disabled=!list.length;
  pack.textContent=packed?"Package again":"Package for Claude Desktop";
  pack.onclick=async()=>{
    pack.disabled=true; pack.textContent="Packaging…";
    try{
      const r=await post("/api/skills/package",{});
      await loadSkills();
      toast(r.skills.length+" skills ready to upload in "+r.dir);
    }catch(e){ toast(e.message,true); await loadSkills() }
  };
  $("#s-skill-show").onclick=async()=>{
    try{ await post("/api/reveal",{path:(S.skills&&S.skills.out_dir)||""}) }
    catch(e){ toast(e.message,true) }
  };
}
/* Which tool calls changed something. The prose above this log warns that
   these write immediately and there is no undo, so the log has to tell the two
   kinds apart rather than styling a read like a write. */
const WRITE_TOOLS=/^(write_cv|edit_cv_fields|create_cv|set_company_logo|set_job_status|update_job_tracking|add_job)$/;

/* Kept apart from the rest of the panel so the poll can refresh it without
   rebuilding the buttons under the cursor. */
function paintAILog(){
  const log=(S.pulse&&S.pulse.mcp&&S.pulse.mcp.recent)||[];
  $("#s-cl-log").innerHTML=log.length
    /* Only the calls that wrote something carry the mark. Putting it on
       read_cv and list_cvs made the glyph mean "a client called a tool", which
       is not what it means anywhere else in the app, and with one client
       connected the column was constant anyway. */
    ? log.map(r=>{
        const wrote=WRITE_TOOLS.test(r.tool);
        return '<div'+(r.ok===false?' class="no"':"")+'>'+
        (wrote ? markHTML({by:r.by||"ai",at:r.at,agent:r.agent},null)
               : '<i class="roi" aria-hidden="true"></i>')+
        '<span class="t">'+(r.ok===false?"refused ":"")+esc(r.tool)+'</span>'+
        '<span class="p">'+esc(r.path||"")+'</span>'+
        '<span class="w" title="'+esc(clientName(r))+'">'+
        ago(r.at*1000)+' ago</span></div>';
      }).join("")
    : '<div><span class="none">Nothing yet. What a model does in this workspace '+
      'shows up here.</span></div>';
}

/* ---- provenance ---------------------------------------------------------
   Who last wrote each field, and which fields no longer say what the base CV
   says. Both arrive on the document itself, from a sidecar the server keeps;
   neither is in the YAML, so neither can reach the rendered page.

   Field addresses are the dotted form of the same path the inspector already
   binds its inputs to, so a mark is a lookup rather than a search. */
const PROV_LABEL={claude:"Claude",openai:"OpenAI",mistral:"Mistral",
  hermes:"Hermes",ai:"An AI client",you:"You"};
const provKey=path=>path.join(".");
function provOf(path){
  const f=S.prov&&S.prov.fields;
  return (f&&f[provKey(path)])||null;
}
function fromBase(path){
  return !!(S.prov&&S.prov.baseSet&&S.prov.baseSet.has(provKey(path)));
}
/* True when anything *under* this path has been touched, which is what an
   outline row needs: a section is marked because one of its bullets was. */
function provUnder(prefix){
  const f=S.prov&&S.prov.fields;
  if(!f) return null;
  const head=provKey(prefix)+".";
  let best=null;
  for(const k in f){
    if(k!==provKey(prefix)&&k.indexOf(head)!==0) continue;
    if(f[k].by==="you") continue;
    if(!best||(f[k].at||0)>(best.at||0)) best=f[k];
  }
  return best;
}
function provHeader(){
  let best=null;
  for(const k of HEADER_KEYS){
    const p=provOf(["cv",k]);
    if(p&&p.by!=="you"&&(!best||(p.at||0)>(best.at||0))) best=p;
  }
  return best;
}
function baseHeader(){
  return HEADER_KEYS.some(k=>fromBase(["cv",k]));
}
function baseUnder(prefix){
  const set=S.prov&&S.prov.baseSet;
  if(!set) return false;
  const head=provKey(prefix)+".";
  for(const k of set) if(k===provKey(prefix)||k.indexOf(head)===0) return true;
  return false;
}
function whoLabel(p){
  return (p&&(PROV_LABEL[p.by]||p.agent||"An AI client"))||"";
}
/* The mark itself. Your own edits draw nothing: the whole point is to pick out
   what you did not write, and marking everything marks nothing. */
/* `rolled` means this mark stands for something underneath rather than for the
   field it sits on: a section is marked because one of its bullets was. The
   distinction matters because the two are not the same claim -- a rolled-up
   block usually also contains your own writing -- and drawing them identically
   made the feature true at the leaf and wrong at every level above it. So a
   rolled mark is hollow and quieter, and says "contains" rather than "changed
   this". */
function markHTML(p,path,rolled){
  let out="";
  if(p&&p.by&&p.by!=="you"){
    const who=whoLabel(p);
    const said=rolled
      ? who+" wrote something in here, "+ago(p.at*1000)+" ago"
      : who+" changed this "+ago(p.at*1000)+" ago"+
        (p.from==null?"":"\nwas: "+String(p.from));
    out+='<span class="pmark'+(rolled?" rolled":"")+'" data-by="'+esc(p.by)+
      '" role="img" aria-label="'+esc(said)+'" title="'+esc(said)+
      '">'+(p.by==="ai"?"&#9679;":
        '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#'+
        esc(p.by)+'-mark"/></svg>')+'</span>';
  }
  if(path&&fromBase(path))
    out+=basebar();
  return out;
}
/* Announced, not just hovered: the rule is the only carrier of its meaning, so
   leaving it as an empty <i> told a screen reader nothing at all. */
const basebar=()=>'<i class="fromb" role="img" aria-label="Differs from '+
  esc(baseName())+'" title="Differs from '+esc(baseName())+'"></i>';
function valueText(v){
  if(v==null) return "(empty)";
  if(Array.isArray(v)) return v.join(" · ");
  if(typeof v==="object") return JSON.stringify(v);
  return String(v);
}
const baseName=()=>{
  const b=S.prov&&S.prov.base;
  return b?b.path.split("/").pop().replace(/\.ya?ml$/,""):"the base CV";
};

function setProv(prov){
  S.prov=prov||null;
  if(S.prov) S.prov.baseSet=new Set(S.prov.from_base||[]);
  paintProv();
}

/* The base says so. A tailored copy has always said what it came from; the
   document it came from said nothing, so the one CV every other one is copied
   from looked like any other file while you edited it. */
const isBase=p=>!!(p&&S.state&&S.state.base&&S.state.base.path===p);
function paintBaseChip(){
  const chip=$("#basechip");
  if(!chip) return;
  if(!isBase(S.path)){ chip.hidden=true; return }
  const n=((S.state&&S.state.documents)||[]).filter(d=>d.base===S.path).length;
  chip.innerHTML='<span class="btag">Base CV</span>'+
    '<span>'+(n?'<b>'+n+'</b> tailored from it':'every tailored CV starts as a copy of it')+
    '</span>';
  chip.title="Changes here reach the next CV you tailor, not the ones already "+
    "copied. Click to see every document.";
  chip.hidden=false;
  chip.onclick=()=>{
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    setView("docs");
  };
}

/* The chip: the whole document's answer, on every tab. */
/* One chip for the whole document, beside the provenance one, rather than a
   card repeated inside every block's editor. */
function paintLink(){
  const chip=$("#linkchip"), j=linkedJob();
  /* The same answer the chip is about to draw decides where Back goes, and it
     is only knowable once the document and the applications have both landed
     -- which is here, not in setView. */
  paintBackLabel();
  paintBaseChip();
  if(!S.path||!S.jready){ chip.hidden=true; return }
  if(!j){
    chip.innerHTML='<span>Link to an application</span>';
    chip.title="Attach this document to the application it was written for.";
    chip.hidden=false;
    chip.onclick=()=>linkJobSheet();
    return;
  }
  chip.innerHTML='<span class="dot '+statusTone(j.status)+'"></span>'+
    '<span>for <b>'+esc(j.company)+'</b></span>'+
    '<span class="dot"></span><span>'+esc(prettyStatus(j.status))+'</span>';
  chip.title="Linked to an application. Click to open it, move it or unlink.";
  chip.hidden=false;
  chip.onclick=()=>linkJobSheet();
}

function paintProv(){
  const chip=$("#provchip"), pv=S.prov;
  if(!pv||(!pv.base&&!pv.last_ai)){ chip.hidden=true; return }
  const bits=[];
  if(pv.base)
    bits.push('<span>from <b>'+esc(baseName())+'</b></span>',
      '<span class="dot"></span>',
      /* The logo teaches itself by sitting next to the word "Claude". The
         divergence rule had no such anchor anywhere in the product, so it gets
         one here: this is the only place both marks appear beside the words
         that define them. */
      '<span><i class="fromb"></i> <b>'+pv.from_base.length+
        '</b> differ from base</span>');
  if(pv.last_ai){
    if(bits.length) bits.push('<span class="dot"></span>');
    bits.push(markHTML(pv.last_ai,null,true)+'<span>'+esc(whoLabel(pv.last_ai))+', '+
      ago(pv.last_ai.at*1000)+' ago</span>');
  }
  chip.innerHTML=bits.join("");
  chip.title="What this document owes to something other than your own typing";
  chip.hidden=false;
}
$("#provchip").onclick=()=>provSheet();

/* The long answer. Every field that differs from the base, and every field an
   AI client wrote, as the names you are actually looking at rather than as
   paths. Clicking one selects it, which is the point of listing them. */
function provSheet(){
  const pv=S.prov||{};
  const seen=new Set(), rows=[];
  const add=key=>{
    if(seen.has(key)) return;
    seen.add(key);
    const path=key.split(".").map(k=>/^\d+$/.test(k)?+k:k);
    const p=(pv.fields||{})[key];
    rows.push({key,path,p});
  };
  (pv.from_base||[]).forEach(add);
  Object.keys(pv.fields||{}).forEach(k=>{ if(pv.fields[k].by!=="you") add(k) });
  rows.sort((a,b)=>((b.p&&b.p.at)||0)-((a.p&&a.p.at)||0));

  /* A row carries both facts, because they are independent: a field can be
     Claude's and match the base, or yours and differ from it. Showing only
     whichever one happened to be true first hid the divergence rule from the
     one screen whose job is to explain it. */
  const body=rows.length?rows.map(r=>{
    const now=getAt(S.data,r.path);
    const was=r.p&&r.p.from!=null?String(r.p.from):null;
    const who=r.p?markHTML(r.p,null)+esc(whoLabel(r.p))+", "+ago(r.p.at*1000)+" ago"
                 :'<span class="mine">your own edit</span>';
    return '<button class="r" type="button" data-sel="'+
        esc(JSON.stringify(r.path))+'">'+
      '<span class="w">'+who+
        (pv.baseSet&&pv.baseSet.has(r.key)?basebar()+'<span>differs</span>':"")+
      '</span>'+
      '<span class="f"><b>'+esc(fieldLabel(r.path.slice(1),S.data))+'</b>'+
      /* The new value first and in full weight. Showing only the struck-out
         old one answered "what did it used to say", which is not the question
         the list is titled after. */
      '<em>'+esc(valueText(now))+'</em>'+
      (was!==null&&was!==""&&was!==valueText(now)?'<s>'+esc(was)+'</s>':"")+
      '</span><span class="go" aria-hidden="true">&#8250;</span></button>';
  }).join("")
    :'<div class="none">Nothing but your own typing.</div>';

  openSheet('<div><h3 id="sheet-title">What is not your own typing</h3><p>'+
    (pv.base?'Tailored from <b>'+esc(baseName())+'</b>'+
      (pv.base.missing?', which is no longer there, so the comparison is '+
        'missing and only the edits below are shown.':'. ')
      :'')+
    'None of this is written into the YAML, so none of it prints.</p></div>'+
    '<div class="provlist">'+body+'</div>'+
    '<div class="foot"><button class="sbtn primary" data-cancel>Close</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $$("#sheet [data-sel]").forEach(el=>el.onclick=()=>{
    const path=JSON.parse(el.dataset.sel);
    if(path[1]==="sections"&&path.length>3)
      select({kind:"entry",name:path[2],i:+path[3]});
    else select({kind:"header"});
    /* The sheet stays open. Closing it after every row made the list a
       one-shot: you could go to one change, and then you were back where you
       started with nothing to compare against. */
  });
}

/* Every block of the document, in the order it renders, so the arrows mean
   "the one after this" rather than "the next thing in some map". */
function blockOrder(){
  const cv=S.data&&S.data.cv;
  if(!cv) return [];
  const out=[{kind:"header"}];
  for(const name of Object.keys(cv.sections||{})){
    const list=cv.sections[name]||[];
    if(!list.length) out.push({kind:"section",name:name});
    else list.forEach((_,i)=>out.push({kind:"entry",name:name,i:i}));
  }
  return out;
}
const sameSel=(a,b)=>!!a&&!!b&&a.kind===b.kind&&a.name===b.name&&a.i===b.i;
function editorStep(delta){
  const all=blockOrder();
  const at=all.findIndex(x=>sameSel(x,S.sel));
  const next=all[at+delta];
  if(at<0||!next) return;
  select(next);
  if($("#ed").hidden) openEditor();
}
$("#ed-prev").onclick=()=>editorStep(-1);
$("#ed-next").onclick=()=>editorStep(1);
$("#ed-close").onclick=closeEditor;

/* Dismissal. A click inside the editor, on a block, or on the outline is not
   a dismissal -- those are all ways of carrying on editing. */
document.addEventListener("pointerdown",e=>{
  if($("#ed").hidden||S.view!=="cvs") return;
  if(e.target.closest("#ed,.hit,#outline,#doclist,#edtabs")) return;
  closeEditor();
});

$("#pane-page").addEventListener("scroll",()=>{
  /* Fixed to the window, so scrolling the sheet moves the block out from under
     it; this walks the card back alongside. */
  if(!$("#ed").hidden) placeEditor();
});

/* ---- the workspace changing underneath us -------------------------------
   Claude edits the same files this app has open, so the editor has to assume
   it is not the only writer. Polling one stat per document is cheap, and it is
   the difference between picking up the model's work and silently saving over
   it. */
async function pulse(){
  if(document.hidden) return;
  let p;
  try{ p=await api("/api/pulse") }catch(e){ return }
  const before=S.pulse;
  S.pulse=p;
  if(S.view==="cvs") paintStatus();
  if(!$("#ovl-settings").hidden&&!$("#sp-ai").hidden) paintAILog();
  if(!before) return;

  /* An application changed in another process, which means an AI client moved
     a status while the table was open. Reload rather than leaving it stale:
     without this the row still reads "applied" until the user navigates. */
  if(before.jobs!==p.jobs){
    loadJobs(true);
    loadAlerts();
  }

  /* A new mark without a new file: an AI client can change the base CV this
     one is compared against, which moves what "differs from the base" means
     here without touching this file at all. */
  if(before.edits!==p.edits&&S.path&&!S.dirty){
    try{ setProv((await api("/api/doc?path="+encodeURIComponent(S.path))).prov);
         buildOutline(); buildInspector(); }catch(e){}
  }

  /* A document appearing or disappearing means Claude created or removed one.
     The edits stamp covers the base as well, since the nomination lives in the
     same sidecar -- so a base set from an AI client shows up here within a
     poll rather than waiting for a reload. */
  const names=o=>JSON.stringify(Object.keys(o.docs).sort());
  if(names(before)!==names(p)||before.edits!==p.edits){
    try{
      const st=await api("/api/state");
      S.state=st; renderDocs(st.documents); paintBase();
    }catch(e){}
  }
  const bp=S.state&&S.state.base&&S.state.base.path;
  if(bp&&bp!==S.path&&before.docs[bp]!==p.docs[bp]) baseThumb(true);
  if(LT.path&&before.docs[LT.path]!==p.docs[LT.path]) ltExternal(p.docs[LT.path]);
  if(!S.path) return;
  const now=p.docs[S.path];
  if(now===undefined||S.docMtime==null||now<=S.docMtime+1e-6) return;
  if(S.dirty){
    /* Fetch their version so the bar can say which fields moved rather than
       just that the file did. Failing that, still warn -- silently losing the
       user's work would be far worse than a vaguer message. */
    let theirs=null;
    try{ theirs=await api("/api/doc?path="+encodeURIComponent(S.path)) }catch(e){}
    showExternalChange(now,theirs);
    return;
  }
  S.docMtime=now;
  await reopenInPlace();
  toast(whoChanged(p)+" updated this file");
}
/* Which client wrote the file that just moved underneath us. The tool call
   that did it carries its own name now, so this is no longer a guess hedged
   behind "an AI client" the moment two were configured. */
function byAI(p){
  const last=p&&p.mcp&&p.mcp.last;
  return !!last&&(Date.now()/1000-last.at)<20;
}
function whoChanged(p){
  if(!byAI(p)) return "Something else";
  return clientName(p.mcp.last);
}

/* Reload without losing your place: same selection, same page, same zoom. */
async function reopenInPlace(){
  const keep={sel:S.sel, open:S.openSection, page:S.page,
              zoom:S.zoom, zoomAuto:S.zoomAuto, tab:S.tab};
  await openDoc(S.path);
  S.page=keep.page; S.zoom=keep.zoom; S.zoomAuto=keep.zoomAuto;
  S.openSection=keep.open;
  if(keep.sel) select(keep.sel);
  hideExternalChange();
}

/* What the model actually changed, as field names rather than a file mtime.
   "Something changed" is not enough to choose between your work and its. */
function changedFields(mine,theirs){
  const out=[];
  const walk=(a,b,path)=>{
    if(out.length>6) return;
    const keys=new Set([...Object.keys(a||{}),...Object.keys(b||{})]);
    for(const k of keys){
      const av=(a||{})[k], bv=(b||{})[k];
      const here=path.concat(k);
      const obj=v=>v&&typeof v==="object";
      if(obj(av)&&obj(bv)&&!Array.isArray(av)&&!Array.isArray(bv)) walk(av,bv,here);
      else if(JSON.stringify(av)!==JSON.stringify(bv)) out.push(here);
    }
  };
  walk((mine||{}).cv,(theirs||{}).cv,[]);
  return out;
}
/* "sections.experience.0.company" is precise and unreadable; "Experience ·
   Northwind" is what the user is actually looking at. */
function fieldLabel(path,data){
  if(path[0]==="sections"){
    const [,name,i,key,at]=path;
    const it=(((data||{}).cv||{}).sections||{})[name];
    const entry=it&&it[i];
    const who=entry!==undefined?entryTitle(entry,+i||0):null;
    /* The position inside the list, when there is one. Without it two bullets
       of the same entry produce the same label, and a list of changes shows
       what looks like a duplicated row. */
    const nth=at==null?"":" "+(+at+1);
    return sectionLabel(name)+(who?" · "+who:"")+
      (key?" · "+String(key).replace(/_/g," ")+nth:"");
  }
  return String(path[path.length-1]).replace(/_/g," ");
}
function describeChange(mine,theirs){
  const fields=changedFields(mine,theirs);
  if(!fields.length) return "";
  const names=fields.slice(0,2).map(f=>fieldLabel(f,theirs));
  const rest=fields.length-names.length;
  return names.join(", ")+(rest>0?" and "+rest+" more":"");
}

function showExternalChange(mtime,theirs){
  S.extMtime=mtime;
  S.extTheirs=theirs||null;
  const who=whoChanged(S.pulse);
  const what=theirs?describeChange(S.data,theirs.data):"";
  $("#extbar-msg").innerHTML=esc(who)+" changed "+
    (what?"<b>"+esc(what)+"</b>":"this file")+" while you were editing.";
  $("#extbar").hidden=false;
}
function hideExternalChange(){
  S.extMtime=null; S.extTheirs=null; $("#extbar").hidden=true;
}
/* Taking theirs throws away work you have not saved, so it says so and is the
   quieter of the two. Keeping yours is the one that loses nothing. */
$("#ext-theirs").onclick=async()=>{
  S.dirty=false;
  await reopenInPlace();
  S.resolved={kept:"theirs", who:whoChanged(S.pulse), at:Date.now()};
  paintStatus();
};
$("#ext-keep").onclick=()=>{
  S.docMtime=S.extMtime;
  S.resolved={kept:"mine", who:whoChanged(S.pulse), at:Date.now()};
  hideExternalChange();
  paintStatus();
};

document.addEventListener("visibilitychange",()=>{ if(!document.hidden) pulse() });
window.addEventListener("focus",pulse);

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
    if(S.view==="cvs"&&!$("#ed").hidden){ e.preventDefault(); return closeEditor() }
    /* Last in the chain: once the sheet, the overlays and the block editor
       have each had their turn, Escape in the editor is the way back out of
       it. Not while typing -- Escape in a field belongs to the field. */
    if(S.view==="cvs"){
      const el=document.activeElement;
      if(el&&/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      e.preventDefault();
      return goBack();
    }
  }
  /* Arrows walk the document while the editor is open, so you can read a CV
     block by block without reaching for the outline. Out of the way of a
     field being typed in. */
  if(S.view==="cvs"&&!$("#ed").hidden&&
     (e.key==="ArrowUp"||e.key==="ArrowDown")){
    const el=document.activeElement;
    if(el&&el.closest("#ed")&&/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
    e.preventDefault();
    return editorStep(e.key==="ArrowDown"?1:-1);
  }
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){ e.preventDefault(); save() }
});
window.addEventListener("beforeunload",e=>{if(S.dirty){e.preventDefault();e.returnValue=""}});

/* ---- boot --------------------------------------------------------------- */
async function boot(){
  let d;
  try{ d=await api("/api/state") }catch(e){ return window.studioError(e.message) }
  S.state=d;
  /* Back where you left off. Which half of the split you work in is a habit,
     not a per-document setting, so it is remembered with the other
     per-machine conveniences. */
  const was=prefs().tab;
  if(was==="form"||was==="yaml") showTab(was);
  renderDocs(d.documents);
  /* Home is the applications list. setView makes the jobs calls itself, and
     letting it do so un-quieted is the point: a jobs store that will not open
     is now a broken home screen, which should say so rather than wait to be
     visited. */
  setView("jobs");
  paintBase();
  /* No document is open, and none can be reached except by opening one, so
     there is no empty editor to write anything into. */
  $("#btn-render").disabled=true;
  buildOutline();   /* nothing is open, so the Outline heading goes too */
  loadAI();
  pulse();
  setInterval(pulse,2500);
  paintStatus();
  const pill=$("#samp-pill");
  pill.hidden=!d.sample; pill.onclick=()=>setSample(false,pill);
  if(!d.sample&&shouldOnboard(d)) onboardingSheet();
  /* A time zone change reloads the page; come back to where it was made. */
  let back=null; try{ back=sessionStorage.getItem("cvs.reopen"); sessionStorage.removeItem("cvs.reopen") }catch(e){}
  if(back) openSettings(back);
}
/* Into the sample folder, or back to your own. Everything on screen belongs
   to one workspace, so the page starts over in the other one. */
async function setSample(on,btn){
  if(btn){ btn.disabled=true; if(on) btn.textContent="Making it…" }
  try{
    const r=await post("/api/sample",{on});
    if(!r.ok) throw new Error(r.error||"Could not switch");
    location.reload();
  }catch(e){ if(btn) btn.disabled=false; toast(e.message,true) }
}

/* =========================================================================
   ATS check
   ========================================================================= */
/* What an applicant tracking system reads out of the PDF, beside the page it
   came from. The parsing checks read the real file; the keywords are a count
   of what the posting asks for and whether the CV says it, and are labelled
   as a heuristic because they are one. */
const ATS={path:null, job:null, paste:"", busy:false};
function atsJobs(){
  return (S.jobs||[]).filter(j=>j.description);
}
async function atsSheet(path,jobId){
  if(!path) return;
  if(!S.jready){ try{ await loadJobs(true) }catch(e){} }
  ATS.path=path; ATS.paste="";
  const linked=(S.jobs||[]).find(j=>j.cv_path===path);
  ATS.job=jobId||(linked&&linked.description?linked.id:"")||"";
  const withPosting=atsJobs();
  const name=path.split("/").pop().replace(/\.ya?ml$/,"");
  $("#sheet").classList.add("ats");
  openSheet(
    '<div class="ats-top"><div><h3 id="sheet-title">ATS check</h3>'+
      '<p>What an applicant tracking system reads from <b>'+esc(name)+'</b>’s PDF, '+
      'and which of a posting’s keywords it uses.</p></div>'+
      '<label class="ats-vs">Against <select id="ats-job">'+
        '<option value="">No posting</option>'+
        withPosting.map(j=>'<option value="'+esc(j.id)+'"'+(j.id===ATS.job?" selected":"")+'>'+
          esc(j.company)+' · '+esc(j.title)+'</option>').join("")+
        '<option value="__paste">Paste a posting…</option>'+
      '</select></label></div>'+
    '<textarea id="ats-paste" class="ats-paste" hidden placeholder="Paste the job posting here"></textarea>'+
    '<div class="ats-body" id="ats-body"><p class="sp-note">Reading the PDF…</p></div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Close</button>'+
      '<button class="sbtn" id="ats-run">Check again</button></div>',
    ()=>$("#sheet").classList.remove("ats"));
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#ats-job").onchange=e=>{
    const v=e.target.value, pasting=v==="__paste";
    $("#ats-paste").hidden=!pasting;
    if(pasting){ $("#ats-paste").focus(); return }
    ATS.job=v; ATS.paste=""; atsRun();
  };
  let t=null;
  $("#ats-paste").oninput=e=>{ clearTimeout(t); ATS.paste=e.target.value;
    t=setTimeout(()=>{ if(ATS.paste.trim().length>80) atsRun() },600) };
  $("#ats-run").onclick=()=>atsRun();
  atsRun();
}
async function atsRun(fix){
  if(ATS.busy) return;
  ATS.busy=true;
  const body=$("#ats-body"); if(!body){ ATS.busy=false; return }
  body.classList.add("busy");
  $("#ats-run").disabled=true;
  const req={path:ATS.path};
  if(ATS.paste.trim()) req.posting=ATS.paste;
  else if(ATS.job) req.job_id=ATS.job;
  /* "No posting" has to say so, or the server falls back to the application
     the CV is attached to. */
  else req.posting="";
  try{
    const r=await post(fix?"/api/ats/fix":"/api/ats",fix?Object.assign({fix},req):req);
    if($("#ats-body")) atsPaint(r);
    if(fix){
      toast("Changed the design. Check again to see it");
      if(S.path===ATS.path&&!S.dirty) openDoc(ATS.path);
      S.baseThumb=null; paintBase();
    }
  }catch(e){ if($("#ats-body")) body.innerHTML='<p class="note">'+esc(e.message)+'</p>' }
  ATS.busy=false;
  if($("#ats-body")){ body.classList.remove("busy"); $("#ats-run").disabled=false }
}
/* Private-use characters are what icon fonts extract as. Shown as a box, the
   way a parser that keeps them would print them, so the finding above can be
   seen in the text below. */
const atsReadable=t=>esc(t).replace(/[-]/g,'<i class="pua" title="An icon, read as an unreadable character">□</i>');
function atsPaint(r){
  const body=$("#ats-body");
  if(!r.ok){ body.innerHTML='<p class="note">'+esc(r.error||"The check could not run.")+'</p>'; return }
  const kw=r.keywords, vs=r.against;
  let match;
  if(kw&&kw.total){
    const chip=(t,on)=>'<span class="kw'+(on?" on":"")+'">'+(on?'<i>✓</i>':'')+esc(t.term)+'</span>';
    match='<section class="ats-sec"><span class="blabel">Keywords from the posting</span>'+
      '<div class="ats-score"><b class="mono">'+kw.found.length+'</b><span>of '+kw.total+
        ' used in this CV'+(vs&&vs.company?' · '+esc(vs.company):'')+'</span></div>'+
      '<div class="meter"><i style="width:'+(kw.rate||0)+'%"></i></div>'+
      (kw.missing.length?'<div class="kwlabel">Not in the CV</div><div class="kws">'+
        kw.missing.map(t=>chip(t,false)).join("")+'</div>':'')+
      (kw.found.length?'<div class="kwlabel">In the CV</div><div class="kws">'+
        kw.found.map(t=>chip(t,true)).join("")+'</div>':'')+
      '<p class="sp-note">Picked out of the posting by how often, and where, it asks for '+
      'them: a prompt, not a verdict. Worth working in only where they are true of you, '+
      'in the words the posting uses.</p></section>';
  }else if(vs&&vs.company&&!vs.has_posting){
    match='<section class="ats-sec"><span class="blabel">Keywords from the posting</span>'+
      '<p class="note">'+esc(vs.company)+' has no saved posting. Paste one, or add it to the '+
      'application, to see which of its keywords this CV uses.</p></section>';
  }else{
    match='<section class="ats-sec"><span class="blabel">Keywords from the posting</span>'+
      '<p class="note">Choose an application with a saved posting, or paste one, to see '+
      'which of its keywords this CV uses.</p></section>';
  }
  const icon={ok:"✓",warn:"!",bad:"×"};
  const problems=r.checks.filter(c=>c.level!=="ok").length;
  const checks='<section class="ats-sec"><span class="blabel">How it parses'+
      '<em>'+(problems?problems+" to look at":"nothing to fix")+'</em></span>'+
    '<ul class="checks">'+r.checks.map(c=>'<li class="'+c.level+'"><i>'+icon[c.level]+'</i>'+
      '<div><b>'+esc(c.title)+'</b><span>'+esc(c.detail)+'</span></div>'+
      (c.fix?'<button class="obtn" data-fix="'+c.fix+'"'+
        (S.path===ATS.path&&S.dirty?' disabled title="Save your changes first"':'')+'>Fix</button>':'')+
      '</li>').join("")+'</ul></section>';
  body.innerHTML='<div class="ats-main">'+match+checks+'</div>'+
    '<div class="ats-read"><span class="blabel">What it reads<em>'+r.pages+' page'+
      (r.pages===1?"":"s")+' · '+r.words+' words</em></span>'+
      '<pre class="mono">'+atsReadable(r.text)+'</pre></div>';
  $$("#ats-body [data-fix]").forEach(b=>b.onclick=()=>{ b.disabled=true; atsRun(b.dataset.fix) });
}
$("#btn-ats").onclick=()=>{
  if(!S.path) return;
  if(S.dirty) toast("Checking the saved file. Save to include your edits");
  atsSheet(S.path);
};

/* =========================================================================
   Setup
   ========================================================================= */
/* The whole window, on the first launch: a welcome, then four steps that each
   leave something real behind -- the CV you already had, the name at the top
   of the base CV, the theme it prints in, a client that can write to it.
   Nothing here is a tour of buttons, and every step can be skipped, because
   the defaults already render.

   It shows while the base is still the placeholder the app wrote, not only on
   the launch that wrote it: quitting halfway is not the same as having set up.
   Closing it counts as done, so it never nags; Settings brings it back. */
const OB_FIELDS=[["name","Name","Your Name"],["headline","Headline","Your Role"],
  ["location","Location","City, Country"],["email","Email","you@example.com"],
  ["phone","Phone","+33-6-12-34-56-78"]];
const OB_STEPS=[["Welcome","What this is"],["Start from","PDF, LinkedIn or blank"],
  ["You","The top of your CV"],["The page","Theme and paper"],["AI","Optional"]];
const OB={step:0, path:null, cv:{}, theme:null, size:null, png:null, busy:0, again:false,
  t:null, err:null, pages:null, src:"pdf", imp:null, importing:false, impErr:null,
  thumbs:null, thumbRun:0};

function shouldOnboard(d){
  return !prefs().onboarded&&!!(d.first_run||d.starter)&&!!(d.base&&!d.base.missing);
}
const onbOpen=()=>!$("#onb").hidden;
async function onboardingSheet(){
  const b=S.state&&S.state.base;
  if(!b||b.missing) return openSettings("workspace");
  Object.assign(OB,{step:0, path:b.path, png:null, cv:{}, err:null, pages:null,
    src:"pdf", imp:null, importing:false, impErr:null, thumbs:null});
  try{
    const doc=await api("/api/doc?path="+encodeURIComponent(b.path));
    const cv=(doc.data&&doc.data.cv)||{}, des=(doc.data&&doc.data.design)||{};
    OB.baseCv=cv; obFill(cv);
    OB.theme=des.theme||"engineeringclassic";
    OB.size=(des.page&&des.page.size)||"a4";
  }catch(e){ return toast(e.message,true) }
  closeOverlays(); if(!$("#sheet").hidden) closeSheet();
  $("#onb").hidden=false;
  obPaint();
}
/* A placeholder is not an answer, so it goes in as a hint instead. Only the
   starter's placeholders, though: what an import read is what you wrote. */
function obFill(cv,imported){
  OB_FIELDS.forEach(([k,,ph])=>{ const v=cv[k]==null?"":String(cv[k]);
    OB.cv[k]=!imported&&v===ph||v==="Your Name"?"":v });
}
/* Leaving part-way keeps what is on screen, the same as Next would have. */
function obClose(){
  const keep=OB.step>=2||(OB.step===1&&OB.imp);
  $("#onb").hidden=true; $("#onb").innerHTML=""; delete $("#onb").dataset.step;
  clearTimeout(OB.t); OB.thumbRun++;
  setPref("onboarded",true);
  if(keep&&OB.step!==-1)
    post("/api/save",{path:OB.path,patches:obPatches()})
      .then(obRefresh).catch(e=>toast(e.message,true));
}
async function obRefresh(){
  try{ const st=await api("/api/state"); S.state=st; renderDocs(st.documents) }catch(e){}
  S.baseThumb=null; paintBase();
}
/* The fields as patches, after the imported CV if there is one: the import is
   the body, the fields are what you corrected at the top of it. Blank means
   absent: RenderCV leaves a null out of the header, where an empty string
   would print a stray separator. */
function obContentPatches(){
  return (OB.imp?[{path:["cv"],value:OB.imp.cv}]:[]).concat(
    OB_FIELDS.map(([k])=>({path:["cv",k],value:OB.cv[k].trim()||(k==="name"?"Your Name":null)})));
}
function obPatches(){
  return obContentPatches().concat([{path:["design","theme"],value:OB.theme},
    {path:["design","page","size"],value:OB.size}]);
}
/* The page itself, rendered from the scratch copy the editor previews into, so
   nothing is written until Next. One at a time, because every preview shares
   that scratch file: a burst of typing is one render, and whatever changed
   while it ran is one more after it, not a second render racing the first. */
function obPreview(now){
  clearTimeout(OB.t);
  OB.t=setTimeout(async()=>{
    if(OB.busy){ OB.again=true; return }
    OB.busy=1; OB.again=false; obShot();
    try{
      const r=await post("/api/preview",{path:OB.path,patches:obPatches()});
      OB.png=r.ok&&r.pngs.length?r.pngs[0]:null; OB.err=r.ok?null:(r.hint||"It did not render.");
      OB.pages=r.ok?r.pages:null;
    }catch(e){ OB.png=null; OB.err=e.message }
    OB.busy=0;
    if(OB.again) obPreview(true); else { obShot(); obTile() }
  },now?0:350);
}
function obShot(){
  const el=$("#onb-shot"); if(!el) return;
  el.classList.toggle("busy",!!OB.busy);
  el.innerHTML=OB.png
    ? '<img alt="The first page of your CV" src="'+esc(OB.png+tok())+'">'
    : '<span>'+esc(OB.busy?"Rendering…":OB.err||"")+'</span>';
  const cap=$("#onb-cap"); if(!cap) return;
  cap.classList.toggle("busy",!!OB.busy);
  const what=OB.step===1&&OB.imp?"Imported from "+OB.imp.name:"Your page, rendered as you type";
  cap.innerHTML='<i></i>'+esc(OB.busy&&!OB.png?"Rendering…":what+(OB.pages?" · "+OB.pages+
    " page"+(OB.pages===1?"":"s")+" · "+themeLabel(OB.theme)+" · "+
    (OB.size==="a4"?"A4":"US Letter"):""));
}

/* The theme tiles are your CV in each theme, as on the Design screen: the
   current one is the live page, the rest render one at a time behind it. */
function obTile(){
  const t=OB.theme, b=$('#onb-themes [data-t="'+t+'"] .pic');
  if(b&&OB.png) b.innerHTML='<img alt="" src="'+esc(OB.png+tok())+'">';
}
async function obThumbs(){
  const run=++OB.thumbRun, key=JSON.stringify(obContentPatches());
  if(!OB.thumbs||OB.thumbs.key!==key) OB.thumbs={key:key,by:{}};
  for(const t of (S.state.themes||[])){
    if(run!==OB.thumbRun||OB.step!==3||!onbOpen()) return;
    if(OB.thumbs.by[t]) continue;
    try{
      const r=await post("/api/theme-preview",{path:OB.path,theme:t,patches:obContentPatches()});
      if(!r.ok||run!==OB.thumbRun) continue;
      OB.thumbs.by[t]=r;
      const b=$('#onb-themes [data-t="'+t+'"]');
      if(b&&t!==OB.theme) b.querySelector(".pic").innerHTML='<img alt="" src="'+esc(r.png+tok())+'">';
      if(b) b.querySelector(".nm em").textContent=r.pages+" page"+(r.pages===1?"":"s");
    }catch(e){ return }
  }
}

/* A PDF or a LinkedIn archive, read on this machine by /api/import: the file
   goes to the local server and no further. Resolves to what was read, or
   throws with a sentence to show. */
function importFile(file){
  return new Promise((ok,fail)=>{
    if(file.size>40*1024*1024) return fail(new Error("That file is over 40 MB."));
    const rd=new FileReader();
    rd.onerror=()=>fail(new Error("That file could not be read."));
    rd.onload=async()=>{
      try{
        const r=await post("/api/import",{name:file.name,
          data:String(rd.result).replace(/^data:[^,]*,/,"")});
        if(!r.ok) return fail(new Error(r.error||"Nothing could be read from that file."));
        ok({name:file.name,cv:r.cv,found:r.found||[],notes:r.notes||[],source:r.source});
      }catch(e){ fail(e) }
    };
    rd.readAsDataURL(file);
  });
}
async function obReadFile(file){
  if(!file) return;
  OB.importing=true; OB.impErr=null; obPaint();
  try{ OB.imp=await importFile(file) }catch(e){ return obImpFail(e.message) }
  obFill(OB.imp.cv,true); OB.importing=false; OB.png=null; OB.pages=null;
  if(onbOpen()){ obPaint(); obPreview(true) }
}
function obImpFail(msg){ OB.importing=false; OB.impErr=msg; if(onbOpen()) obPaint() }

const OB_ICON={
  folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  base:'<path d="M8 4h9l3 3v13H8z"/><path d="M4 8v12h11"/>',
  lock:'<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  up:'<path d="M12 15V3M7 8l5-5 5 5"/><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>',
  tick:'<path d="M5 12l5 5L20 7"/>',
  arrow:'<path d="M5 12h14M13 6l6 6-6 6"/>',
  spark:'<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>'+
    '<path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
};
const obIcon=(k,s,w)=>'<svg width="'+s+'" height="'+s+'" viewBox="0 0 24 24" fill="none" '+
  'stroke="currentColor" stroke-width="'+(w||2)+'" aria-hidden="true">'+OB_ICON[k]+'</svg>';
/* The window has no frame of its own, so a screen that covers it all carries
   the lights and a strip to drag it by. */
function obBar(){
  return '<div class="onb-bar" data-tauri-drag-region>'+($("#lights").hidden?"":
    '<div class="lights"><button data-w="w-close" title="Close" aria-label="Close"></button>'+
    '<button data-w="w-min" title="Minimise" aria-label="Minimise"></button>'+
    '<button data-w="w-max" title="Maximise" aria-label="Maximise"></button></div>')+'</div>';
}

function obWelcome(){
  const st=S.state||{};
  const letters="CV Studio".split("").map((ch,i)=>'<span aria-hidden="true" style="animation-delay:'+
    (0.55+i*0.06).toFixed(2)+'s">'+esc(ch)+'</span>').join("");
  const facts=[
    ["folder","Your files, in a folder you own",'<span class="mono">'+esc(shortPath(st.workspace))+
      '</span>. Plain YAML you can open, copy and back up without this app. '+
      '<button class="linkish" id="onb-reveal">Open it</button>'],
    ["base","One base CV","Every CV you tailor for an application starts as a copy of it, "+
      "so it is worth two minutes now."],
    ["lock","Nothing leaves this machine","No account, no telemetry. A model only sees what "+
      "you connect it to."]];
  return '<div class="onb-hero">'+
    '<div class="onb-orbs" aria-hidden="true"><span></span><span></span><span></span><span></span></div>'+
    '<div class="onb-mark"><img src="/static/brand-mark-256.png" alt=""></div>'+
    '<h1 class="onb-word" id="onb-title" aria-label="Welcome to CV Studio">'+letters+'</h1>'+
    '<span class="onb-rule" aria-hidden="true"></span>'+
    '<p class="onb-lede">A CV editor that shows you the page, and an application tracker '+
      'beside it. An AI client can read and write both, if you connect one.</p>'+
    '<div class="onb-facts">'+facts.map(([ic,t,x],i)=>
      '<div class="onb-fact" style="animation-delay:'+(1.85+i*0.15).toFixed(2)+'s">'+
        '<span class="ic">'+obIcon(ic,17)+'</span><b>'+t+'</b><span>'+x+'</span></div>').join("")+
    '</div>'+
    '<div class="onb-go"><button class="onb-cta" id="onb-next">Get started '+
      obIcon("arrow",16,2.4)+'</button>'+
      '<button class="onb-skip" id="onb-skip">Skip setup, I\'ll look around first</button>'+
      '<div class="onb-or" aria-hidden="true"><span>or</span></div>'+
      '<button class="onb-demo" id="onb-demo"><span class="dm-ic">'+obIcon("spark",18,2)+'</span>'+
        '<span class="dm-tx"><b>Try it with sample data</b>'+
        '<small>66 made-up applications, CVs and letters. Your own folder stays untouched.</small></span>'+
        '<span class="dm-go">'+obIcon("arrow",15,2.4)+'</span></button></div>'+
    '<div class="onb-pips" aria-label="Step 1 of '+OB_STEPS.length+'">'+
      OB_STEPS.map((_,k)=>'<i'+(k===0?' class="on"':'')+'></i>').join("")+'</div>'+
  '</div>';
}

function obRail(){
  const i=OB.step;
  return '<aside class="onb-rail">'+
    '<div class="onb-brand"><img src="/static/brand-mark-256.png" alt="">CV Studio</div>'+
    '<p>Two minutes to set up your base CV. Every step can be skipped: the defaults '+
      'already render.</p>'+
    '<ol class="onb-steps" aria-label="Setup steps">'+OB_STEPS.map(([t,sub],k)=>
      '<li'+(k===i?' aria-current="step"':k<i?' class="done"':'')+'>'+
        '<span class="num"><b>'+(k<i?obIcon("tick",14,3):k+1)+'</b><i></i></span>'+
        '<span class="txt"><b>'+t+'</b><span>'+sub+'</span></span></li>').join("")+'</ol>'+
    '<div class="onb-later"><button id="onb-later">Finish later</button>'+
      '<span>What you have entered is kept. Settings → Workspace runs this again.</span></div>'+
  '</aside>';
}

const OB_CAN=["Tailor your base CV to a job posting, and show you what changed",
  "Look at the page it rendered, and fix what does not fit",
  "Track applications from your inbox: replies, interviews, rejections",
  "Check a CV the way an applicant tracking system reads it"];

function obStep(){
  const i=OB.step, st=S.state||{};
  let head, lede, main="", side="", say="";
  const shot='<div class="onb-cap" id="onb-cap"></div><div class="onb-shot" id="onb-shot"></div>';
  if(i===1&&OB.imp){
    head="Here is what we read";
    lede="From "+esc(OB.imp.name)+". The page beside this is your CV, rebuilt and rendered "+
      "in CV Studio.";
    main='<div class="onb-found">'+OB.imp.found.map(f=>'<div><i>'+obIcon("tick",13,3)+'</i>'+
        '<b>'+esc(f.what)+'</b><span>'+esc(String(f.count))+'</span></div>').join("")+'</div>'+
      (OB.imp.notes.length?'<div class="onb-note"><i>!</i><span><b>'+
        (OB.imp.notes.length===1?"One thing to check.":OB.imp.notes.length+" things to check.")+
        '</b>'+(OB.imp.notes.length===1?" "+esc(OB.imp.notes[0]):'<ul>'+OB.imp.notes.map(n=>
          '<li>'+esc(n)+'</li>').join("")+'</ul>')+' Fix it on the page, or ask a connected '+
        'AI client to tidy the import.</span></div>':'')+
      '<p class="onb-small">Nothing is saved until you press Next. The file stays where it is; '+
        'only what was read goes into your base CV. '+
        '<button class="linkish" id="onb-again">Import a different file</button></p>';
    side=shot;
  }else if(i===1){
    head="Start from what you have";
    lede="Bring the CV you already have, or start from a blank one. Either way you get a "+
      "base CV every tailored copy starts from.";
    const srcs=[
      ["pdf","PDF","Import a PDF of your CV","Contact details come out exactly. Jobs, "+
        "education and skills are sorted into sections, and a connected AI client can tidy "+
        "what the rules miss."],
      ["linkedin",'<svg viewBox="0 0 448 512" aria-hidden="true"><use href="#board-linkedin"/></svg>',
        "Import from LinkedIn","Your data archive or your profile saved as PDF."],
      ["blank","+","Start from a blank CV","The starter layout, filled in as you go."]];
    main='<div class="onb-srcs" role="radiogroup" aria-label="Start from">'+srcs.map(([k,g,t,x])=>
      '<button class="onb-src" role="radio" data-src="'+k+'" aria-checked="'+String(OB.src===k)+'">'+
        '<span class="row"><span class="tile">'+g+'</span><span class="what"><b>'+t+'</b>'+
        '<span>'+x+'</span></span><span class="radio"></span></span>'+
        (k==="linkedin"&&OB.src==="linkedin"?'<span class="how">'+
          '<span><b>Your data archive (.zip), exact.</b> LinkedIn → Settings → Data privacy → '+
            'Get a copy of your data. Tick Profile, Positions, Education and Skills; it '+
            'arrives by email in about ten minutes.</span>'+
          '<span><b>Or your profile as a PDF.</b> On your profile, More → Save to PDF.</span>'+
          '<span class="quiet">CV Studio never signs in to LinkedIn. You bring the file.</span>'+
        '</span>':'')+
      '</button>').join("")+'</div>';
    if(OB.src==="blank") side=shot;
    else side='<div class="onb-drop'+(OB.importing?' busy':'')+'" id="onb-drop">'+
      '<span class="ic">'+obIcon("up",30,1.8)+'</span>'+
      '<b>'+(OB.importing?"Reading it…":"Drop your "+(OB.src==="linkedin"?"LinkedIn file":"CV")+
        " here")+'</b>'+
      '<span>'+(OB.src==="linkedin"?"Your LinkedIn data archive (.zip), or your profile "+
        "saved as PDF.":"A PDF of your CV, your LinkedIn profile saved as PDF, or your "+
        "LinkedIn data archive (.zip).")+'</span>'+
      (OB.impErr?'<span class="err" role="alert">'+esc(OB.impErr)+'</span>':'')+
      '<button class="onb-btn" id="onb-pick"'+(OB.importing?" disabled":"")+'>Choose a file…</button>'+
      '<small>Read on this machine. Nothing is uploaded.</small>'+
      '<input type="file" id="onb-file" accept=".pdf,.zip,application/pdf,application/zip" hidden>'+
    '</div>';
    if(OB.src!=="blank") say="No file? Next starts from the blank CV.";
  }else if(i===2){
    head="The top of your CV";
    lede="What prints above everything else. The rest you can write in the editor, or ask "+
      "a model to write from what you already have.";
    main='<div class="onb-fields">'+OB_FIELDS.map(([k,l,ph])=>'<label>'+
      '<span>'+l+(k==="phone"?' <em>optional</em>':'')+'</span>'+
      '<input data-ob="'+k+'" autocomplete="off" placeholder="'+esc(ph)+'" value="'+
        esc(OB.cv[k])+'"></label>').join("")+'</div>';
    side=shot;
  }else if(i===3){
    head="How it prints";
    lede="Pick a theme and a paper size. Every other option is in Design later. The page "+
      "beside this is yours, rendered, not a sample.";
    const by=(OB.thumbs&&OB.thumbs.key===JSON.stringify(obContentPatches())&&OB.thumbs.by)||{};
    main='<div class="onb-lab" id="onb-pl">Paper</div>'+
      '<div class="onb-seg" id="onb-size" role="radiogroup" aria-labelledby="onb-pl">'+
        '<button role="radio" data-s="a4" aria-checked="'+String(OB.size==="a4")+'">A4</button>'+
        '<button role="radio" data-s="us-letter" aria-checked="'+String(OB.size!=="a4")+
          '">US Letter</button></div>'+
      '<div class="onb-lab" id="onb-tl">Theme</div>'+
      '<div class="onb-themes" id="onb-themes" role="radiogroup" aria-labelledby="onb-tl">'+
      (st.themes||[]).map(t=>{
        const img=t===OB.theme&&OB.png?OB.png:by[t]?by[t].png:null;
        return '<button role="radio" data-t="'+esc(t)+'" aria-checked="'+String(t===OB.theme)+'">'+
          '<span class="pic">'+(img?'<img alt="" src="'+esc(img+tok())+'">':thumbHTML(t))+'</span>'+
          '<span class="nm"><span>'+esc(themeLabel(t))+'</span><em>'+(by[t]?by[t].pages+" page"+
            (by[t].pages===1?"":"s"):"")+'</em></span></button>'}).join("")+'</div>';
    side=shot;
  }else{
    head="Connect an AI client";
    lede="Optional. Connected, it can tailor a CV to a posting, look at the page it rendered, "+
      "and keep your applications in step with your mail.";
    const cs=S.ai||[];
    main='<div class="onb-ai">'+(cs.length?cs.map(c=>{
      const live=c.state==="connected";
      return '<div class="onb-client" data-client="'+c.id+'">'+
        '<span class="badge"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#'+c.id+
          '-mark"/></svg></span>'+
        '<span class="nm"><b>'+esc(c.label)+'</b><span>'+esc(live?aiSay(c):c.state==="absent"
          ?"Connect, then "+c.restart.replace(/^./,x=>x.toLowerCase()):aiSay(c))+'</span></span>'+
        (live?'<span class="on"><i></i>'+esc(aiPill(c))+'</span>'
             :'<button class="onb-btn" data-ob-connect="'+c.id+'">Connect</button>')+
      '</div>'}).join(""):'<p class="onb-small">Checking…</p>')+'</div>';
    side='<div class="onb-can"><b>Once one is connected, you can ask it to</b>'+
      OB_CAN.map(x=>'<div><svg width="16" height="16" viewBox="0 0 24 24" fill="none" '+
        'stroke="#c08a3e" stroke-width="2.4" aria-hidden="true">'+OB_ICON.tick+'</svg>'+
        '<span>'+x+'</span></div>').join("")+
      '<span>It works on your CV Studio folder and nothing else.</span></div>';
  }
  const last=i===OB_STEPS.length-1;
  return obRail()+'<div class="onb-work">'+
    '<div class="onb-step"><section class="onb-main">'+
      '<span class="onb-kicker">Step '+(i+1)+' of '+OB_STEPS.length+'</span>'+
      '<h1 id="onb-title">'+head+'</h1><p>'+lede+'</p>'+main+'</section>'+
      '<aside class="onb-side">'+side+'</aside></div>'+
    '<footer class="onb-foot"><span class="say">'+say+'</span>'+
      '<button class="onb-btn" id="onb-back">Back</button>'+
      '<button class="onb-btn primary" id="onb-next">'+(last?"Open my CV":"Next")+'</button>'+
    '</footer></div>';
}

function obPaint(){
  const i=OB.step, root=$("#onb");
  /* Re-painting the step you are on keeps its place and skips the slide-in:
     only moving between steps should move. */
  const same=root.dataset.step===String(i), keep=same&&root.querySelector(".onb-step");
  const top=keep?keep.scrollTop:0;
  root.innerHTML=obBar()+(i===0?obWelcome():obStep());
  root.dataset.step=String(i);
  const stepEl=root.querySelector(".onb-step");
  if(stepEl&&same){ stepEl.style.animation="none"; stepEl.scrollTop=top }

  $$("#onb .onb-bar [data-w]").forEach(b=>b.onclick=()=>$("#"+b.dataset.w).click());
  const rv=$("#onb-reveal");
  if(rv) rv.onclick=async()=>{ try{ await post("/api/reveal",{}) }catch(e){ toast(e.message,true) } };
  const sk=$("#onb-skip"); if(sk) sk.onclick=obClose;
  const dm=$("#onb-demo");
  if(dm) dm.onclick=()=>{ dm.classList.add("busy"); dm.querySelector("b").textContent=t("Making it…");
    setSample(true,null); dm.disabled=true };
  const lt=$("#onb-later"); if(lt) lt.onclick=obClose;
  const bk=$("#onb-back"); if(bk) bk.onclick=()=>{ OB.step--; obPaint() };
  $("#onb-next").onclick=obNext;

  $$("#onb [data-src]").forEach(b=>b.onclick=()=>{
    if(OB.src===b.dataset.src) return;
    OB.src=b.dataset.src; OB.impErr=null; obPaint();
    if(OB.src==="blank"&&!OB.png) obPreview(true);
  });
  const drop=$("#onb-drop");
  if(drop){
    const file=$("#onb-file");
    $("#onb-pick").onclick=()=>file.click();
    file.onchange=()=>obReadFile(file.files[0]);
    drop.ondragover=e=>{ e.preventDefault(); drop.classList.add("over") };
    drop.ondragleave=()=>drop.classList.remove("over");
    drop.ondrop=e=>{ e.preventDefault(); drop.classList.remove("over");
      if(!OB.importing) obReadFile(e.dataTransfer.files[0]) };
  }
  const ag=$("#onb-again");
  if(ag) ag.onclick=()=>{ OB.imp=null; OB.impErr=null; OB.png=null; OB.pages=null;
    obFill(OB.baseCv||{}); obPaint() };
  $$("#onb [data-ob]").forEach(el=>el.oninput=()=>{ OB.cv[el.dataset.ob]=el.value; obPreview() });
  $$("#onb-themes [data-t]").forEach(bt=>bt.onclick=()=>{
    const was=OB.theme; OB.theme=bt.dataset.t;
    $$("#onb-themes [data-t]").forEach(x=>x.setAttribute("aria-checked",String(x===bt)));
    /* The tile you left gets its own render back, if it has one. */
    const old=$('#onb-themes [data-t="'+was+'"] .pic'), by=OB.thumbs&&OB.thumbs.by[was];
    if(old&&by) old.innerHTML='<img alt="" src="'+esc(by.png+tok())+'">';
    obPreview(true);
  });
  $$("#onb-size [data-s]").forEach(bt=>bt.onclick=()=>{
    OB.size=bt.dataset.s;
    $$("#onb-size [data-s]").forEach(x=>x.setAttribute("aria-checked",String(x===bt)));
    obPreview(true);
  });
  $$("#onb [data-ob-connect]").forEach(bt=>bt.onclick=async()=>{
    bt.disabled=true; bt.textContent="Connecting…";
    try{ await post("/api/ai/connect",{client:bt.dataset.obConnect}) }
    catch(e){ toast(e.message,true) }
    await loadAI(); if(onbOpen()&&OB.step===4) obPaint();
  });
  if(i===4&&!S.ai) loadAI().then(()=>{ if(OB.step===4&&onbOpen()) obPaint() });

  if($("#onb-shot")){ obShot(); if(!OB.png&&!OB.busy) obPreview(true) }
  if(i===3) obThumbs();
  const f=$("#onb input[data-ob]")||$("#onb-next"); if(f&&!same) f.focus();
}

async function obNext(){
  const btn=$("#onb-next");
  /* The page is written when you leave the step that changed it, so Back and
     Finish later both keep what you did. */
  if(OB.step>=2||(OB.step===1&&OB.imp)){
    btn.disabled=true;
    try{ await post("/api/save",{path:OB.path,patches:obPatches()}) }
    catch(e){ btn.disabled=false; return toast(e.message,true) }
    btn.disabled=false;
  }
  if(OB.step<OB_STEPS.length-1){ OB.step++; return obPaint() }
  OB.step=-1;   /* saved already: closing must not write it again */
  obClose();
  await obRefresh();
  openDoc(OB.path);
}
/* Nothing behind the setup should hear its keys: Escape is not a way out of a
   screen that has its own Finish later, and the arrows belong to its fields. */
window.addEventListener("keydown",e=>{ if(onbOpen()) e.stopPropagation() },true);


/* =========================================================================
   Cover letters
   ========================================================================= */
/* A letter is a Markdown file laid out by the app itself, in the look of the
   CV it names. It is written on the page: the body is editable where it
   prints, the letterhead comes from the CV, and place, date, language and the
   CV it looks like are set beside it. The PDF tab is the exact render. */
const LT={path:null, doc:null, meta:{}, body:"", dirty:false, tab:"write", pages:null,
  png:null, rendering:false, mdText:null, t:null};
const isLetterPath=p=>/\.md$/i.test(p||"");

async function openLetter(path){
  if(S.view==="jobs"||S.view==="docs") S.fromList=S.view;
  closeOverlays();
  try{
    const d=await api("/api/doc?path="+encodeURIComponent(path));
    Object.assign(LT,{path, doc:d, meta:Object.assign({},d.meta), body:d.body, dirty:false,
      tab:"write", pages:null, png:null, mdText:null});
  }catch(e){ return toast(e.message,true) }
  setView("letter");
  ltPaint();
  ltRender();
}
function ltJob(){
  const id=LT.meta.application;
  return (S.jobs||[]).find(j=>j.id===id||j.letter_path===LT.path)||null;
}
function ltBackLabel(){
  const to=(!ltJob()&&S.fromList==="docs")?"docs":"jobs";
  $("#lt-back").textContent=to==="docs"?"Documents":"Applications";
  $$("#nav button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.view===to)));
}
$("#lt-back").onclick=()=>{
  if(LT.dirty&&!confirm("This letter has unsaved changes. Leave without saving?")) return;
  LT.path=null;
  const j=ltJob();
  if(j){ setView("jobs"); selectJob(j.id); return }
  setView(S.fromList==="docs"?"docs":"jobs");
};

/* The Markdown a letter uses, as HTML for the page and back. Paragraphs,
   '- ' lists, **bold**, *italic*, [links](url): what letters.py prints. */
function ltInline(t){
  let out="", pos=0;
  const re=/\*\*(.+?)\*\*|__(.+?)__|\*(.+?)\*|_(.+?)_|\[([^\]]+)\]\(([^)\s]+)\)/g; let m;
  while((m=re.exec(t))){
    out+=esc(t.slice(pos,m.index));
    if(m[1]||m[2]) out+="<b>"+ltInline(m[1]||m[2])+"</b>";
    else if(m[3]||m[4]) out+="<i>"+ltInline(m[3]||m[4])+"</i>";
    else out+='<a href="'+esc(m[6])+'">'+ltInline(m[5])+"</a>";
    pos=re.lastIndex;
  }
  return out+esc(t.slice(pos));
}
function ltToHTML(body){
  return String(body||"").trim().split(/\n\s*\n/).filter(c=>c.trim()).map(c=>{
    const lines=c.split("\n").filter(l=>l.trim());
    if(lines.every(l=>/^\s*[-*•]\s+/.test(l)))
      return "<ul>"+lines.map(l=>"<li>"+ltInline(l.replace(/^\s*[-*•]\s+/,"").trim())+"</li>").join("")+"</ul>";
    return "<p>"+ltInline(lines.map(l=>l.trim()).join(" "))+"</p>";
  }).join("")||"<p><br></p>";
}
function ltInlineMD(node){
  let out="";
  node.childNodes.forEach(n=>{
    if(n.nodeType===3){ out+=n.nodeValue.replace(/ /g," "); return }
    if(n.nodeType!==1) return;
    const t=n.tagName, inner=ltInlineMD(n);
    const fw=n.style&&(n.style.fontWeight==="bold"||Number(n.style.fontWeight)>=600);
    const it=n.style&&n.style.fontStyle==="italic";
    if(t==="BR") out+=" ";
    else if(!inner.trim()) out+=inner;
    else if(t==="B"||t==="STRONG"||fw) out+="**"+inner.trim()+"**"+(/\s$/.test(inner)?" ":"");
    else if(t==="I"||t==="EM"||it) out+="*"+inner.trim()+"*"+(/\s$/.test(inner)?" ":"");
    else if(t==="A") out+="["+inner+"]("+(n.getAttribute("href")||"")+")";
    else out+=inner;
  });
  return out;
}
function ltToMD(root){
  const out=[];
  const blockOf=n=>{
    if(n.nodeType===3){ const t=n.nodeValue.trim(); if(t) out.push(t); return }
    if(n.nodeType!==1) return;
    if(n.tagName==="UL"||n.tagName==="OL"){
      const items=[...n.querySelectorAll(":scope>li")].map(li=>"- "+ltInlineMD(li).replace(/\s+/g," ").trim())
        .filter(x=>x!=="- ");
      if(items.length) out.push(items.join("\n"));
      return;
    }
    if(/^(P|DIV|H\d)$/.test(n.tagName)&&n.querySelector("ul,ol,p,div")){ n.childNodes.forEach(blockOf); return }
    const t=ltInlineMD(n).replace(/\s+/g," ").trim();
    if(t) out.push(t);
  };
  root.childNodes.forEach(blockOf);
  return out.join("\n\n");
}

/* Lengths in the letter's own units, drawn at the width the page is shown. */
const LT_PAPER={"a4":[210,297],"a5":[148,210],"us-letter":[215.9,279.4],"us-executive":[184.15,266.7]};
function ltMM(v){
  const m=String(v||"").trim().match(/^([\d.]+)\s*(cm|mm|in|pt)?$/); if(!m) return 20;
  const n=parseFloat(m[1]);
  return {cm:n*10,mm:n,in:n*25.4,pt:n*0.3528}[m[2]||"cm"];
}
function ltFont(fam){
  if(!fam) return;
  const id="cvfont-"+fam.replace(/\W+/g,"-");
  if(document.getElementById(id)) return;
  const l=document.createElement("link");
  l.id=id; l.rel="stylesheet"; l.href="/cvfont/"+encodeURIComponent(fam)+".css";
  document.head.append(l);
}

function ltPaint(){
  const d=LT.doc, h=(d&&d.head)||{}, j=ltJob();
  const bar=$("#lt-bar"); if(bar) bar.remove();
  $("#lt-title").textContent="Cover letter"+(j?" · "+j.company:LT.meta.company?" · "+LT.meta.company:"");
  $("#lt-file").textContent=LT.path.split("/").pop();
  ltBackLabel();
  $$("#lt-tabs [data-lt]").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.lt===LT.tab)));
  $("#lt-save").disabled=!LT.dirty;
  const st=$("#lt-stage");
  if(LT.tab==="pdf"){
    st.innerHTML=LT.png&&LT.png.length?'<div class="lt-pdf">'+LT.png.map((u,i)=>
      '<img alt="Page '+(i+1)+' of the letter" style="width:min(720px,100%)" src="'+esc(u+tok())+'">').join("")+'</div>'
      :'<p class="sp-note">'+(LT.rendering?"Laying it out…":"Not rendered yet.")+'</p>';
  }else if(LT.tab==="md"){
    const text=LT.mdText!=null?LT.mdText:(LT.dirty?null:d.text);
    st.innerHTML='<textarea class="lt-md" id="lt-md" spellcheck="true" aria-label="The letter as Markdown"></textarea>';
    const ta=$("#lt-md");
    ta.value=text!=null?text:"Saving…";
    if(text==null) ltSave().then(()=>{ ta.value=LT.doc.text });
    ta.oninput=()=>{ LT.mdText=ta.value; LT.dirty=true; $("#lt-save").disabled=false };
  }else{
    ltFont(h.font); ltFont(h.name_font);
    const [wmm,hmm]=LT_PAPER[h.paper]||LT_PAPER.a4;
    const W=Math.min(700,Math.max(480,st.clientWidth-60)), u=W/wmm, pt=u*0.3528;
    const mg=h.margins||{}, to=LT.meta.to;
    const toLines=Array.isArray(to)?to:String(to||"").split("\n").filter(x=>x.trim());
    st.innerHTML='<div class="lt-paper" id="lt-paper" style="width:'+W+'px;min-height:'+(hmm*u)+'px;'+
      'padding:'+(ltMM(mg.top)*u)+'px '+(ltMM(mg.right)*u)+'px '+(ltMM(mg.bottom)*u)+'px '+(ltMM(mg.left)*u)+'px;'+
      'box-sizing:border-box;font-family:\''+esc(h.font||"Source Sans 3")+'\',sans-serif;font-size:'+(10.5*pt)+'px;'+
      'line-height:1.52;color:'+esc(h.body_color||"#000")+'">'+
      '<div class="lh" tabindex="0" role="link" aria-label="Letterhead, from '+esc(h.cv||"the CV")+'. Opens that CV." id="lt-lh">'+
        '<span class="tag">From '+esc((h.cv||"the CV").split("/").pop().replace(/\.ya?ml$/,""))+' · <u>change it there</u></span>'+
        '<div style="font-family:\''+esc(h.name_font||h.font)+'\',sans-serif;font-size:'+(24*pt)+'px;line-height:1.1;'+
          'font-weight:'+(h.name_bold?700:400)+';color:'+esc(h.name_color)+'">'+esc(h.name)+'</div>'+
        (h.headline?'<div style="font-size:'+(11*pt)+'px;color:'+esc(h.headline_color)+'">'+esc(h.headline)+'</div>':'')+
        '<div style="margin-top:'+(4*pt)+'px;font-size:'+(9.5*pt)+'px;color:'+esc(h.contact_color)+'">'+
          (h.contact||[]).map(esc).join(' &nbsp;•&nbsp; ')+'</div>'+
        '<div style="margin-top:'+(6*pt)+'px;border-top:'+Math.max(1,0.6*pt)+'px solid '+esc(h.rule_color)+'"></div>'+
      '</div>'+
      (toLines.length?'<div style="margin-top:'+(16*pt)+'px">'+toLines.map(esc).join("<br>")+'</div>':'')+
      '<div style="margin-top:'+(16*pt)+'px;text-align:right;color:'+esc(h.contact_color)+'" title="Set the place and date beside the letter">'+
        esc(LT.doc.date_line||"")+'</div>'+
      '<div class="subj" id="lt-subj" contenteditable="true" spellcheck="true" data-ph="Subject" '+
        'aria-label="Subject" style="margin-top:'+(18*pt)+'px;font-weight:700">'+esc(LT.meta.subject||"")+'</div>'+
      '<div class="ltbody" id="lt-edit" contenteditable="true" spellcheck="true" aria-label="The letter" '+
        'style="margin-top:'+(10*pt)+'px;display:flex;flex-direction:column;gap:'+(8*pt)+'px;text-align:justify">'+
        ltToHTML(LT.body)+'</div>'+
      '<div style="margin-top:'+(24*pt)+'px;font-weight:700">'+esc(h.name)+'</div>'+
    '</div>';
    try{ document.execCommand("styleWithCSS",false,false); document.execCommand("defaultParagraphSeparator",false,"p") }catch(e){}
    const ed=$("#lt-edit"), sj=$("#lt-subj");
    ed.oninput=()=>ltChanged();
    sj.oninput=()=>{ LT.meta.subject=sj.textContent.replace(/\s+/g," ").trim(); ltDirty() };
    sj.onkeydown=e=>{ if(e.key==="Enter"){ e.preventDefault(); ed.focus() } };
    [ed,sj].forEach(el=>el.onpaste=e=>{ e.preventDefault();
      document.execCommand("insertText",false,(e.clipboardData||window.clipboardData).getData("text/plain")) });
    ed.onkeydown=e=>{
      if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="k"){ e.preventDefault(); ltLink() }
    };
    const lh=$("#lt-lh"), open=()=>{ if(h.cv){
      if(LT.dirty&&!confirm("This letter has unsaved changes. Leave without saving?")) return;
      LT.path=null; openDoc(h.cv) } };
    lh.onclick=open; lh.onkeydown=e=>{ if(e.key==="Enter") open() };
    ltPageMark();
  }
  ltPanel();
}
/* Where the first page ends, drawn on the sheet, so a letter that runs over
   says so while it is being written. */
function ltPageMark(){
  const paper=$("#lt-paper"); if(!paper) return;
  paper.querySelectorAll(".pgend").forEach(x=>x.remove());
  const h=LT.doc.head||{}, [wmm,hmm]=LT_PAPER[h.paper]||LT_PAPER.a4;
  const u=paper.offsetWidth/wmm, pageH=hmm*u-ltMM((h.margins||{}).bottom)*u;
  if(paper.scrollHeight>hmm*u+2){
    const m=document.createElement("div"); m.className="pgend"; m.style.top=pageH+"px";
    m.innerHTML="<span>End of page 1</span>"; paper.append(m);
  }
}
function ltChanged(){
  LT.body=ltToMD($("#lt-edit"));
  ltDirty();
  clearTimeout(LT.t); LT.t=setTimeout(ltPageMark,120);
}
function ltDirty(){
  LT.dirty=true; $("#lt-save").disabled=false;
  const w=$("#lt-words"); if(w) w.textContent=ltWords(LT.body);
  const m=$("#lt-meterfill"); if(m) m.style.width=Math.min(100,ltWords(LT.body)/350*100)+"%";
}
const ltWords=b=>(String(b||"").replace(/\[([^\]]+)\]\([^)]+\)/g,"$1").match(/[\p{L}\p{N}'’-]+/gu)||[]).length;

function ltPanel(){
  const j=ltJob(), n=ltWords(LT.body), m=LT.meta, docs=(S.state&&S.state.documents)||[];
  const cvs=docs.filter(d=>d.group!=="Cover letters"&&!d.letter);
  const today=!m.date||m.date==="today";
  const lang=m.language||"en";
  const fits=LT.pages==null?(LT.rendering?"Checking the page…":"Not laid out yet.")
    :LT.pages===1?"Fits on one page.":"Runs to "+LT.pages+" pages. Letters read best on one.";
  const scaffold=/Open with something only you could write|Commencez par ce que vous seul/.test(LT.body);
  const say="In CV Studio, write the cover letter "+LT.path+(j?" for my "+j.company+" application, against its posting.":".");
  $("#lt-panel").innerHTML=
    '<div style="display:flex;flex-direction:column;gap:8px"><h4>Length</h4>'+
      '<span><span class="big" id="lt-words">'+n+'</span> <span class="muted2">of about 350 words</span></span>'+
      '<div class="lt-meter'+(n>420?" over":"")+'"><i id="lt-meterfill" style="width:'+Math.min(100,n/350*100)+'%"></i></div>'+
      '<span class="muted2">'+fits+'</span></div>'+
    '<hr>'+
    '<div style="display:flex;flex-direction:column;gap:10px"><h4>This letter</h4><div class="fg">'+
      '<label>For</label><span>'+(j?'<b style="font-weight:600">'+esc(j.company)+'</b> · '+esc(j.title)
        :'<span class="muted2">No application</span>')+'</span>'+
      '<label for="lt-cv">Looks like</label><select id="lt-cv">'+cvs.map(d=>'<option value="'+esc(d.path)+'"'+
        (d.path===(m.looks_like||(LT.doc.head||{}).cv)?" selected":"")+'>'+esc(d.label)+'</option>').join("")+'</select>'+
      '<label for="lt-lang">Language</label><select id="lt-lang">'+((S.state&&S.state.languages)||[]).map(l=>
        '<option value="'+l.code+'"'+(l.code===lang?" selected":"")+'>'+esc(l.native)+'</option>').join("")+'</select>'+
      '<label for="lt-place">Written from</label><input id="lt-place" value="'+esc(m.place||"")+'" placeholder="City">'+
      '<label for="lt-date">Date</label><span class="today"><label style="display:flex;gap:5px;align-items:center;color:var(--t800)">'+
        '<input type="checkbox" id="lt-today"'+(today?" checked":"")+'>Today</label>'+
        '<input type="date" id="lt-date" value="'+(today?"":esc(String(m.date)))+'"'+(today?" hidden":"")+'></span>'+
      '<label for="lt-to">Addressed to</label><textarea id="lt-to" placeholder="Optional. Printed above the date.">'+
        esc(Array.isArray(m.to)?m.to.join("\n"):(m.to||""))+'</textarea>'+
    '</div></div>'+
    '<hr>'+
    (scaffold?sayBox("To have your AI client write it, ask it:",say)+'<hr>':'')+
    '<div class="muted2" style="font-size:12.5px">Click anywhere in the letter and type. Select text for bold, '+
      'italic, a link or a list. The letterhead is the CV’s: change it there, and every letter that '+
      'looks like it follows.</div>';
  if(scaffold) wireSay(say);
  const set=(k,v)=>{ LT.meta[k]=v; ltDirty(); ltSave().then(()=>{ if(LT.tab==="write") ltPaint() }) };
  $("#lt-cv").onchange=e=>set("looks_like",e.target.value);
  $("#lt-lang").onchange=e=>set("language",e.target.value);
  $("#lt-place").onchange=e=>set("place",e.target.value.trim()||null);
  $("#lt-today").onchange=e=>{ const d=$("#lt-date"); d.hidden=e.target.checked;
    if(!d.hidden&&!d.value) d.value=new Date().toISOString().slice(0,10);
    set("date",e.target.checked?"today":($("#lt-date").value||new Date().toISOString().slice(0,10))) };
  $("#lt-date").onchange=e=>set("date",e.target.value||"today");
  $("#lt-to").onchange=e=>{ const v=e.target.value.split("\n").map(x=>x.trim()).filter(Boolean);
    set("to",v.length?v:null) };
}

async function ltSave(){
  if(!LT.path) return;
  const body=LT.tab==="md"&&LT.mdText!=null?{path:LT.path,text:LT.mdText}
    :{path:LT.path,meta:LT.meta,body:LT.body};
  try{
    const r=await post("/api/save",body);
    LT.doc=r; LT.meta=Object.assign({},r.meta); LT.body=r.body; LT.mdText=null; LT.dirty=false;
    $("#lt-save").disabled=true;
    ltRender();
  }catch(e){ toast(e.message,true) }
}
/* Laid out after every save, so the page count and the PDF tab are always
   the letter as it stands. */
async function ltRender(){
  if(!LT.path) return;
  LT.rendering=true;
  try{
    const r=await post("/api/render",{path:LT.path});
    if(r.ok){ LT.pages=r.pages; LT.png=r.pngs }
    else toast(r.hint||"The letter did not lay out.",true);
  }catch(e){}
  LT.rendering=false;
  if(S.view==="letter"){ if(LT.tab==="pdf") ltPaint(); else ltPanel() }
  S.docThumbs[LT.path]=null;
}
$("#lt-save").onclick=()=>ltSave().then(()=>toast("Saved"));
$$("#lt-tabs [data-lt]").forEach(b=>b.onclick=async()=>{
  if(b.dataset.lt===LT.tab) return;
  if(LT.tab==="md"&&LT.mdText!=null) await ltSave();
  LT.tab=b.dataset.lt; ltPaint();
});
document.addEventListener("keydown",e=>{
  if(S.view!=="letter") return;
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="s"){ e.preventDefault(); ltSave().then(()=>toast("Saved")) }
});

/* The selection's own bar: the four things a letter prints. */
function ltLink(){
  const url=prompt("Link to","https://");
  if(url&&url!=="https://") document.execCommand("createLink",false,url.trim());
  ltChanged();
}
document.addEventListener("selectionchange",()=>{
  let bar=$("#lt-bar");
  const sel=document.getSelection(), ed=$("#lt-edit");
  const inside=S.view==="letter"&&LT.tab==="write"&&ed&&sel.rangeCount&&!sel.isCollapsed&&
    ed.contains(sel.anchorNode)&&ed.contains(sel.focusNode);
  if(!inside){ if(bar) bar.remove(); return }
  const r=sel.getRangeAt(0).getBoundingClientRect();
  if(!bar){
    bar=document.createElement("div"); bar.id="lt-bar"; bar.className="lt-bar";
    bar.setAttribute("role","toolbar"); bar.setAttribute("aria-label","Format the selection");
    bar.innerHTML='<button data-c="bold" title="Bold (⌘B)"><b>B</b></button>'+
      '<button data-c="italic" title="Italic (⌘I)"><i style="font-family:Georgia,serif">I</i></button>'+
      '<button data-c="link" title="Link (⌘K)">Link</button>'+
      '<button data-c="insertUnorderedList" title="Bullet list">• List</button>';
    bar.onmousedown=e=>e.preventDefault();
    bar.onclick=e=>{ const b=e.target.closest("[data-c]"); if(!b) return;
      if(b.dataset.c==="link") return ltLink();
      document.execCommand(b.dataset.c); ltChanged() };
    document.body.append(bar);
  }
  bar.style.left=Math.max(8,r.left+r.width/2-90)+"px";
  bar.style.top=Math.max(60,r.top-42)+"px";
  bar.querySelector('[data-c=bold]').setAttribute("aria-pressed",String(document.queryCommandState("bold")));
  bar.querySelector('[data-c=italic]').setAttribute("aria-pressed",String(document.queryCommandState("italic")));
});

/* Export: the PDF to attach, Word for recruiters who ask for it, plain text to
   paste into a form. */
$("#lt-export").onclick=e=>{
  e.stopPropagation();
  let m=$("#lt-menu"); if(m){ m.remove(); return }
  m=document.createElement("div"); m.className="lt-menu"; m.id="lt-menu"; m.setAttribute("role","menu");
  m.innerHTML=[["pdf","PDF","To attach to the application"],["docx","Word (.docx)","For recruiters who ask for one"],
    ["txt","Plain text","To paste into a form's cover letter box"]].map(([f,t,x])=>
    '<button role="menuitem" data-f="'+f+'">'+t+'<small>'+x+'</small></button>').join("");
  $("#lt-export").parentElement.append(m);
  m.onclick=async ev=>{ const b=ev.target.closest("[data-f]"); if(!b) return; m.remove();
    if(LT.dirty) await ltSave();
    window.open("/api/letter/export?path="+encodeURIComponent(LT.path)+"&format="+b.dataset.f+tok()) };
  setTimeout(()=>document.addEventListener("click",function off(){ m.remove();
    document.removeEventListener("click",off) }),0);
};
/* A letter an AI client is writing lands here as it writes, unless there are
   edits here it would overwrite. */
async function ltExternal(stamp){
  if(S.view!=="letter"||!LT.path||LT.dirty||!stamp||!LT.doc||stamp<=LT.doc.mtime+0.001) return;
  try{
    const d=await api("/api/doc?path="+encodeURIComponent(LT.path));
    Object.assign(LT,{doc:d, meta:Object.assign({},d.meta), body:d.body});
    ltPaint(); ltRender();
  }catch(e){}
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
  if("photo" in cv) out.push(["cv","photo"]);
  const sections=cv.sections||{};
  for(const name of Object.keys(sections)){
    (sections[name]||[]).forEach((it,i)=>{
      if(it===null||typeof it!=="object") out.push(["cv","sections",name,i]);
      else for(const k of Object.keys(it)) out.push(["cv","sections",name,i,k]);
    });
  }
  return out;
}
/* RenderCV types a section by what is in it: every section is a
   list[OneLineEntry] or a list[ExperienceEntry] and never a mixture, so a new
   entry has to have the shape of its neighbours and a new section has to pick
   one. These are RenderCV's own entry models, with every required field blank
   and the useful optional ones set to null.

   null and not "": an empty string is a valid str, but it is not a valid date,
   and `start_date: ""` fails validation outright. An absent value has to look
   absent. */
const ENTRY_TYPES=[
  ["ExperienceEntry","Experience","A job: employer, role, dates and bullets",
    ()=>({company:"",position:"",location:null,start_date:null,end_date:null,
          highlights:[""]})],
  ["EducationEntry","Education","A degree: school, subject, dates and bullets",
    ()=>({institution:"",area:"",degree:null,location:null,start_date:null,
          end_date:null,highlights:[""]})],
  ["NormalEntry","Project","Anything with a name, dates and bullets",
    ()=>({name:"",location:null,start_date:null,end_date:null,highlights:[""]})],
  ["OneLineEntry","One line","A label and its details, side by side",
    ()=>({label:"",details:""})],
  ["BulletEntry","Bullet","A single bullet point",()=>({bullet:""})],
  ["TextEntry","Text","A paragraph of prose",()=>""],
  ["PublicationEntry","Publication","Title, authors, journal, DOI",
    ()=>({title:"",authors:[""],journal:null,doi:null,date:null})],
  ["NumberedEntry","Numbered","A numbered line",()=>({number:""})],
  ["ReversedNumberedEntry","Numbered, counting down","A numbered line, in reverse",
    ()=>({reversed_number:""})],
];
/* What a section already holds, so "add" means "another one of these" rather
   than a question you have to answer every time. Keys rather than a guess at
   the model name: two types can share a shape, and the keys are what the form
   is going to draw anyway. */
function blankLike(list){
  const first=(list||[])[0];
  if(first===undefined) return null;
  if(first===null||typeof first!=="object") return "";
  const out={};
  for(const k of Object.keys(first))
    out[k]=Array.isArray(first[k])?[""]:(typeof first[k]==="number"?null:
      (/(^|_)date$/.test(k)?null:""));
  return out;
}
const sectionLabel=n=>String(n).replace(/_/g," ").replace(/^./,c=>c.toUpperCase());
function entryTitle(it,i){
  if(it===null||typeof it!=="object")
    return String(it||"").split(/\s+/).slice(0,4).join(" ")||("item "+(i+1));
  /* Every RenderCV entry type keeps its headline under a different key, and
     a publication or a bullet reading "entry 3" in the outline is no use. */
  return it.company||it.institution||it.name||it.title||it.label||it.position||
    it.bullet||("entry "+(i+1));
}
/* The line under the title. entryTitle answers "what is this", usually with an
   employer or a school -- and two jobs at the same company then read
   identically in the editor and the outline alike. This answers "which one",
   with the role and the years that actually separate them. */
const SUB_KEYS=["position","degree","area","title","label","authors"];
function entrySub(it){
  if(!it||typeof it!=="object") return "";
  const top=entryTitle(it,0), out=[];
  for(const k of SUB_KEYS){
    const v=it[k];
    if(typeof v==="string"&&v.trim()&&v.trim()!==top){ out.push(v.trim()); break }
  }
  const span=it.date||[it.start_date,it.end_date].filter(Boolean).join(" – ");
  if(span) out.push(String(span));
  return out.join(" · ");
}
function wordsIn(v){
  if(v==null) return 0;
  if(Array.isArray(v)) return v.reduce((a,x)=>a+wordsIn(x),0);
  if(typeof v==="object") return Object.values(v).reduce((a,x)=>a+wordsIn(x),0);
  return String(v).trim()?String(v).trim().split(/\s+/).length:0;
}

/* ---- documents ---------------------------------------------------------- */
/* The rail is grouped, because a CV and a cover letter are different kinds of
   thing and reading them as one list means reading every label to find either.
   The groups come from the server, which already knows -- the folder a
   document sits in is what decides it. */
const DOC_GROUPS=["My CVs","Cover letters","Applications"];
function renderDocs(docs){
  S.state.documents=docs;
  const host=$("#doclist");
  const newRow='<button class="row" id="doc-new"><span class="mark"></span>'+
    '<span class="lbl" style="color:var(--c200)">+ New document…</span></button>';
  if(!docs.length){
    host.innerHTML='<p style="color:var(--c300);font-size:12.5px;padding:6px 8px">'+
      'Nothing here yet.</p>'+newRow;
  }else{
    const groups=DOC_GROUPS.filter(g=>docs.some(d=>d.group===g));
    /* Only a document in another language than the base CV says which. */
    const srcLang=(baseFamily()[0]||{}).lang||"en";
    /* The base CV leads its group with its translations tucked under it,
       each named by its language rather than a fourth "my-cv". */
    const fam=baseFamily(), famAt=new Map(fam.map((m,i)=>[m.path,i]));
    const order=d=>famAt.has(d.path)?famAt.get(d.path):fam.length;
    host.innerHTML=groups.map(g=>{
      const inG=docs.filter(d=>d.group===g).map((d,i)=>[d,i])
        .sort((a,b)=>order(a[0])-order(b[0])||a[1]-b[1]).map(x=>x[0]);
      const rows=inG.map(d=>{
        const tr=famAt.get(d.path)>0;
        const other=(d.lang||"en")!==srcLang;
        const label=tr?langOf(d.lang).native:other?String(d.label).replace(/\.[a-z]{2}(-[a-z]{2})?$/i,""):d.label;
        const pp=S.pages[d.path];
        const job=S.jobs.find(j=>j.cv_path===d.path||j.letter_path===d.path);
        return '<button class="row'+(tr?" tr":"")+(d.path===S.path?" sel":"")+
          '" data-path="'+esc(d.path)+'" title="'+esc(d.path)+
          (d.base?"\ntailored from "+esc(d.base):"")+
          (d.ai?"\n"+esc(whoLabel(d.ai))+" worked on this "+
            ago(d.ai.at*1000)+" ago":"")+
          (job?"\n"+esc(job.title+" · "+job.company):"")+'">'+
          '<span class="mark"></span>'+
          '<span class="lbl">'+esc(label)+'</span>'+
          (other&&!tr?lchip(d.lang):tr?'<span class="flg-only">'+flag(d.lang)+'</span>':'')+
          (isBase(d.path)?'<span class="btag" title="The base CV: every tailored CV '+
            'starts as a copy of it">base</span>':'')+
          markHTML(d.ai,null,true)+
          (job?'<span class="tie" title="Linked to '+
            esc(job.title+" · "+job.company)+'"></span>':"")+
          '<span class="ct mono">'+(pp?pp+"pp":"")+'</span></button>';
      }).join("");
      /* Only worth naming the groups once there is more than one of them. */
      return (groups.length>1
        ? '<div class="rail-sub">'+esc(g==="My CVs"?"CVs":g)+'</div>' : "")+rows;
    }).join("")+newRow;
  }
  $$("#doclist [data-path]").forEach(b=>b.onclick=()=>{
    if(b.dataset.path===S.path) return;
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    openDoc(b.dataset.path);
  });
  $("#doc-new").onclick=()=>newDocumentSheet();
}

async function openDoc(path){
  if(isLetterPath(path)) return openLetter(path);
  closeOverlays();
  /* Which list you came from. Not a history stack -- one bit, read once on the
     way out. The application a document belongs to is still the better answer
     when there is one, and goBack asks for that first. */
  if(S.view==="jobs"||S.view==="docs") S.fromList=S.view;
  setView("cvs");
  S.path=path; S.dirty=false; S.savedAt=null; S.sel=null; S.openSection=null;
  S.prov=null; $("#provchip").hidden=true;
  S.render=null; S.renderMs=null; S.fill=null; S.themePages={}; S.zoomAuto=true;
  hideExternalChange();
  /* The Design panel still holds the last document's controls, and its inputs
     are read straight into the patch list. Empty it until it is rebuilt. */
  $("#dz-advanced").innerHTML=""; S.thumbs=null;
  $("#pane-page").innerHTML='<div class="skel" style="width:472px;height:668px"></div>';
  $("#btn-render").disabled=false;
  renderDocs(S.state.documents);
  try{
    const doc=await api("/api/doc?path="+encodeURIComponent(path));
    S.doc=doc;
    S.docMtime=doc.mtime;
    S.data=doc.data?JSON.parse(JSON.stringify(doc.data)):null;
    setProv(doc.prov);
    $("#yaml").value=doc.yaml; paint();
    const dz=(doc.data&&doc.data.design)||{};
    DZ.theme=dz.theme||null;
    setYamlError(doc.parse_error);
    paintTitle(); paintLink(); paintLang(); paintPhotoChip();
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
  const name=link?(link.title+" · "+link.company)
                 :(cv.headline?cv.headline+(cv.name?" · "+cv.name:""):(cv.name||(doc&&doc.label)||""));
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
        '" data-o="header"><span>Header</span>'+
        markHTML(provHeader(),null,true)+(baseHeader()?basebar():"")+
        '</button>';
  for(const name of Object.keys(sections)){
    const list=sections[name]||[];
    const on=S.openSection===name;
    const spath=["cv","sections",name];
    h+='<button class="orow'+(on?" sel":"")+'" data-o="section" data-name="'+esc(name)+'">'+
       '<span>'+esc(sectionLabel(name))+'</span>'+
       markHTML(provUnder(spath),null,true)+(baseUnder(spath)?basebar():"")+
       '<span class="ct mono">'+list.length+'</span></button>';
    if(on&&list.length){
      h+='<div class="okids">'+list.map((it,i)=>{
        const epath=["cv","sections",name,i];
        return '<button class="okid'+(S.sel&&S.sel.kind==="entry"&&S.sel.name===name&&S.sel.i===i
          ?" sel":"")+'" data-o="entry" data-name="'+esc(name)+'" data-i="'+i+'">'+
        esc(entryTitle(it,i))+markHTML(provUnder(epath),null,true)+
        (baseUnder(epath)?basebar():"")+'</button>';
      }).join("")+'</div>';
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
/* `from` says which pane the selection came from, so that pane is not rebuilt
   under the hands of whoever is using it. Everything else is the same work. */
function select(sel,from){
  if(sel&&sel.kind==="section"){
    const list=((S.data.cv.sections||{})[sel.name])||[];
    S.openSection=sel.name;
    sel=list.length?{kind:"entry",name:sel.name,i:0}:{kind:"section",name:sel.name};
  }else if(sel&&sel.kind==="entry"){ S.openSection=sel.name }
  S.sel=sel;
  buildOutline();
  /* The page first: the editor anchors itself to the highlighted block, and
     paintHits is what draws it. Placing the card before that happened left it
     pointing at whatever was selected a moment ago, which showed up as an
     editor that stayed put while the arrows walked the document. */
  trackSelectionOnPage();
  revealSelectedOnPage();
  /* Only refresh the editor if it is already open. Selecting from the outline
     is navigation -- it moves the highlight on the page and scrolls to it --
     and having that throw a form open over the page every time would put back
     the panel this replaced. */
  if(!$("#ed").hidden) openEditor();
  /* Rebuilding the form is how the mark gets into it, but not when the form
     is where the selection came from: that would tear out the field being
     typed in, taking the caret with it. Move the mark instead. */
  if(S.tab==="form") from==="form"?markFormBlock():buildForm();
  if(S.tab==="yaml") markYamlSelection();
}
function markFormBlock(){
  const sel=S.sel;
  $$("#pane-form .formblock").forEach(el=>el.classList.toggle("on",
    !!sel&&sel.kind==="entry"&&el.dataset.block===sel.name&&+el.dataset.bi===sel.i));
}

/* ---- the block editor ---------------------------------------------------
   Opened by clicking a block on the page, anchored beside it. */
function openEditor(){
  const ed=$("#ed");
  if(!S.sel||!(S.data&&S.data.cv)){ closeEditor(); return }
  ed.hidden=false;
  buildInspector();
  /* Nothing above the first block and nothing below the last, so say so rather
     than leaving two buttons that silently do nothing at the ends. */
  const all=blockOrder(), at=all.findIndex(x=>sameSel(x,S.sel));
  $("#ed-prev").disabled=at<=0;
  $("#ed-next").disabled=at<0||at>=all.length-1;
  /* The sheet changes size here, so it happens before anything is measured:
     a block measured first is measured where it used to be. */
  makeRoom(true); refit();
  revealSelectedOnPage();
  placeEditor();
}
/* Walking the document with the arrows has to bring the page along, or the
   editor fills with an entry you cannot see. Only when the block is actually
   out of the pane: if you clicked it, it is already in front of you, and
   scrolling under the click would be the app moving on its own. */
function revealSelectedOnPage(){
  const pane=$("#pane-page");
  const hit=pane&&pane.querySelector(".pgwrap .hit.sel");
  if(!hit) return;
  const p=pane.getBoundingClientRect(), b=hit.getBoundingClientRect();
  const pad=24;
  if(b.top>=p.top+pad&&b.bottom<=p.bottom-pad) return;
  /* Instant, because placeEditor measures the block straight after this and a
     smooth scroll would hand it a rectangle still in motion. */
  pane.scrollTop+=b.top-p.top-pad;
}
function closeEditor(){
  const ed=$("#ed");
  if(ed.hidden) return;
  ed.hidden=true;
  ed.removeAttribute("style");
  makeRoom(false); refit();
}
/* Beside the block, never on top of it: you have to be able to read what you
   are editing.

   Fixed to the viewport rather than absolute inside .pgwrap. Anchoring it to
   the page sounded right -- it would travel with the sheet for free -- but the
   pane scrolls and clips, so a block near the left edge produced a negative
   offset and the whole label column was cut off against the rail. Positioned
   against the window instead, it cannot be clipped, and the scroll handler
   re-places it. */
/* Whether the sheet can spare the width. It depends on the sheet and the pane,
   not on which block is selected, so it does not flip while the arrows walk
   the document -- the page shifts once when the card comes out and holds
   still. On a window too narrow for both, the card overlaps instead: a page
   pushed half out of its own pane would be worse. */
function makeRoom(on){
  const pane=$("#pane-page"), img=pane.querySelector(".pg");
  const room=($("#ed").offsetWidth||380)+26;
  pane.style.setProperty("--ed-room",room+"px");
  /* Auto-fit resizes the sheet into whatever is left, so there is always room
     for the card. A zoom you chose is a fixed size, and the card only stands
     beside it when it genuinely fits: shrinking your page to make space for a
     panel would be the app overruling a choice you made. */
  const fits=S.zoomAuto||(!!img&&pane.clientWidth-52-room>=img.offsetWidth);
  pane.classList.toggle("ed-open",!!on&&fits);
}
function placeEditor(){
  const ed=$("#ed");
  const pane=$("#pane-"+S.tab);
  if(!pane) return;
  const paneBox=pane.getBoundingClientRect();
  const hit=S.tab==="page"&&$("#pane-page .pgwrap .hit.sel");
  ed.style.position="fixed";
  const w=ed.offsetWidth||380, h=ed.offsetHeight||320, gap=14, edge=12;

  if(!hit){
    /* Off the page tab, or before a render, there is no block to point at, so
       it sits in the corner of the pane rather than at nothing. */
    ed.dataset.side="none";
    ed.style.left=Math.round(paneBox.right-w-edge)+"px";
    ed.style.top=Math.round(paneBox.top+edge)+"px";
    return;
  }
  const b=hit.getBoundingClientRect();
  /* Whichever side has room, preferring the right; never over the rail. */
  const fitsRight=b.right+gap+w<=paneBox.right+ (innerWidth-paneBox.right) - edge;
  const fitsLeft=b.left-gap-w>=paneBox.left+edge;
  let x, side;
  if(fitsRight){ x=b.right+gap; side="right" }
  else if(fitsLeft){ x=b.left-gap-w; side="left" }
  else{
    /* Neither margin is wide enough, so it overlaps the sheet on the side
       with the most room rather than being pushed off the pane. */
    const roomRight=innerWidth-b.right, roomLeft=b.left-paneBox.left;
    side=roomRight>=roomLeft?"right":"left";
    x=side==="right"?innerWidth-w-edge:paneBox.left+edge;
  }
  ed.dataset.side=side;
  ed.style.left=Math.round(Math.min(Math.max(x,paneBox.left+edge),
                                    innerWidth-w-edge))+"px";
  /* Level with the top of the block, held inside the pane so a block low on
     the sheet does not open an editor over the status bar. */
  const top=Math.min(Math.max(b.top,paneBox.top+edge),
                     Math.max(paneBox.top+edge,paneBox.bottom-h-edge));
  ed.style.top=Math.round(top)+"px";
  /* When it had to be held back like that, the card is no longer level with
     the block, so the pointer slides down the edge to keep aiming at it --
     otherwise it points confidently at the wrong paragraph. */
  ed.style.setProperty("--ptr",
    Math.round(Math.min(Math.max(b.top-top+6,10),Math.max(10,h-20)))+"px");
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
  /* The mark sits in the label rather than beside the input: the input is
     where you type, and anything parked in it reads as part of the value.

     A list is one control here but many fields underneath, so it answers for
     everything it contains -- otherwise a rewritten bullet shows no mark in
     the Form tab, where the whole list is a single textarea. */
  const arr=Array.isArray(value);
  const mark=arr?markHTML(provUnder(path),null,true)+
                 (baseUnder(path)?basebar():"")
                :markHTML(provOf(path),path);
  return '<label title="'+esc(label)+'">'+esc(human(label))+
    mark+'</label>'+inputFor(path,value,opts);
}
/* Monospace is for things you read character by character -- a URL or a
   DOI. A phone number and a date are prose, and setting them in mono next
   to sans-set siblings looks like a bug rather than a decision. */
const MONO_KEYS=/^(url|website|doi)$/;

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
        /* An emptied date has to be absent, not empty. RenderCV accepts "" for
           any other optional string, but a date is parsed, and `start_date: ""`
           fails validation outright -- so clearing one broke the render of a
           document that was fine, and the blank entries Add creates start out
           with exactly these fields empty. */
        if(v==="") v=(/(^|_)date$/.test(String(path[path.length-1]))||was===null)
          ? null : v;
      }
      setAt(S.data,path,v);
      /* Keep growing as you type, or it clips again the moment the text runs
         past the bottom of the box. */
      if(el.tagName==="TEXTAREA") autoGrow(el);
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
    head.textContent="Header";
    meta.textContent=[cv.name,wordsIn(Object.fromEntries(
      HEADER_KEYS.map(k=>[k,cv[k]])))+" words"].filter(Boolean).join(" · ");
    body.innerHTML='<div class="fg">'+HEADER_KEYS.map(k=>
      fieldRow(k,["cv",k],cv[k],{mono:MONO_KEYS.test(k)})).join("")+'</div>';
    wireInspector(); return;
  }
  const list=(cv.sections||{})[sel.name]||[];
  if(sel.kind==="section"||!list.length){
    head.textContent=sectionLabel(sel.name);
    meta.textContent=list.length+" item"+(list.length===1?"":"s");
    body.innerHTML='<p class="note muted">This section is empty. Add entries in the '+
      'YAML tab.</p>';
    wireInspector(); return;
  }
  const it=list[sel.i];
  head.textContent=sectionLabel(sel.name)+" · "+entryTitle(it,sel.i);
  meta.textContent=[entrySub(it),wordsIn(it)+" words"].filter(Boolean).join(" · ");

  if(it===null||typeof it!=="object"){
    body.innerHTML='<div class="block"><span class="blabel">Text</span>'+
      inputFor(["cv","sections",sel.name,sel.i],it,{multi:true})+'</div>';
    wireInspector(); return;
  }
  const scalars=Object.keys(it).filter(k=>!Array.isArray(it[k]));
  const arrays=Object.keys(it).filter(k=>Array.isArray(it[k]));
  let h='';
  if(scalars.length) h+='<div class="fg">'+scalars.map(k=>
    fieldRow(k,["cv","sections",sel.name,sel.i,k],it[k],{mono:MONO_KEYS.test(k)})).join("")+'</div>';
  arrays.forEach(k=>{ h+=arrayBlock(k,["cv","sections",sel.name,sel.i,k],it[k]) });
  body.innerHTML=h;
  wireInspector();
}

/* Bullets are a card of rows rather than one blob of text: the row you are
   editing is the one highlighted, and it can be added to or taken away. */
function arrayBlock(label,path,list){
  const p=esc(JSON.stringify(path));
  return '<div class="block"><span class="blabel">'+esc(label.replace(/_/g," "))+
    markHTML(provUnder(path),null,true)+(baseUnder(path)?basebar():"")+'</span>'+
    '<div class="card" data-arr='+"'"+p+"'"+'>'+
    (list.length?list.map((x,i)=>
      '<div class="crow" data-i="'+i+'"><span class="cidx mono"><span>'+(i+1)+
      '</span>'+markHTML(provOf(path.concat(i)),path.concat(i))+'</span>'+
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
}

/* Attaching a document to an application it was written for, from the document
   side. The Jobs screen can already pick a document for an application; this is
   the same join made from the end you are more often standing at. */
function linkJobSheet(){
  const isLetter=(S.state.documents||[]).some(
    d=>d.path===S.path&&d.group==="Cover letters");
  const key=isLetter?"letter_path":"cv_path";
  const open=S.jobs.filter(j=>!j[key]);
  const now=linkedJob();
  openSheet(
    '<div><h3>'+(now?"Linked application":"Link to an application")+'</h3><p>'+
    (now
      ? esc(docLabel(S.path))+' is the '+(isLetter?"cover letter":"CV")+
        ' on <b>'+esc(now.title)+' · '+esc(now.company)+'</b>. Pick another to '+
        'move it, or unlink it below.'
      : esc(docLabel(S.path))+' becomes the '+(isLetter?"cover letter":"CV")+
        ' on the application you pick. A document belongs to one application, '+
        'and an application takes one of each.')+'</p></div>'+
    (open.length
      ? '<div class="fg w88"><label>Application</label><select id="lj-job">'+
        open.map(j=>'<option value="'+esc(j.id)+'">'+esc(j.title)+' · '+
          esc(j.company)+'</option>').join("")+'</select></div>'
      : '<div class="fg w88"><p class="note muted">Every application already has '+
        'one. Start a new application, or swap the document over from the '+
        'Applications screen.</p></div>')+
    '<div class="foot">'+
    (now?'<button class="sbtn" id="lj-show">Show in Applications</button>'+
         '<button class="sbtn danger" id="lj-unlink">Unlink</button>':"")+
    '<div class="grow"></div>'+
    '<button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn" id="lj-new">New application…</button>'+
    (open.length?'<button class="sbtn primary" id="lj-ok">'+
      (now?"Move it":"Link")+'</button>':"")+
    '</div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  if(now){
    $("#lj-show").onclick=()=>{ closeSheet(); setView("jobs"); selectJob(now.id) };
    $("#lj-unlink").onclick=async()=>{
      const k=now.cv_path===S.path?"cv_path":"letter_path";
      try{
        await post("/api/jobs/update",{id:now.id,[k]:null});
        await loadJobs(); closeSheet(); renderDocs(S.state.documents);
        paintTitle(); paintLink(); toast("Unlinked");
      }catch(e){ toast(e.message,true) }
    };
  }
  $("#lj-new").onclick=()=>{ closeSheet(); newJobSheet({[key]:S.path}) };
  const ok=$("#lj-ok");
  if(ok) ok.onclick=async()=>{
    ok.disabled=true;
    try{
      await post("/api/jobs/update",{id:$("#lj-job").value,[key]:S.path});
      await loadJobs(); closeSheet(); buildInspector();
      renderDocs(S.state.documents); paintTitle(); paintLink();
      toast("Linked");
    }catch(e){ toast(e.message,true); ok.disabled=false }
  };
}
const docLabel=p=>{
  const d=(S.state.documents||[]).find(x=>x.path===p);
  return d?d.label:String(p||"").split("/").pop();
};

/* The application this document was written for. Knowing it here is what
   makes "Show in Jobs" possible without hunting through the table. */
function linkedJob(){
  return S.jobs.find(j=>j.cv_path===S.path||j.letter_path===S.path)||null;
}
/* ---- the Form tab: the same fields, whole document at once ---------------- */
/* The selection is the thread through all three views. Losing it when you
   switch tabs turns one cockpit into three unrelated views of a YAML file:
   you spot something wrong on the page, switch to Form to fix it, and have
   to find the entry again by eye. */
const selMark=sel=>sameBlock({k:sel.kind,name:sel.name,i:sel.i},S.sel)?" on":"";
/* Bring the marked block into view without yanking the pane around when it
   is already on screen. */
function revealSelected(root){
  const el=root.querySelector(".formblock.on");
  if(!el) return;
  const box=el.getBoundingClientRect(), pane=root.getBoundingClientRect();
  if(box.top<pane.top||box.bottom>pane.bottom)
    el.scrollIntoView({block:"center",behavior:"auto"});
}
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
      /* Every entry gets the same head, a text entry included: it is where the
         remove lives, and an entry you can add but not take away again is half
         a control. */
      const hd='<div class="entry-hd"><b>'+esc(entryTitle(it,i))+'</b>'+
        '<span class="sub">'+esc(entrySub(it))+'</span>'+
        '<button class="mini rm" data-rm="'+esc(name)+'" data-i="'+i+
        '" title="Remove this entry" aria-label="Remove '+esc(entryTitle(it,i))+
        '">&#10005;</button></div>';
      const body=(it===null||typeof it!=="object")
        ? '<div class="fg wide">'+
            fieldRow("text",["cv","sections",name,i],it,{multi:true})+'</div>'
        : '<div class="fg wide">'+Object.keys(it).map(k=>
            fieldRow(k,["cv","sections",name,i,k],it[k],{mono:MONO_KEYS.test(k)})).join("")+
          '</div>';
      h+='<div class="entry formblock'+selMark({kind:"entry",name:name,i:i})+
        '" data-block="'+esc(name)+'" data-bi="'+i+'">'+hd+body+'</div>';
    });
    h+='<div class="addrow"><button class="obtn" data-add="'+esc(name)+
      '">Add to '+esc(sectionLabel(name))+'</button></div>';
    h+='</div></details>';
  }
  h+='<div class="addrow end"><button class="obtn" id="add-section">'+
    'Add a section</button></div>';
  $("#pane-form").innerHTML=h;
  /* The form's multiline fields ship at rows="3" and never grew, so a summary
     of four lines showed three and a half and the last one was cut through
     the middle of the letters. The inspector has always grown its bullets;
     the form simply never asked. */
  $$("#pane-form textarea").forEach(autoGrow);
  revealSelected($("#pane-form"));
  $$("#pane-form [data-add]").forEach(b=>b.onclick=()=>addEntry(b.dataset.add));
  $$("#pane-form [data-rm]").forEach(b=>b.onclick=()=>removeEntry(b.dataset.rm,+b.dataset.i));
  $("#add-section").onclick=addSectionSheet;
}
/* ---- adding and removing whole entries -----------------------------------
   Structure cannot go through the working copy the way a field edit does. A
   patch names a path that is already in the document, and a new entry is by
   definition a path that is not, so this asks the server in as many words and
   takes back the file it wrote. Whatever is in the form travels with it -- see
   save(ops) -- or the reload would hand back the document as it stood before
   the last few keystrokes. */
async function addEntry(name){
  const list=((S.data&&S.data.cv&&S.data.cv.sections||{})[name])||[];
  const blank=blankLike(list);
  /* Neighbours to copy: adding is a button, not a questionnaire. Only an empty
     section has nothing to go on, and then there is a real question to ask. */
  if(blank===null)
    return typeSheet("Add to "+sectionLabel(name),
      "This section is empty, so there is nothing to copy the shape of.",
      null,(make)=>putEntry(name,list.length,make()));
  putEntry(name,list.length,blank);
}
async function putEntry(name,at,value){
  await save([{op:"add_entry",section:name,at:at,value:value}]);
  select({kind:"entry",name:name,i:at});
  /* Straight into the first field: you pressed Add because you have something
     to type, and a blank entry with the caret somewhere else is a second
     thing to do. */
  const first=$("#pane-form .formblock.on input,#pane-form .formblock.on textarea");
  if(first){ first.focus(); first.select&&first.select() }
}
async function removeEntry(name,at){
  const list=((S.data&&S.data.cv&&S.data.cv.sections||{})[name])||[];
  if(!list.length) return;
  const last=list.length===1;
  if(!confirm('Remove "'+entryTitle(list[at],at)+'" from '+sectionLabel(name)+"?"+
    (last?"\n\nIt is the only entry, so the section goes with it: RenderCV "+
          "reads a section's type from its entries and cannot render an empty "+
          "one.":"")+
    "\n\nThis is written to the file straight away."))
    return;
  await save([{op:"remove_entry",section:name,at:at}]);
  const left=(((S.data&&S.data.cv&&S.data.cv.sections)||{})[name])||[];
  select(left.length?{kind:"entry",name:name,i:Math.min(at,left.length-1)}:null);
}
function addSectionSheet(){
  typeSheet("New section",
    "A section is a list of one kind of entry -- RenderCV reads its type from "+
    "what is in it -- so it starts with one blank entry of the kind you pick.",
    "certifications",(make,name)=>addSection(name,make()));
}
async function addSection(name,value){
  await save([{op:"add_section",name:name,value:[value]}]);
  showTab("form");
  select({kind:"entry",name:name,i:0});
  const first=$("#pane-form .formblock.on input,#pane-form .formblock.on textarea");
  if(first) first.focus();
}
/* One sheet for both questions. A new section needs a name as well as a kind;
   an empty existing section only needs the kind, so the name row is left out
   rather than shown greyed. */
function typeSheet(title,blurb,namePlaceholder,go){
  const taken=Object.keys((S.data&&S.data.cv&&S.data.cv.sections)||{});
  openSheet(
    '<div><h3 id="sheet-title">'+esc(title)+'</h3><p>'+esc(blurb)+'</p></div>'+
    '<div class="fg w88">'+
      (namePlaceholder?'<label>Name</label><input id="ts-name" autocomplete="off" '+
        'placeholder="'+esc(namePlaceholder)+'">':"")+
      '<label>Entries</label><select id="ts-type">'+
        ENTRY_TYPES.map(([id,label,hint])=>'<option value="'+esc(id)+'">'+
          esc(label)+' \u2014 '+esc(hint)+'</option>').join("")+
      '</select>'+
    '</div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="ts-go">Add</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#ts-go").onclick=()=>{
    const make=(ENTRY_TYPES.find(t=>t[0]===$("#ts-type").value)||[])[3];
    if(!make) return;
    let name=null;
    if(namePlaceholder){
      /* A section name is a YAML key and a heading at once. Fold it to the
         shape the rest of the file uses and let sectionLabel put the capital
         back for display, rather than carrying "Certifications & Awards" into
         the source as a key. */
      name=$("#ts-name").value.trim().toLowerCase()
        .replace(/[^a-z0-9]+/g,"_").replace(/^_+|_+$/g,"");
      if(!name) return toast("Give the section a name.",true);
      if(taken.includes(name)) return toast("There is already a "+
        sectionLabel(name)+" section.",true);
    }
    closeSheet();
    go(make,name);
  };
}

/* Put the caret in a field and the page goes to that entry: highlights it, and
   scrolls to it if it had drifted off. This is what the split is for -- the
   two halves are one document, not a form and a picture of a form. It replaced
   a "Show on page" button, which made sense when the page was a tab away and
   read as furniture once it was sitting right there.

   focusin rather than click: the caret arrives by Tab as often as by mouse. */
$("#pane-form").addEventListener("focusin",e=>{
  const block=e.target.closest(".formblock");
  if(!block||!block.dataset.block) return;
  const at={kind:"entry",name:block.dataset.block,i:+block.dataset.bi};
  if(sameSel(at,S.sel)) return;
  select(at,"form");
});
bindFields($("#pane-form"));
bindFields($("#insp-body"));

/* ---- tabs, zoom and paging ------------------------------------------------ */
function showTab(tab){
  /* The card belongs to the page on its own: it is opened by a block and
     points at one. Beside a form or the source there is a better place for
     those fields already -- the pane on the left, which follows the
     selection -- so the card goes away rather than stacking a third copy of
     the same entry on top of the second. */
  if(tab!=="page") closeEditor();
  S.tab=tab;
  $$("#edtabs button").forEach(x=>
    x.setAttribute("aria-selected",String(x.dataset.tab===tab)));
  $("#pane-form").hidden=tab!=="form";
  $("#pane-yaml").hidden=tab!=="yaml";
  $("#split").hidden=tab==="page";
  if(tab==="yaml"){ paint(); markYamlSelection() }
  if(tab==="form") buildForm();   /* re-read the model, in case the page moved on */
  trackSelectionOnPage();         /* the selection may have moved while away */
  refit();                        /* the page just got a different amount of room */
  setPref("tab",tab);
}
$$("#edtabs button").forEach(b=>b.onclick=()=>showTab(b.dataset.tab));

/* ---- the divider ---------------------------------------------------------
   Where it sits is the one layout choice in the app, so it is remembered: a
   form you have to widen again every time you open it is a form you stop
   using. A share rather than a width, so it survives the window changing
   size. The stops keep both halves usable -- a 90% form with a sliver of page
   beside it is not a split view, it is the old tab with a decoration. */
function setSplit(pct){
  const v=Math.min(68,Math.max(26,pct));
  $("#panes").style.setProperty("--split",v.toFixed(2)+"%");
  $("#split").setAttribute("aria-valuenow",Math.round(v));
  return v;
}
function splitAt(clientX){
  const b=$("#panes").getBoundingClientRect();
  return setSplit((clientX-b.left)/b.width*100);
}
(()=>{
  const bar=$("#split");
  let on=false, frame=0;
  bar.addEventListener("pointerdown",e=>{
    on=true; bar.classList.add("on"); bar.setPointerCapture(e.pointerId);
    document.body.classList.add("dragging");
    e.preventDefault();
  });
  bar.addEventListener("pointermove",e=>{
    if(!on) return;
    /* One layout per frame. Dragging fires faster than the page can be
       re-fitted, and doing the work every event makes the drag feel heavier
       than the thing it is moving. */
    if(frame) return;
    frame=requestAnimationFrame(()=>{ frame=0; splitAt(e.clientX); refit() });
  });
  const drop=e=>{
    if(!on) return;
    on=false; bar.classList.remove("on");
    document.body.classList.remove("dragging");
    if(frame){ cancelAnimationFrame(frame); frame=0 }
    setPref("split",splitAt(e.clientX));
    refit();
  };
  bar.addEventListener("pointerup",drop);
  bar.addEventListener("pointercancel",drop);
  /* Reachable without a mouse, in the steps someone dragging would land on. */
  bar.addEventListener("keydown",e=>{
    const step=e.key==="ArrowLeft"?-2:e.key==="ArrowRight"?2:0;
    if(!step) return;
    e.preventDefault();
    const b=$("#panes").getBoundingClientRect();
    const now=$("#pane-form").hidden?$("#pane-yaml"):$("#pane-form");
    setPref("split",setSplit((now.offsetWidth/b.width*100)+step));
    refit();
  });
})();
setSplit(+prefs().split||46);
$("#z-in").onclick=()=>setZoom(S.zoom+.1);
$("#z-out").onclick=()=>setZoom(S.zoom-.1);
/* The readout is also the way back: once you have zoomed, one click refits. */
$("#z-lvl").onclick=()=>{ S.zoomAuto=true; refit() };
$("#pg-prev").onclick=()=>setPage(S.page-1);
$("#pg-next").onclick=()=>setPage(S.page+1);
function setZoom(z){ S.zoom=Math.min(3,Math.max(.2,z)); S.zoomAuto=false; refit() }
function setPage(i){
  const n=(S.render&&S.render.pngs.length)||0;
  S.page=Math.min(Math.max(0,i),Math.max(0,n-1)); paintPage();
}

/* ---- rendering ------------------------------------------------------------ */
async function save(ops){
  if(!S.path||S.busy) return;
  S.busy=true; $("#btn-render").disabled=true;
  try{
    const body=S.tab==="yaml" ? {path:S.path,yaml:$("#yaml").value}
                              : {path:S.path,patches:collectPatches()};
    if(ops&&ops.length) body.ops=ops;
    const r=await post("/api/save",body);
    S.doc=r; S.data=r.data?JSON.parse(JSON.stringify(r.data)):null; paintLang(); paintPhotoChip();
    /* Our own write, so take its timestamp: the poll must not read it back as
       somebody else having changed the file. */
    S.docMtime=r.mtime; hideExternalChange();
    S.dirty=false; S.savedAt=Date.now();
    setProv(r.prov);
    $("#yaml").value=r.yaml; paint(); setYamlError(r.parse_error);
    buildOutline(); buildInspector(); if(S.tab==="form") buildForm();
    /* The Design screen measures "changed" against the file, which just moved. */
    if(!$("#ovl-design").hidden&&S.schema){ paintAdvanced(); paintDesignNav() }
    /* A refused operation is not a failure of the save, so it cannot be left
       to the catch. Saying nothing would be worse: you press Add, the file is
       rewritten without it, and the form comes back looking untouched. */
    (r.missed||[]).filter(m=>m.op).forEach(m=>toast(
      "Could not "+String(m.op.op||"do that").replace(/_/g," ")+": "+m.why,true));
    await doRender();
  }catch(e){ toast(e.message,true) }
  finally{ S.busy=false; $("#btn-render").disabled=false; paintStatus() }
}
$("#btn-render").onclick=()=>save();   /* not `save`: the click event is not ops */

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
  const b=S.state&&S.state.base;
  if(b&&b.path===S.path&&r.pngs.length){
    S.baseThumb={path:S.path,png:r.pngs[0],failed:false};
    mountBase($("#docbase"),"bhero"); mountBase($("#baserow"),"baserow");
  }
  if(DZ.theme) S.themePages[DZ.theme]=r.pages;
  if(S.page>=r.pngs.length) S.page=Math.max(0,r.pngs.length-1);
  paintPage();
  if(grew) renderDocs(S.state.documents);
  S.fill=r.pngs.length?await measureFill(r.pngs[r.pngs.length-1]+tok()):null;
  paintBudget();
  if(!$("#ovl-design").hidden){ paintThemes(); paintEffect() }
}

/* The page is the thing you came to look at, so it gets the room. "100%" means
   actual size -- the sheet at 96dpi, the width it would print -- rather than an
   arbitrary base that made the readout lie by a factor of 1.7. RenderCV renders
   at 144dpi, so a CSS pixel at 100% is two thirds of an image pixel. */
const PAGE_GUTTER=52;
function pageCssWidth(img){ return img.naturalWidth*(2/3) }
/* Fit to the width, not to the whole sheet. A CV is read top to bottom, and
   fitting its height into a laptop window puts the body type at about four
   pixels -- unreadable, on the one screen whose whole job is reading it.
   Capped at 150%, where the 144dpi render stops having pixels to spare. */
/* The width the card is holding, when it is out. Auto-fit has to subtract it:
   fitting the sheet to the whole pane produces a sheet exactly as wide as the
   pane, which leaves nothing to shift it into, so the page never made room and
   the card always ended up over the text. The page fits beside the card. */
function edRoom(host){
  return host.classList.contains("ed-open")
    ? parseFloat(getComputedStyle(host).getPropertyValue("--ed-room"))||0 : 0;
}
function fitZoom(host,img){
  const w=(host.clientWidth-PAGE_GUTTER*2-edRoom(host))/pageCssWidth(img);
  return Math.max(.2,Math.min(1.5,w));
}
/* Resizing the sheet is not a new render, and rebuilding the pane for it made
   the page blink every time you dragged the divider or touched the zoom. The
   hits and the margin marks are placed in percentages of the wrapper, so they
   follow the image on their own and only a new render has to redraw them. */
function refit(){
  const host=$("#pane-page"), img=host.querySelector(".pg");
  if(!img||!img.naturalHeight) return;
  if(S.zoomAuto) S.zoom=fitZoom(host,img);
  img.style.width=Math.round(pageCssWidth(img)*S.zoom)+"px";
  $("#z-lvl").textContent=Math.round(S.zoom*100)+"%";
  $("#z-lvl").classList.toggle("auto",!!S.zoomAuto);
}
function paintPage(){
  const host=$("#pane-page"), r=S.render;
  if(!r||!r.pngs.length){ $("#pg-idx").textContent="–"; return }
  const url=r.pngs[S.page]+tok();
  const draw=()=>{ refit(); paintHits() };
  host.innerHTML='<div class="pgwrap"><img class="pg" src="'+url+'" alt="Page '+
    (S.page+1)+'"></div>';
  const img=host.querySelector(".pg");
  if(img.complete&&img.naturalHeight) draw();
  else img.onload=()=>{ if(host.querySelector(".pg")===img) draw() };
  const nomap=$("#nomap");
  if(r.map&&r.map.length){ nomap.hidden=true }
  else{
    nomap.hidden=false;
    nomap.textContent="page not clickable";
    nomap.title=(r.map_why||"The render could not be mapped back to the "+
      "document.")+"\n\nEverything else works: the page is real, and the "+
      "outline and the form still select.";
  }
  $("#pg-idx").textContent=(S.page+1)+" / "+r.pngs.length;
  $("#pg-prev").disabled=S.page===0;
  $("#pg-next").disabled=S.page>=r.pngs.length-1;
}
/* Re-fit while the window is being resized, but only while nobody has chosen a
   zoom of their own. */
/* The page is on screen in every tab now, so it refits in every tab. */
addEventListener("resize",()=>{
  /* In this order: the room the card holds decides the fit, the fit decides
     where the block is, and the block decides where the card goes. */
  if(!$("#ed").hidden) makeRoom(true);
  refit();
  if(!$("#ed").hidden) placeEditor();
});

/* ---- clicking the page --------------------------------------------------
   Every block of the rendered page is a band, and the bands tile the page, so
   a click always lands on something rather than between two things. They are
   the same selection the outline makes: point at a job on the page and the
   inspector is editing that job. */
const bandsOn=page=>((S.render&&S.render.map)||[]).filter(b=>b.page===page+1);
const sameBlock=(b,sel)=>!!sel&&(
  b.k==="header" ? sel.kind==="header"
  : b.k==="section" ? sel.kind==="section"&&sel.name===b.name
  : sel.kind==="entry"&&sel.name===b.name&&sel.i===b.i);

function bandLabel(b){
  if(b.k==="header") return "Header";
  const cv=(S.data&&S.data.cv)||{};
  const label=sectionLabel(b.name);
  if(b.k==="section") return label;
  const it=((cv.sections||{})[b.name]||[])[b.i];
  return label+" · "+(it===undefined?("entry "+(b.i+1)):entryTitle(it,b.i));
}

function paintHits(){
  const wrap=$("#pane-page .pgwrap"), img=wrap&&wrap.querySelector(".pg");
  if(!wrap||!img||!img.naturalHeight) return;
  wrap.querySelectorAll(".hit").forEach(el=>el.remove());
  const box=(S.render&&S.render.map_box)||null;
  /* The sheet in points, taken from the same measurement the bands are in.
     Falling back to the image only when the map could not report it: "the PNG
     is 144dpi, so two pixels to a point" was a guess about somebody else's
     renderer, in a file whose every other number is measured. */
  const pageH=(box&&box.page_height)||img.naturalHeight/2;
  const pageW=(box&&box.page_width)||img.naturalWidth/2;
  /* Hug the text column when we know where it is. Spanning the whole sheet
     reads as a band laid across the paper rather than a mark on the entry. */
  const pad=6;
  const left=box?Math.max(0,(box.x0-pad)/pageW*100):0;
  const right=box?Math.max(0,(pageW-box.x1-pad)/pageW*100):0;
  const pct=y=>(Math.max(0,Math.min(pageH,y))/pageH)*100;
  wrap.insertAdjacentHTML("beforeend", bandsOn(S.page).map(b=>{
    const top=pct(b.y0), bottom=b.y1==null?100:pct(b.y1);
    if(bottom-top<=0) return "";
    const name=bandLabel(b);
    return '<button class="hit'+(sameBlock(b,S.sel)?" sel":"")+'" tabindex="-1"'+
      ' style="top:'+top.toFixed(3)+'%;height:'+(bottom-top).toFixed(3)+'%;'+
      'left:'+left.toFixed(3)+'%;right:'+right.toFixed(3)+'%"'+
      ' data-k="'+b.k+'" data-name="'+esc(b.name==null?"":b.name)+'"'+
      ' data-i="'+(b.i==null?"":b.i)+'" title="'+esc(name)+'"'+
      ' aria-label="Edit '+esc(name)+'"></button>';
  }).join(""));
  wrap.querySelectorAll(".hit").forEach(el=>el.onclick=()=>{
    const k=el.dataset.k;
    if(k==="header") select({kind:"header"});
    else if(k==="section") select({kind:"section",name:el.dataset.name});
    else select({kind:"entry",name:el.dataset.name,i:+el.dataset.i});
    /* Beside a form or the source, clicking a block is navigation: select()
       has already scrolled the left pane to it and marked it. */
    if(S.tab==="page") openEditor();
  });
  paintPageMarks(wrap,img,left);
}

/* The mark in the margin, beside the block it belongs to.

   It goes to the left of the text column, which the band map already measures
   as box.x0 -- that strip is blank on every theme, so the mark reads as an
   annotation on the page rather than something printed on it. Which it is: the
   page underneath is the PNG RenderCV produced, and this is a div on top. What
   you export has never been near it.

   Entry granularity, because that is the granularity of the bands: the probes
   in cv_map sit at column 0 and a theme nests its bullets inside the entry
   call, so there is nothing to hang a per-bullet mark on yet. A mark against
   the job you rewrote is the useful half of that anyway. */
/* Entries and the header only. A section heading is not a thing anybody edits
   -- its mark would come from its entries, and it sits directly above the
   first of them, so marking both puts two marks a few millimetres apart
   saying the same thing. The outline is where a section answers for itself. */
function bandProv(b){
  if(b.k==="header") return provHeader();
  if(b.k==="section") return null;
  return provUnder(["cv","sections",b.name,b.i]);
}
function paintPageMarks(wrap,img,leftPct){
  wrap.querySelectorAll(".pgmark").forEach(el=>el.remove());
  if(!S.prov) return;
  const pageH=img.naturalHeight/2;
  const seen=new Set();
  const html=bandsOn(S.page).map(b=>{
    const p=bandProv(b);
    if(!p) return "";
    /* A block pushed over a page break owns a band on each page it touches;
       one mark per block per page is the honest count. */
    const id=b.k+"/"+b.name+"/"+b.i;
    if(seen.has(id)) return "";
    seen.add(id);
    /* The first block of the page reaches up into the top margin, so its y0
       is zero and a mark placed there hangs off the sheet. Hold it far enough
       down to sit beside the text it marks. */
    const top=(Math.max(14,Math.min(pageH,b.y0))/pageH)*100;
    return '<span class="pgmark" data-by="'+esc(p.by)+'" title="'+
      esc(whoLabel(p)+" wrote something in "+bandLabel(b)+", "+
          ago(p.at*1000)+" ago")+
      '" style="top:'+top.toFixed(3)+'%;left:'+leftPct.toFixed(3)+'%">'+
      (p.by==="ai"?"&#9679;":'<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#'+
        esc(p.by)+'-mark"/></svg>')+'</span>';
  }).join("");
  wrap.insertAdjacentHTML("beforeend",html);
}

/* Keep the page in step with a selection made anywhere else, following it to
   whichever page it is actually on. */
function trackSelectionOnPage(){
  if(!S.render||!S.render.map) return;
  const hit=S.render.map.find(b=>sameBlock(b,S.sel));
  if(hit&&hit.page-1!==S.page){ S.page=hit.page-1; paintPage() }
  else paintHits();
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
  /* Any ink on the page should light a segment: rounding 8% to zero made a
     nearly-empty page and a blank one look the same. */
  const on=pct==null?0:(S.fill>0?Math.max(1,Math.ceil(S.fill*6)):0);
  [...b.querySelectorAll(".bar i")].forEach((el,i)=>el.classList.toggle("on",i<on));
  b.querySelector(".bar").style.visibility=pct==null?"hidden":"";
  b.querySelector(".cap").textContent=fillCaption(r.pages,pct);
}
/* The sentence changes with the number but never claims more than the
   measurement supports. */
function fillCaption(pages,pct){
  if(pct==null) return "Page fill could not be measured.";
  const p="Page "+pages+" is "+pct+"% full";
  if(pct>=96) return p+". No room left on it.";
  if(pages>1&&pct<=35) return p+". Most of the last page is empty.";
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
  if(S.view==="docs") drawDocuments();
  if(S.view==="cvs"&&S.path){
    paintTitle(); paintLink();
    if(!$("#ed").hidden) buildInspector();
  }
  /* The rail marks which documents belong to an application, so it has to
     be redrawn once we know what the applications are. */
  if(S.state) renderDocs(S.state.documents);
  paintStatus();
}

/* The sidebar is the filter. Statuses come from the store rather than a list
   written here, so a status added to jobs.py shows up without a UI change. */
function statusCounts(){
  const c={};
  S.jobs.forEach(j=>{ c[j.status]=(c[j.status]||0)+1 });
  return c;
}
const NO_LETTER=j=>!j.letter_path;
const SAVED={"No cover letter":NO_LETTER};

/* Attention is computed by the server, not here. The same four rules answer
   the desktop notification and the digest an AI client reads out, and three
   copies of "what counts as overdue" would have drifted apart within a month.
   Needs-follow-up used to live in SAVED above and is now one of them. */
const ATTENTION=[
  ["interview_soon",   "Interview soon"],
  ["followup_due",     "Follow-up due"],
  ["interview_passed", "Interview, no outcome"],
  ["silent",           "No reply"],
];

async function loadAlerts(){
  try{ S.alerts=await api("/api/alerts") }catch(e){ S.alerts=null; return }
  if(S.view==="jobs") drawRail();
  notifyAlerts();
}

/* ---- Notifications ---------------------------------------------------- */
/* Nothing outside this app knows when an interview is, so a reminder has to
   come from here. Opt-in, from Settings > Notifications: before each
   interview at the leads chosen there, and once a day for follow-ups, in one
   notification rather than one per application. Each is sent once: what was
   sent is remembered on this machine. They come while the app is open. */
const notifyOn=()=>!!prefs().notify;
const notifyIv=()=>{ const v=prefs().notify_iv??"60,10"; return v==="off"?[]:String(v).split(",").map(Number).sort((a,b)=>b-a) };
const notifyFu=()=>{ const v=prefs().notify_fu??"9"; return v==="off"?null:+v };
function notifyApi(){
  const N=window.__TAURI__&&window.__TAURI__.notification;
  if(N) return {
    granted:()=>N.isPermissionGranted(),
    ask:async()=>(await N.requestPermission())==="granted",
    send:(title,body)=>N.sendNotification({title,body})};
  if(!("Notification" in window)) return null;
  return {
    granted:async()=>Notification.permission==="granted",
    ask:async()=>(await Notification.requestPermission())==="granted",
    send:(title,body,onclick)=>{ const n=new Notification(title,{body,icon:"/static/brand-mark-256.png"});
      n.onclick=()=>{ window.focus(); if(onclick) onclick(); n.close() } }};
}
function notifySeen(){ return Object.assign({},prefs().notified||{}) }
function notifyMark(k){
  const m=notifySeen(), cut=Date.now()-14*864e5;
  for(const x in m) if(m[x]<cut) delete m[x];
  m[k]=Date.now(); setPref("notified",m);
}
async function notifySend(key,title,body,onclick){
  const A=notifyApi(); if(!A) return false;
  try{ if(!(await A.granted())) return false; A.send(title,body,onclick); if(key) notifyMark(key); return true }
  catch(e){ return false }
}
const inWords=ms=>{ const m=Math.max(1,Math.round(ms/6e4));
  if(m<60) return t(m===1?"in 1 minute":"in {n} minutes",{n:m});
  const h=Math.round(m/60);
  if(h<24) return t(h===1?"in 1 hour":"in {n} hours",{n:h});
  return t("tomorrow") };
async function notifyTick(){
  if(!notifyOn()||!S.jready) return;
  const now=Date.now(), seen=notifySeen();
  /* Interviews: the largest lead whose moment has come and not been sent.
     Opening the app half an hour before still gets one, saying so. */
  const leads=notifyIv();
  for(const j of S.jobs||[]){
    const at=interviewMoment(j); if(!at||at<=now||!leads.length) continue;
    const base="iv:"+j.id+":"+at.getTime()+":";
    const due=leads.filter(L=>now>=at-L*6e4);
    if(!due.length) continue;
    const L=due[due.length-1];
    if(seen[base+L]) continue;
    const ok=await notifySend(base+L,t("Interview {when} · {co}",{when:inWords(at-now),co:j.company}),
      j.title+" · "+interviewLine(j),()=>{ setView("jobs"); selectJob(j.id) });
    if(ok) due.forEach(x=>notifyMark(base+x));
  }
  /* Follow-ups: once a day, from the hour chosen. */
  const hr=notifyFu();
  if(hr!=null&&new Date().getHours()>=hr){
    const today=isoToday(), key="fu:"+today;
    if(!seen[key]){
      const list=(S.jobs||[]).filter(j=>j.followup_date&&!DEAD_ST.has(j.status)&&String(j.followup_date).slice(0,10)<=today);
      if(list.length){
        const late=list.filter(j=>String(j.followup_date).slice(0,10)<today).length;
        const names=list.map(j=>j.company); const shown=names.length>4?names.slice(0,4).join(", ")+" +"+(names.length-4):names.join(", ");
        await notifySend(key,list.length===1?t("Follow up with {co} today",{co:list[0].company})
            :t("{n} follow-ups to send",{n:list.length}),
          shown+(late?" · "+t(late===1?"1 is late":"{n} are late",{n:late}):""),
          ()=>{ S.jfilter={kind:"alert",value:"followup_due"}; setView("jobs") });
      }else notifyMark(key);
    }
  }
}
setInterval(notifyTick,30000);
async function notifyAlerts(){ notifyTick() }
function fillNotify(){
  const on=$("#s-notify"), iv=$("#s-notify-iv"), fu=$("#s-notify-fu"), st=$("#s-notify-state"), A=notifyApi();
  const pr=prefs();
  on.checked=notifyOn(); iv.value=pr.notify_iv??"60,10"; fu.value=String(pr.notify_fu??"9");
  $$("#sp-notify .nf-sub").forEach(r=>r.classList.toggle("off",!on.checked));
  const say=async()=>{
    if(!A){ st.textContent=t("This window cannot show notifications."); on.disabled=true; return }
    const g=await A.granted().catch(()=>false);
    st.textContent=!on.checked?t("Off. Turn them on to be reminded of interviews and follow-ups.")
      :g?t("On, while CV Studio is open."):t("Blocked by your system. Allow CV Studio in your notification settings.");
  };
  say();
  on.onchange=async()=>{
    if(on.checked&&A){
      let g=await A.granted().catch(()=>false);
      if(!g) g=await A.ask().catch(()=>false);
      if(!g){ on.checked=false; toast(t("Notifications are blocked for CV Studio in your system settings."),true) }
    }
    setPref("notify",on.checked);
    fillNotify(); notifyTick();
  };
  iv.onchange=()=>{ setPref("notify_iv",iv.value); notifyTick() };
  fu.onchange=()=>{ setPref("notify_fu",fu.value); notifyTick() };
  $("#s-notify-test").onclick=async()=>{
    const ok=await notifySend(null,t("CV Studio"),t("This is how a reminder will look."));
    if(!ok) toast(t("Notifications are blocked for CV Studio in your system settings."),true);
  };
}

function drawRail(){
  const c=statusCounts(), f=S.jfilter;
  /* The .mark span is what .row.sel paints ochre. Without it this rail marked
     its selection with a background lift alone, at 1.28:1 -- the documents rail
     next door emits the span and gets the bar, so the two rails disagreed about
     what "selected" looks like. */
  const row=(label,count,kind,value)=>
    '<button class="row'+(f.kind===kind&&f.value===value?" sel":"")+'" data-k="'+kind+
    '" data-v="'+esc(value)+'"><span class="mark"></span>'+
    '<span class="lbl">'+esc(label)+'</span>'+
    (count==null?"":'<span class="ct mono">'+count+'</span>')+'</button>';
  let h=row("All",S.jobs.length,"all","");
  S.statuses.forEach(s=>{ if(c[s]) h+=row(prettyStatus(s),c[s],"status",s) });
  /* The funnel hands over a node to filter by, and its label often matches a
     status already listed above -- "Draft" under "Draft", the second one with
     no count. Only add it when it is actually saying something new. */
  const shown=new Set(S.statuses.filter(x=>c[x]).map(prettyStatus));
  if(S.fnode&&S.labels[S.fnode]&&!shown.has(S.labels[S.fnode]))
    h+=row(S.labels[S.fnode],null,"node",S.fnode);
  $("#statuslist").innerHTML=h;

  /* Only buckets with something in them. An Attention list showing four zeroes
     is worse than no list: it trains you to stop looking at it. */
  const a=S.alerts;
  const live=a?ATTENTION.filter(([k])=>a.counts[k]>0):[];
  $("#attentionwrap").hidden=!live.length;
  $("#attentionlist").innerHTML=live.map(([k,label])=>
    row(label,a.counts[k],"alert",k)).join("");

  $("#savedlist").innerHTML=Object.keys(SAVED).map(k=>
    row(k,S.jobs.filter(SAVED[k]).length,"saved",k)).join("");
  $$("#statuslist [data-k],#attentionlist [data-k],#savedlist [data-k]").forEach(b=>b.onclick=()=>{
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
  else if(f.kind==="alert"){
    /* Filter against the server's answer rather than re-deriving the rule
       here, which is the whole point of computing it in one place. */
    const ids=new Set(((S.alerts&&S.alerts[f.value])||[]).map(x=>x.id));
    rows=rows.filter(j=>ids.has(j.id));
  }
  if(q) rows=rows.filter(j=>(j.company+" "+j.title+" "+(j.notes||"")+" "+(j.source||""))
    .toLowerCase().includes(q));
  return rows;
}

/* ---- the base CV --------------------------------------------------------
   One document every tailored CV is copied from. It is pinned above the
   applications rather than listed among the documents because it is what they
   are all made of: the question "which CV is this one a version of" now has
   one answer instead of one per file. */
/* Not baseName(): that one already means "the file this document was copied
   from" a few hundred lines up, and two answers to one name is how the two
   ideas get confused in the first place. */
function baseLabel(){ const b=S.state&&S.state.base;
  return b?b.path.split("/").pop().replace(/\.ya?ml$/,""):null }
/* The band above the applications and the card on the Documents screen are the
   same statement about the same document, so it is written once and mounted
   twice. The handlers hang off data attributes rather than ids: both elements
   are in the DOM whichever screen is showing, and one id in two places is one
   id too many. */
function baseHTML(b){
  if(!b)
    return '<span class="bl">Base CV</span>'+
      '<span class="bsub">Not chosen yet. Every tailored CV starts as a copy '+
      'of one.</span>'+
      '<button class="obtn" data-base-pick>Choose\u2026</button>'+
      '<div class="grow"></div>';
  if(b.missing)
    return '<span class="bl">Base CV</span>'+
      '<span class="bn">'+esc(baseLabel())+'</span>'+
      '<span class="bsub">is no longer in the workspace</span>'+
      '<button class="obtn" data-base-pick>Choose another\u2026</button>'+
      '<div class="grow"></div>';
  const pages=S.pages[b.path];
  const th=S.baseThumb&&S.baseThumb.path===b.path?S.baseThumb:null;
  const fam=baseFamily();
  const tailored=((S.state&&S.state.documents)||[]).filter(d=>d.base===b.path).length;
  const ico=d=>'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" '+
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+d+'</svg>';
  /* The page on its own little stage, the way Documents shows it, with what
     it is, the languages it comes in and how much has been made from it. */
  return '<div class="bstage">'+
      '<span class="btag">'+t("Base CV")+'</span>'+
      '<button class="bthumb'+(th&&th.png?"":" empty")+'" data-base-open aria-label="'+
        t("Open the base CV")+'">'+
      (th&&th.png?'<img alt="" src="'+esc(th.png+tok())+'">'
        :'<span>'+(th&&th.failed?"Doesn\u2019t render":"Rendering\u2026")+'</span>')+
      '</button></div>'+
    '<div class="bmeta"><span class="bn">'+esc(baseLabel())+'</span>'+
      (fam.length>1?'<span class="bflags">'+fam.map(m=>flag(m.lang)||lchip(m.lang)).join("")+'</span>':'')+
    '</div>'+
    '<span class="bsub">'+[pages?pages+" page"+(pages===1?"":"s"):"",
      tailored?tailored+" tailored from it":"Every tailored CV starts here"].filter(Boolean).join(" · ")+'</span>'+
    '<div class="bacts"><button class="obtn bopen" data-base-open>Open</button>'+
      '<button class="obtn bico" data-base-design title="Design" aria-label="Design">'+
        ico('<circle cx="13.5" cy="6.5" r="1"/><circle cx="17.5" cy="10.5" r="1"/><circle cx="8.5" cy="7.5" r="1"/>'+
          '<circle cx="6.5" cy="12.5" r="1"/><path d="M12 2a10 10 0 0 0 0 20c1.1 0 2-.9 2-2 0-.5-.2-1-.5-1.3-.3-.4-.5-.8-.5-1.3 0-1.1.9-2 2-2h2.4A5.6 5.6 0 0 0 22 9.8C22 5.5 17.5 2 12 2z"/>')+'</button>'+
      '<button class="obtn bico" data-base-pick title="Change base…" aria-label="Change base…">'+
        ico('<path d="M17 3l4 4-4 4"/><path d="M3 7h18"/><path d="M7 21l-4-4 4-4"/><path d="M21 17H3"/>')+'</button>'+
    '</div>';
}
/* The same base, on Documents, at the size of a page you can read. */
function baseHeroHTML(b){
  if(!b||b.missing) return '<div class="bmain"><div class="bbody"><span class="bl">Base CV</span>'+
    (b?'<h2 class="bn">'+esc(baseLabel())+'</h2><span class="bmeta">is no longer in the '+
      'workspace</span>':'<h2 class="bn">Not chosen yet</h2>')+
    '<p class="bwhy">Every CV you tailor for an application starts as a copy of the base.</p>'+
    '<div class="bacts"><button class="pbtn" data-base-pick>'+(b?"Choose another":"Choose")+
      '&#8230;</button></div></div></div>';
  const fam=multiLang()?baseFamily():baseFamily().slice(0,1);
  let m=fam.find(x=>x.lang===S.baseLang)||fam[0];
  const docs=(S.state&&S.state.documents)||[];
  const me=docs.find(d=>d.path===m.path);
  const pages=S.pages[m.path];
  const th=m.source?(S.baseThumb&&S.baseThumb.path===b.path?S.baseThumb:null):S.docThumbs[m.path];
  const kids=docs.filter(d=>d.base===m.path);
  const used=new Set((S.jobs||[]).map(j=>j.cv_path).filter(Boolean));
  const sent=kids.filter(d=>used.has(d.path)).length;
  const L=langOf(m.lang), src=langOf(fam[0].lang);
  const behind=p=>{ const d=S.driftBy&&S.driftBy[p]; return d&&d.changes&&d.changes.length };
  const tabs='<div class="btabs" role="tablist" aria-label="Base CV language">'+fam.map(x=>
      '<button role="tab" data-blang="'+esc(x.lang)+'" aria-selected="'+String(x===m)+'">'+
        lchip(x.lang,x===m)+esc(langOf(x.lang).native)+
        (x.source&&fam.length>1?'<span class="src">source</span>':'')+
        (behind(x.path)?'<span class="behind"><i></i>'+behind(x.path)+' behind</span>':'')+
      '</button>').join("")+
    '<button class="add" data-blang-add>+ Add a language</button></div>';
  const why=m.source
    ? (fam.length>1?'Every CV you tailor in '+esc(L.english)+' starts as a copy of this one. '+
        'Its translations say what they are missing when it changes.'
       :'Every CV you tailor for an application starts as a copy of this one, and shows '+
        'what it changed from it. Improving the base improves every CV you tailor from now on.')
    : 'The '+esc(L.english)+' translation of <b>'+esc(docLabel(fam[0].path))+'</b>. CVs for '+
      'postings in '+esc(L.english)+' start from it. Its design follows the '+esc(src.english)+' one.';
  return tabs+'<div class="bmain"><button class="bthumb'+(th&&th.png?"":" empty")+'" data-base-open'+
      ' aria-label="Open the base CV">'+
      (th&&th.png?'<img alt="Page one of the base CV" src="'+esc(th.png+tok())+'">'
        :'<span>'+(th&&th.failed?"Doesn’t render":"Rendering…")+'</span>')+
    '</button>'+
    '<div class="bbody"><span class="bl">Base CV'+(fam.length>1?' · '+esc(L.native):'')+'</span>'+
      '<h2 class="bn">'+esc(m.source?baseLabel():docLabel(m.path))+'</h2>'+
      '<span class="bmeta">'+[pages?pages+" page"+(pages===1?"":"s"):"",
        me&&me.mtime?"updated "+mtimeLabel(me.mtime):""].filter(Boolean).join(" · ")+
      '</span>'+
      '<p class="bwhy">'+why+'</p>'+
      '<div class="bstats"><div><b>'+kids.length+'</b><span>tailored from it</span></div>'+
        '<div><b>'+sent+'</b><span>attached to applications</span></div>'+
        (!m.source&&behind(m.path)?'<div><b>'+behind(m.path)+'</b><span>changes in '+
          esc(src.english)+' to carry over</span></div>':'')+'</div>'+
      '<div class="bacts"><button class="pbtn" data-base-open>Open</button>'+
        (!m.source&&behind(m.path)?'<button class="obtn" data-base-drift>What changed</button>':'')+
        '<button class="obtn" data-base-design>Design</button>'+
        (m.source?'<button class="obtn" data-base-pick>Change base&#8230;</button>':'')+'</div>'+
    '</div></div>';
}

function mountBase(el,cls){
  if(!el) return;
  const b=S.state&&S.state.base;
  el.className=cls+(b&&b.missing?" gone":"")+(cls==="bhero"&&!b?" none":"");
  el.innerHTML=cls==="bhero"?baseHeroHTML(b):baseHTML(b);
  /* On Documents the card shows whichever language tab is picked. */
  const shown=cls==="bhero"&&b&&!b.missing
    ?((baseFamily().find(x=>x.lang===S.baseLang)||{}).path||b.path):b&&b.path;
  el.querySelectorAll("[data-base-open]").forEach(open=>open.onclick=()=>{
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    openDoc(shown);
  });
  const design=el.querySelector("[data-base-design]");
  if(design) design.onclick=async()=>{
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    await openDoc(b.path); openDesign();
  };
  const pick=el.querySelector("[data-base-pick]"); if(pick) pick.onclick=baseSheet;
  el.querySelectorAll("[data-blang]").forEach(t=>t.onclick=()=>{
    S.baseLang=t.dataset.blang; mountBase(el,cls);
  });
  const add=el.querySelector("[data-blang-add]");
  if(add) add.onclick=()=>addLanguageSheet(b.path);
  const dr=el.querySelector("[data-base-drift]");
  if(dr) dr.onclick=()=>driftSheet(shown,S.driftBy[shown]);
}
function paintBase(){
  applyMultiLang();
  mountBase($("#baserow"),"baserow");
  mountBase($("#docbase"),"bhero");
  paintBaseChip();
  baseThumb(false);
}

/* The base as it actually prints, not a sketch of its theme: the sketches in
   Design say what a theme looks like, and this card is about one document.
   Whatever is on disk is shown at once; a page older than the YAML is shown
   while a fresh one renders, then swapped, so the card never waits on Typst
   and never settles on a page that is out of date. */
let thumbBusy=null;
async function baseThumb(force){
  const b=S.state&&S.state.base;
  if(!b||b.missing) return;
  if(!force&&S.baseThumb&&S.baseThumb.path===b.path) return;
  if(thumbBusy===b.path) return;
  thumbBusy=b.path;
  const put=(png,failed)=>{
    S.baseThumb={path:b.path,png:png||(S.baseThumb&&S.baseThumb.path===b.path
      ?S.baseThumb.png:null),failed:!!failed};
    mountBase($("#baserow"),"baserow"); mountBase($("#docbase"),"bhero");
  };
  try{
    const t=await api("/api/thumb?path="+encodeURIComponent(b.path));
    put(t.png);
    if(!t.fresh){
      const r=await post("/api/render",{path:b.path});
      if(r.ok){ S.pages[b.path]=r.pages; put(r.pngs[0]) }
      else put(null,true);
    }
  }catch(e){ put(null,true) }
  finally{ thumbBusy=null }
}

/* A document's age. "720h" is what ago() would say about a CV last touched in
   the spring, which is true and useless, so anything older than yesterday gets
   the same date the applications table uses. */
function mtimeLabel(t){
  if(!t) return "";
  const d=new Date(t*1000), now=new Date();
  const same=(a,b)=>a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()
    &&a.getDate()===b.getDate();
  if(same(d,now)) return "today";
  const y=new Date(now); y.setDate(y.getDate()-1);
  return same(d,y)?"yesterday":shortDate(d);
}

/* ---- languages ------------------------------------------------------------
   A CV's language is RenderCV's locale; a translation is its own document,
   linked to the one it was translated from. CV Studio writes the part with
   one right answer -- dates, month names, section titles -- and the prose is
   translated by you or by your AI client. Nothing here asks a client to do
   anything: a client acts when you ask it to, so the app says what to ask. */
const langOf=c=>((S.state&&S.state.languages)||[]).find(l=>l.code===c)||
  {code:c||"en",native:String(c||"en").toUpperCase(),english:String(c||"en").toUpperCase()};
/* Flags for the languages people most often write a CV in here, drawn rather
   than emoji: Windows prints a flag emoji as two letters. English is the US
   flag and Portuguese the Brazilian one, as asked; the code is printed beside
   the flag either way, so a flag is never the only thing saying which. */
const FLAGS={
  fr:'<rect width="10" height="20" fill="#0055A4"/><rect x="10" width="10" height="20" fill="#fff"/>'+
    '<rect x="20" width="10" height="20" fill="#EF4135"/>',
  es:'<rect width="30" height="20" fill="#AA151B"/><rect y="5" width="30" height="10" fill="#F1BF00"/>',
  en:'<rect width="30" height="20" fill="#fff"/>'+[0,2,4,6,8,10,12].map(i=>'<rect y="'+(i*20/13).toFixed(2)+
    '" width="30" height="'+(20/13).toFixed(2)+'" fill="#B22234"/>').join("")+
    '<rect width="13" height="10.77" fill="#3C3B6E"/>'+[[2.5,2.2],[6.5,2.2],[10.5,2.2],[4.5,5.4],[8.5,5.4],
    [2.5,8.6],[6.5,8.6],[10.5,8.6]].map(([x,y])=>'<circle cx="'+x+'" cy="'+y+'" r=".75" fill="#fff"/>').join(""),
  pt:'<rect width="30" height="20" fill="#009B3A"/><path d="M15 2.2 27.6 10 15 17.8 2.4 10z" fill="#FEDF00"/>'+
    '<circle cx="15" cy="10" r="4.6" fill="#002776"/>',
};
const flag=c=>FLAGS[c]?'<svg class="flg" viewBox="0 0 30 20" aria-hidden="true">'+FLAGS[c]+'</svg>':"";
/* The flag beside an application's language follows the choice at once. */
document.addEventListener("change",e=>{
  const sel=e.target.closest&&e.target.closest(".ap-lang select"); if(!sel) return;
  sel.parentNode.querySelector(".flg")?.remove();
  sel.insertAdjacentHTML("beforebegin",flag(sel.value));
});
const lchip=(c,on)=>{ const k=String(c||"en").toLowerCase();
  return '<span class="lchip'+(on?' on':'')+(FLAGS[k]?' fl':'')+'">'+flag(k)+esc(k.toUpperCase())+'</span>' };
/* The base CV and its translations: the set the Documents tabs move between. */
function baseFamily(){
  const b=S.state&&S.state.base; if(!b||b.missing) return [];
  const docs=(S.state&&S.state.documents)||[], me=docs.find(d=>d.path===b.path);
  return [{path:b.path,lang:(me&&me.lang)||"en",source:true}].concat(docs
    .filter(d=>d.translation_of===b.path)
    .map(d=>({path:d.path,lang:d.lang,source:false})));
}
const baseIn=lang=>baseFamily().find(m=>m.lang===lang)||null;
/* Whether CVs come in more than one language here. Most people apply in one
   country, and for them every tab, flag and language question is noise, so it
   is off until a translation exists or Settings turns it on. Off hides the
   language machinery; nothing is deleted, and translations come back as they
   were when it is turned on again. */
function multiLang(){
  const p=prefs().multilang;
  return p==null?baseFamily().length>1:!!p;
}
function applyMultiLang(){ document.documentElement.classList.toggle("mono",!multiLang()) }
/* Which AI clients could do the translating, in words, for the prompts. */
function clientsSay(){
  const on=(S.ai||[]).filter(c=>c.state==="connected").map(c=>c.label);
  return on.length?on.join(" or "):null;
}
function copyText(text,btn){
  const done=()=>{ if(btn){ const t=btn.textContent; btn.textContent="Copied";
    setTimeout(()=>btn.textContent=t,1400) } };
  try{ navigator.clipboard.writeText(text).then(done,()=>toast("Select the text and copy it",true)) }
  catch(e){ toast("Select the text and copy it",true) }
}
/* What to say to a client, ready to copy, with who could hear it. */
function sayBox(title,text){
  const who=clientsSay();
  return '<div class="say-box"><b>'+title+'</b><div class="say" id="say-text">'+esc(text)+'</div>'+
    '<div class="row"><span class="grow">'+(who?"Paste it into "+esc(who)+
      ". It works through CV Studio, so you can watch it land here."
      :"No AI client is connected yet. Settings → AI clients sets one up.")+'</span>'+
    (who?'':'<button class="obtn" id="say-setup">AI clients</button>')+
    '<button class="obtn" id="say-copy">Copy</button></div></div>';
}
function wireSay(text){
  const c=$("#say-copy"); if(c) c.onclick=()=>copyText(text,c);
  const st=$("#say-setup"); if(st) st.onclick=()=>{ closeSheet(); openSettings("ai") };
}

function addLanguageSheet(src){
  const have=new Set(((S.state&&S.state.documents)||[])
    .filter(d=>d.path===src||d.translation_of===src).map(d=>d.lang));
  const from=langOf((((S.state&&S.state.documents)||[]).find(d=>d.path===src)||{}).lang);
  const all=((S.state&&S.state.languages)||[]).filter(l=>!have.has(l.code));
  let pick=(all.find(l=>l.code==="fr")||all[0]||{}).code;
  openSheet(
    '<div><h3 id="sheet-title">Add a language to '+esc(docLabel(src))+'</h3>'+
      '<p>A copy in another language, saved beside it and linked to it. CVs you tailor '+
      'for a posting in that language start from it.</p></div>'+
    '<div class="fg w88"><label for="al-q">Language</label>'+
      '<input id="al-q" autocomplete="off" placeholder="Search '+all.length+' languages"></div>'+
    '<div class="al-list" id="al-list" role="radiogroup" aria-label="Language"></div>'+
    '<ul class="al-does">'+
      '<li><span>Dates, month names and “present” print in the new language.</span></li>'+
      '<li><span>Common section titles are translated: Experience, Education, Skills and the like.</span></li>'+
      '<li><span>Your name, contact details, links, company names and dates stay exactly as they are, '+
        'and the design follows '+esc(from.english)+'.</span></li>'+
    '</ul>'+
    '<p>Your text stays in '+esc(from.english)+' until you, or your AI client, translate it.</p>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
      '<button class="sbtn primary" id="al-go">Add</button></div>');
  const paint=()=>{
    const q=$("#al-q").value.trim().toLowerCase();
    const list=all.filter(l=>!q||(l.native+" "+l.english+" "+l.code).toLowerCase().includes(q));
    $("#al-list").innerHTML=list.map(l=>'<button role="radio" data-l="'+l.code+'" aria-checked="'+
      String(l.code===pick)+'">'+lchip(l.code,l.code===pick)+'<span><b>'+esc(l.native)+'</b> '+
      (l.english!==l.native?'<em>'+esc(l.english)+'</em>':'')+'</span></button>').join("")||
      '<p class="note muted">No language by that name.</p>';
    $$("#al-list [data-l]").forEach(b=>b.onclick=()=>{ pick=b.dataset.l; paint() });
    $("#al-go").textContent="Add "+(pick?langOf(pick).english:"");
    $("#al-go").disabled=!pick;
  };
  $("#al-q").oninput=paint; paint();
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#al-go").onclick=async()=>{
    $("#al-go").disabled=true;
    try{
      const r=await post("/api/language/add",{path:src,language:pick});
      if(!r.ok){ $("#al-go").disabled=false; return toast(r.error,true) }
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      S.baseLang=r.lang;
      if(S.view==="docs") drawDocuments();
      translateNextSheet(r);
    }catch(e){ $("#al-go").disabled=false; toast(e.message,true) }
  };
}
/* After the copy is written: what is done, and what to ask a client for. */
function translateNextSheet(r){
  const to=langOf(r.lang), from=langOf(r.from_lang);
  const titled=Object.keys(r.sections_titled||{}).length, left=r.sections_to_title||[];
  const text="In CV Studio, translate "+r.path+" into "+to.english+". It is the "+
    to.english+" copy of "+r.of+".";
  openSheet(
    '<div><h3 id="sheet-title">'+esc(to.english)+' copy made</h3><p>'+
      'Dates and month names are in '+esc(to.english)+(titled?', and '+titled+' section title'+
      (titled===1?' is':'s are')+' translated':'')+'. The rest of the text is still in '+
      esc(from.english)+'.'+(left.length?' Still to title: '+left.map(esc).join(", ")+'.':'')+
    '</p>'+(r.font?'<p class="muted">Its font, '+esc(r.font.language)+' Noto Sans, is '+
      'downloading now ('+r.font.mb+' MB, once). Until it arrives the page prints in a font '+
      'your computer has.</p>':'')+'</div>'+
    sayBox("To have your AI client translate it, ask it:",text)+
    '<p>Or open it and translate it yourself, field by field.</p>'+
    '<div class="foot"><button class="sbtn" data-cancel>Close</button>'+
      '<button class="sbtn primary" id="tn-open">Open the '+esc(to.english)+' CV</button></div>');
  wireSay(text);
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#tn-open").onclick=()=>{ closeSheet(); openDoc(r.path) };
}
/* What the source changed since this translation last caught up. */
function driftSheet(path,drift){
  const to=langOf(drift.lang), from=langOf(drift.from_lang||"en");
  const text="In CV Studio, bring "+path+" up to date with "+drift.of+
    ": translate what changed into "+to.english+".";
  openSheet(
    '<div><h3 id="sheet-title">What '+esc(docLabel(drift.of))+' changed</h3><p>'+
      'Since this '+esc(to.english)+' version was last brought up to date. Nothing '+
      'here has been changed for you.</p></div>'+
    '<ul class="drift-list">'+drift.changes.map(c=>'<li><span class="w">'+esc(c.where)+'</span>'+
      (c.kind!=="added"&&c.before?'<span class="l">'+lchip(from.code)+'<span class="then">'+
        esc(c.before)+'</span></span>':'')+
      (c.kind!=="removed"?'<span class="l">'+lchip(from.code,true)+'<span>'+esc(c.after||"")+
        '</span></span>':'<span class="l">'+lchip(from.code,true)+'<span class="muted">removed</span></span>')+
      (c.translation?'<span class="l">'+lchip(to.code)+'<span>'+esc(c.translation)+
        '</span></span>':'')+'</li>').join("")+'</ul>'+
    sayBox("To have your AI client carry these over, ask it:",text)+
    '<div class="foot"><button class="sbtn left" data-cancel>Close</button>'+
      '<button class="sbtn primary" id="ds-done">Mark as done</button></div>');
  wireSay(text);
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#ds-done").onclick=()=>{ closeSheet(); markTranslationDone(path) };
}
async function markTranslationDone(path){
  try{
    await post("/api/language/done",{path});
    if(S.path===path){ const d=await api("/api/doc?path="+encodeURIComponent(path));
      S.doc.drift=d.drift; paintLang() }
    S.driftBy=null;
    if(S.view==="docs") drawDocuments();
    toast("Marked as up to date");
  }catch(e){ toast(e.message,true) }
}
/* The editor's bar: which language this is, the others to switch to, and
   what it is missing from its source. */
function paintLang(){
  const sw=$("#langsw"), bar=$("#driftbar");
  const fam=S.doc&&S.doc.family, members=(fam&&fam.members)||[];
  if(members.length<2){ sw.hidden=true }
  else{
    sw.hidden=false;
    sw.innerHTML=members.map(m=>'<button data-lp="'+esc(m.path)+'"'+
      (m.path===S.path?' aria-current="true"':'')+' title="'+esc(m.path)+
      (m.source?" · the source":" · translated from "+docLabel(fam.source))+'">'+
      lchip(m.lang,m.path===S.path)+'<span class="ln">'+esc(langOf(m.lang).native)+'</span></button>').join("");
    $$("#langsw [data-lp]").forEach(b=>b.onclick=()=>{
      if(b.dataset.lp===S.path) return;
      if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
      openDoc(b.dataset.lp);
    });
  }
  const dr=S.doc&&S.doc.drift;
  if(!dr||(!dr.missing&&!dr.changes.length)){ bar.hidden=true; return }
  bar.hidden=false;
  const from=langOf(dr.from_lang||"en"), n=dr.changes.length;
  $("#driftbar-msg").innerHTML=dr.missing
    ? 'The '+esc(from.english)+' CV this was translated from, <b>'+esc(dr.of)+'</b>, is missing.'
    : '<b>'+esc(docLabel(dr.of))+' changed since this was translated.</b> '+n+' thing'+
      (n===1?'':'s')+' in it '+(n===1?'is':'are')+' not in this '+esc(langOf(dr.lang).english)+
      ' version yet.';
  $("#drift-show").hidden=!!dr.missing; $("#drift-done").hidden=!!dr.missing;
  $("#drift-show").onclick=()=>driftSheet(S.path,dr);
  $("#drift-done").onclick=()=>markTranslationDone(S.path);
}

/* Two lanes, and the second is the reason this screen exists. A CV written for
   an application is reachable from that application's row; one written for
   nothing was reachable from nowhere, because the only list of documents lived
   inside the editor and you needed a document open to see it. Each is shown as
   its page, since two CVs are told apart by looking at them. */
function drawDocuments(){
  mountBase($("#docbase"),"bhero");
  const docs=(S.state&&S.state.documents)||[];
  $("#dcount").textContent=String(docs.length);
  const basePath=(S.state&&S.state.base&&S.state.base.path)||null;
  const owner={};
  for(const j of (S.jobs||[])){
    if(j.cv_path) owner[j.cv_path]=j;
    if(j.letter_path) owner[j.letter_path]=j;
  }
  /* The base's translations live in its tabs, not in the lanes. */
  const family=new Set(baseFamily().map(m=>m.path));
  /* The base CV's language first, then the rest by how many documents use it. */
  const srcLang=(baseFamily()[0]||{}).lang||"en", nOf={};
  docs.forEach(d=>{ const c=d.lang||"en"; nOf[c]=(nOf[c]||0)+1 });
  const langs=Object.keys(nOf).sort((a,b)=>(b===srcLang)-(a===srcLang)||nOf[b]-nOf[a]);
  const filt=$("#dfilter");
  if(langs.length<2||!multiLang()){ filt.hidden=true; S.docLang=null }
  else{
    filt.hidden=false;
    filt.innerHTML=[null,...langs].map(c=>'<button data-dl="'+(c||"")+'" aria-pressed="'+
      String((S.docLang||null)===c)+'">'+(c?flag(c)+esc(langOf(c).native):"All languages")+
      '</button>').join("");
    $$("#dfilter [data-dl]").forEach(b=>b.onclick=()=>{ S.docLang=b.dataset.dl||null;
      drawDocuments() });
  }
  const attached=[], loose=[];
  for(const d of docs){
    if(family.has(d.path)) continue;
    if(S.docLang&&(d.lang||"en")!==S.docLang) continue;
    (owner[d.path]?attached:loose).push(d);
  }
  baseDrift();
  const recent=(a,b)=>(b.mtime||0)-(a.mtime||0);
  attached.sort(recent); loose.sort(recent);

  const card=d=>{
    const j=owner[d.path], letter=d.group==="Cover letters";
    const copied=d.base?d.base.split("/").pop().replace(/\.ya?ml$/,""):null;
    const about=j
      ? '<span class="dot '+statusTone(j.status)+'"></span>'+esc(j.company)+' \u00b7 '+esc(j.title)
      : copied?'Copied from '+esc(copied):letter?'Cover letter':'Not attached';
    const th=S.docThumbs[d.path];
    return '<button class="dcard" data-open="'+esc(d.path)+'" title="'+esc(d.path)+'">'+
      '<span class="pg">'+(th&&th.png?'<img alt="" loading="lazy" src="'+esc(th.png+tok())+'">'
        :'<span>'+(th&&th.failed?"Doesn\u2019t render":"Rendering\u2026")+'</span>')+
        ((d.lang||"en")!==srcLang?'<span class="langs">'+lchip(d.lang,true)+'</span>':'')+
        (letter?'<span class="tag">Letter</span>':'')+'</span>'+
      '<span class="meta"><b>'+esc(d.label)+'</b><span>'+about+'</span>'+
        '<em>'+esc(mtimeLabel(d.mtime))+(S.pages[d.path]?" \u00b7 "+S.pages[d.path]+
          " page"+(S.pages[d.path]===1?"":"s"):"")+'</em></span></button>';
  };
  const lane=(title,list,why,empty)=>
    '<section class="dsec"><h2>'+title+'<span class="n">'+list.length+'</span></h2>'+
    '<p class="why">'+why+'</p>'+
    (list.length?'<div class="dgrid">'+list.map(card).join("")+'</div>'
      :'<div class="dempty">'+empty+'</div>')+'</section>';

  $("#doclanes").innerHTML=
    lane("Written for an application",attached,
      "Each is attached to the application it was tailored for. Opening one here is the "+
      "same as opening it from that row.",
      "None yet. On Applications, <b>Tailor a CV</b> on a row copies the base CV for it.")+
    lane("Everything else",loose,
      "CVs and letters no application points at: a master copy, an old version, a draft "+
      "you have not attached yet.",
      "Nothing here. <b>New document</b> or <b>Import</b> puts a CV here.");
  paintStatus();
  $$("#doclanes .dcard").forEach(b=>{
    b.onclick=()=>{
      if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
      openDoc(b.dataset.open);
    };
  });
  docThumbs();
}
/* Every card's page. What is on disk is shown at once; anything missing or
   older than its YAML is rendered one at a time behind it, and only while
   Documents is on screen, so a big workspace never queues up Typst for a
   screen nobody is looking at. */
let docThumbRun=0;
async function docThumbs(){
  const run=++docThumbRun;
  const basePath=(S.state&&S.state.base&&S.state.base.path)||null;
  const docs=((S.state&&S.state.documents)||[]).filter(d=>d.path!==basePath);
  const stale=[];
  for(const d of docs){
    const have=S.docThumbs[d.path];
    if(have&&have.mtime===d.mtime) continue;
    try{
      const t=await api("/api/thumb?path="+encodeURIComponent(d.path));
      if(run!==docThumbRun) return;
      S.docThumbs[d.path]={png:t.png,mtime:t.fresh?d.mtime:null};
      if(t.png) docPaintThumb(d.path);
      if(!t.fresh) stale.push(d);
    }catch(e){ return }
  }
  for(const d of stale){
    if(run!==docThumbRun||S.view!=="docs") return;
    try{
      const r=await post("/api/render",{path:d.path});
      if(r.ok){ S.pages[d.path]=r.pages; S.docThumbs[d.path]={png:r.pngs[0],mtime:d.mtime} }
      else S.docThumbs[d.path]={png:(S.docThumbs[d.path]||{}).png,failed:true,mtime:d.mtime};
      docPaintThumb(d.path);
    }catch(e){ return }
  }
}
/* How far behind each translation of the base is, for its tab. Fetched once
   per visit to Documents: a translation only falls behind when its source is
   saved, and the tabs are not worth a request per save. */
async function baseDrift(){
  if(S.driftBy) return;
  S.driftBy={};
  for(const m of baseFamily().filter(x=>!x.source)){
    try{ const d=await api("/api/doc?path="+encodeURIComponent(m.path)); S.driftBy[m.path]=d.drift }
    catch(e){}
  }
  if(S.view==="docs") mountBase($("#docbase"),"bhero");
}
function docPaintThumb(path){
  if(baseFamily().some(m=>m.path===path&&!m.source)) mountBase($("#docbase"),"bhero");
  const b=$('#doclanes .dcard[data-open="'+CSS.escape(path)+'"] .pg'); if(!b) return;
  const th=S.docThumbs[path], tag=b.querySelector(".tag"), lg=b.querySelector(".langs");
  b.innerHTML=th&&th.png?'<img alt="" src="'+esc(th.png+tok())+'">'
    :'<span>'+(th&&th.failed?"Doesn\u2019t render":"Rendering\u2026")+'</span>';
  if(lg) b.append(lg);
  if(tag) b.append(tag);
  const em=b.parentElement.querySelector(".meta em"), d=((S.state&&S.state.documents)||[])
    .find(x=>x.path===path);
  if(em&&d) em.textContent=mtimeLabel(d.mtime)+(S.pages[path]?" \u00b7 "+S.pages[path]+
    " page"+(S.pages[path]===1?"":"s"):"");
}
function baseSheet(){
  const b=S.state&&S.state.base;
  /* Letters are excluded for the same reason the New document sheet filters
     its Base on list: a cover letter as the thing every CV is copied from
     produces nonsense. */
  const docs=(S.state.documents||[]).filter(d=>d.group!=="Cover letters");
  if(!docs.length) return toast("There are no CVs in the workspace yet.",true);
  openSheet(
    '<div><h3 id="sheet-title">Base CV</h3><p>Every new CV starts as a copy of '+
    'this one, and a tailored CV is measured against whatever it was copied '+
    'from. Changing it touches no document: CVs already tailored keep the base '+
    'they were made from.</p></div>'+
    '<div class="fg w88"><label>Use</label><select id="bs-doc">'+
      docs.map(d=>'<option value="'+esc(d.path)+'"'+
        (b&&b.path===d.path?" selected":"")+'>'+esc(d.label)+'</option>').join("")+
    '</select></div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="bs-go">Set as base</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#bs-go").onclick=async()=>{
    const path=$("#bs-doc").value;
    try{
      const r=await post("/api/base",{path:path});
      S.state.base=r.base;
      closeSheet(); paintBase();
      toast(baseLabel()+" is the base CV");
    }catch(e){ toast(e.message,true) }
  };
}
/* ---- tailoring a CV for an application -----------------------------------
   The one action the home screen exists to offer. Not a dialog: a dialog is
   for choices, and every choice here has already been made -- the base is
   pinned, the name comes from the application, and the link is the whole
   point. The New document sheet stays for when you do want the choices. */
function uniqueDocName(stem){
  /* Two applications to one company for one role is ordinary -- re-applying, or
     two openings -- so the second one gets a suffix rather than a dead end. */
  const taken=p=>(S.state.documents||[]).some(d=>d.path==="profile/"+p+".yaml");
  if(!taken(stem)) return stem;
  for(let n=2;n<50;n++) if(!taken(stem+"-"+n)) return stem+"-"+n;
  return stem+"-"+Date.now();
}
async function tailorFor(id,force){
  const j=(S.jobs||[]).find(x=>x.id===id);
  if(!j||S.tailoring.has(id)) return;
  const b=S.state&&S.state.base;
  if(!b||b.missing){
    toast(b?"The base CV is missing, so there is nothing to copy."
           :"Choose a base CV first \u2014 the tailored copy starts from it.",true);
    return baseSheet();
  }
  /* In the posting's language, from the base CV in that language. Without
     one, ask: translating the base now makes it for every later posting. */
  const lang=j.language||j.language_guess;
  const fam=baseFamily(), from=(lang&&baseIn(lang))||fam[0];
  if(multiLang()&&lang&&!baseIn(lang)&&!force) return tailorLangSheet(j,lang);
  const name=uniqueDocName(derivedName(j.title,j.company));
  S.tailoring.add(id); drawJobs();
  try{
    /* /api/new already records what it was copied from, so the new document's
       vs-base marks work with nothing extra done here. */
    const r=await post("/api/new",{name:name,kind:"cv",from:from.path});
    const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
    try{
      await post("/api/jobs/update",{id:j.id,cv_path:r.path});
      await loadJobs();
    }catch(e){
      /* The document exists either way, and an unlinked document is the
         recoverable half: the link chip in the editor attaches it. A job
         pointing at a file that was never written would not be. */
      toast("Created "+name+", but linking it to "+j.company+" failed: "+
            e.message+" Use the link chip in the editor.",true);
    }
    openDoc(r.path);
    toast("Tailored from "+docLabel(from.path));
  }catch(e){
    toast(e.message,true);
  }finally{
    S.tailoring.delete(id);
    if(S.view==="jobs") drawJobs();
  }
}

function tailorLangSheet(j,lang){
  const to=langOf(lang), src=langOf((baseFamily()[0]||{}).lang);
  openSheet(
    '<div><h3 id="sheet-title">This posting is in '+esc(to.english)+'</h3><p>Your base CV has no '+
      esc(to.english)+' version, so a CV tailored now would start in '+esc(src.english)+'.</p></div>'+
    '<p>Adding '+esc(to.english)+' to the base CV makes a copy with the dates and section '+
      'titles already in '+esc(to.english)+', for this application and every later one. '+
      'Your AI client, or you, then translate the text.</p>'+
    '<div class="foot"><button class="sbtn left" data-cancel>Cancel</button>'+
      '<button class="sbtn" id="tl-anyway">Tailor in '+esc(src.english)+'</button>'+
      '<button class="sbtn primary" id="tl-add">Add '+esc(to.english)+' to the base CV</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#tl-anyway").onclick=()=>{ closeSheet(); tailorFor(j.id,true) };
  $("#tl-add").onclick=async()=>{
    $("#tl-add").disabled=true;
    try{
      const r=await post("/api/language/add",{path:baseFamily()[0].path,language:lang});
      if(!r.ok){ $("#tl-add").disabled=false; return toast(r.error,true) }
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      translateNextSheet(r);
    }catch(e){ $("#tl-add").disabled=false; toast(e.message,true) }
  };
}

/* What the list is showing, in words, for the header above it. */
function filterTitle(){
  const f=S.jfilter;
  if(f.kind==="status") return prettyStatus(f.value);
  if(f.kind==="node") return (S.labels&&S.labels[f.value])||"Applications";
  if(f.kind==="saved") return f.value;
  if(f.kind==="alert"){
    const a=ATTENTION.find(([k])=>k===f.value);
    return a?a[1]:"Applications";
  }
  return "All applications";
}
function drawJobs(){
  drawRail();
  drawNextUp();
  const rows=visibleJobs();
  $("#jtitle").textContent=filterTitle();
  $("#jcount").textContent=S.jready?String(rows.length):"";
  const docName=p=>p?p.split("/").pop().replace(/\.ya?ml$/,""):null;
  $("#jobrows").innerHTML=rows.length?rows.map(j=>{
    const cv=docName(j.cv_path), letter=docName(j.letter_path);
    /* A row with a letter and no CV used to read "no CV yet" and drop the
       letter on the floor, which was wrong before and would now be worse: the
       offer to make one would be standing on top of a document that exists. */
    const docs=cv?esc(cv)+(letter?" +letter":""):(letter?esc(letter):null);
    const openable=cv?j.cv_path:j.letter_path;
    const ap=appliedAt(j);
    const due=j.followup_date&&j.followup_date<=isoToday();
    const jb=jobBoard(j);
    return '<button class="trow'+(DEAD_STATUS.has(j.status)?" dead":"")+
      (S.jsel===j.id?" sel":"")+'" data-id="'+esc(j.id)+'">'+
      '<span class="co">'+companyMark(j)+'<span class="con">'+
        esc(j.company)+'</span></span>'+
      '<span class="role"><b>'+esc(j.title)+'</b>'+
        (jb?'<span class="via" title="Found on '+esc(jb.label)+'">'+boardMark(jb)+'</span>':'')+
      '</span>'+
      '<span>'+(docs?'<span class="docs" data-open="'+esc(openable)+'">'+docs+'</span>'
               :S.tailoring.has(j.id)
                 ?'<span class="docs busy">Tailoring\u2026</span>'
                 /* The busy label is rendered from state rather than written
                    onto the node, because the workspace poll can redraw this
                    whole table underneath a copy that is still running. */
                 /* Quiet until you are on the row. Six ochre offers down one
                    column was the loudest thing on the screen, and the least
                    urgent. */
                 :'<span class="docs make" data-tailor="'+esc(j.id)+'" title="'+
                  'Copy the base CV, name it after this application, and open it'+
                  '"><i class="nt">Not tailored</i><u class="tl">Tailor a CV</u></span>')+'</span>'+
      '<span class="st"><span class="dot '+statusTone(j.status)+'"></span>'+
        esc(prettyStatus(j.status))+'</span>'+
      '<span class="when'+(ap?"":" none")+'">'+(ap?esc(shortDate(ap)):"–")+'</span>'+
      '<span class="when'+(j.followup_date?(due?" due":""):" none")+'">'+
        (j.followup_date?esc(shortDate(j.followup_date)):"–")+'</span>'+
      '</button>';
  }).join(""):(S.jobs.length
    ? '<div class="empty"><h3>Nothing matches</h3>'+
      '<p>Try another filter, or clear the search.</p></div>'
    /* The home screen of an empty workspace. This used to be the only place
       the app explained itself, on a CVs screen nobody lands on any more. */
    : '<div class="empty"><h3>No applications yet</h3>'+
      '<p>Add the roles you are applying for. Each one gets a CV tailored from '+
      'your base, in a click, and the funnel shows where they actually go.</p>'+
      '<p>'+(S.state&&S.state.base&&!S.state.base.missing
        ? "Your base CV is <b>"+esc(baseLabel())+"</b>. Every application starts "+
          "as a copy of it."
        : "Pick a base CV above and every application can start from it.")+'</p>'+
      '<div class="cta"><button class="sbtn primary" id="jb-first">Add an application'+
      '</button><button class="sbtn" id="jb-ai">Connect an AI client</button>'+
      '</div></div>');

  $$("#jobrows [data-id]").forEach(b=>b.onclick=e=>{
    if(e.target.closest("[data-open],[data-tailor]")) return;
    selectJob(b.dataset.id);
  });
  $$("#jobrows [data-tailor]").forEach(el=>el.onclick=e=>{
    e.stopPropagation();
    tailorFor(el.dataset.tailor);
  });
  const first=$("#jb-first"); if(first) first.onclick=()=>newJobSheet();
  const jai=$("#jb-ai"); if(jai) jai.onclick=()=>$("#btn-ai").click();
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
  ["status","Status","status"],["followup_date","Follow-up","date"],
  ["score","Fit","fit"],["source","Source","text"],
  ["language","Language","lang"],
];
/* Everything you set once when the application is created and rarely touch
   again. Company and Role are here rather than at the top because the peek's
   own header already prints them, and they used to be the first two fields
   you read -- restating the title one line below itself. */
const JOB_MORE=[
  ["company","Company","text"],["title","Role","text"],
  ["location","Location","text"],["url","Link","text"],
  ["salary_expected","Salary","number"],["contact_email","Contact","text"],
];
/* The rows the arrows walk: what the table is currently showing, in the order
   it is showing it, so Down always means "the row under this one". */
const peekRows=()=>visibleJobs();
function peekStep(delta){
  const rows=peekRows();
  const i=rows.findIndex(x=>x.id===S.jsel);
  if(i<0) return;
  const next=rows[i+delta];
  if(!next) return;
  S.jsel=next.id;
  drawJobs();
  drawJobInspector();
  const row=$('#jobrows [data-id="'+next.id+'"]');
  if(row) row.scrollIntoView({block:"nearest"});
}
function closePeek(){
  S.jsel=null;
  $("#jpeek").hidden=true;
  $("#v-jobs").classList.remove("peeking");
  drawJobs();
  paintStatus();
}


/* ---- an application's posting ---------------------------------------------
   Stored as Markdown when an AI client saved it, and as pasted text when a
   person did. Both read the same: headings from "#" lines, or from a short line
   ending in a colon; lists from "-", "*" or "•"; bold and links inline. */
function postInline(t){
  let out=esc(t);
  out=out.replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/(^|\W)\*(\S.*?\S|\S)\*(?=\W|$)/g,"$1<i>$2</i>");
  out=out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  out=out.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,'$1<a href="$2" target="_blank" rel="noreferrer">$2</a>');
  return out;
}
function postingHTML(text){
  const lines=String(text||"").replace(/\r/g,"").split("\n");
  let html="", list=null, para=[];
  /* A line break inside a pasted paragraph is usually meant: "Location: ..."
     under the title is its own line, not the rest of the title's sentence. */
  const flushP=()=>{ if(para.length){ html+="<p>"+para.map(postInline).join("<br>")+"</p>"; para=[] } };
  const flushL=()=>{ if(list){ html+="<ul>"+list.map(i=>"<li>"+postInline(i)+"</li>").join("")+"</ul>"; list=null } };
  for(const raw of lines){
    const l=raw.trim();
    if(!l){ flushP(); flushL(); continue }
    let m;
    if((m=l.match(/^#{1,6}\s+(.+)$/))||(l.length<60&&/:$/.test(l)&&!/^[-*•]/.test(l)&&!/https?:/.test(l))){
      flushP(); flushL();
      html+="<h4>"+postInline((m?m[1]:l.replace(/:$/,"")).replace(/\*\*/g,""))+"</h4>"; continue;
    }
    if((m=l.match(/^[-*•]\s+(.+)$/))||(m=l.match(/^\d+[.)]\s+(.+)$/))){ flushP(); (list=list||[]).push(m[1]); continue }
    flushL(); para.push(l);
  }
  flushP(); flushL();
  return html;
}
/* The facts a posting buries, pulled up as chips. Only what reads the same
   in every posting: a place, a salary range, travel, sponsorship. */
function postingChips(j){
  const t=String(j.description||""), out=[];
  const where=j.location||((t.match(/location\s*:\s*([^\n.]+)/i)||[])[1]||"").trim();
  const mode=(t.match(/\b(remote|hybrid|on-?site)\b/i)||[])[1];
  if(where) out.push(where+(mode&&!new RegExp(mode,"i").test(where)?" · "+mode[0].toUpperCase()+mode.slice(1).toLowerCase():""));
  else if(mode) out.push(mode[0].toUpperCase()+mode.slice(1).toLowerCase());
  const pay=t.match(/[$€£]\s?\d[\d,.]*\s?[kK]?\s?(?:[-–]|to)\s?[$€£]?\s?\d[\d,.]*\s?[kK]?(?:\s?(?:USD|EUR|GBP))?|\d[\d\s.,]*\s?(?:[kK]\s?)?€\s?(?:[-–]|à|to)\s?\d[\d\s.,]*\s?(?:[kK]\s?)?€/);
  if(pay) out.push(pay[0].replace(/\s+/g," ").trim());
  const tr=t.match(/travel[^.\n]{0,40}?(\d{1,2}\s?(?:[-–]\s?\d{1,2}\s?)?%)/i);
  if(tr) out.push("Travel "+tr[1].replace(/\s/g,""));
  if(/visa sponsorship|sponsor(?:ship)? (?:a |your )?visa/i.test(t)) out.push("Visa sponsorship");
  return out;
}
/* Where it was found, chosen rather than typed: the boards the app knows,
   with their marks, then the ways that are not a board. */
const SRC_FIRST=["linkedin","indeed","welcometothejungle","glassdoor","greenhouse","lever","wellfound"];
const SRC_OTHER=[["careers","Company’s careers page","↗"],["referral","Referral","♥"],
  ["recruiter","Recruiter","☎"]];
function sourceMark(j){
  const b=jobBoard(j);
  if(b) return boardMark(b)+'<span class="nm">'+esc(b.label)+'</span>';
  if(j.source) return '<span class="gl">…</span><span class="nm">'+esc(j.source)+'</span>';
  return '<span class="nm" style="color:var(--t500)">Not set</span>';
}
function sourceMenu(j,btn){
  let m=$("#ap-menu"); if(m){ m.remove(); return }
  const cur=(jobBoard(j)||{}).id, said=String(j.source||"");
  const boards=SRC_FIRST.map(id=>BOARDS.find(b=>b.id===id)).filter(Boolean)
    .concat(BOARDS.filter(b=>!SRC_FIRST.includes(b.id)));
  m=document.createElement("div"); m.id="ap-menu"; m.className="ap-menu"; m.setAttribute("role","listbox");
  m.setAttribute("aria-label","Where you found it");
  m.innerHTML=boards.map(b=>'<button role="option" data-src="'+esc(b.label)+'" aria-selected="'+
      String(b.id===cur)+'">'+boardMark(b)+esc(b.label)+'</button>').join("")+'<hr>'+
    SRC_OTHER.map(([k,l,g])=>'<button role="option" data-src="'+esc(l)+'" aria-selected="'+
      String(!cur&&said===l)+'"><span class="gl">'+g+'</span>'+esc(l)+'</button>').join("")+
    '<button role="option" data-src=""><span class="gl">…</span>Other…</button>';
  document.body.append(m);
  const r=btn.getBoundingClientRect();
  m.style.left=Math.min(r.left,innerWidth-260)+"px";
  m.style.top=Math.min(r.bottom+6,innerHeight-370)+"px";
  m.onclick=e=>{ const o=e.target.closest("[data-src]"); if(!o) return; m.remove();
    let v=o.dataset.src;
    if(!v){ v=(prompt("Where did you find it?",jobBoard(j)?"":said)||"").trim(); if(!v) return }
    saveJob(j.id,{source:v}) };
  setTimeout(()=>document.addEventListener("pointerdown",function off(ev){
    if(!m.contains(ev.target)){ m.remove(); document.removeEventListener("pointerdown",off,true) } },true),0);
}
/* A document on the application, as its first page. */
async function apThumb(el,path){
  let th=S.docThumbs[path];
  if(!th){
    try{ const t=await api("/api/thumb?path="+encodeURIComponent(path));
      th={png:t.png}; if(!t.fresh){ const r=await post("/api/render",{path});
        if(r.ok) th={png:r.pngs[0]} } }catch(e){ th={png:null,failed:true} }
    S.docThumbs[path]=th;
  }
  if(el.isConnected) el.innerHTML=th&&th.png?'<img alt="" src="'+esc(th.png+tok())+'">'
    :'<span>'+(th&&th.failed?"Doesn’t render":"Rendering…")+'</span>';
}
function drawJobInspector(){
  const j=S.jobs.find(x=>x.id===S.jsel);
  const peek=$("#jpeek"), head=$("#jinsp-title"), body=$("#jinsp-body");
  $("#v-jobs").classList.toggle("peeking",!!j);
  if(!j){ peek.hidden=true; return }
  peek.hidden=false;
  head.textContent=j.title||j.company;
  head.title=j.title||j.company;
  const sub=[j.company,j.location].filter(Boolean).join(" \u00b7 ");
  $("#jinsp-sub").textContent=sub; $("#jinsp-sub").title=sub;
  $("#jinsp-logo").innerHTML=companyMark(j);
  $("#jinsp-status").innerHTML='<span class="dot '+statusTone(j.status)+'"></span>'+esc(prettyStatus(j.status));

  const rows=peekRows(), at=rows.findIndex(x=>x.id===j.id);
  $("#jpk-idx").textContent=at<0?"":(at+1)+" of "+rows.length;
  $("#jpk-prev").disabled=at<=0;
  $("#jpk-next").disabled=at<0||at>=rows.length-1;

  const field=([k,label,kind])=>{
    let ctl;
    if(kind==="status") ctl='<span class="statusctl"><span class="dot '+statusTone(j.status)+'"></span><select data-j="status">'+S.statuses.map(s=>
      '<option value="'+s+'"'+(s===j.status?" selected":"")+'>'+esc(prettyStatus(s))+
      '</option>').join("")+'</select></span>';
    else if(kind==="lang"){
      /* Stored once said; until then a guess from the posting, offered. */
      const guess=j.language_guess, cur=j.language||"";
      ctl='<select data-j="language">'+
        '<option value=""'+(cur?"":" selected")+'>'+(guess?"Looks like "+
          esc(langOf(guess).native):"Not set")+'</option>'+
        ((S.state&&S.state.languages)||[]).map(l=>'<option value="'+l.code+'"'+
          (l.code===cur?" selected":"")+'>'+esc(l.native)+'</option>').join("")+'</select>';
    }
    else if(kind==="fit") ctl='<div class="fit" role="group" aria-label="Fit">'+
      [1,2,3,4,5].map(n=>'<button data-fit="'+n+'"'+((j.score||0)>=n?' class="on"':"")+
      ' title="'+n+' of 5" aria-label="'+n+' of 5"></button>').join("")+
      '<span class="fitv">'+(j.score?j.score+" / 5":"not rated")+'</span></div>';
    else ctl='<input data-j="'+k+'"'+(kind==="date"?' type="date"':"")+
      (kind==="number"?' type="number" class="mono"':"")+' value="'+
      esc(j[k]==null?"":j[k])+'">';
    return '<label>'+esc(label)+'</label>'+ctl;
  };
  const more=JOB_MORE.map(field).join("");

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

  /* One row of facts, the documents as pages, and the posting beside them,
     read as a posting. The fields set once when the application was made are
     folded away, with the delete under them. */
  const fact=(label,ctl)=>'<div class="ap-fact'+(label==="Interview"?" wide":label==="Language"?" lang":"")+
    '"><span>'+t(label)+'</span>'+ctl+'</div>';
  const statusCtl='<span class="statusctl"><span class="dot '+statusTone(j.status)+'"></span>'+
    '<select data-j="status" aria-label="Status">'+S.statuses.map(s=>'<option value="'+s+'"'+
    (s===j.status?" selected":"")+'>'+esc(prettyStatus(s))+'</option>').join("")+'</select></span>';
  const fitCtl='<div class="fit" role="group" aria-label="Fit">'+[1,2,3,4,5].map(n=>
    '<button data-fit="'+n+'"'+((j.score||0)>=n?' class="on"':"")+' title="'+n+' of 5" aria-label="'+
    n+' of 5"></button>').join("")+'</div>';
  const guess=j.language_guess, curL=j.language||"";
  const langCtl='<span class="ap-lang">'+flag(curL||guess)+'<select data-j="language" aria-label="Language">'+
    '<option value=""'+(curL?"":" selected")+'>'+(guess?"Looks like "+esc(langOf(guess).native):"Not set")+'</option>'+
    ((S.state&&S.state.languages)||[]).map(l=>'<option value="'+l.code+'"'+(l.code===curL?" selected":"")+'>'+
      esc(l.native)+'</option>').join("")+'</select></span>';
  const facts='<div class="ap-facts">'+
    fact("Status",statusCtl)+
    fact("Follow up",'<input data-j="followup_date" type="date" aria-label="Follow up" value="'+esc(j.followup_date||"")+'">')+
    fact("Fit",fitCtl)+
    fact("Found on",'<button class="ap-src" id="ap-src" aria-haspopup="listbox">'+sourceMark(j)+
      '<span class="caret">▾</span></button>')+
    fact("Language",langCtl)+
    fact("Interview",interviewCtl(j))+'</div>';

  const cvName=j.cv_path?j.cv_path.split("/").pop().replace(/\.(ya?ml|md)$/,""):null;
  const ltName=j.letter_path?j.letter_path.split("/").pop().replace(/\.(ya?ml|md)$/,""):null;
  const tailoring=S.tailoring&&S.tailoring.has(j.id);
  const cvCard=j.cv_path
    ? '<div class="ap-doc"><div class="pg" data-open-doc="'+esc(j.cv_path)+'" data-thumb="'+esc(j.cv_path)+
        '" role="button" tabindex="0" aria-label="Open the CV"><span>Rendering…</span></div>'+
      '<div class="t"><b>CV</b><span>'+esc(cvName)+'</span></div>'+
      '<div class="a"><button class="obtn" data-open-doc="'+esc(j.cv_path)+'">Open</button>'+
        '<button class="obtn" id="job-ats" title="ATS check: what an applicant tracking system reads">ATS</button></div></div>'
    : '<div class="ap-doc"><div class="pg none"><span>No CV for this one yet</span>'+
        '<button class="obtn" data-tailor-here'+(tailoring?" disabled":"")+'>'+(tailoring?"Tailoring…":"Tailor a CV")+'</button></div>'+
      '<div class="t"><b>CV</b><span>Copied from your base CV</span></div></div>';
  const ltCard=j.letter_path
    ? '<div class="ap-doc"><div class="pg" data-open-doc="'+esc(j.letter_path)+'" data-thumb="'+esc(j.letter_path)+
        '" role="button" tabindex="0" aria-label="Open the cover letter"><span>Rendering…</span></div>'+
      '<div class="t"><b>Cover letter</b><span>'+esc(ltName)+'</span></div>'+
      '<div class="a"><button class="obtn" data-open-doc="'+esc(j.letter_path)+'">Open</button></div></div>'
    : '<div class="ap-doc"><div class="pg none"><span>No cover letter</span>'+
        '<button class="obtn" id="ap-write">Write one</button></div>'+
      '<div class="t"><b>Cover letter</b><span>In the look of the CV</span></div></div>';
  const jb=jobBoard(j), words=(String(j.description||"").match(/\S+/g)||[]).length;
  /* The posting is not a document you send, so it is not a tile beside
     them: it is one card on the right, its link and its text together. */
  /* A board with a website is named; a referral or a recruiter is not a
     place, so the link says where it goes instead. */
  let site=""; try{ site=new URL(j.url).hostname.replace(/^www\./,"") }catch(e){}
  const web=jb&&jb.host?jb:null, who=web?web.label:site;

  const hist=(j.status_history||[]);
  const timeline=hist.length?'<div class="tl">'+hist.map(h=>
    '<div class="tli"><div class="spine"><i></i><u></u></div>'+
    '<div class="ev"><span>'+esc(prettyStatus(h.status))+'</span>'+
    '<span class="when mono">'+esc(shortDate(h.at))+'</span></div></div>').join("")+'</div>'
    :'<p class="note muted">No history yet.</p>';
  const chips=postingChips(j);
  const openLink=j.url
    ? '<a class="obtn" href="'+esc(j.url)+'" target="_blank" rel="noreferrer" title="'+esc(j.url)+'">'+
        (web?boardMark(web):'')+esc(who?t("Open on {site}",{site:who}):t("Open the posting"))+'<span class="ext">↗</span></a>'
    : '<button class="obtn" id="ap-addlink">'+t("Add the link")+'</button>';
  const pasteBox=(val,first)=>'<label class="ap-plabel" for="ap-paste">'+
      t(first?"Paste it here while it is up":"The posting, as text")+'</label>'+
    '<textarea class="posting-edit" id="ap-paste" placeholder="'+
      esc(t("Paste the posting. Headings and lists come through: a short line ending in a colon, or lines starting with -."))+'">'+
      esc(val||"")+'</textarea>'+
    '<div class="ap-pfoot">'+(first?'<span>'+t("Or ask your AI client to save it:")+' <code>'+
      esc(t("save the posting for {co}",{co:j.company}))+'</code></span>':'<span></span>')+
      '<span class="grow"></span>'+(first?'':'<button class="obtn" id="ap-post-cancel">'+t("Cancel")+'</button>')+
      '<button class="pbtn" id="ap-post-save">'+t(first?"Save the posting":"Save")+'</button></div>';
  const posting='<article class="ap-pcard" aria-label="'+esc(t("The posting"))+'">'+
    '<div class="ap-phead"><div class="tx"><b>'+t("The posting")+'</b><span>'+
      esc(words?t("{n} words · kept here in case the advert comes down",{n:words})
        :t("Not saved yet · adverts come down, and a tailored CV and a letter are written against it"))+
      '</span></div>'+openLink+
      (words?'<button class="obtn" id="ap-post-edit">'+t("Edit")+'</button>':'')+'</div>'+
    '<div class="ap-pbody" id="ap-pbody">'+(words
      ? (chips.length?'<div class="ap-chips">'+chips.map(c=>'<span>'+esc(c)+'</span>').join("")+'</div>':'')+
        '<div class="ap-post" id="ap-post">'+postingHTML(j.description)+'</div>'
      : pasteBox("",true))+'</div></article>';

  body.innerHTML=
    '<div class="peek-grid">'+
      '<div class="col">'+
        facts+
        '<div class="block"><span class="blabel">Documents</span><div class="ap-docs">'+cvCard+ltCard+'</div></div>'+
        (j.cv_path?'<div class="block" id="jdiff-block" hidden><span class="blabel">'+
          'Changed from the base</span><div class="bdiff" id="jdiff" data-path="'+
          esc(j.cv_path)+'"></div></div>':'')+
        '<div class="block"><span class="blabel">History</span>'+timeline+'</div>'+
        '<details class="fold"><summary>Company, role and the rest</summary>'+
          '<div class="fg2" style="margin-top:11px">'+more+'</div>'+
          '<div class="card" style="margin-top:12px">'+docRow("CV","cv_path","My CVs")+
            docRow("Cover letter","letter_path","Cover letters")+'</div></details>'+
        '<div class="block ruled foot-del"><button class="sbtn danger" id="job-del">'+
          'Delete this application</button></div>'+
      '</div>'+
      '<div class="col">'+
        '<div class="block"><span class="blabel">Notes</span>'+
          '<textarea data-j="notes" class="notes">'+esc(j.notes||"")+'</textarea></div>'+
        '<div class="block grow">'+posting+'</div>'+
      '</div>'+
    '</div>';
  body.querySelectorAll("[data-thumb]").forEach(el=>apThumb(el,el.dataset.thumb));
  $("#ap-src").onclick=e=>{ e.stopPropagation(); sourceMenu(j,$("#ap-src")) };
  const wr=$("#ap-write");
  if(wr) wr.onclick=async()=>{
    wr.disabled=true;
    try{ const r=await post("/api/letter/new",{job_id:j.id});
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      await loadJobs(true); openDoc(r.path) }
    catch(e){ wr.disabled=false; toast(e.message,true) }
  };
  const th=body.querySelector("[data-tailor-here]");
  if(th) th.onclick=()=>tailorFor(j.id);
  const wirePaste=()=>{
    const sv=$("#ap-post-save"), ta=$("#ap-paste");
    if(sv) sv.onclick=()=>{ const v=ta.value.trim(); if(!v&&!j.description) return ta.focus();
      saveJob(j.id,{description:v||null}) };
    const cn=$("#ap-post-cancel"); if(cn) cn.onclick=()=>selectJob(j.id);
  };
  wirePaste();
  const ed=$("#ap-post-edit");
  if(ed) ed.onclick=()=>{ ed.hidden=true; $("#ap-pbody").innerHTML=pasteBox(j.description,false);
    wirePaste(); $("#ap-paste").focus() };
  const al=$("#ap-addlink");
  if(al) al.onclick=()=>{ const u=prompt(t("The link to the posting"),"https://");
    if(u&&/^https?:\/\/\S+\.\S+/.test(u.trim())) saveJob(j.id,{url:u.trim()}) };
  const diff=$("#jdiff");
  if(diff) fillBaseDiff(diff,j.cv_path);
  const ab=$("#job-ats");
  if(ab) ab.onclick=()=>atsSheet(j.cv_path,j.description?j.id:"");
  body.querySelectorAll("[data-j]").forEach(el=>{
    el.onchange=()=>{
      let v=el.value;
      if(el.type==="number") v=v===""?null:Number(v);
      saveJob(j.id,{[el.dataset.j]:v===""?null:v});
    };
  });
  wireInterview(j);
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
$("#jpk-prev").onclick=()=>peekStep(-1);
$("#jpk-next").onclick=()=>peekStep(1);
$("#jpk-close").onclick=closePeek;

/* Arrow keys walk the list with the peek open, which is the whole point of it
   being a peek rather than a page: you can read every application in the
   funnel without ever closing anything. They stay out of the way of a field
   being typed into, and of the select and date inputs, which use the arrows
   themselves. */
document.addEventListener("keydown",e=>{
  if(S.view!=="jobs"||$("#jpeek").hidden) return;
  if(!$("#sheet").hidden||!$("#ovl-settings").hidden||!$("#ovl-design").hidden) return;
  if(e.key==="Escape"){ e.preventDefault(); return closePeek() }
  if(e.key!=="ArrowUp"&&e.key!=="ArrowDown") return;
  const el=document.activeElement;
  if(el&&el.closest("#jinsp-body")&&
     /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
  e.preventDefault();
  peekStep(e.key==="ArrowDown"?1:-1);
});

/* When and where the interview is: the time as the invitation gave it, the
   zone it gave it in (guessed from where the job is), and what that is for
   you. */
function interviewCtl(j){
  const zone=j.interview_tz||"", guess=guessTz(j);
  const mine=userTz();
  return '<div class="ap-iv"><input type="datetime-local" id="ap-iv-at" aria-label="'+t("Interview time")+
    '" value="'+esc(String(j.interview_at||"").slice(0,16))+'">'+
    '<select id="ap-iv-tz" aria-label="'+t("Time zone of the interview")+'">'+
    '<option value=""'+(zone?"":" selected")+'>'+t("Your time")+' · '+esc(tzCity(mine))+'</option>'+
    tzOptions(zone,guess&&guess!==mine?guess:null)+'</select></div>'+
    (j.interview_at?'<small class="ap-iv-say">'+esc(interviewLine(j))+'</small>':'');
}
function wireInterview(j){
  const at=$("#ap-iv-at"), tz=$("#ap-iv-tz");
  if(!at) return;
  at.onchange=()=>{
    const patch={interview_at:at.value?at.value+":00":null};
    /* A first time for a job somewhere else starts in that place's zone. */
    if(at.value&&!j.interview_at&&!tz.value){ const g=guessTz(j); if(g&&g!==userTz()) patch.interview_tz=g }
    saveJob(j.id,patch);
  };
  tz.onchange=()=>saveJob(j.id,{interview_tz:tz.value||null});
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
$("#btn-newdoc").onclick=()=>newDocumentSheet();
/* Import makes a new CV from a file rather than overwriting one: the design is
   the base CV's, so it prints like everything else, and the base is left
   alone. What was read, and anything to check, is shown before it is made. */
$("#btn-importdoc").onclick=()=>{ $("#importdoc-file").value=""; $("#importdoc-file").click() };
$("#importdoc-file").onchange=async()=>{
  const file=$("#importdoc-file").files[0]; if(!file) return;
  const btn=$("#btn-importdoc"); btn.disabled=true; btn.textContent="Reading…";
  let imp;
  try{ imp=await importFile(file) }
  catch(e){ return toast(e.message,true) }
  finally{ btn.disabled=false; btn.innerHTML="Import&#8230;" }
  importSheet(imp);
};
function importSheet(imp){
  const b=S.state&&S.state.base, base=b&&!b.missing?b.path:null;
  const stem=slug(imp.cv.name&&imp.cv.name!=="Your Name"?imp.cv.name:
    file_stem(imp.name))||"imported";
  openSheet(
    '<div><h3 id="sheet-title">Import '+esc(imp.name)+'</h3><p>'+
      (base?'A new CV, in the base CV&#8217;s design. The base itself is not changed.'
           :'A new CV from what was read.')+'</p></div>'+
    '<ul class="imp-found">'+imp.found.map(f=>'<li><b>'+esc(f.what)+'</b><span>'+
      esc(String(f.count))+'</span></li>').join("")+'</ul>'+
    (imp.notes.length?'<div class="imp-notes"><b>To check</b><ul>'+imp.notes.map(n=>
      '<li>'+esc(n)+'</li>').join("")+'</ul></div>':'')+
    '<div class="fg w88"><label for="imp-name">Save as</label>'+
      '<input id="imp-name" class="mono" autocomplete="off" value="'+
        esc(uniqueDocName(stem+"-cv"))+'"></div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="imp-go">Create and open</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#imp-go").onclick=async()=>{
    const name=$("#imp-name").value.trim().replace(/\.ya?ml$/i,"");
    if(!name) return toast("Give it a name",true);
    $("#imp-go").disabled=true;
    try{
      const r=await post("/api/new",{name,kind:"cv",from:base});
      await post("/api/save",{path:r.path,patches:[{path:["cv"],value:imp.cv}]});
      closeSheet();
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      openDoc(r.path); toast("Imported "+imp.name);
    }catch(e){ $("#imp-go").disabled=false; toast(e.message,true) }
  };
  $("#imp-name").select();
}
const file_stem=n=>String(n||"").replace(/\.[^.]+$/,"");

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
/* Five roles, not three. A rejection and a reply-you-are-waiting-on used to be
   the same grey, which is the one distinction the chart exists to make. The
   spine and the waiting stages stay recessive so the outcomes carry the colour;
   every node is labelled, so nothing here is colour alone. */
/* The funnel nodes mapped onto the same vocabulary the Jobs table uses, so a
   status cannot mean one thing in the table and another in the chart. The
   spine and the waiting stages stay recessive; outcomes carry the colour. */
const FN_TOTAL=new Set(["all","applied_s"]);
/* An outcome's colour. Two splits matter here and both used to be painted
   over: an offer is not the same state as "still interviewing" (they shared
   --fn-positive, so "I have an offer" and "nothing decided yet" were the same
   colour), and a rejection after three rounds is not a rejection after nobody
   read past page one -- the README says those say very different things, and
   all four dead ends were one tone. */
const FN_TONE={
  pending:"draft", awaiting:"waiting", still_iv:"live",
  interview_s:"live", offer_s:"offer", deciding:"offer",
  accepted:"won", refused:"closed",
  rejected:"lost", ghosted:"lost",
  rejected_iv:"lost-late", ghosted_iv:"lost-late",
};
const fnTone=id=>FN_TOTAL.has(id)?"t-total":"t-"+(FN_TONE[id]||"draft");
/* A band takes the colour of where it lands: that is the outcome it reports. */
const fnBand=id=>"b-"+(FN_TONE[id]||"draft");

$$("#range button").forEach(b=>b.onclick=()=>{
  $$("#range button").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
  S.since=b.dataset.since; S.funnel=null; loadFunnel("morph");
});
function sinceDate(){
  if(!S.since) return null;
  const d=new Date();
  if(S.since==="6m") d.setMonth(d.getMonth()-6); else d.setDate(d.getDate()-30);
  return d.toISOString().slice(0,10);
}

/* The chart's colours are the stage's own variables (--sk-*), so they follow
   the theme with it: the funnel's light palette on white, the dark one on the
   dark stage. SVG only reads a variable from a style, never an attribute. */
const skTone=id=>FN_TOTAL.has(id)?"total":(FN_TONE[id]||"draft");
const skCol=id=>"var(--sk-"+skTone(id)+")";
/* Where an application can still change. These are the stages the lights
   travel to: what is in motion is what is still in play. */
const FN_LIVE=["awaiting","still_iv","deciding"];
const IV_STATUSES=new Set(["interviewing","offer","accepted","refused",
  "rejected_interviewing","ghosted_interviewing"]);
const reduceMotion=()=>matchMedia("(prefers-reduced-motion: reduce)").matches;

/* mode: "enter" plays the entrance, "morph" moves the old shape to the new
   one (a range change), "still" redraws in place (a click, a resize). */
async function loadFunnel(mode="enter"){
  const host=$("#chart");
  if(S.funnel){ return drawFunnel(mode) }
  if(mode!=="morph") host.innerHTML='<p class="note"><span class="spin"></span> Loading…</p>';
  try{
    await ensureD3();
    const since=sinceDate();
    S.funnel=await api("/api/funnel"+(since?"?since="+since:""));
    if(!S.jready) await loadJobs(true);
  }catch(e){
    host.innerHTML='<div class="empty"><h3>Could not load the funnel</h3><p>'+
      esc(e.message)+'</p></div>';
    return;
  }
  drawFunnel(mode);
}

/* The applications the range covers, from the list the Applications screen
   already has, so nothing here can disagree with it. */
function fnJobs(){
  const since=S.funnel&&S.funnel.since;
  return (S.jobs||[]).filter(j=>!since||String(j.created_at||"")>=since);
}
function fnEvent(j,status){
  const e=(j.status_history||[]).find(e=>e.status===status);
  return e&&e.at?new Date(String(e.at).slice(0,19)):null;
}
const DAY=86400000;

let fnTimer=null;
function drawFunnel(mode="still"){
  const f=S.funnel, t=f.totals, page=$("#fn-page");
  const animate=mode!=="still"&&!reduceMotion();
  const live=fnJobs().filter(j=>["applied","interviewing","offer"].includes(j.status)).length;
  const first=fnJobs().map(j=>j.created_at).filter(Boolean).sort()[0];
  $("#fn-sub").textContent=t.total?t.total+" application"+(t.total===1?"":"s")+
    (first&&!f.since?" since "+shortDate(first):"")+" · "+live+" still in play":"";
  if(!t.total){
    page.classList.remove("fx-in");
    $("#fn-body").hidden=true;
    let e=$("#fn-none");
    if(!e){ e=document.createElement("div"); e.id="fn-none"; e.className="empty"; page.append(e) }
    e.innerHTML='<h3>Nothing tracked'+(f.since?' in this range':' yet')+'</h3><p>Add applications '+
      'and this shows how far they get: how many reach an interview, how many convert to '+
      'an offer, where the rest drop out, and which sources are worth your time.</p>';
    return;
  }
  const none=$("#fn-none"); if(none) none.remove();
  $("#fn-body").hidden=false;
  if(mode==="enter"&&animate){
    page.classList.remove("fx-in"); void page.offsetWidth; page.classList.add("fx-in");
    clearTimeout(fnTimer); fnTimer=setTimeout(()=>page.classList.remove("fx-in"),3400);
  }
  drawJourney(mode,animate);
  /* The column beside the chart first: the chart is sized to fill the
     height it sets. */
  drawMomentum();
  paintFunnelJobs();
  drawSankey(mode,animate);
  drawSources();
  drawReplies();
  drawRates();
}

/* Numbers that count up to their value, or from the one they showed before a
   range change. */
function countUp(root,animate,delay0=0){
  root.querySelectorAll("[data-to]").forEach((el,i)=>{
    const to=+el.dataset.to, from=+(el.dataset.from||0), suf=el.dataset.suf||"";
    if(!animate||to===from){ el.textContent=to+suf; return }
    const d=+(el.dataset.delay||delay0), dur=1100, t0=performance.now()+d;
    el.textContent=from+suf;
    const step=now=>{
      const k=Math.min(1,Math.max(0,(now-t0)/dur)), e=1-Math.pow(1-k,3);
      el.textContent=Math.round(from+(to-from)*e)+suf;
      if(k<1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

function fnStages(){
  const t=S.funnel.totals, c=S.funnel.by_status||{};
  const heard=t.interviewed+(c.rejected||0);
  return [["Sent",t.applied],["Heard back",heard],["Interviewed",t.interviewed],
          ["Offers",t.offers],["Accepted",c.accepted||0]];
}
function drawJourney(mode,animate){
  const host=$("#fn-journey"), st=fnStages(), t=S.funnel.totals, sent=st[0][1];
  const prev=S.fnPrevJourney||[];
  const conv=[["replied"],["of replies"],["became offers"],["accepted"]];
  /* The accent marks the step that is actually leaking, once there are
     enough applications under it to call it a rate. */
  let worst=-1, low=101;
  st.forEach(([,n],i)=>{ if(!i) return; const d=st[i-1][1];
    if(d>=10){ const r=n/d*100; if(r<low){ low=r; worst=i } } });
  let h="";
  st.forEach(([label,n],i)=>{
    if(i){
      const d=st[i-1][1], r=d?Math.round(n/d*100):null;
      h+='<div class="fj-conv'+(i===worst?" acc":"")+'" style="animation-delay:'+(.55+i*.18)+'s">'+
        '<svg width="26" height="12" viewBox="0 0 26 12" aria-hidden="true"><path d="M0 6h22M17 1l5 5-5 5" '+
        'fill="none" stroke="currentColor" stroke-width="1.6"/></svg><b>'+(r==null?"–":r+"%")+'</b>'+
        '<span>'+(i===worst?"biggest drop":conv[i-1][0])+'</span></div>';
    }
    const sub=i?(sent?Math.round(n/sent*100)+"% of sent":"–"):
      (t.total?Math.round(n/t.total*100)+"% of "+t.total+" tracked":"");
    const w=sent?Math.max(n?1.5:0,n/sent*100):0;
    h+='<div class="fj-stg fx-r" style="animation-delay:'+(.35+i*.18)+'s"><span class="sl">'+label+'</span>'+
      '<b data-to="'+n+'" data-from="'+(mode==="morph"&&prev[i]!=null?prev[i]:0)+'" data-delay="'+
      (mode==="morph"?0:450+i*180)+'">'+n+'</b><small>'+sub+'</small>'+
      '<span class="fj-bar"><span style="width:'+w+'%;animation-delay:'+(.6+i*.18)+'s"></span></span></div>';
  });
  host.innerHTML=h;
  countUp(host,animate);
  S.fnPrevJourney=st.map(s=>s[1]);
}

/* ---- the chart ---------------------------------------------------------- */
function skBand(x0,x1,y0,y1,w){
  const xm=(x0+x1)/2, a=y0-w/2, b=y1-w/2, c=y0+w/2, d=y1+w/2;
  return "M"+x0+" "+a+"C"+xm+" "+a+" "+xm+" "+b+" "+x1+" "+b+"L"+x1+" "+d+
    "C"+xm+" "+d+" "+xm+" "+c+" "+x0+" "+c+"Z";
}
function skCenter(l){
  const x0=l.source.x1, x1=l.target.x0, xm=(x0+x1)/2;
  return x0+" "+l.y0+"C"+xm+" "+l.y0+" "+xm+" "+l.y1+" "+x1+" "+l.y1;
}
function drawSankey(mode,animate){
  const f=S.funnel, host=$("#chart");
  const nodes=f.nodes.filter(n=>n.count>0);
  const idx=new Map(nodes.map((n,i)=>[n.id,i]));
  const links=f.links.filter(l=>idx.has(l.source)&&idx.has(l.target))
    .map(l=>({source:idx.get(l.source),target:idx.get(l.target),value:l.value,
              sid:l.source,tid:l.target}));
  if(!links.length){ host.innerHTML=""; return }
  const W=Math.max(560,host.clientWidth||900);
  const side=$(".fn-side").offsetHeight;
  const H=Math.max(380,Math.min(640,side>200&&innerWidth>1180?side-86:nodes.length*42));
  /* The right-hand pad is where the last column's labels sit. */
  const PAD=Math.max(170,Math.min(220,W*0.22));
  const layout=d3.sankey().nodeWidth(10).nodePadding(22).nodeAlign(d3.sankeyLeft)
    .extent([[2,16],[W-PAD,H-6]]);
  const g=layout({nodes:nodes.map(n=>({...n})),links:links.map(l=>({...l}))});
  const total=f.totals.total||1;

  /* Every link on a path through a node: what lights up when you hover it. */
  const up=id=>g.links.filter(l=>l.tid===id).flatMap(l=>[l,...up(l.sid)]);
  const down=id=>g.links.filter(l=>l.sid===id).flatMap(l=>[l,...down(l.tid)]);
  const lineage=new Map(g.nodes.map(n=>[n.id,new Set([...up(n.id),...down(n.id)])]));
  const touches=l=>!S.fnode||lineage.get(S.fnode).has(l);

  const defs=g.links.map((l,i)=>'<linearGradient id="skg'+i+'" gradientUnits="userSpaceOnUse" x1="'+
    l.source.x1+'" x2="'+l.target.x0+'" y1="0" y2="0"><stop offset="0" style="stop-color:'+skCol(l.sid)+
    ';stop-opacity:var(--sk-a0)"/><stop offset="1" style="stop-color:'+skCol(l.tid)+';stop-opacity:var(--sk-a1)"/>'+
    '</linearGradient>').join("");
  const bands=g.links.map((l,i)=>'<path class="sk-link'+(touches(l)?"":" sk-dim")+'" data-i="'+i+
    '" d="'+skBand(l.source.x1,l.target.x0,l.y0,l.y1,Math.max(1,l.width))+'" fill="url(#skg'+i+')" '+
    'style="animation-delay:'+(.9+l.source.depth*.28).toFixed(2)+'s"><title>'+esc(l.source.label)+
    ' → '+esc(l.target.label)+': '+l.value+'</title></path>').join("");

  /* Lights along the whole path to each stage still in play, more of them
     where more applications are waiting. */
  const chain=id=>{ const l=g.links.find(l=>l.tid===id); return l?[...chain(l.sid),l]:[] };
  let parts="";
  FN_LIVE.forEach(id=>{
    const n=g.nodes.find(n=>n.id===id); if(!n) return;
    const ch=chain(id); if(!ch.length) return;
    const d="M"+skCenter(ch[0])+ch.slice(1).map(l=>" L"+skCenter(l)).join("");
    /* A stream rather than a line: more lights where more is waiting, spread
       across the narrowest band they pass through, each its own size, glow
       and pace, so they drift like a current instead of marching in step. */
    const band=Math.max(2,Math.min(...ch.map(l=>l.width)));
    const cnt=Math.max(4,Math.min(22,Math.round(n.count*1.2)));
    for(let k=0;k<cnt;k++){
      const r1=((k*73)%97)/97, r2=((k*41+13)%89)/89, r3=((k*29+7)%83)/83;
      const dur=6+r1*3.5, jit=(r2-.5)*band*.8;
      parts+='<circle r="'+(1.5+r3*1.3).toFixed(2)+'" style="fill:'+skCol(id)+';opacity:'+(.55+r1*.45).toFixed(2)+
        '" filter="url(#skglow)" transform="translate(0 '+jit.toFixed(1)+')"><animateMotion dur="'+dur.toFixed(2)+
        's" begin="'+(-(dur/cnt)*k-r3*dur).toFixed(2)+'s" repeatCount="indefinite" path="'+d+'" keyPoints="0;1" '+
        'keyTimes="0;1" calcMode="spline" keySplines=".35 0 .65 1"/></circle>';
    }
  });

  const bars=g.nodes.map(n=>{
    const h=Math.max(3,n.y1-n.y0);
    const off=S.fnode&&S.fnode!==n.id&&![...lineage.get(S.fnode)].some(l=>l.sid===n.id||l.tid===n.id);
    return '<g class="sk-hit'+(off?" sk-dim":"")+'" data-node="'+esc(n.id)+'" role="button" tabindex="0" '+
      'aria-label="'+esc(n.label)+': '+n.count+'. List them" style="animation-delay:'+
      (.7+n.depth*.28).toFixed(2)+'s">'+
      '<rect x="'+(n.x0-6)+'" y="'+(n.y0-10)+'" width="'+(n.x1-n.x0+12)+'" height="'+(h+20)+
      '" fill="transparent"/>'+
      '<rect class="sk-node'+(S.fnode===n.id?" sk-on":"")+'" data-n="'+esc(n.id)+'" x="'+n.x0+
      '" y="'+n.y0+'" width="'+(n.x1-n.x0)+'" height="'+h+'" rx="2.5" style="fill:'+skCol(n.id)+'"/>'+
      (FN_LIVE.includes(n.id)?'<rect class="sk-ring" x="'+(n.x0-3)+'" y="'+(n.y0-3)+'" width="'+
        (n.x1-n.x0+6)+'" height="'+(h+6)+'" rx="4" style="stroke:'+skCol(n.id)+'"/>':"")+'</g>';
  }).join("");

  const chips=g.nodes.map(n=>{
    const cy=(n.y0+n.y1)/2, off=S.fnode&&S.fnode!==n.id&&
      ![...lineage.get(S.fnode)].some(l=>l.sid===n.id||l.tid===n.id);
    return '<div class="sk-chip'+(n.id==="accepted"?" won":"")+(off?" sk-dim":"")+'" data-chip="'+
      esc(n.id)+'" style="left:'+(n.x1+8)+'px;top:'+(cy-12)+'px;animation-delay:'+
      (1.2+n.depth*.28).toFixed(2)+'s"><i style="background:'+skCol(n.id)+'"></i>'+esc(n.label)+
      ' <b>'+n.count+'</b><span>'+Math.round(n.count/total*100)+'%</span>'+
      (FN_LIVE.includes(n.id)?'<em>live</em>':'')+'</div>';
  }).join("");

  host.innerHTML='<svg width="'+W+'" height="'+H+'" viewBox="0 0 '+W+' '+H+'" role="img" '+
    'aria-label="Application funnel">'+
    '<defs>'+defs+'<filter id="skglow" x="-3" y="-3" width="7" height="7"><feGaussianBlur '+
    'stdDeviation="1.6" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/>'+
    '</feMerge></filter></defs><g>'+bands+'</g><g class="sk-parts">'+parts+'</g><g>'+bars+
    '</g></svg>'+chips;
  $("#fn-legend").hidden=!FN_LIVE.some(id=>g.nodes.some(n=>n.id===id));

  /* Hover traces a stage's whole path; the rest steps back. */
  const paths=[...host.querySelectorAll(".sk-link")];
  const trace=id=>{
    const set=id?lineage.get(id):null;
    paths.forEach((p,i)=>p.classList.toggle("sk-dim",set?!set.has(g.links[i]):!touches(g.links[i])));
    host.querySelector(".sk-tip")?.remove();
    if(!id) return;
    const n=g.nodes.find(n=>n.id===id);
    const want=new Set((S.nodes&&S.nodes[id])||[]);
    const names=fnJobs().filter(j=>want.has(j.status)).map(j=>j.company);
    const who=names.slice(0,3).join(", ")+(names.length>3?" +"+(names.length-3):"");
    const tip=document.createElement("div");
    tip.className="sk-tip";
    tip.innerHTML='<b>'+esc(n.label)+' · '+n.count+'</b><span>'+Math.round(n.count/total*100)+
      '% of everything tracked</span>'+(who?'<span class="who">'+esc(who)+'</span>':'')+
      '<span class="go">'+(S.fnode===id?"Click to clear":"Click to list them")+'</span>';
    host.append(tip);
    const x=Math.min(n.x1+30,W-tip.offsetWidth-4), y=Math.max(-8,n.y0-tip.offsetHeight-8);
    tip.style.left=x+"px"; tip.style.top=(y<0?n.y1+10:y)+"px";
  };
  host.querySelectorAll("[data-node]").forEach(el=>{
    const id=el.dataset.node, pick=()=>fnPick(id);
    el.onclick=pick;
    el.onmouseenter=el.onfocus=()=>trace(id);
    el.onmouseleave=el.onblur=()=>trace(null);
    el.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); pick() } };
  });

  if(mode==="morph"&&animate&&S.fnGeom) skMorph(host,g,S.fnGeom);
  S.fnGeom={links:new Map(g.links.map(l=>[l.sid+">"+l.tid,{x0:l.source.x1,x1:l.target.x0,y0:l.y0,
    y1:l.y1,w:Math.max(1,l.width)}])),nodes:new Map(g.nodes.map(n=>[n.id,{x0:n.x0,y0:n.y0,y1:n.y1}]))};
}

/* A range change moves each band and stage from where it was to where it
   now is, rather than wiping the chart and drawing another. */
function skMorph(host,g,old){
  const paths=[...host.querySelectorAll(".sk-link")], rects=host.querySelectorAll(".sk-node");
  const chips=host.querySelectorAll(".sk-chip"), rings=host.querySelectorAll(".sk-ring");
  const parts=host.querySelector(".sk-parts");
  const from=g.links.map(l=>{
    const o=old.links.get(l.sid+">"+l.tid);
    return o||{x0:l.source.x1,x1:l.target.x0,y0:l.y0,y1:l.y1,w:0};
  });
  const nfrom=g.nodes.map(n=>old.nodes.get(n.id)||{x0:n.x0,y0:(n.y0+n.y1)/2,y1:(n.y0+n.y1)/2});
  const lerp=(a,b,k)=>a+(b-a)*k, dur=650, t0=performance.now();
  if(parts) parts.style.opacity="0";
  const frame=now=>{
    const k=Math.min(1,(now-t0)/dur), e=k<.5?4*k*k*k:1-Math.pow(-2*k+2,3)/2;
    g.links.forEach((l,i)=>{ const a=from[i];
      paths[i].setAttribute("d",skBand(lerp(a.x0,l.source.x1,e),lerp(a.x1,l.target.x0,e),
        lerp(a.y0,l.y0,e),lerp(a.y1,l.y1,e),lerp(a.w,Math.max(1,l.width),e))) });
    g.nodes.forEach((n,i)=>{ const a=nfrom[i], y0=lerp(a.y0,n.y0,e), y1=lerp(a.y1,n.y1,e);
      rects[i].setAttribute("y",y0); rects[i].setAttribute("height",Math.max(3,y1-y0));
      chips[i].style.top=((y0+y1)/2-12)+"px" });
    rings.forEach(r=>r.style.opacity=String(e));
    if(k<1) requestAnimationFrame(frame);
    else if(parts){ parts.style.transition="opacity .6s"; parts.style.opacity="1" }
  };
  requestAnimationFrame(frame);
}

/* Clicking a stage lists who is in it, beside the chart, in place of what is
   still in play; clicking it again goes back. */
function fnPick(id){
  S.fnode=S.fnode===id?null:id;
  paintFunnelJobs();
  drawSankey("still",false);
}

/* ---- beside the chart --------------------------------------------------- */
function drawMomentum(){
  const host=$("#fn-momentum"), jobs=fnJobs(), now=new Date();
  const monday=d=>{ const x=new Date(d); x.setHours(0,0,0,0); x.setDate(x.getDate()-((x.getDay()+6)%7)); return x };
  const sentAt=jobs.map(j=>fnEvent(j,"applied")||(j.status!=="pending"&&j.created_at?
    new Date(String(j.created_at).slice(0,19)):null)).filter(Boolean);
  const ivAt=jobs.map(j=>fnEvent(j,"interviewing")).filter(Boolean);
  const start=monday(S.funnel.since?new Date(S.funnel.since):
    (sentAt.length?new Date(Math.min(...sentAt)):now));
  const thisWk=monday(now);
  const weeks=Math.max(1,Math.min(26,Math.round((thisWk-start)/(7*DAY))+1));
  const w0=new Date(thisWk-(weeks-1)*7*DAY);
  const bucket=d=>Math.floor((monday(d)-w0)/(7*DAY));
  const sent=Array(weeks).fill(0), iv=Array(weeks).fill(0);
  sentAt.forEach(d=>{ const b=bucket(d); if(b>=0&&b<weeks) sent[b]++ });
  ivAt.forEach(d=>{ const b=bucket(d); if(b>=0&&b<weeks) iv[b]++ });
  const mx=Math.max(1,...sent), tot=sent.reduce((a,b)=>a+b,0), ivs=iv.reduce((a,b)=>a+b,0);
  const avg=(tot/weeks).toFixed(1).replace(/\.0$/,"");
  const mid=new Date(+w0+Math.floor(weeks/2)*7*DAY);
  host.innerHTML='<div class="fn-ch"><h2>Momentum</h2><span>applications sent, by week</span></div>'+
    '<div class="fn-mhead"><div><span class="fn-big" data-to="'+sent[weeks-1]+'" data-delay="900">'+
    sent[weeks-1]+'</span><span style="font-size:13px;color:var(--t500)"> this week</span></div>'+
    '<div class="sub">'+avg+' a week on average<br><b>● '+ivs+' interview'+(ivs===1?"":"s")+'</b> in '+
    weeks+' week'+(weeks===1?"":"s")+'</div></div>'+
    '<div class="fn-weeks" role="img" aria-label="'+esc(sent.join(", "))+' sent per week, oldest first">'+
    sent.map((s,i)=>'<div class="fn-wk" title="Week of '+shortDate(new Date(+w0+i*7*DAY))+': '+s+
      ' sent'+(iv[i]?", "+iv[i]+" interview"+(iv[i]===1?"":"s"):"")+'"><span class="iv">'+
      ('<i style="animation-delay:'+(1+i*.035).toFixed(3)+'s"></i>').repeat(Math.min(iv[i],4))+'</span>'+
      '<span class="bar'+(i===weeks-1?" now":"")+'" style="height:'+Math.max(2,Math.round(s/mx*118))+
      'px;animation-delay:'+(1+i*.035).toFixed(3)+'s"></span></div>').join("")+'</div>'+
    '<div class="fn-wax"><span>'+shortDate(w0)+'</span><span>'+(weeks>6?MONTHS_LONG[mid.getMonth()]:"")+
    '</span><span>This week</span></div>';
  countUp(host,$("#fn-page").classList.contains("fx-in"));
}
const MONTHS_LONG=Array.from({length:12},(_,i)=>{ try{ return new Intl.DateTimeFormat(uiLocale(),{month:"long"})
  .format(new Date(2026,i,15)) }catch(e){ return String(i+1) } });

function fnNext(j){
  const days=d=>Math.max(0,Math.round((Date.now()-d)/DAY));
  if(j.status==="interviewing"){
    const at=interviewMoment(j);
    if(at&&at>new Date()) return t("Interview")+" "+fmtWhen(at,userTz());
    const d=fnEvent(j,"interviewing"); return d?"Interviewing since "+shortDate(d):"";
  }
  if(j.status==="offer"){ const d=fnEvent(j,"offer"); return d?"Offer since "+shortDate(d):"" }
  if(j.status!=="applied"){
    const last=(j.status_history||[]).slice(-1)[0];
    return last&&last.at?shortDate(last.at):"";
  }
  const d=fnEvent(j,"applied");
  if(!d) return "";
  const n=days(d); return n?"Sent "+n+" day"+(n===1?"":"s")+" ago":"Sent today";
}
function paintFunnelJobs(){
  const host=$("#fn-jobs"), hint=$("#fn-hint");
  if(!host) return;
  const row=j=>'<button class="fn-jrow" data-id="'+esc(j.id)+'">'+companyMark(j)+
    '<span class="fj-who"><span class="con">'+esc(j.company)+'</span><span class="fj-role">'+
    esc(j.title)+'</span></span><span class="fj-st"><span class="st"><span class="dot '+
    statusTone(j.status)+'"></span>'+esc(prettyStatus(j.status))+'</span><small>'+
    esc(fnNext(j))+'</small></span></button>';
  if(!S.fnode){
    hint.textContent="Hover a stage to trace its path · click to list them";
    const live=fnJobs().filter(j=>["applied","interviewing","offer"].includes(j.status))
      .sort((a,b)=>{ const r={offer:0,interviewing:1,applied:2};
        return r[a.status]-r[b.status]||String(b.updated_at).localeCompare(String(a.updated_at)) });
    host.innerHTML='<div class="fn-ch"><h2>Still in play</h2><span>'+live.length+'</span></div>'+
      (live.length?live.slice(0,5).map(row).join("")
        :'<p class="note">Nothing is waiting on an answer. Everything here has an outcome.</p>')+
      (live.length>5?'<div class="fn-jfoot"><button class="obtn" id="fn-open">All '+live.length+
        ' in Applications</button></div>':'');
  }else{
    const want=new Set((S.nodes&&S.nodes[S.fnode])||[]);
    const rows=fnJobs().filter(j=>want.has(j.status));
    const label=(S.labels&&S.labels[S.fnode])||S.fnode;
    hint.innerHTML="Showing <b>"+esc(label)+"</b> · click it again to clear";
    host.innerHTML='<div class="fn-ch"><span class="sw" style="background:'+skCol(S.fnode)+
      '"></span><h2>'+esc(label)+'</h2><span>'+rows.length+'</span><span class="grow"></span>'+
      '<button class="x" id="fn-clear" title="Back to still in play" aria-label="Clear">&#10005;</button></div>'+
      (rows.length?'<div class="fn-jlist">'+rows.map(row).join("")+'</div>'
        :'<p class="note">Nothing sits at this stage yet.</p>')+
      '<div class="fn-jfoot"><button class="obtn" id="fn-open">Show in Applications</button></div>';
    $("#fn-clear").onclick=()=>fnPick(S.fnode);
  }
  const open=$("#fn-open");
  if(open) open.onclick=()=>{
    S.jfilter=S.fnode?{kind:"node",value:S.fnode}:{kind:"all",value:""}; S.jsel=null;
    $("#jobq").value=""; setView("jobs");
  };
  host.querySelectorAll("[data-id]").forEach(b=>b.onclick=()=>{
    S.jfilter={kind:"all",value:""};
    setView("jobs"); selectJob(b.dataset.id);
  });
}

/* ---- underneath --------------------------------------------------------- */
/* Interview rate by where the posting was found: the number that changes
   what you do next week. Grouped by board, so "LinkedIn" and "linkedin" are
   one source. */
function fnSources(){
  const by=new Map();
  fnJobs().filter(j=>j.status!=="pending").forEach(j=>{
    const b=jobBoard(j), said=String(j.source||"").trim();
    const key=b?b.id:said.toLowerCase()||"-";
    const e=by.get(key)||{label:b?b.label:said||"Not set",board:b,said,sent:0,iv:0};
    e.sent++; if(IV_STATUSES.has(j.status)) e.iv++;
    by.set(key,e);
  });
  return [...by.values()].map(e=>({...e,rate:e.sent?e.iv/e.sent:0}))
    .sort((a,b)=>(a.label==="Not set")-(b.label==="Not set")||b.rate-a.rate||b.sent-a.sent);
}
function drawSources(){
  const host=$("#fn-sources"), src=fnSources(), known=src.filter(s=>s.label!=="Not set");
  let h='<div class="fn-ch"><h2>Where interviews come from</h2><span>interview rate by source</span></div>';
  if(known.length<2){
    host.innerHTML=h+'<p class="fn-empty">Set where you found each application (on the '+
      'application, under Found on) and this shows which sources turn into interviews.</p>';
    return;
  }
  const glyph=s=>{
    if(s.board) return boardMark(s.board);
    if(/referr/i.test(s.said)) return "♥";
    if(/recruit/i.test(s.said)) return "☎";
    if(/career|company|site/i.test(s.said)) return "↗";
    return esc(initials(s.label));
  };
  h+=src.slice(0,7).map((s,i)=>{
    const p=Math.round(s.rate*100), best=i<2&&s.iv>0&&s.sent>=3;
    return '<div class="fn-src"><span class="mk">'+glyph(s)+'</span><span class="sn">'+esc(s.label)+
      '</span><span class="track"><span class="fill'+(best?" best":"")+'" style="width:'+Math.max(p,1)+
      '%;animation-delay:'+(1.3+i*.07).toFixed(2)+'s"></span></span><span class="sv"><b>'+p+
      '%</b>'+s.iv+' of '+s.sent+'</span></div>';
  }).join("");
  host.innerHTML=h;
}

function fnReplyDays(){
  const days=[]; let never=0;
  fnJobs().forEach(j=>{
    if(j.status==="ghosted"){ never++; return }
    const h=j.status_history||[], i=h.findIndex(e=>e.status==="applied");
    if(i<0||!h[i+1]||String(h[i+1].status).startsWith("ghosted")) return;
    const d=Math.round((new Date(String(h[i+1].at).slice(0,19))-new Date(String(h[i].at).slice(0,19)))/DAY);
    if(!isNaN(d)) days.push(Math.max(0,d));
  });
  return {days:days.sort((a,b)=>a-b),never};
}
function drawReplies(){
  const host=$("#fn-reply"), {days,never}=fnReplyDays();
  let h='<div class="fn-ch"><h2>How long they take to answer</h2></div>';
  if(!days.length){
    host.innerHTML=h+'<p class="fn-empty">No answers yet. Each one shows here as a dot, by how '+
      'many days it took.</p>';
    return;
  }
  const med=S.funnel.totals.median_reply_days??days[days.length>>1];
  const cap=Math.max(21,Math.min(42,days[days.length-1]));
  const by=new Map(); days.forEach(d=>{ const k=Math.min(d,cap); by.set(k,(by.get(k)||0)+1) });
  const tall=Math.max(...by.values()), step=Math.min(11,Math.floor(92/Math.max(1,tall)));
  let dots="";
  by.forEach((n,d)=>{ for(let j=0;j<n;j++) dots+='<i style="left:'+(d/cap*100).toFixed(2)+'%;bottom:'+
    (j*step)+'px;animation-delay:'+(1.4+d*.03+j*.05).toFixed(2)+'s"></i>' });
  let ghost="";
  for(let j=0;j<Math.min(never,30);j++) ghost+='<i class="g" style="left:'+((j%3)*11)+'px;bottom:'+
    (Math.floor(j/3)*11)+'px;animation-delay:'+(1.9+j*.03).toFixed(2)+'s"></i>';
  const p90=days[Math.min(days.length-1,Math.floor(days.length*.9))];
  const ticks=[0,7,14,21,28,35,42].filter(d=>d<=cap);
  h+='<div style="display:flex;align-items:baseline;gap:8px;margin:0 0 14px"><span class="fn-big" data-to="'+
    med+'" data-delay="1200">'+med+'</span><span style="font-size:13px;color:var(--t500)">day'+
    (med===1?"":"s")+', median, for the '+days.length+' that answered</span></div>'+
    '<div style="display:flex;gap:18px;align-items:flex-end"><div style="flex:1;min-width:0">'+
    '<div class="fn-dots" role="img" aria-label="Days to a first answer: '+esc(days.join(", "))+'">'+dots+
    '<span class="med" style="left:'+(Math.min(med,cap)/cap*100).toFixed(2)+'%"></span></div>'+
    '<div class="fn-dax">'+ticks.map(d=>'<span>'+(d?d/7+" wk":"0")+'</span>').join("")+'</div></div>'+
    (never?'<div style="width:34px;flex:none"><div class="fn-dots" style="height:'+
      Math.min(100,Math.ceil(Math.min(never,30)/3)*11)+'px;margin:0" role="img" aria-label="'+never+
      ' never answered">'+ghost+'</div><div class="fn-dax" style="justify-content:center">never</div></div>':'')+
    '</div><p>'+(days.length<5?"A few more answers and this will say when to stop waiting."
      :p90<=21?"9 in 10 answers came within "+p90+" days. Past that, follow up once, or let it go."
      :"Answers can take a while here: 1 in 10 came after "+p90+" days.")+'</p>';
  host.className="fn-card fn-reply";
  host.innerHTML=h;
  countUp(host,$("#fn-page").classList.contains("fx-in"));
}

/* Up to three readings, each shown only when the numbers support it, so the
   panel says less on a thin month rather than inventing something. */
const FN_ICON={
  up:'<path d="M4 17l6-6 4 4 6-7"/><path d="M14 8h6v6"/>',
  leak:'<path d="M12 3v12M6 11l6 6 6-6M5 21h14"/>',
  lost:'<path d="M6 6l12 12M18 6L6 18"/>',
  wait:'<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>'};
function readings(){
  const t=S.funnel.totals, c=S.funnel.by_status||{}, out=[];
  const src=fnSources().filter(s=>s.label!=="Not set");
  const best=src.find(s=>s.sent>=3&&s.iv>0);
  const worst=[...src].reverse().find(s=>s.sent>=5&&s!==best);
  if(best&&worst&&best.rate>=worst.rate*2)
    out.push(["up","<b>"+esc(best.label)+" works best.</b> "+best.iv+" of "+best.sent+
      " became interviews; "+esc(worst.label)+", "+worst.iv+" of "+worst.sent+"."]);
  const st=fnStages();
  let w=-1, low=101;
  st.forEach(([,n],i)=>{ if(!i) return; const d=st[i-1][1];
    if(d>=10){ const r=n/d*100; if(r<low){ low=r; w=i } } });
  if(w>0){
    const [,n]=st[w], d=st[w-1][1];
    const say=[null,
      ["Most applications go unanswered.",n+" of "+d+" sent have heard back."],
      ["Replies are mostly no.",n+" of "+d+" answers were an interview."],
      ["The offer stage leaks most.",n+" of "+d+" interviews became offers"+
        ((c.rejected_interviewing||0)?"; "+c.rejected_interviewing+" were turned down after interviewing.":".")],
      ["Offers are not turning into a yes.",n+" of "+d+" accepted."]][w];
    out.push(["leak","<b>"+say[0]+"</b> "+say[1]]);
  }
  const early=c.rejected||0, late=c.rejected_interviewing||0;
  if(early) out.push(["lost","<b>"+early+" rejection"+(early===1?"":"s")+" came before any interview"+
    (late?", "+late+" after":"")+".</b> Only the first kind points at the CV."]);
  const ghost=(c.ghosted||0)+(c.ghosted_interviewing||0);
  if(ghost) out.push(["wait","<b>"+ghost+" never answered,</b> "+Math.round(ghost/Math.max(1,t.applied)*100)+
    "% of everything sent."]);
  if(!out.length) out.push(["wait","<b>Not enough has happened yet</b> to read anything into."]);
  return out.slice(0,3);
}
function drawRates(){
  $("#fn-rates").innerHTML='<div class="fn-ch"><h2>What it says</h2></div>'+readings().map(([k,txt])=>
    '<div class="fn-ins"><span class="ic '+k+'"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" '+
    'stroke="currentColor" stroke-width="2.2" aria-hidden="true">'+FN_ICON[k]+'</svg></span><p>'+txt+
    '</p></div>').join("");
}
$("#ex-csv").onclick=()=>window.open("/api/jobs/export?format=csv"+tok());
$("#ex-json").onclick=()=>window.open("/api/jobs/export?format=json"+tok());

/* Re-lay on resize. Debounced, because a sankey layout on every pixel of a
   window drag is wasted work. */
let sizeTimer=null;
window.addEventListener("resize",()=>{
  if(S.view==="funnel"&&S.funnel){
    clearTimeout(sizeTimer); sizeTimer=setTimeout(()=>drawSankey("still",false),140);
  }
});

/* =========================================================================
   Calendar

   Interviews, follow-ups and what happened, by date. Nothing here is new data:
   the interview time and its zone, the follow-up date, and the status history
   the funnel is drawn from. Days are days where you are (userTz).
   ========================================================================= */
S.calView="overview"; S.calAnchor=null; S.calTick=null; S.calPop=null;
const DEAD_ST=new Set(["accepted","refused","rejected","ghosted","rejected_interviewing","ghosted_interviewing"]);
const LIVE_ST=new Set(["applied","interviewing","offer"]);
/* A moment as its day where you are, "2026-09-25". */
function dayIn(date,tz){
  const p=new Intl.DateTimeFormat("en-CA",{timeZone:tz||userTz(),year:"numeric",month:"2-digit",day:"2-digit"})
    .formatToParts(date);
  const g=k=>p.find(x=>x.type===k).value;
  return g("year")+"-"+g("month")+"-"+g("day");
}
/* The wall-clock time a moment shows in a zone, "2026-09-25T10:00". */
function wallIn(date,tz){
  const p=new Intl.DateTimeFormat("en-CA",{timeZone:tz,hourCycle:"h23",year:"numeric",month:"2-digit",day:"2-digit",
    hour:"2-digit",minute:"2-digit"}).formatToParts(date);
  const g=k=>p.find(x=>x.type===k).value;
  return g("year")+"-"+g("month")+"-"+g("day")+"T"+g("hour")+":"+g("minute");
}
const hmIn=(date,tz)=>wallIn(date,tz).slice(11);
const todayKey=()=>dayIn(new Date());
const keyDate=k=>{ const [y,m,d]=k.split("-").map(Number); return new Date(Date.UTC(y,m-1,d,12)) };
const addDays=(k,n)=>{ const d=keyDate(k); d.setUTCDate(d.getUTCDate()+n); return d.toISOString().slice(0,10) };
const dayDiff=(a,b)=>Math.round((keyDate(b)-keyDate(a))/DAY);
const monday=k=>{ const w=(keyDate(k).getUTCDay()+6)%7; return addDays(k,-w) };
const fmtKey=(k,o)=>{ try{ return new Intl.DateTimeFormat(uiLocale(),Object.assign({timeZone:"UTC"},o)).format(keyDate(k)) }
  catch(e){ return k } };
const otherTz=j=>{ const at=interviewMoment(j); return j.interview_tz&&at&&tzOffset(j.interview_tz,at)!==tzOffset(userTz(),at) };
const GLOBE='<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '+
  'aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z"/></svg>';

/* Every dated thing, in one list: {kind, day, job, at?, late?}. */
/* ---- Next up, above the applications list ------------------------------ */
/* The home screen's one look ahead: the next interview with a countdown, the
   follow-ups that are late, and this week in seven days. It shows on the
   whole list only, and only when one of those has something in it. */
function drawNextUp(){
  const el=$("#nextup"); if(!el) return;
  clearInterval(S.nuTick);
  const f=S.jfilter||{kind:"all"}, q=($("#jobq").value||"").trim();
  if(!S.jready||f.kind!=="all"||q||S.jsel){ el.hidden=true; return }
  const now=new Date(), today=todayKey(), ev=calEvents();
  const next=ev.filter(e=>e.kind==="iv"&&e.at>now).sort((a,b)=>a.at-b.at)[0];
  const fus=ev.filter(e=>e.kind==="fu"&&e.day<=today).sort((a,b)=>a.day.localeCompare(b.day));
  const late=fus.filter(e=>e.late), dueToday=fus.filter(e=>!e.late);
  if(!next&&!fus.length){ el.hidden=true; return }
  const soon=next&&next.at-now<3*36e5;
  const names=list=>{ const n=list.map(e=>e.job.company);
    if(n.length>3) return n.slice(0,3).join(", ")+" +"+(n.length-3);
    try{ return new Intl.ListFormat(uiLocale(),{type:"conjunction"}).format(n) }catch(e){ return n.join(", ") } };
  const ivPart=j=>'<div class="nu-iv">'+companyMark(j)+'<div class="nu-tx">'+
      '<span class="ey"><i></i>'+t(soon?"Interview today":"Next interview")+'</span>'+
      '<span class="who">'+esc(j.company)+' <span>· '+esc(j.title)+'</span></span>'+
      '<span class="sub">'+esc(interviewLine(j))+'</span></div>';
  let html="", cols="";
  if(soon){
    const j=next.job, cvName=j.cv_path?j.cv_path.split("/").pop().replace(/\.(ya?ml|md)$/,""):null;
    const words=(String(j.description||"").match(/\S+/g)||[]).length;
    const li=(ok,yes,no)=>'<li><i class="'+(ok?"ok":"no")+'">'+(ok?"✓":"")+'</i>'+esc(ok?yes:no)+'</li>';
    html=ivPart(j)+'<div class="left"><small>'+t("Starts in")+'</small><b id="nu-left"></b></div>'+
      '<ul class="ready" aria-label="'+esc(t("Ready for it"))+'">'+
        li(cvName,t("CV tailored")+(cvName?" · "+cvName:""),t("No tailored CV yet"))+
        li(words,t("Posting saved"),t("Posting not saved"))+
        li(j.notes,t("Notes written"),t("Notes for this round"))+'</ul>'+
      '<button class="pbtn" data-nu-open="'+esc(j.id)+'">'+t("Prepare")+'</button></div>';
    el.className="nu soon"; el.style.gridTemplateColumns="minmax(0,1fr)";
  }else{
    if(next){
      const j=next.job;
      html+=ivPart(j)+'<div class="end"><div class="cd" id="nu-cd"></div><div class="acts">'+
        '<button class="obtn" data-nu-ics="'+esc(j.id)+'">'+t("Add to calendar")+'</button>'+
        '<button class="obtn dark" data-nu-open="'+esc(j.id)+'">'+t("Prepare")+'</button></div></div></div>';
      cols+="minmax(0,1.35fr) ";
    }
    if(fus.length){
      const lead=late.length?late:dueToday, oldest=late.length?dayDiff(late[0].day,today):0;
      const head=late.length?t(late.length===1?"1 follow-up overdue":"{n} follow-ups overdue",{n:late.length})
        :t(dueToday.length===1?"1 follow-up due today":"{n} follow-ups due today",{n:dueToday.length});
      const sub=late.length?(oldest===1?t("The oldest is 1 day late"):t("The oldest is {n} days late",{n:oldest}))
        +(dueToday.length?" · "+t("{n} more due today",{n:dueToday.length}):"")
        :(next?"":t("No interview booked"));
      html+='<div class="nu-fu"><span class="stack">'+lead.slice(0,3).map(e=>companyMark(e.job)).join("")+'</span>'+
        '<div class="nu-tx"><span class="ey '+(late.length?"late":"due")+'"><i></i>'+esc(head)+'</span>'+
        '<span class="who" title="'+esc(lead.map(e=>e.job.company).join(", "))+'">'+esc(names(lead))+'</span>'+
        '<span class="sub">'+(sub?esc(sub)+' · ':'')+'<button class="lnk" data-nu-show>'+t("Show them")+' →</button></span></div></div>';
      cols+="minmax(0,1fr) ";
    }
    if(next){
      const mon=monday(today);
      const days=Array.from({length:7},(_,i)=>{
        const k=addDays(mon,i), iv=ev.some(e=>e.kind==="iv"&&e.day===k),
          fu=ev.filter(e=>e.kind==="fu"&&e.day===k), lt=fu.some(e=>e.late);
        const tip=[iv?t("Interview")+" · "+ev.filter(e=>e.kind==="iv"&&e.day===k).map(e=>e.job.company).join(", "):"",
          fu.length?t("Follow up")+" · "+fu.map(e=>e.job.company).join(", "):""].filter(Boolean).join("\n");
        return '<button data-nu-day="'+k+'"'+(k===today?' class="today"':'')+' title="'+esc(tip||fmtKey(k,{weekday:"long",day:"numeric",month:"long"}))+'">'+
          '<small>'+esc(fmtKey(k,{weekday:"short"}))+'</small>'+Number(k.slice(8))+
          '<span class="mk">'+(iv?'<span class="d"></span>':'')+(fu.length?'<span class="r'+(lt?" late":"")+'"></span>':'')+'</span></button>';
      }).join("");
      html+='<div class="nu-wk"><div class="hd">'+t("This week")+'<button data-nu-cal>'+t("Open calendar")+' →</button></div>'+
        '<div class="days">'+days+'</div></div>';
      cols+="300px";
    }
    el.className="nu"+(next?"":" slim");
    el.style.gridTemplateColumns=cols.trim();
  }
  const wasHidden=el.hidden;
  el.innerHTML=html; el.hidden=false;
  el.style.animation=wasHidden?"":"none";
  $$("#nextup [data-nu-open]").forEach(b=>b.onclick=()=>selectJob(b.dataset.nuOpen));
  $$("#nextup [data-nu-ics]").forEach(b=>b.onclick=()=>window.open("/api/calendar.ics?id="+encodeURIComponent(b.dataset.nuIcs)+tok()));
  const sh=$("#nextup [data-nu-show]"); if(sh) sh.onclick=()=>{ S.jfilter={kind:"alert",value:"followup_due"}; S.fnode=null; drawJobs() };
  const cal=$("#nextup [data-nu-cal]"); if(cal) cal.onclick=()=>{ S.calView="overview"; setView("cal") };
  $$("#nextup [data-nu-day]").forEach(b=>b.onclick=()=>{ S.calView="week"; S.calAnchor=b.dataset.nuDay; setView("cal") });
  if(next){
    const p=n=>String(n).padStart(2,"0");
    /* Units short enough to sit beside the digits; French counts days in "j". */
    const U={d:UI_LANG==="fr"?"j":"d", m:UI_LANG==="en"?"m":"min"};
    const tick=()=>{
      const cd=$("#nu-cd"), lf=$("#nu-left");
      if(!cd&&!lf){ clearInterval(S.nuTick); return }
      const left=Math.max(0,next.at-new Date());
      const d=Math.floor(left/864e5), h=Math.floor(left/36e5)%24, m=Math.floor(left/6e4)%60, s=Math.floor(left/1e3)%60;
      if(cd) cd.innerHTML=(d?'<b>'+d+'</b><u>'+U.d+'</u>':'')+'<b>'+p(h)+'</b><u>h</u><b>'+p(m)+'</b><u>'+
        U.m+'</u><b class="s">'+p(s)+'</b><u>s</u>';
      if(lf) lf.textContent=(h?h+" h ":"")+m+" min";
      if(left<=0) drawNextUp();
    };
    tick(); S.nuTick=setInterval(tick,1000);
  }
}

function calEvents(){
  const out=[], today=todayKey();
  (S.jobs||[]).forEach(j=>{
    const at=interviewMoment(j);
    if(at) out.push({kind:"iv",day:dayIn(at),at,job:j});
    if(j.followup_date&&!DEAD_ST.has(j.status)){
      const d=String(j.followup_date).slice(0,10);
      out.push({kind:"fu",day:d,job:j,late:d<today});
    }
    let seenIv=false;
    (j.status_history||[]).forEach((h,i)=>{
      if(!h.at) return;
      const d=dayIn(new Date(String(h.at).slice(0,19)));
      const st=h.status;
      if(st==="applied") out.push({kind:"sent",day:d,job:j});
      else if(st==="interviewing"&&!seenIv){ seenIv=true; out.push({kind:"rp",day:d,job:j}) }
      else if(st==="offer") out.push({kind:"of",day:d,job:j});
      else if(DEAD_ST.has(st)&&st!=="accepted") out.push({kind:"closed",day:d,job:j});
      else if(st==="accepted") out.push({kind:"of",day:d,job:j});
    });
  });
  return out;
}

function openCalendar(){
  if(!S.calAnchor) S.calAnchor=todayKey();
  const go=()=>{ drawCalendar(true) };
  if(!S.jready) loadJobs(true).then(go); else go();
}
function drawCalendar(animate){
  const page=$("#cal-page"), v=S.calView;
  $$("#cal-views button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.cv===v)));
  $("#cal-nav").hidden=v==="overview";
  const ev=calEvents(), today=todayKey();
  const wkEnd=addDays(monday(today),6);
  const ivs=ev.filter(e=>e.kind==="iv"&&e.day>=today&&e.day<=wkEnd).length;
  const late=ev.filter(e=>e.kind==="fu"&&e.late).length;
  $("#cal-sub").textContent=[ivs?t(ivs===1?"1 interview this week":"{n} interviews this week",{n:ivs}):"",
    late?t(late===1?"1 follow-up overdue":"{n} follow-ups overdue",{n:late}):""].filter(Boolean).join(" · ");
  clearInterval(S.calTick);
  if(animate&&!reduceMotion()){ page.classList.remove("cal-in"); void page.offsetWidth; page.classList.add("cal-in");
    setTimeout(()=>page.classList.remove("cal-in"),3200) }
  if(v==="overview") drawCalOverview(ev);
  else if(v==="month") drawCalMonth(ev);
  else drawCalWeek(ev);
}
$$("#cal-views button").forEach(b=>b.onclick=()=>{ S.calView=b.dataset.cv; S.calPop=null; drawCalendar(true) });
$$("#cal-nav [data-step]").forEach(b=>b.onclick=()=>{
  const n=+b.dataset.step;
  if(S.calView==="week") S.calAnchor=addDays(S.calAnchor,7*n);
  else{ const d=keyDate(S.calAnchor); d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth()+n); S.calAnchor=d.toISOString().slice(0,10) }
  S.calPop=null; drawCalendar(false);
});
$("#cal-today").onclick=()=>{ S.calAnchor=todayKey(); S.calPop=null; drawCalendar(false) };
$("#cal-ics").onclick=()=>window.open("/api/calendar.ics"+tok());
const openJob=id=>{ S.jfilter={kind:"all",value:""}; setView("jobs"); selectJob(id) };

/* ---- overview ------------------------------------------------------------ */
function clockSVG(date,tz,label,delay){
  const [h,m]=hmIn(date,tz).split(":").map(Number);
  const ha=((h%12)+m/60)*30, ma=m*6;
  const ticks=Array.from({length:12},(_,k)=>'<line class="tk'+(k%3?"":" big")+'" x1="50" y1="8" x2="50" y2="'+
    (k%3?11:14)+'" stroke-width="'+(k%3?1.2:2)+'" transform="rotate('+k*30+' 50 50)"/>').join("");
  return '<div class="cal-clock"><svg width="92" height="92" viewBox="0 0 100 100" aria-hidden="true">'+
    '<circle class="face" cx="50" cy="50" r="46" stroke-width="2"/>'+ticks+
    '<line class="cal-hand hh" x1="50" y1="50" x2="50" y2="27" stroke-width="4" stroke-linecap="round" style="--a:'+ha+
      'deg;animation-delay:'+delay+'s"/>'+
    '<line class="cal-hand mh" x1="50" y1="50" x2="50" y2="15" stroke-width="2.5" stroke-linecap="round" style="--a:'+ma+
      'deg;animation-delay:'+(delay+.1)+'s"/><circle class="pin" cx="50" cy="50" r="3.5"/></svg>'+
    '<b>'+hmIn(date,tz)+'</b><span>'+esc(label)+'</span></div>';
}
function drawCalOverview(ev){
  const now=new Date(), today=todayKey();
  const next=ev.filter(e=>e.kind==="iv"&&e.at>now).sort((a,b)=>a.at-b.at)[0];
  let hero;
  if(next){
    const j=next.job, mine=userTz(), theirs=j.interview_tz, two=otherTz(j);
    const words=(String(j.description||"").match(/\S+/g)||[]).length;
    const diffH=two?Math.round((tzOffset(mine,next.at)-tzOffset(theirs,next.at))/60):0;
    const cvName=j.cv_path?j.cv_path.split("/").pop().replace(/\.(ya?ml|md)$/,""):null;
    hero='<section class="cal-hero cal-r"><div>'+
      '<div class="cal-next"><span class="tag"><i></i>'+t("NEXT UP")+'</span><span>'+t("Interview")+'</span></div>'+
      '<div class="cal-who">'+companyMark(j)+'<div><b>'+esc(j.company)+'</b><span>'+esc(j.title)+
        (j.location?' · '+esc(j.location):'')+'</span></div></div>'+
      '<div class="cal-cd" id="cal-cd"></div>'+
      '<div class="cal-checks">'+
        '<span class="cal-check"><i class="'+(cvName?"ok":"no")+'">'+(cvName?"✓":"")+'</i>'+
          (cvName?t("CV tailored")+' · '+esc(cvName):t("No tailored CV yet"))+'</span>'+
        '<span class="cal-check"><i class="'+(words?"ok":"no")+'">'+(words?"✓":"")+'</i>'+
          (words?t("Posting saved"):t("Posting not saved"))+'</span>'+
        '<span class="cal-check"><i class="'+(j.notes?"ok":"no")+'">'+(j.notes?"✓":"")+'</i>'+
          (j.notes?t("Notes written"):t("Notes for this round"))+'</span></div>'+
      '<div class="cal-acts"><button class="obtn dark" data-open-job="'+esc(j.id)+'">'+t("Prepare")+'</button>'+
        '<button class="obtn" data-ics="'+esc(j.id)+'">'+t("Add to my calendar")+'</button></div></div>'+
      '<div class="cal-clocks"><div class="row">'+clockSVG(next.at,mine,t("Your time")+" · "+tzCity(mine),.5)+
        (two?clockSVG(next.at,theirs,t("In {city}",{city:tzCity(theirs)}),.7):'')+'</div>'+
        (two?'<small>'+esc(diffH>0?t("{city} is {n} h behind you",{city:tzCity(theirs),n:diffH})
          :t("{city} is {n} h ahead of you",{city:tzCity(theirs),n:-diffH}))+'</small>':'')+'</div></section>';
  }else{
    const fu=ev.filter(e=>e.kind==="fu"&&!e.late).sort((a,b)=>a.day.localeCompare(b.day))[0];
    hero='<section class="cal-hero cal-r"><div class="cal-empty"><div class="cal-next"><span class="tag"><i></i>'+
      t("NEXT UP")+'</span></div><b>'+t("No interview scheduled")+'</b><p>'+
      (fu?esc(t("Next: follow up with {co}, {day}.",{co:fu.job.company,day:fmtKey(fu.day,{weekday:"long",day:"numeric",month:"long"})}))
        :t("When an application gets an interview time, it counts down here, in your time and theirs."))+
      '</p></div><div></div></section>';
  }
  $("#cal-body").innerHTML='<div class="cal-top">'+hero+calMini(ev)+'</div>'+calJourneys(ev);
  wireCal();
  if(next){
    const tick=()=>{
      const el=$("#cal-cd"); if(!el){ clearInterval(S.calTick); return }
      const left=Math.max(0,next.at-new Date());
      const p=n=>String(n).padStart(2,"0");
      const d=Math.floor(left/864e5), h=Math.floor(left/36e5)%24, m=Math.floor(left/6e4)%60, s=Math.floor(left/1e3)%60;
      el.innerHTML=(d?'<div><div class="n">'+d+'</div><div class="u">'+t(d===1?"day":"days")+'</div></div>':'')+
        '<div><div class="n">'+p(h)+'</div><div class="u">'+t("hours")+'</div></div>'+
        '<div><div class="n">'+p(m)+'</div><div class="u">'+t("min")+'</div></div>'+
        '<div class="s"><div class="n">'+p(s)+'</div><div class="u">'+t("sec")+'</div></div>'+
        '<div class="when"><b>'+esc(fmtKey(next.day,{weekday:"long",day:"numeric",month:"long"}))+'</b><br>'+
        esc(hmIn(next.at,userTz()))+' '+t("your time")+'</div>';
    };
    tick(); S.calTick=setInterval(tick,1000);
  }
}
function calMini(ev){
  const today=todayKey(), first=today.slice(0,8)+"01", start=monday(first);
  const month=first.slice(0,7);
  const busy={}, iv=new Set();
  ev.forEach(e=>{ busy[e.day]=(busy[e.day]||0)+1; if(e.kind==="iv") iv.add(e.day) });
  let cells="";
  for(let i=0;i<42;i++){
    const k=addDays(start,i); if(i>=35&&k.slice(0,7)!==month) break;
    const inm=k.slice(0,7)===month, n=busy[k]||0, lv=!inm?"":n>=4?" l3":n>=2?" l2":n?" l1":"";
    cells+='<button class="cal-md'+(inm?"":" out")+lv+(k===today?" today":"")+'" data-day="'+k+'" style="animation-delay:'+
      (.4+i*.018).toFixed(3)+'s"'+(inm?'':' tabindex="-1"')+' aria-label="'+esc(fmtKey(k,{day:"numeric",month:"long"}))+
      (iv.has(k)?', '+t("Interview"):'')+'">'+Number(k.slice(8))+(inm&&iv.has(k)?'<i></i>':'')+'</button>';
  }
  const dows=Array.from({length:7},(_,i)=>'<span class="dw">'+esc(fmtKey(addDays("2026-09-21",i),{weekday:"narrow"}))+'</span>').join("");
  const acc=["var(--bd-inner)","color-mix(in srgb,var(--acc) 22%,var(--field))","color-mix(in srgb,var(--acc) 45%,var(--field))",
    "color-mix(in srgb,var(--acc) 75%,var(--field))"];
  return '<section class="cal-card cal-mini cal-r" style="animation-delay:.1s"><div class="cal-ch"><h2>'+
    esc(fmtKey(first,{month:"long"}))+'</h2><span>'+t("how busy each day was")+'</span></div>'+
    '<div class="cal-mgrid">'+dows+cells+'</div><div class="cal-legend"><span style="display:flex;align-items:center;gap:4px">'+
    t("Quiet")+'<span class="ramp">'+acc.map(c=>'<i style="background:'+c+'"></i>').join("")+'</span>'+t("Busy")+
    '</span><span style="display:flex;align-items:center;gap:6px"><i class="dia"></i>'+t("Interview")+'</span></div></section>';
}
/* Every live application as a lane across eight weeks, three of them ahead: how long it has been
   waiting, when it moved to interviews or an offer, and what is ahead. */
function calJourneys(ev){
  const today=todayKey(), start=addDays(monday(today),-35), days=56, end=addDays(start,days-1);
  const pct=k=>Math.max(0,Math.min(100,(dayDiff(start,k)+.5)/days*100));
  /* Labels near the right edge go to the left of their mark, so none runs
     off the card. */
  const labAt=(p,gap)=>p>80?"right:calc("+(100-p)+"% + "+gap+"px)":"left:calc("+p+"% + "+gap+"px)";
  const recent=j=>{ const h=(j.status_history||[]).slice(-1)[0]; return h&&dayIn(new Date(String(h.at).slice(0,19)))>=start };
  const rank=j=>{ const at=interviewMoment(j); if(at&&at>new Date()) return 0;
    return {offer:1,interviewing:2,applied:3}[j.status]||4 };
  const jobs=(S.jobs||[]).filter(j=>LIVE_ST.has(j.status)||(DEAD_ST.has(j.status)&&recent(j)))
    .sort((a,b)=>rank(a)-rank(b)||(interviewMoment(a)||0)-(interviewMoment(b)||0)||
      String(b.updated_at).localeCompare(String(a.updated_at)));
  const shown=jobs.slice(0,14);
  let ticks="";
  for(let w=0;w<=8;w++){ const k=addDays(start,7*w); if(Math.abs(dayDiff(k,today))<4||dayDiff(start,k)>=days) continue;
    ticks+='<span class="jr-tick" style="left:'+pct(k)+'%">'+esc(fmtKey(k,{day:"numeric",month:"short"}))+'</span>' }
  let wk=""; for(let i=5;i<days;i+=7) wk+='<span class="jr-wkend" style="left:'+(i/days*100)+'%;width:'+(2/days*100)+'%"></span>';
  const lanes=shown.map((j,i)=>{
    const h=(j.status_history||[]).filter(x=>x.at).map(x=>({st:x.status,day:dayIn(new Date(String(x.at).slice(0,19)))}));
    const stage=st=>st==="applied"?"wait":st==="interviewing"?"live":(st==="offer"||st==="accepted")?"offer":null;
    let segs="", marks="";
    h.forEach((x,n)=>{
      const sg=stage(x.st); if(!sg) return;
      const to=n+1<h.length?h[n+1].day:(LIVE_ST.has(j.status)?today:x.day);
      if(to<start||x.day>end) return;
      const a=pct(x.day<start?start:x.day), b=pct(to>end?end:to)+(to===today?.5/days*100:0);
      segs+='<span class="jr-seg '+sg+'" style="left:'+a+'%;width:'+Math.max(.6,b-a)+'%;animation-delay:'+
        (.5+i*.06+n*.1).toFixed(2)+'s"></span>';
    });
    const first=h.find(x=>x.st==="interviewing"); if(first&&first.day>=start) marks+='<span class="jr-dot rp" style="left:'+pct(first.day)+'%"></span>';
    const of=h.find(x=>x.st==="offer"); if(of&&of.day>=start) marks+='<span class="jr-dot of" style="left:'+pct(of.day)+'%"></span>';
    const aheadKeys=[];
    const at=interviewMoment(j);
    if(at&&at>new Date()&&dayIn(at)<=end){
      const k=dayIn(at); aheadKeys.push(k);
      const lab=fmtKey(k,{weekday:"short"})+" "+hmIn(at,userTz())+(otherTz(j)?" · "+hmIn(at,j.interview_tz)+" "+tzCity(j.interview_tz):"");
      marks+='<span class="jr-iv" style="left:'+pct(k)+'%;animation-delay:'+(1.5+i*.07).toFixed(2)+'s"></span>'+
        '<span class="jr-lab iv" style="'+labAt(pct(k),14)+';animation-delay:'+(1.7+i*.07).toFixed(2)+'s">'+esc(lab)+'</span>';
    }
    if(j.followup_date&&!DEAD_ST.has(j.status)){
      const k=String(j.followup_date).slice(0,10), late=k<today;
      if(k>=start&&k<=end&&!aheadKeys.length){
        if(!late) aheadKeys.push(k);
        marks+='<span class="jr-ring'+(late?" late":"")+'" style="left:'+pct(k)+'%;animation-delay:'+(1.5+i*.07).toFixed(2)+'s"></span>'+
          '<span class="jr-lab'+(late?" late":"")+'" style="'+labAt(pct(k),12)+';animation-delay:'+(1.7+i*.07).toFixed(2)+'s">'+
          esc(late?t("Follow-up overdue"):k===addDays(today,1)?t("Follow up tomorrow"):t("Follow up")+" · "+fmtKey(k,{day:"numeric",month:"short"}))+'</span>';
      }
    }
    const closed=DEAD_ST.has(j.status)&&j.status!=="accepted";
    if(closed){ const k=h.slice(-1)[0].day; marks+='<span class="jr-x" style="left:'+pct(k)+'%">×</span>'+
      '<span class="jr-lab x" style="left:calc('+pct(k)+'% + 10px)">'+esc(prettyStatus(j.status))+'</span>' }
    if(aheadKeys.length&&!closed){ const far=aheadKeys.sort().slice(-1)[0];
      segs+='<span class="jr-dash" style="left:'+(pct(today)+.5/days*100)+'%;width:'+Math.max(0,pct(far)-pct(today)-.5/days*100)+'%"></span>' }
    return '<div class="jr-lane'+(closed?" closed":"")+' cal-r" data-open-job="'+esc(j.id)+'" style="animation-delay:'+(.3+i*.05).toFixed(2)+
      's"><div class="jr-who">'+companyMark(j)+'<span style="min-width:0"><b>'+esc(j.company)+'</b><small>'+esc(j.title)+
      (j.location?' · '+esc(j.location):'')+'</small></span></div><div class="jr-track">'+segs+marks+'</div></div>';
  }).join("");
  return '<section class="cal-card cal-jr cal-r" style="--jr-lx:236px;animation-delay:.2s"><div class="cal-ch" style="align-items:center">'+
    '<h2 style="font-size:15px">'+t("Journeys")+'</h2><span>'+t("each application from sent to where it is now, and what is ahead")+'</span>'+
    '<div class="jr-legend"><span><i class="bar" style="background:color-mix(in srgb,var(--fn-wait) 40%,transparent)"></i>'+t("Waiting")+'</span>'+
    '<span><i class="bar" style="background:color-mix(in srgb,var(--fn-positive) 55%,transparent)"></i>'+t("Interviewing")+'</span>'+
    '<span><i class="bar" style="background:color-mix(in srgb,var(--fn-offer) 55%,transparent)"></i>'+t("Offer")+'</span>'+
    '<span><i style="width:9px;height:9px;border-radius:2px;background:var(--fn-positive);transform:rotate(45deg)"></i>'+t("Interview")+'</span>'+
    '<span><i style="width:10px;height:10px;border-radius:50%;box-shadow:inset 0 0 0 2px var(--fn-wait)"></i>'+t("Follow-up")+'</span></div></div>'+
    (shown.length?'<div class="jr-axis">'+ticks+'</div><div class="jr-body"><div class="jr-bg">'+wk+
      '<span class="jr-now" data-label="'+esc(t("Today"))+'" style="left:'+pct(today)+'%"></span></div>'+lanes+'</div>'+
      (jobs.length>shown.length?'<div class="jr-more">'+esc(t("{n} more in Applications",{n:jobs.length-shown.length}))+'</div>':'')
      :'<p class="cal-none">'+t("Nothing in play in these seven weeks.")+'</p>')+'</section>';
}

/* ---- month --------------------------------------------------------------- */
function calChip(e){
  const j=e.job, co=esc(j.company);
  const drag=(e.kind==="iv"||e.kind==="fu")?' draggable="true" data-drag="'+e.kind+':'+esc(j.id)+'"':'';
  if(e.kind==="iv") return '<button class="cal-chip iv" data-open-job="'+esc(j.id)+'"'+drag+' title="'+esc(interviewLine(j))+
    '"><i class="pt"></i><span>'+hmIn(e.at,userTz())+' '+co+'</span>'+(otherTz(j)?'<i class="gl">'+GLOBE+'</i>':'')+'</button>';
  if(e.kind==="fu") return '<button class="cal-chip fu'+(e.late?" late":"")+'" data-open-job="'+esc(j.id)+'"'+drag+'><span>'+
    esc(e.late?t("Overdue"):t("Follow up"))+' · '+co+'</span></button>';
  if(e.kind==="of") return '<button class="cal-chip of" data-open-job="'+esc(j.id)+'"><span>'+t("Offer")+' · '+co+'</span></button>';
  if(e.kind==="rp") return '<button class="cal-chip rp" data-open-job="'+esc(j.id)+'"><span>↗ '+co+' · '+t("interviews")+'</span></button>';
  return "";
}
function drawCalMonth(ev){
  const today=todayKey(), first=S.calAnchor.slice(0,8)+"01", month=first.slice(0,7), start=monday(first);
  $("#cal-title").textContent=fmtKey(first,{month:"long",year:"numeric"});
  const by={}; ev.forEach(e=>(by[e.day]=by[e.day]||[]).push(e));
  const order={iv:0,fu:1,of:2,rp:3};
  let cells="";
  const weeks=dayDiff(start,addDays(first.slice(0,7)+"-28",4))>=35?6:5;
  for(let i=0;i<weeks*7;i++){
    const k=addDays(start,i), list=(by[k]||[]), inm=k.slice(0,7)===month;
    const chips=list.filter(e=>order[e.kind]!=null).sort((a,b)=>order[a.kind]-order[b.kind]||(a.at||0)-(b.at||0));
    const sent=list.filter(e=>e.kind==="sent").length, closed=list.filter(e=>e.kind==="closed").length;
    const dnum=Number(k.slice(8));
    cells+='<div class="cal-cell'+(inm?"":" out")+(((i%7)>=5)?" wkend":"")+(k===today?" today":"")+'" data-drop="'+k+'">'+
      '<div class="d"><span>'+dnum+(dnum===1?" "+esc(fmtKey(k,{month:"short"})):"")+'</span></div>'+
      chips.slice(0,3).map(calChip).join("")+(chips.length>3?'<small style="font-size:11px;color:var(--t500)">+'+(chips.length-3)+'</small>':'')+
      ((sent||closed)?'<div class="act">'+(sent?'<span><i style="background:var(--fn-wait)"></i>'+esc(t("{n} sent",{n:sent}))+'</span>':'')+
        (closed?'<span><i style="background:var(--bd-field)"></i>'+esc(t("{n} closed",{n:closed}))+'</span>':'')+'</div>':'')+'</div>';
  }
  const dows=Array.from({length:7},(_,i)=>'<div class="dw">'+esc(fmtKey(addDays("2026-09-21",i),{weekday:"short"}))+'</div>').join("");
  $("#cal-body").innerHTML='<div class="cal-month"><section class="cal-card cal-grid cal-r" style="grid-template-rows:auto repeat('+
    weeks+',minmax(112px,1fr))">'+dows+cells+'</section>'+calComing(ev)+'</div>';
  wireCal(); wireDrag();
}
function calComing(ev){
  const today=todayKey(), end=addDays(today,7), now=new Date();
  const late=ev.filter(e=>e.kind==="fu"&&e.late).sort((a,b)=>a.day.localeCompare(b.day));
  const up=ev.filter(e=>(e.kind==="iv"&&e.at>=now&&e.day<=end)||(e.kind==="fu"&&!e.late&&e.day<=end)||
      (e.kind==="rp"&&e.day===today)||(e.kind==="of"&&e.day===today))
    .sort((a,b)=>a.day.localeCompare(b.day)||(a.at||0)-(b.at||0));
  const col={iv:"var(--fn-positive)",fu:"var(--fn-wait)",rp:"var(--fn-offer)",of:"var(--fn-offer)"};
  const when=e=>(e.day===today?t("Today")+" · ":"")+fmtKey(e.day,{weekday:"short",day:"numeric"})+
    (e.kind==="iv"?" · "+hmIn(e.at,userTz())+(otherTz(e.job)?" "+t("your time"):""):"");
  const what=e=>e.kind==="iv"?t("Interview")+" · "+e.job.company:e.kind==="fu"?t("Follow up with {co}",{co:e.job.company})
    :e.kind==="rp"?t("{co} moved to interviews",{co:e.job.company}):t("Offer")+" · "+e.job.company;
  return '<section class="cal-card cal-side cal-r" style="animation-delay:.1s"><div class="cal-ch"><h2>'+t("Coming up")+
    '</h2><span>'+t("next 7 days")+'</span></div>'+
    (late.length?'<div class="cal-late"><b>'+esc(t(late.length===1?"1 follow-up overdue":"{n} follow-ups overdue",{n:late.length}))+'</b> · '+
      late.slice(0,4).map(e=>esc(e.job.company)+" ("+esc(fmtKey(e.day,{day:"numeric",month:"short"}))+")").join(", ")+'</div>':'')+
    (up.length?up.map(e=>'<button class="cal-item" data-open-job="'+esc(e.job.id)+'"><span class="bar" style="background:'+col[e.kind]+'"></span>'+
      '<span class="tx"><small>'+esc(when(e))+'</small><b>'+esc(what(e))+'</b><span>'+esc(e.job.title)+'</span>'+
      (e.kind==="iv"&&otherTz(e.job)?'<em>'+GLOBE+esc(hmIn(e.at,e.job.interview_tz)+" "+t("in")+" "+tzCity(e.job.interview_tz))+'</em>':'')+
      '</span></button>').join(""):'<p class="cal-none">'+t("Nothing in the next seven days.")+'</p>')+'</section>';
}
/* Drag an interview or a follow-up to another day. An interview keeps its
   hour where you are, and is written back in the zone it was given in. */
function wireDrag(){
  let what=null;
  $$("#cal-body [data-drag]").forEach(el=>el.ondragstart=e=>{ what=el.dataset.drag; e.dataTransfer.setData("text/plain",what);
    e.dataTransfer.effectAllowed="move" });
  $$("#cal-body [data-drop]").forEach(c=>{
    c.ondragover=e=>{ if(what){ e.preventDefault(); c.classList.add("drop") } };
    c.ondragleave=()=>c.classList.remove("drop");
    c.ondrop=async e=>{
      e.preventDefault(); c.classList.remove("drop");
      const [kind,id]=(what||"").split(":"); what=null;
      const j=(S.jobs||[]).find(x=>x.id===id), day=c.dataset.drop; if(!j) return;
      if(kind==="fu") await saveJob(id,{followup_date:day});
      else{
        const at=interviewMoment(j), hm=hmIn(at,userTz());
        const moved=wallToInstant(day+"T"+hm,userTz());
        await saveJob(id,{interview_at:wallIn(moved,j.interview_tz||machineTz())+":00"});
      }
      toast(t("Moved to {day}",{day:fmtKey(day,{weekday:"long",day:"numeric",month:"long"})}));
      drawCalendar(false);
    };
  });
}

/* ---- week ---------------------------------------------------------------- */
function drawCalWeek(ev){
  const today=todayKey(), start=monday(S.calAnchor), end=addDays(start,6);
  $("#cal-title").textContent=fmtKey(start,{day:"numeric",month:"short"})+" – "+fmtKey(end,{day:"numeric",month:"short",year:"numeric"});
  const days=Array.from({length:7},(_,i)=>addDays(start,i));
  const ivs=ev.filter(e=>e.kind==="iv"&&e.day>=start&&e.day<=end);
  const fus=ev.filter(e=>e.kind==="fu"&&e.day>=start&&e.day<=end);
  const hrs=ivs.map(e=>+hmIn(e.at,userTz()).slice(0,2));
  const h0=Math.min(9,...hrs), h1=Math.max(19,...hrs.map(h=>h+1)), HH=56;
  S.calH0=h0;
  const head='<div class="cal-wh"><div></div>'+days.map(k=>'<div'+(k===today?' class="today"':'')+'><small>'+
    esc(fmtKey(k,{weekday:"short"}))+'</small><b>'+Number(k.slice(8))+'</b></div>').join("")+'</div>';
  const allday='<div class="cal-wa"><div>'+t("Follow-ups")+'</div>'+days.map(k=>'<div data-drop="'+k+'">'+
    fus.filter(e=>e.day===k).map(calChip).join("")+'</div>').join("")+'</div>';
  const hours=Array.from({length:h1-h0},(_,i)=>h0+i);
  const blocks=ivs.map(e=>{
    const [h,m]=hmIn(e.at,userTz()).split(":").map(Number), col=days.indexOf(e.day);
    const top=(h-h0+m/60)*HH+2;
    return '<button class="cal-blk'+(S.calPop===e.job.id?" on":"")+'" data-pop="'+esc(e.job.id)+'" style="left:calc('+col+
      ' * (100% / 7) + 4px);width:calc(100% / 7 - 8px);top:'+top+'px;height:'+(HH-4)+'px"><b>'+hmIn(e.at,userTz())+' '+
      esc(e.job.company)+'</b><span>'+esc(e.job.title)+'</span>'+(otherTz(e.job)?'<em>'+GLOBE+esc(hmIn(e.at,e.job.interview_tz)+
      " "+t("in")+" "+tzCity(e.job.interview_tz))+'</em>':'')+'</button>';
  }).join("");
  let nowl="";
  const ni=days.indexOf(today);
  if(ni>=0){ const [h,m]=hmIn(new Date(),userTz()).split(":").map(Number);
    if(h>=h0&&h<h1) nowl='<span class="cal-nowline" style="left:calc('+ni+' * (100% / 7));width:calc(100% / 7);top:'+((h-h0+m/60)*HH)+'px"></span>' }
  const grid='<div class="cal-wg"><div>'+hours.map(h=>'<div class="cal-hr">'+String(h).padStart(2,"0")+':00</div>').join("")+
    '</div><div class="cal-wcols">'+hours.map(()=>'<div class="ln"></div>').join("")+
    days.map((_,i)=>'<span class="col" style="left:calc('+i+' * (100% / 7))"></span>').join("")+blocks+nowl+'</div></div>';
  $("#cal-body").innerHTML='<section class="cal-card cal-week cal-r">'+head+allday+grid+calPopHTML(ivs)+'</section>';
  wireCal(); wireDrag();
  $$("#cal-body [data-pop]").forEach(b=>b.onclick=()=>{ S.calPop=S.calPop===b.dataset.pop?null:b.dataset.pop; drawCalWeek(calEvents()) });
  const x=$("#cal-pop-x"); if(x) x.onclick=()=>{ S.calPop=null; drawCalWeek(calEvents()) };
}
function calPopHTML(ivs){
  const e=ivs.find(x=>x.job.id===S.calPop); if(!e) return "";
  const j=e.job, two=otherTz(j);
  const col=((dayDiff(monday(e.day),e.day))+7)%7, [h,m]=hmIn(e.at,userTz()).split(":").map(Number);
  const top=Math.max(8,Math.min(360,110+(h-S.calH0+m/60)*56-40));
  const side=col>=4?"right:calc("+(7-col)+" * ((100% - 64px) / 7) + 12px)":"left:calc(64px + "+(col+1)+" * ((100% - 64px) / 7) + 12px)";
  return '<div class="cal-pop" style="'+side+';top:'+top+'px" role="dialog" aria-label="'+esc(t("Interview")+" · "+j.company)+'">'+
    '<button class="x" id="cal-pop-x" aria-label="'+esc(t("Close"))+'">✕</button>'+
    '<div class="hd">'+companyMark(j)+'<div><b>'+esc(t("Interview")+" · "+j.company)+'</b><span>'+esc(j.title)+
      (j.location?" · "+esc(j.location):"")+'</span></div></div>'+
    '<div class="tz"><div><small>'+t("Your time")+'</small><b>'+esc(fmtKey(e.day,{weekday:"short",day:"numeric"})+" · "+hmIn(e.at,userTz()))+'</b></div>'+
      (two?'<div><small>'+esc(t("In {city}",{city:tzCity(j.interview_tz)}))+'</small><b>'+esc(hmIn(e.at,j.interview_tz))+'</b></div>'
        :'<div><small>'+t("Status")+'</small><b>'+esc(prettyStatus(j.status))+'</b></div>')+'</div>'+
    (j.notes?'<p>'+esc(String(j.notes).slice(0,220))+'</p>':'')+
    '<div class="a"><button class="pbtn" data-open-job="'+esc(j.id)+'">'+t("Open application")+'</button>'+
      '<button class="obtn" data-ics="'+esc(j.id)+'">'+t("Add to my calendar")+'</button></div></div>';
}
function wireCal(){
  $$("#cal-body [data-open-job]").forEach(el=>el.onclick=ev=>{ if(ev.target.closest("[data-pop]")) return; openJob(el.dataset.openJob) });
  $$("#cal-body [data-ics]").forEach(el=>el.onclick=ev=>{ ev.stopPropagation(); window.open("/api/calendar.ics?id="+encodeURIComponent(el.dataset.ics)+tok()) });
  $$("#cal-body .cal-md[data-day]").forEach(el=>el.onclick=()=>{ if(el.classList.contains("out")) return;
    S.calView="month"; S.calAnchor=el.dataset.day; drawCalendar(false) });
}

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
        '<i>✓</i>Add a Draft row to the applications</label>'+
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
        (S.pages[d.path]?" · "+S.pages[d.path]+" page"+(S.pages[d.path]===1?"":"s"):"")+
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
        catch(e){ toast("Document created, but the application row failed: "+e.message,true) }
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
  ember:{bars:[[5,"52%",1,0,"#8a4b2a"],[2,"100%",0,2,"#d8c3b0"],[3,"94%"],
    [3,"80%"]]},
  harvard:{centre:true,bars:[[5,"64%",1],[2,"84%",0,3],[3,"100%"],[3,"90%"]]},
  ink:{bars:[[6,"46%",1],[3,"100%",0,4,"#2b2b2b"],[3,"92%"],[3,"76%"]]},
  opal:{bars:[[5,"58%",1,0,"#2f6f6b"],[3,"88%",0,3],[3,"96%"],[3,"84%"]]},
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

/* One design for every language of a CV, kept on the source: a design edited
   on a translation would be overwritten the next time the source changed. */
$("#btn-design").onclick=async()=>{
  const fam=S.doc&&S.doc.family;
  if(fam&&fam.source&&fam.source!==S.path){
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    toast("Every language of this CV shares one design, so it is edited on "+
      docLabel(fam.source)+".");
    await openDoc(fam.source);
  }
  openDesign();
};
/* What each group is for, in a line. RenderCV's own group names are kept as
   the titles; anything it adds later just goes without a line. */
const DZ_GROUPS={
  photo:"One photo for your CV Studio folder. Each CV shows it or not, so you can leave it off where recruiters expect CVs without one.",
  page:"Paper size, margins, and what prints around the edges.",
  colors:"Every colour on the page. Links and section titles are the ones people notice.",
  typography:"Fonts, sizes and weight for each part of the page.",
  links:"How links look on the page and in the PDF.",
  header:"Your name, headline and contact line at the top of page one.",
  section_titles:"How each section heading is drawn.",
  sections:"Spacing between entries, and whether a section may break across pages.",
  entries:"The layout inside each entry: the date column, bullets and spacing.",
  templates:"The text RenderCV fills in. Words in capitals are replaced with your data.",
};
const human=k=>{ const t=String(k).replace(/_/g," "); return t.charAt(0).toUpperCase()+t.slice(1) };
async function openDesign(){
  if(!S.path) return toast("Open a document first");
  $("#ovl-settings").hidden=true; $("#ovl-design").hidden=false;
  DZ.section=DZ.section||"theme";
  paintDesignHead(); paintThemes(); paintEffect();
  await ensureSchema();
  paintAdvanced(); paintDesignNav();
  fillThemePreviews();
}
/* Whose design this is. The base CV's is the one every tailored copy starts
   from, so it says so; any other document's is its own. */
function paintDesignHead(){
  const doc=(S.state.documents||[]).find(d=>d.path===S.path);
  const base=S.state.base&&S.state.base.path===S.path;
  $("#dz-list").textContent=$("#back").textContent;
  $("#dz-doc").textContent=(doc&&doc.label)||S.path.split("/").pop();
  $("#dz-base").hidden=!base;
  $("#dz-note").textContent=base
    ? "Tailored CVs copied from it start with this design"
    : "Only this document. The base CV keeps its own.";
}
$("#dz-list").onclick=()=>{ closeOverlays(); goBack() };
$("#dz-pdf").onclick=()=>$("#btn-pdf").click();
async function ensureSchema(){
  const theme=DZ.theme||(S.state.themes||[])[0];
  if(S.schema&&S.schemaTheme===theme) return;
  try{ S.schema=await api("/api/design-schema?theme="+encodeURIComponent(theme));
       S.schemaTheme=theme }
  catch(e){ S.schema={groups:[]}; S.schemaTheme=theme }
}

/* The theme tiles are this document, rendered in each theme -- not a sample
   CV and not a sketch. The current theme's tile is the live render; the rest
   are rendered one at a time in the background when Design opens, and again
   only once the content has changed since. A drawn sketch holds the place
   until a tile's render lands. */
function thumbKey(){ return S.path+"|"+JSON.stringify(contentPatches()) }
function contentPatches(){
  return collectPatches().filter(p=>p.path[0]!=="design");
}
let thumbRun=0;
async function fillThemePreviews(){
  const run=++thumbRun, key=thumbKey();
  if(!S.thumbs||S.thumbs.key!==key) S.thumbs={key:key,by:{}};
  const cur=DZ.theme||(S.state.themes||[])[0];
  for(const t of (S.state.themes||[])){
    if(run!==thumbRun||$("#ovl-design").hidden||S.thumbs.key!==key) return;
    if(t===cur||S.thumbs.by[t]) continue;
    try{
      const r=await post("/api/theme-preview",{path:S.path,theme:t,patches:contentPatches()});
      if(r.ok&&S.thumbs.key===key){
        S.thumbs.by[t]={png:r.png,pages:r.pages};
        S.themePages[t]=r.pages;
        paintThemes();
      }
    }catch(e){ return }
  }
}
function paintThemes(){
  const themes=(S.state&&S.state.themes)||[];
  const cur=DZ.theme||themes[0];
  const by=(S.thumbs&&S.thumbs.by)||{};
  const live=S.render&&S.render.pngs&&S.render.pngs[0];
  $("#themegrid").innerHTML=themes.map(t=>{
    const img=t===cur&&live?live+tok():(by[t]?by[t].png+tok():null);
    const pp=S.themePages[t];
    return '<button class="thumbwrap'+(t===cur?" sel":"")+'" role="radio" aria-checked="'+
      String(t===cur)+'" data-theme="'+esc(t)+'">'+
      (img?'<img src="'+esc(img)+'" alt="">':thumbHTML(t))+
      '<span class="thumbcap"><span>'+esc(themeLabel(t))+'</span>'+
      '<em>'+(pp?pp+" page"+(pp===1?"":"s"):"")+'</em></span></button>';
  }).join("");
  $$("#themegrid [data-theme]").forEach(b=>b.onclick=()=>{
    if(b.dataset.theme===cur) return;
    DZ.theme=b.dataset.theme; S.schema=null;
    paintThemes(); touch();
    ensureSchema().then(()=>{ paintAdvanced(); paintDesignNav() });
  });
}

/* Every design option, generated from RenderCV's own schema rather than a
   hand-written list, so it stays correct when RenderCV adds or renames one.
   All groups are built at once and only the chosen one is shown: the patch
   list is read off these inputs, so a group that was never opened still has
   to be there to say what it holds. */
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
function controlHTML(f,v){
  const dp=esc(JSON.stringify(f.path)), lbl=esc(human(f.path[f.path.length-1]));
  const at=' data-d=\''+dp+'\' aria-label="'+lbl+'"';
  if(f.kind==="color")
    return '<input type="color"'+at+' data-kind="color" value="'+rgb2hex(v)+
      '"><span class="hex mono">'+esc(String(v==null?"":v))+'</span>';
  if(f.kind==="dimension"){
    const d=splitDim(v);
    return '<input type="number" step="0.05"'+at+' data-kind="dimension" value="'+
      esc(d.n)+'"><select class="unit" aria-label="Unit">'+
      UNITS.map(x=>'<option'+(x===d.u?" selected":"")+'>'+x+'</option>').join("")+'</select>';
  }
  if(f.kind==="enum")
    return '<select'+at+' data-kind="enum">'+(f.options||[]).map(o=>
      '<option'+(String(o)===String(v)?" selected":"")+'>'+esc(o)+'</option>').join("")+
      '</select>';
  if(f.kind==="bool")
    return '<input type="checkbox"'+at+' data-kind="bool"'+(v?" checked":"")+'>';
  if(f.kind==="number")
    return '<input type="number"'+at+' data-kind="number" value="'+esc(v==null?"":v)+'">';
  if(f.kind==="list")
    return '<input type="text"'+at+' data-kind="list" value="'+esc((v||[]).join(", "))+
      '" placeholder="comma separated">';
  /* Templates are several lines -- the main column of an entry is a title
     line, a summary and the highlights -- and a one-line input silently
     drops the line breaks, so writing it back would flatten the template. */
  if(f.path[0]==="templates"){
    const t=v==null?"":String(v);
    return '<textarea'+at+' data-kind="text" rows="'+Math.max(1,t.split("\n").length)+
      '" spellcheck="false">'+esc(t)+'</textarea>';
  }
  return '<input type="text"'+at+' data-kind="text" value="'+esc(v==null?"":v)+'">';
}
/* ---- photo -----------------------------------------------------------------
   One photo per workspace, cropped square and shrunk here before it is sent,
   so a phone photo never reaches every PDF at full size. A CV shows it by
   pointing cv.photo at it; that is an edit like any other, saved with Save. */
const photoOn=()=>!!(S.data&&S.data.cv&&S.data.cv.photo);
const PH_ICON='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '+
  'stroke-width="2" aria-hidden="true"><circle cx="12" cy="9" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/></svg>';
const PH_TIP='<div class="ph-note"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" '+
  'stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/>'+
  '<path d="M12 8v5M12 16v.5"/></svg><span>Expected on CVs in much of Europe, often left off in '+
  'the UK, the US and Ireland. The ATS check says so when an application is in one of those.'+
  '</span></div>';
function setPhoto(on){
  if(!S.data||!S.data.cv) return;
  const ref=S.doc&&S.doc.photo_ref;
  if(on&&!ref) return;
  S.data.cv.photo=on?ref:null;
  touch(); paintPhotoChip();
  if(!$("#ovl-design").hidden){ paintPhoto(); paintDesignNav() }
}
function paintPhoto(){
  const host=$("#dz-photo"), ph=S.state&&S.state.photo;
  if(!ph){
    host.innerHTML='<div class="ph-drop" id="ph-drop"><span class="ic">'+
      PH_ICON.replace(/16/g,"24")+'</span><b>Add a photo</b>'+
      '<span>Drop a JPEG or PNG here. You crop it to a square next, and only the cropped '+
      'copy is kept.</span><button class="obtn" id="ph-pick">Choose a photo&#8230;</button>'+
      '<input type="file" id="ph-file" accept="image/jpeg,image/png,image/webp" hidden></div>'+PH_TIP+
      '<p class="sp-note">Its size and place on the page appear here once there is a photo.</p>';
    const drop=$("#ph-drop"), file=$("#ph-file");
    $("#ph-pick").onclick=()=>file.click();
    file.onchange=()=>file.files[0]&&cropSheet(file.files[0]);
    drop.ondragover=e=>{ e.preventDefault(); drop.classList.add("over") };
    drop.ondragleave=()=>drop.classList.remove("over");
    drop.ondrop=e=>{ e.preventDefault(); drop.classList.remove("over");
      const f=e.dataTransfer.files[0]; if(f) cropSheet(f) };
    return;
  }
  const on=photoOn(), kb=Math.max(1,Math.round(ph.bytes/1024));
  host.innerHTML='<div class="ph-card"><div class="ph-top"><img alt="Your photo" src="'+
      esc(ph.url+tok())+'"><div class="m"><b>photo.jpg</b><span>Square · '+kb+
      ' KB · in your CV Studio folder</span><span class="acts">'+
      '<button class="obtn" id="ph-replace">Replace&#8230;</button>'+
      '<button class="obtn" id="ph-crop">Crop&#8230;</button>'+
      '<button class="del" id="ph-del">Remove</button></span></div>'+
      '<input type="file" id="ph-file" accept="image/jpeg,image/png,image/webp" hidden></div>'+
    '<label class="sw"><input type="checkbox" id="ph-on"'+(on?" checked":"")+'><span class="tr"></span>'+
      '<span class="tx"><b>Show it on this CV</b><span>CVs you tailor from this one start the '+
      'same way. Turn it off on any of them.</span></span></label></div>'+PH_TIP;
  const file=$("#ph-file");
  $("#ph-replace").onclick=()=>file.click();
  file.onchange=()=>file.files[0]&&cropSheet(file.files[0]);
  $("#ph-crop").onclick=()=>cropSheet(ph.url+tok());
  $("#ph-on").onchange=e=>setPhoto(e.target.checked);
  $("#ph-del").onclick=removePhoto;
}
async function removePhoto(){
  if(!confirm("Remove the photo from your CV Studio folder? It comes off every CV that shows it."))
    return;
  try{
    const r=await post("/api/photo/remove",{});
    S.state.photo=null;
    if(S.path){
      const d=await api("/api/doc?path="+encodeURIComponent(S.path));
      S.doc=d; S.data=d.data?JSON.parse(JSON.stringify(d.data)):null; S.docMtime=d.mtime;
      paint(); scheduleLive();
    }
    paintPhotoChip();
    if(!$("#ovl-design").hidden){ paintAdvanced(); paintDesignNav() }
    toast("Photo removed"+(r.cleared&&r.cleared.length?" from "+r.cleared.length+" CV"+
      (r.cleared.length===1?"":"s"):""));
  }catch(e){ toast(e.message,true) }
}
/* Drag to move, zoom to fill the square; 600 x 600 JPEG out. */
function cropSheet(src){
  const url=typeof src==="string"?src:URL.createObjectURL(src);
  openSheet(
    '<div><h3 id="sheet-title">Crop your photo</h3><p>Drag to move it, and zoom until your '+
      'face fills most of the square.</p></div>'+
    '<div class="crop" id="crop"><img class="dim" alt=""><div class="win"><img alt="The part '+
      'of the photo that will be kept"></div><div class="win grid" aria-hidden="true"></div></div>'+
    '<label class="cropzoom">Zoom <input type="range" id="crop-z" min="1" max="4" step="0.01" '+
      'value="1"></label>'+
    '<div class="foot"><span class="left sp-note">Saved as a 600 × 600 JPEG in your CV Studio '+
      'folder. The original is not kept.</span><button class="sbtn" data-cancel>Cancel</button>'+
      '<button class="sbtn primary" id="crop-go" disabled>Use this photo</button></div>');
  const box=$("#crop"), imgs=box.querySelectorAll("img"), z=$("#crop-z");
  const st={w:0,h:0,base:1,s:1,ox:0,oy:0};
  const clamp=()=>{
    const mx=Math.max(0,st.w*st.s/2-130), my=Math.max(0,st.h*st.s/2-130);
    st.ox=Math.min(mx,Math.max(-mx,st.ox)); st.oy=Math.min(my,Math.max(-my,st.oy));
  };
  const draw=()=>{
    clamp();
    imgs.forEach(im=>{ im.style.width=(st.w*st.s)+"px";
      im.style.transform="translate(calc(-50% + "+st.ox+"px), calc(-50% + "+st.oy+"px))" });
  };
  const im=new Image();
  im.onload=()=>{
    st.w=im.naturalWidth; st.h=im.naturalHeight;
    st.base=260/Math.min(st.w,st.h); st.s=st.base;
    imgs.forEach(x=>x.src=url); draw(); $("#crop-go").disabled=false;
  };
  im.onerror=()=>{ toast("That image could not be opened.",true); closeSheet() };
  im.src=url;
  z.oninput=()=>{ st.s=st.base*Number(z.value); draw() };
  let drag=null;
  box.onpointerdown=e=>{ drag={x:e.clientX,y:e.clientY,ox:st.ox,oy:st.oy};
    box.setPointerCapture(e.pointerId); box.classList.add("dragging") };
  box.onpointermove=e=>{ if(!drag) return;
    st.ox=drag.ox+e.clientX-drag.x; st.oy=drag.oy+e.clientY-drag.y; draw() };
  box.onpointerup=box.onpointercancel=()=>{ drag=null; box.classList.remove("dragging") };
  box.onwheel=e=>{ e.preventDefault();
    z.value=String(Math.min(4,Math.max(1,Number(z.value)-e.deltaY/400))); z.oninput() };
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#crop-go").onclick=async()=>{
    const side=260/st.s, sx=st.w/2-(130+st.ox)/st.s, sy=st.h/2-(130+st.oy)/st.s;
    const cv=document.createElement("canvas"); cv.width=cv.height=600;
    const g=cv.getContext("2d"); g.fillStyle="#fff"; g.fillRect(0,0,600,600);
    g.drawImage(im,sx,sy,side,side,0,0,600,600);
    const data=cv.toDataURL("image/jpeg",0.85).replace(/^data:[^,]*,/,"");
    $("#crop-go").disabled=true;
    try{
      const r=await post("/api/photo",{data,path:S.path||null});
      if(!r.ok){ $("#crop-go").disabled=false; return toast(r.error,true) }
      S.state.photo=r.photo;
      if(S.doc&&r.ref) S.doc.photo_ref=r.ref;
      closeSheet();
      /* A first photo goes straight onto the CV it was added from. */
      if(S.data&&S.data.cv&&!photoOn()) setPhoto(true); else scheduleLive();
      if(!$("#ovl-design").hidden){ paintAdvanced(); paintDesignNav() }
      paintPhotoChip();
      toast("Photo saved");
    }catch(e){ $("#crop-go").disabled=false; toast(e.message,true) }
    if(typeof src!=="string") URL.revokeObjectURL(url);
  };
}
/* The chip in the editor's bar: whether this CV shows the photo, and a switch
   for it, with a word when the application it is for is somewhere CVs usually
   go without one. */
const PHOTO_UNUSUAL=/\b(united kingdom|uk|u\.k\.|england|scotland|wales|northern ireland|london|manchester|edinburgh|glasgow|bristol|united states|usa|u\.s\.a?\.?|us|new york|san francisco|seattle|boston|austin|chicago|los angeles|ireland|dublin|cork)\b/i;
function paintPhotoChip(){
  const chip=$("#photochip");
  const letter=S.path&&S.path.startsWith("letters/");
  if(!S.path||letter||!(S.state&&S.state.photo)||!S.data){ chip.hidden=true; return }
  const on=photoOn();
  chip.hidden=false;
  chip.classList.toggle("off",!on);
  chip.innerHTML=PH_ICON.replace(/16/g,"12")+'<span>Photo '+(on?"on":"off")+'</span>';
  chip.title="Whether this CV shows your photo";
  chip.onclick=e=>{ e.stopPropagation(); photoPop() };
}
function photoPop(){
  let pop=$("#photopop");
  if(pop){ pop.remove(); return }
  const j=typeof linkedJob==="function"?linkedJob():null;
  const unusual=j&&PHOTO_UNUSUAL.test((j.country||"")+" "+(j.location||""));
  pop=document.createElement("div");
  pop.className="photopop"; pop.id="photopop"; pop.setAttribute("role","dialog");
  pop.setAttribute("aria-label","Photo on this CV");
  const base=S.doc&&S.doc.prov&&S.doc.prov.base&&S.doc.prov.base.path;
  pop.innerHTML='<label class="sw"><input type="checkbox" id="pp-on"'+(photoOn()?" checked":"")+
    '><span class="tr"></span><span class="tx"><b>Show the photo on this CV</b><span>'+
    (base?'For this CV only. '+esc(docLabel(base))+' keeps its own setting.'
      :'For this CV only.')+'</span></span></label>'+
    (unusual?'<div class="why"><i></i><span>This application is in '+esc(j.location||j.country)+
      '. Recruiters there usually ask for CVs without a photo.</span></div>':'');
  document.body.append(pop);
  const r=$("#photochip").getBoundingClientRect();
  pop.style.left=Math.min(r.left,innerWidth-330)+"px"; pop.style.top=(r.bottom+6)+"px";
  $("#pp-on").onchange=e=>setPhoto(e.target.checked);
  $("#pp-on").focus();
  const off=ev=>{ if(!pop.contains(ev.target)&&ev.target!==$("#photochip")){ pop.remove();
    document.removeEventListener("pointerdown",off,true) } };
  document.addEventListener("pointerdown",off,true);
}

/* RenderCV keeps the photo's size and place under Header, where they mean
   nothing until there is a photo. They get a group of their own, beside the
   photo itself, second after Theme. */
function designGroups(){
  const raw=(S.schema&&S.schema.groups)||[];
  const isPhoto=f=>f.path[0]==="header"&&/^photo_/.test(f.path[f.path.length-1]);
  const photo=raw.flatMap(g=>g.fields.filter(isPhoto));
  const rest=raw.map(g=>({...g,fields:g.fields.filter(f=>!isPhoto(f))})).filter(g=>g.fields.length);
  return [{name:"photo",fields:photo}].concat(rest);
}
function paintAdvanced(){
  const host=$("#dz-advanced"), groups=designGroups();
  if(!groups.length){
    host.innerHTML='<p class="sp-note">RenderCV did not offer a schema for this theme, '+
      'so only the theme can be chosen here. Everything else can still be edited '+
      'in the YAML tab.</p>';
    return;
  }
  const cur=(S.data&&S.data.design)||{};
  host.innerHTML=groups.map(g=>{
    /* Deeper paths get a heading of their own -- typography.font_family.body
       sits under "Font family" as "Body" -- in the order RenderCV lists them. */
    let h='', open=null, rows='';
    const flush=()=>{ if(rows) h+=(open?'<h3 class="dz-sub">'+esc(human(open))+'</h3>':"")+
      '<div class="dz-card">'+rows+'</div>'; rows="" };
    g.fields.forEach(f=>{
      const sub=f.path.length>2?f.path[1]:null;
      if(sub!==open){ flush(); open=sub }
      const raw=getAt(cur,f.path);
      const v=(raw===undefined||raw===null)?f.default:raw;
      rows+='<div class="dz-row" data-def=\''+esc(JSON.stringify(f.default==null?null:f.default))+
        '\' data-was=\''+esc(JSON.stringify(v==null?null:v))+'\'><label><i class="dz-dot" title="Changed from the theme default"></i><span>'+
        esc(human(g.name==="photo"?String(f.path[f.path.length-1]).replace(/^photo_/,"")
          :f.path[f.path.length-1]))+'</span></label>'+
        '<div class="dctl">'+controlHTML(f,v)+'</div>'+
        '<button class="dreset" title="Back to the theme default">Reset</button></div>';
    });
    flush();
    return '<div class="dz-group" data-group="'+esc(g.name)+'" hidden>'+h+'</div>';
  }).join("");
  markChanged();
  showDesignSection();
}
/* Read a row's control the way the patch list will, so "changed" means what
   would actually be written differs from the theme's own value. */
function rowValue(row){
  const el=row.querySelector("[data-d]"), kind=el.dataset.kind;
  if(kind==="color") return hex2rgb(el.value);
  if(kind==="bool") return el.checked;
  if(kind==="number") return el.value===""?null:Number(el.value);
  if(kind==="list") return el.value.split(",").map(x=>x.trim()).filter(Boolean);
  if(kind==="dimension"){
    const u=row.querySelector("select.unit");
    return el.value===""?null:String(el.value)+((u&&u.value)||"cm");
  }
  return el.value;
}
const sameVal=(a,b)=>{
  if(a==null||a==="") a=null; if(b==null||b==="") b=null;
  if(Array.isArray(a)||Array.isArray(b)) return JSON.stringify(a||[])===JSON.stringify(b||[]);
  if(typeof a==="string"&&typeof b==="string"&&/rgb\(/.test(a)&&/rgb\(/.test(b))
    return a.replace(/\s/g,"")===b.replace(/\s/g,"");
  return String(a)===String(b);
};
function markChanged(){
  $$("#dz-advanced .dz-row").forEach(r=>{
    r.classList.toggle("chg",!sameVal(rowValue(r),JSON.parse(r.dataset.def)));
  });
}
function resetRow(row){
  const def=JSON.parse(row.dataset.def), el=row.querySelector("[data-d]"), kind=el.dataset.kind;
  if(kind==="color"){ el.value=rgb2hex(def); const sp=row.querySelector(".hex");
    if(sp) sp.textContent=def||"" }
  else if(kind==="bool") el.checked=!!def;
  else if(kind==="dimension"){ const d=splitDim(def); el.value=d.n;
    const u=row.querySelector("select.unit"); if(u) u.value=d.u }
  else if(kind==="list") el.value=(def||[]).join(", ");
  else el.value=def==null?"":def;
}
function paintDesignNav(){
  const groups=designGroups();
  const cur=DZ.theme||(S.state.themes||[])[0];
  const items=[{k:"theme",t:"Theme",ct:themeLabel(cur||""),n:0}].concat(groups.map(g=>{
    const el=$('#dz-advanced [data-group="'+CSS.escape(g.name)+'"]');
    return {k:g.name,t:human(g.name),ct:g.name==="photo"
      ?(!(S.state&&S.state.photo)?"None":photoOn()?"On":"Off"):String(g.fields.length),
      n:el?el.querySelectorAll(".dz-row.chg").length:0};
  }));
  $("#dz-nav").innerHTML=items.map(i=>
    '<button data-sec="'+esc(i.k)+'" aria-current="'+String(i.k===DZ.section)+'">'+
    '<span class="nm">'+esc(i.t)+'</span>'+
    (i.n?'<span class="chg" title="Changed from the theme default"><i class="dz-dot"></i>'+
      i.n+'</span>':"")+
    '<span class="ct">'+esc(i.ct)+'</span></button>').join("")+
    '<div class="dz-key"><i class="dz-dot"></i>Changed from the theme default</div>';
  $$("#dz-nav [data-sec]").forEach(b=>b.onclick=()=>{
    DZ.section=b.dataset.sec; paintDesignNav(); showDesignSection();
    $(".dz-scroll").scrollTop=0;
  });
}
function showDesignSection(){
  const k=DZ.section||"theme", theme=k==="theme";
  $("#themegrid").hidden=!theme;
  $$("#dz-advanced .dz-group").forEach(g=>{ g.hidden=g.dataset.group!==k||
    (k==="photo"&&!(S.state&&S.state.photo)) });
  $("#dz-photo").hidden=k!=="photo";
  if(k==="photo") paintPhoto();
  $("#dz-h").textContent=theme?"Theme":human(k);
  $("#dz-desc").textContent=theme
    ? "The starting point for every other setting. Each tile is this document in that theme."
    : (DZ_GROUPS[k]||"");
  const grp=$('#dz-advanced [data-group="'+CSS.escape(k)+'"]');
  $("#dz-reset").hidden=theme||!grp||!grp.querySelector(".dz-row.chg");
  if(grp) grp.querySelectorAll("textarea").forEach(fitArea);
}
/* A template box as tall as what it holds, so no line of it is hidden. */
function fitArea(t){ t.style.height="auto"; t.style.height=(t.scrollHeight+2)+"px" }
$("#dz-reset").onclick=()=>{
  const grp=$('#dz-advanced [data-group="'+CSS.escape(DZ.section)+'"]');
  if(!grp) return;
  grp.querySelectorAll(".dz-row.chg").forEach(resetRow);
  markChanged(); paintDesignNav(); showDesignSection(); touch();
};
/* Only what you actually changed is written. Writing every field back made
   a document carry eighty-odd copies of its theme's defaults, which then stop
   following the theme when you switch it. */
function advancedPatches(){
  return $$("#dz-advanced .dz-row").map(row=>{
    const el=row.querySelector("[data-d]"), v=rowValue(row);
    if(el.dataset.kind==="dimension"&&v===null) return null;
    if(sameVal(v,JSON.parse(row.dataset.was))) return null;
    return {path:["design"].concat(JSON.parse(el.dataset.d)),value:v};
  }).filter(Boolean);
}
function designPatches(){
  const out=[];
  if(DZ.theme) out.push({path:["design","theme"],value:DZ.theme});
  return out.concat(advancedPatches());
}
$("#dz-advanced").addEventListener("click",e=>{
  const b=e.target.closest(".dreset");
  if(!b) return;
  resetRow(b.closest(".dz-row"));
  markChanged(); paintDesignNav(); showDesignSection(); touch();
});
$("#dz-advanced").addEventListener("input",e=>{
  if(e.target.dataset&&e.target.dataset.kind==="color"){
    const sp=e.target.parentElement.querySelector(".hex");
    if(sp) sp.textContent=hex2rgb(e.target.value);
  }
  if(e.target.tagName==="TEXTAREA") fitArea(e.target);
  markChanged(); paintDesignNav(); showDesignSection();
  touch();
});
$("#dz-advanced").addEventListener("change",touch);

/* What the current combination actually costs, read off the last render
   rather than predicted. */
function paintEffect(){
  const r=S.render;
  $("#dz-pages").innerHTML=r&&r.pngs
    ? r.pngs.map((u,i)=>'<img src="'+esc(u+tok())+'" alt="Page '+(i+1)+'">').join("")
    : '<p class="note muted">Rendering…</p>';
  if(!r){ $("#dz-effect").innerHTML='<span>Nothing rendered yet</span>'; return }
  const pct=S.fill==null?null:Math.round(S.fill*100);
  const known=Object.keys(S.themePages).filter(t=>t!==DZ.theme);
  const shortest=known.sort((a,b)=>S.themePages[a]-S.themePages[b])[0];
  let note="";
  if(shortest&&S.themePages[shortest]<r.pages)
    note=themeLabel(shortest)+" fits this on "+S.themePages[shortest]+
      " page"+(S.themePages[shortest]===1?"":"s");
  const sep='<span class="sep">·</span>';
  $("#dz-effect").innerHTML=
    '<span><b>'+r.pages+'</b> page'+(r.pages===1?"":"s")+'</span>'+sep+
    '<span>page '+r.pages+' is <b>'+(pct==null?"–":pct+"%")+'</b> full</span>'+sep+
    '<span><b>'+(r.ats_words||0)+'</b> words</span>'+
    (note?sep+'<span class="why">'+esc(note)+'</span>':"");
}

/* =========================================================================
   Settings and updates
   ========================================================================= */
/* Preferences are per-machine conveniences, so they live beside the app
   rather than in the workspace: a workspace copied to another machine should
   carry documents, not window preferences. The server keeps them in a file
   and hands them over with the page (see load_prefs); this browser's storage
   keeps a copy for a page served without them. */
const PREFS_KEY="cvstudio.prefs";
function prefs(){ return window.CVS_PREFS||(window.CVS_PREFS={}) }
function setPref(k,v){
  const p=prefs(); p[k]=v;
  try{ localStorage.setItem(PREFS_KEY,JSON.stringify(p)) }catch(e){}
  post("/api/prefs",{set:{[k]:v}}).catch(()=>{});
}
if(window.CVS_PREFS_MOVE&&Object.keys(prefs()).length)
  setTimeout(()=>post("/api/prefs",{replace:prefs()}).catch(()=>{}),0);
/* "system" means take the attribute off and let prefers-color-scheme decide;
   anything else pins it. Everything downstream is a CSS variable, so nothing
   needs redrawing -- including the funnel, which is styled rather than filled. */
const ACCENTS=[["ochre","Ochre"],["indigo","Indigo"],["teal","Teal"],
  ["rose","Rose"],["moss","Moss"]];
function applyAppearance(){
  const p=prefs(), a=p.appearance||"system";
  if(a==="system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme=a;
  /* Ochre is what :root already defines, so it is the absence of an override
     rather than one more rule to keep in step with the others. */
  const acc=p.accent||"ochre";
  if(acc==="ochre") delete document.documentElement.dataset.accent;
  else document.documentElement.dataset.accent=acc;
}
applyAppearance();

function openSettings(pane){
  $("#ovl-design").hidden=true;
  $("#ovl-settings").hidden=false;
  fillSettings();
  showSettingsPane(pane||"workspace");
}
$("#btn-settings").onclick=()=>{
  if($("#ovl-settings").hidden) openSettings("workspace");
  else closeOverlays();
};
/* The one shortcut every desktop user tries. */
document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&e.key===","){
    e.preventDefault();
    if($("#ovl-settings").hidden) openSettings("workspace"); else closeOverlays();
  }
});
function showSettingsPane(which){
  $$("#set-rail button").forEach(x=>
    x.setAttribute("aria-selected",String(x.dataset.s===which)));
  ["workspace","editor","region","notify","ai","api","updates","about"].forEach(k=>
    $("#sp-"+k).hidden = k!==which);
  if(which==="updates") checkUpdates(true);
  if(which==="ai") loadAI();
}
$$("#set-rail button").forEach(b=>b.onclick=()=>showSettingsPane(b.dataset.s));
$$("[data-copy]").forEach(b=>b.onclick=async()=>{
  try{ await navigator.clipboard.writeText($("#"+b.dataset.copy).textContent);
       toast("Copied") }
  catch(e){ toast("Select the text and copy manually",true) }
});

function fillSettings(){
  fillNotify();
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
  $("#s-setup").onclick=()=>{ closeOverlays(); onboardingSheet() };
  const ul=$("#s-uilang");
  ul.value=pr.ui_lang||"";
  ul.onchange=()=>{ setPref("ui_lang",ul.value||null); location.reload() };
  const ml=$("#s-multilang"), cvl=$("#s-cvlang"), bcv=st.base;
  ml.checked=multiLang();
  ml.onchange=()=>{ setPref("multilang",ml.checked); applyMultiLang(); paintBase();
    if(S.view==="docs") drawDocuments(); if(S.jsel&&S.view==="jobs") drawJobs(); fillSettings() };
  /* With one language, which one it is, written onto the base CV. With
     several, each base CV already says its own on Documents. */
  $("#s-cvlang-row").hidden=ml.checked||!bcv||bcv.missing;
  const cur=(((st.documents||[]).find(d=>bcv&&d.path===bcv.path))||{}).lang||"en";
  cvl.innerHTML=(st.languages||[]).map(l=>'<option value="'+l.code+'"'+(l.code===cur?" selected":"")+'>'+
    esc(l.native)+(l.native!==l.english?' · '+esc(l.english):'')+'</option>').join("");
  cvl.onchange=async()=>{
    const l=(st.languages||[]).find(x=>x.code===cvl.value); if(!l||!bcv) return;
    try{
      const r=await post("/api/language/set",{path:bcv.path,language:l.code});
      if(r&&r.ok===false) throw new Error(r.error||"Could not save");
      const s2=await api("/api/state"); S.state=s2; renderDocs(s2.documents);
      S.baseThumb=null; paintBase(); toast(t("Saved"));
    }catch(e){ toast(e.message,true) }
  };
  const tzs=$("#s-tz");
  tzs.innerHTML='<option value="">'+esc(t("Match system"))+' · '+esc(tzCity(machineTz()))+'</option>'+
    tzOptions(pr.tz||"",null);
  tzs.value=pr.tz||"";
  tzs.onchange=()=>{ setPref("tz",tzs.value||null);
    try{ sessionStorage.setItem("cvs.reopen","region") }catch(e){}
    location.reload() };
  drawTzMap(tzs);
  const sb=$("#s-sample");
  sb.textContent=st.sample?"Back to my workspace":"Open sample data";
  sb.onclick=()=>setSample(!st.sample,sb);
  $("#s-exp").onclick=()=>window.open("/api/jobs/export?format=json"+tok());
  /* Revealing is deliberate and one click; copying never needs it. */
  const key=$("#s-key");
  key.hidden=!(S.state&&S.state.api_token);
  key.textContent=S.keyShown?"Hide the key":"Show the key";
  key.onclick=()=>{ S.keyShown=!S.keyShown; fillSettings() };
  $("#s-check").onclick=()=>checkUpdates(true);

  const live=$("#s-live");
  live.checked=pr.live!==false;
  live.onchange=()=>setPref("live",live.checked);
  const delay=$("#s-delay");
  delay.value=String(pr.delay||700);
  delay.onchange=()=>setPref("delay",Number(delay.value));
  const acc=prefs().accent||"ochre";
  $("#s-accent").innerHTML=ACCENTS.map(([id,label])=>
    '<button data-accent="'+id+'" title="'+label+'" aria-label="'+label+
    '" aria-pressed="'+String(id===acc)+'"></button>').join("");
  $$("#s-accent button").forEach(b=>{
    /* Painted from the theme's own token rather than a colour repeated here,
       so a swatch can never drift from what it selects. */
    b.style.background=getComputedStyle(document.documentElement)
      .getPropertyValue("--sw-"+b.dataset.accent).trim();
    b.onclick=()=>{ setPref("accent",b.dataset.accent); applyAppearance();
      $$("#s-accent button").forEach(x=>
        x.setAttribute("aria-pressed",String(x===b))); };
  });
  const ap=$("#s-appearance");
  ap.value=pr.appearance||"system";
  ap.onchange=()=>{ setPref("appearance",ap.value); applyAppearance() };
  const dt=$("#s-deftheme");
  if(dt&&!dt.dataset.filled){
    dt.innerHTML=(st.themes||[]).map(t=>"<option value=\""+esc(t)+"\">"+esc(themeLabel(t))+"</option>").join("");
    dt.dataset.filled="1";
  }
  if(dt){ dt.value=pr.theme||(st.themes||[])[0]||"";
          dt.onchange=()=>setPref("theme",dt.value) }

  /* The hand-setup snippets come from the server: each client has its own
     config format, and there is no reason for two places to know both. */
  fillAIPanel();

  /* Masked by default: this pane ends up in screenshots and screen shares,
     and the key in it is live. Copy still copies the real thing. */
  const shown=st.api_token&&S.keyShown?st.api_token
    :st.api_token?"•".repeat(Math.min(24,st.api_token.length)):"";
  const auth=st.api_token?' \
  -H "X-API-Key: '+shown+'"':"";
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

/* ---- what a new version says about itself ------------------------------
   The release notes are assembled from CHANGELOG.md and land in the updater
   manifest, so `body` is a real account of what changed rather than the same
   install note every time. The install note is still on the end of it, inside
   a <details>, which is for someone downloading the file by hand -- this app
   is already installed. Cut it off.

   Rendered rather than printed: the notes are markdown, and a wall of "- **"
   reads worse than nothing. Escaped first, then the three marks the notes
   actually use are put back, so nothing in a release body can inject markup. */
function updateNotes(body){
  const head=String(body||"").split(/\n---\s*\n|<details/)[0].trim();
  if(!head) return "";
  const inline=t=>esc(t)
    .replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>")
    .replace(/`([^`]+)`/g,'<code>$1</code>');
  /* Blocks, not lines. The notes are hard-wrapped, so a bullet is its "- "
     line plus every continuation under it; taking each line as its own block
     turned one bullet into a list item followed by a stray paragraph. */
  const blocks=[];
  for(const raw of head.split("\n")){
    const item=raw.match(/^\s*[-*]\s+(.*)$/);
    const last=blocks.length?blocks[blocks.length-1]:null;
    if(item) blocks.push({list:true,text:item[1]});
    else if(!raw.trim()){ if(last) blocks.push(null) }
    else if(last) last.text+=" "+raw.trim();
    else blocks.push({list:false,text:raw.trim()});
  }
  let html="", list=false;
  for(const b of blocks){
    if(!b){ if(list){ html+="</ul>"; list=false } continue }
    if(b.list&&!list){ html+="<ul>"; list=true }
    if(!b.list&&list){ html+="</ul>"; list=false }
    html+=b.list?"<li>"+inline(b.text)+"</li>":"<p>"+inline(b.text)+"</p>";
  }
  return html+(list?"</ul>":"");
}

/* One download, wherever it was started from: the panel in Settings and the
   panel that comes to you both report into `say`. */
async function installUpdate(up,say,done){
  const T=window.__TAURI__;
  let total=0, got=0;
  try{
    await up.downloadAndInstall(e=>{
      if(e.event==="Started") total=e.data.contentLength||0;
      if(e.event==="Progress"){
        got+=e.data.chunkLength||0;
        say(total?"Downloading "+Math.round(got/total*100)+"%":"Downloading\u2026");
      }
      if(e.event==="Finished") say("Installing\u2026");
    });
    say("Restarting\u2026");
    if(T.process&&T.process.relaunch) await T.process.relaunch();
  }catch(err){ say("Update failed: "+err); if(done) done(err) }
}

/* An update used to arrive as a toast saying to go and look in Settings, which
   is a notification about a notification. It comes to you now. Once per
   version: saying Later means later, not at every launch until you give in --
   Settings still has it whenever you want it. */
function updateSheet(up){
  if(!$("#sheet").hidden) return;      /* never over something being filled in */
  const have=(S.state&&S.state.version)||"";
  const notes=updateNotes(up.body);
  openSheet(
    '<div><h3 id="sheet-title">CV Studio '+esc(up.version)+' is ready</h3>'+
    (have?'<p>You have '+esc(have)+'. It installs and restarts in one step; '+
          'nothing in your workspace is touched.</p>':"")+'</div>'+
    (notes?'<div class="relnotes" id="u-notes">'+notes+'</div>':"")+
    '<div class="foot"><span class="mono" id="u-say"></span>'+
    '<button class="sbtn" id="u-later">Later</button>'+
    '<button class="sbtn primary" id="u-now">Install and restart</button></div>');
  $("#u-later").onclick=()=>{ setPref("skipUpdate",up.version); closeSheet() };
  $("#u-now").onclick=()=>{
    $("#u-now").disabled=true; $("#u-later").disabled=true;
    installUpdate(up,t=>{ $("#u-say").textContent=t },
      ()=>{ $("#u-now").disabled=false; $("#u-later").disabled=false });
  };
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
      $("#u-go").onclick=()=>{
        $("#u-go").disabled=true;
        installUpdate(up,t=>{ st.textContent=t },
          ()=>{ $("#u-go").disabled=false });
      };
    }
    if(!loud&&prefs().skipUpdate!==up.version) updateSheet(up);
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

/* The YAML tab is where the model actually writes, so it is the one view where
   losing the selection hurts most. The source lines come from ruamel, which
   knows exactly where it parsed each node -- no guessing, no string search.
   Drawn as a band behind the text rather than by re-marking the highlighted
   HTML, so syntax colouring and the invisible textarea both stay untouched. */
function selectedLines(){
  const m=(S.doc&&S.doc.lines)||{}, sel=S.sel;
  if(!sel) return null;
  if(sel.kind==="header") return m.header||null;
  if(sel.kind==="entry"){
    return m[sel.name+"/"+sel.i]||m[sel.name]||null;
  }
  return m[sel.name]||null;
}
/* The other direction of the same thread. S.doc.lines maps every block to the
   span of source that holds it; this reads it backwards, so the line the caret
   is on says which entry you are in and the page can follow the source the way
   it follows the form. Narrowest span wins, or an entry would always lose to
   the section around it. */
function blockAtLine(line){
  const m=(S.doc&&S.doc.lines)||{};
  let best=null, width=Infinity;
  for(const key of Object.keys(m)){
    const span=m[key];
    if(!span) continue;
    const end=Math.max(span[0]+1,span[1]);
    if(line<span[0]||line>=end) continue;
    const w=end-span[0];
    if(w>=width) continue;
    width=w;
    const cut=key.lastIndexOf("/");
    best=key==="header" ? {kind:"header"}
       : cut<0 ? {kind:"section",name:key}
       : {kind:"entry",name:key.slice(0,cut),i:+key.slice(cut+1)};
  }
  return best;
}
/* selectionchange rather than click: the caret arrives by arrow key and by
   typing at least as often as by mouse. Nothing happens unless it has crossed
   into a different block, so walking within one entry costs nothing. */
document.addEventListener("selectionchange",()=>{
  if(S.view!=="cvs"||S.tab!=="yaml") return;
  const ta=$("#yaml");
  if(document.activeElement!==ta) return;
  const line=ta.value.slice(0,ta.selectionStart).split("\n").length-1;
  const at=blockAtLine(line);
  if(at&&!sameSel(at,S.sel)) select(at,"yaml");
});
function markYamlSelection(){
  const wrap=$(".edwrap"), ta=$("#yaml");
  if(!wrap||!ta) return;
  let band=wrap.querySelector(".yband");
  const span=selectedLines();
  if(!span){ if(band) band.remove(); return }
  if(!band){
    band=document.createElement("div");
    band.className="yband";
    wrap.insertBefore(band,wrap.firstChild);
  }
  /* Line height and padding come from the computed style rather than repeating
     the numbers here, so the band cannot drift if the type changes. */
  const cs=getComputedStyle(ta);
  const lh=parseFloat(cs.lineHeight), top=parseFloat(cs.paddingTop);
  band.style.top=(top+span[0]*lh)+"px";
  band.style.height=(Math.max(1,span[1]-span[0])*lh)+"px";
  band.style.transform="translateY("+(-ta.scrollTop)+"px)";
}
/* Keep it pinned while the source scrolls under it. */
$("#yaml").addEventListener("scroll",()=>{
  const band=$(".edwrap .yband");
  if(band) band.style.transform="translateY("+(-$("#yaml").scrollTop)+"px)";
});

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
    cjkfonts.register()

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
