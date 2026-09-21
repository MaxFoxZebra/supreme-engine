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

try:
    import cv_map
except ImportError:  # clicking the page is a bonus, not a requirement
    cv_map = None

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
VERSION = "0.7.0"

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
# Three clients, three config formats, and TOML twice over is not one format.
# Claude Desktop keeps JSON. OpenAI keeps TOML at ~/.codex/config.toml, which
# the ChatGPT desktop app, Codex CLI and the IDE extension all read, so
# configuring it once covers all three. Mistral Vibe keeps TOML too, at
# ~/.vibe/config.toml, but as an array of tables with the server's name inside
# each one rather than in its header, so it needs its own reader and writer.
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


# --------------------------------------------------------------------------
# Company logos
#
# Local files only. The app makes no network calls, and pointing an <img> at a
# remote logo would quietly break that: every render would tell someone else's
# server which companies you are applying to.
#
# So a logo is a file in the workspace, and a job names it. A company with no
# logo gets a monogram instead, which is most of them and has to look
# deliberate rather than broken.
# --------------------------------------------------------------------------

LOGO_DIR = "assets/logos"
LOGO_TYPES = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"}


def logo_dir() -> Path:
    return WORKSPACE / "assets" / "logos"


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
    safe = "".join(c for c in company.lower() if c.isalnum() or c in "-_") or "logo"
    dest = logo_dir() / f"{safe}{src.suffix.lower()}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return {"ok": True, "logo": dest.name, "path": rel(dest),
            "kb": round(dest.stat().st_size / 1024, 1)}


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
        if not is_cv_yaml(f):
            continue
        path = rel(f)
        out.append({"path": path, "label": label, "group": group,
                    "mtime": f.stat().st_mtime, "ai": last_ai(path),
                    "base": (docs.get(path, {}).get("base") or {}).get("path")})
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
            "prov": provenance(path, plain)}


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

    def starts_of(node) -> list[int]:
        try:
            return [node.lc.key(k)[0] for k in node]
        except Exception:
            return []

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
    if cv_map is None or not result.get("typ"):
        return None
    try:
        outline = outline_of(load_doc(source).get("data"))
        if not outline:
            return None
        return cv_map.build_map(Path(result["typ"]), outline, source.parent)
    except Exception:
        return None


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
    blocks = block_map(result, source)
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
            "/api/render": {"post": {"summary":
                "Render to PDF and PNG, with a map of where each block landed",
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
            "/api/alerts": {"get": {"summary":
                "Applications needing attention: interviews due, follow-ups "
                "due, interviews with no outcome, and silence since applying",
                "responses": ok}},
            "/api/jobs/export": {"get": {"summary": "Export every job as JSON or CSV",
                "parameters": [{"name": "format", "in": "query",
                                "schema": {"type": "string", "enum": ["json", "csv"]}}],
                "responses": ok}},
            "/api/ai": {"get": {"summary":
                "Whether each AI client is wired up to this build and workspace",
                "responses": ok}},
            "/api/ai/connect": {"post": {"summary":
                "Add this workspace to one AI client's MCP config",
                "requestBody": body({"client": {"type": "string",
                                                "enum": ["claude", "openai", "mistral"]}}),
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
                jobs_out = jobstore.list_jobs(
                    WORKSPACE, q.get("status", [None])[0], q.get("q", [None])[0],
                    q.get("node", [None])[0])
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
            if u.path == "/api/ai":
                return self._json({"clients": ai_clients()})
            if u.path == "/api/pulse":
                return self._json(pulse())
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
                wrote = {}
                if "yaml" in payload:
                    wrote = write_doc(p, payload["yaml"], "save")
                elif "patches" in payload:
                    wrote = apply_patches(p, payload["patches"], "save")
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
                # Which CV this was tailored from is the whole basis of "what
                # did this change from the base", and the Base on picker knew
                # it all along -- it was simply thrown away on write.
                note_lineage(dest, rel(safe_path(src)) if src else None)
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
  --fn-band:.34; --fn-flow:.45;
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
  --fn-band:.42; --fn-flow:.34;
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
    --fn-band:.42; --fn-flow:.34;
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

/* ---------- title bar (46px) ------------------------------------------- */
#chrome{
  position:relative;
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

/* Centred on the window, not on whatever space the buttons left over --
   otherwise it drifts as the per-view actions change width. */
.doctitle{position:absolute;left:50%;transform:translateX(-50%);
  display:flex;align-items:baseline;gap:9px;max-width:38%;min-width:0;
  overflow:hidden;pointer-events:none}
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

/* The AI clients sit in the chrome because whether they are connected is a
   running state of the app, not a setting you visit once. One mark each, each
   with its own dot. Claude's mark keeps its own colour so it reads as Claude's
   rather than ours; the placeholder ring takes the chrome's. */
.cbtn.ai{display:flex;align-items:center;gap:10px;padding:4px 9px}
.aic{display:flex;align-items:center;gap:4px}
.aic svg{flex:none}
.aic[data-client=claude] svg{color:#D97757}
.aic .dot{width:6px;height:6px;border-radius:50%;background:var(--dot-idle);
  flex:none;transition:background .15s}
.aic[data-state=connected] .dot{background:var(--fn-won)}
.aic[data-state=elsewhere] .dot,
.aic[data-state=other-workspace] .dot,
.aic[data-state=unreadable] .dot{background:var(--bad)}
.aic[data-state=absent] svg,.aic[data-state=unknown] svg{opacity:.45}
.pbtn{font-size:12px;color:var(--c800);padding:5px 13px;border-radius:5px;background:var(--acc);
  font-weight:500;white-space:nowrap}
.pbtn:hover:not(:disabled){background:var(--acc-hover)}

/* ---------- shell ------------------------------------------------------ */
main{flex:1;min-height:0;display:flex;background:var(--app)}
.view{flex:1;display:flex;min-height:0;min-width:0;position:relative}
.rail{flex:none;background:var(--c650);border-right:1px solid #000;display:flex;
  flex-direction:column;min-height:0;overflow-y:auto}
.rail-cvs{width:230px} .rail-jobs{width:196px;padding:12px 7px;gap:1px}
.rail-label{padding:14px 13px 7px;font-size:10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--c300)}
.rail-jobs .rail-label{padding:5px 9px 7px}
.rail-jobs .rail-label+.rail-label,.rail-jobs .rail-label:not(:first-child){padding-top:16px}
.rail-list{display:flex;flex-direction:column;padding:0 7px}
/* group headings inside a rail list: quieter than the rail's own label, so
   the documents stay the thing you read and the kinds just separate them */
.rail-sub{padding:12px 10px 4px;font-size:10px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--c300)}
.rail-list>.rail-sub:first-child{padding-top:2px}
/* a document that belongs to an application wears a small ochre tie */
.row .tie{width:5px;height:5px;border-radius:50%;background:var(--acc);
  flex:none;opacity:.75}


/* a sidebar row: 3px marker, label, mono count */
.row{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:5px;
  text-align:left;width:100%}
.row:hover{background:var(--c550)}
.row .mark{width:3px;height:15px;border-radius:2px;background:transparent;flex:none}
.row .lbl{font-size:13px;color:var(--c100);flex:1;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.row .ct{font-size:10.5px;color:var(--c300);flex:none}
/* Attention borrows the ochre already used for an overdue follow-up rather
   than introducing a second warning colour. The section hides itself when
   every bucket is empty, so its presence is the signal and it does not need
   to shout. The count carries the colour; the labels stay ordinary text. */
.rail-label.attn{color:var(--acc-text)}
#attentionlist .row .ct{color:var(--acc-text);font-variant-numeric:tabular-nums}
#attentionlist .row.sel .ct{color:var(--acc-text-dark)}
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
.budget .bar i{height:5px;flex:1;background:var(--c300);border-radius:1px}
.budget .bar i.on{background:var(--acc)}
.budget .cap{font-size:11.5px;color:var(--c200);line-height:1.4}

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
.pgmark[data-by=ai]{color:#8a877f;font-size:9px}
.pgmark[data-by=you]{display:none}

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

.pane{flex:1;min-height:0;overflow:auto}
.pane-page{display:grid;justify-items:center;align-content:start;padding:26px}
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
.formblock{border-radius:4px;transition:background .12s,box-shadow .12s}
.formblock.on{background:var(--acc-wash);box-shadow:inset 3px 0 0 var(--acc)}
.entry.formblock.on{border-left-color:transparent}
.entry-hd{font-size:12.5px;font-weight:600;margin-bottom:8px;display:flex;gap:8px;
  align-items:baseline}
.entry-hd button{font-size:12px;color:var(--acc-text);margin-left:auto}
.entry-hd button:hover{text-decoration:underline}

.fg{display:grid;grid-template-columns:70px 1fr;gap:8px 10px;align-items:center}
.fg.wide{grid-template-columns:120px 1fr;align-items:start}
.fg.w88{grid-template-columns:88px 1fr;gap:10px 12px}
.fg>label{font-size:12px;font-weight:500;color:var(--t600);text-align:left;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fg.wide>label{padding-top:6px}
/* background-COLOR, not the shorthand: the shorthand resets background-image
   and silently strips the chevron off every select. */
.inp,.fg input,.fg select,.fg textarea{background-color:var(--field);
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
.insp-cvs{width:312px} .insp-funnel{width:296px}

/* ---------- the application peek ----------------------------------------
   Sized to the record rather than to a habit: wide enough for two columns of
   fields, capped so it never swallows the list it belongs to. It is not modal
   -- the table underneath stays live, and clicking another row moves the peek
   to it rather than stacking a second one. */
.peek{position:absolute;top:0;right:0;bottom:0;z-index:40;
  width:clamp(420px,46vw,860px);display:flex;flex-direction:column;
  background:var(--panel);border-left:1px solid var(--rule-strong);
  box-shadow:-18px 0 40px -24px rgba(0,0,0,.55);animation:peekin .12s ease-out}
@keyframes peekin{from{transform:translateX(10px);opacity:.4}to{transform:none;opacity:1}}
.peek-head{flex:none;display:flex;align-items:center;gap:14px;padding:0 6px 0 16px;
  height:40px;background:var(--bar);border-bottom:1px solid var(--rule-strong)}
.peek-who{flex:1;min-width:0;display:flex;align-items:baseline;gap:9px}
.peek-who b{font-size:13.5px;font-weight:600;color:var(--t900);flex:none;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%}
.peek-who span{font-size:12.5px;color:var(--t600);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.peek-nav{flex:none;display:flex;align-items:center;gap:2px}
.peek-nav button{width:26px;height:26px;border-radius:5px;color:var(--t500);
  font-size:13px;line-height:1}
.peek-nav button:hover:not(:disabled){background:var(--paper-hover);color:var(--t900)}
.peek-nav #jpk-idx{font-size:11px;color:var(--t500);padding:0 5px;min-width:44px;
  text-align:center}
.peek-nav #jpk-close{margin-left:6px}
.peek-body{flex:1;min-height:0;overflow-y:auto;padding:16px;
  display:flex;flex-direction:column;gap:16px}
/* The peek is absolutely positioned, so the table has to be told to stop
   underneath it. Giving way rather than being covered means the row you are
   arrowing through stays readable beside the record it opened. */
.peeking .tablewrap{margin-right:clamp(420px,46vw,860px)}
@media(max-width:1100px){ .peeking .tablewrap{margin-right:0} }

/* Two columns once there is room for two. Below that it stacks, which is the
   old behaviour and still correct on a small window. */
.peek .fg2{display:grid;grid-template-columns:78px minmax(0,1fr);gap:9px 11px;
  align-items:center}
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
.blabel{font-size:10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--t500);font-weight:500}

.card{border:1px solid var(--bd-field);border-radius:4px;background:var(--field);overflow:hidden}
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
#status{height:23px;flex:none;display:flex;align-items:center;gap:8px;padding:0 16px;
  background:var(--c700);font-size:10.5px;color:var(--c300);user-select:none}
#status .sep::before{content:"\00b7"}
/* a decision you just took about someone else's edit, not routine chatter */
#st-right.said{color:var(--acc-text);font-weight:500}
/* the MCP boundary having just stopped something */
#st-right.blocked{color:var(--bad);font-weight:500}
#status .warn{color:var(--acc-text-dark)}

/* ---------- jobs table --------------------------------------------------- */
.tablewrap{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0;
  background:var(--app)}
.thead,.trow{display:grid;
  grid-template-columns:minmax(0,1.25fr) minmax(0,1.5fr) minmax(0,1.15fr) 186px 86px 96px;
  align-items:center}
.thead{height:26px;flex:none;background:var(--bar);border-bottom:1px solid var(--rule-strong);
  font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--t500)}
.thead>*,.trow>*{padding:0 12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* the company mark, and the column it leads */
.co{display:flex;align-items:center;gap:9px;min-width:0}
.con{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.colog{width:20px;height:20px;border-radius:4px;flex:none;object-fit:contain;
  background:var(--bar)}
span.colog{display:grid;place-items:center;font-size:9.5px;font-weight:600;
  color:#fff;letter-spacing:.02em}
.trow .role b{font-weight:400}

.tbody{flex:1;min-height:0;overflow-y:auto}
.trow{height:32px;font-size:12.5px;border-bottom:1px solid var(--bd-inner);width:100%;
  text-align:left;color:var(--t900)}
.trow:nth-child(even){background:var(--row-alt)}
.trow:hover{background:var(--row-hover)}
.trow .role{display:flex;gap:8px;align-items:baseline;min-width:0}
.trow .role b{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trow .role i{font-style:normal;font-size:11.5px;color:var(--t500);flex:none}
.trow .docs{font-size:11px;color:var(--acc-text)}
/* the most repeated string in the table, so it has to clear AA */
.trow .docs.none{color:var(--t500);font-size:12px}
.trow.sel .docs.none{color:#cdc6b5}
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
.fn-left{flex:1;min-width:0;display:flex;flex-direction:column;
  background:var(--app);min-height:0}
/* Matches the inspector's header exactly, so the rule beneath the two of
   them is one line across the window rather than two that disagree. */
.fn-bar{height:33px;flex:none;display:flex;align-items:center;padding:0 24px;
  background:var(--bar);border-bottom:1px solid var(--rule-strong)}
#chart{flex:1;min-height:0;padding:18px 24px 22px;overflow:auto}
#fn-jobs{flex:none;max-height:42%;overflow-y:auto;border-top:1px solid var(--rule);
  padding:12px 24px 18px}
.fn-hint{margin:0;font-size:12px;color:var(--t500)}
.fn-jhead{display:flex;align-items:baseline;gap:10px;margin-bottom:8px}
.fn-jhead b{font-size:12.5px}
.fn-jhead span{font-size:11.5px;color:var(--t500)}
.fn-jlist{display:flex;flex-direction:column;border:1px solid var(--bd-field);
  border-radius:8px;overflow:hidden}
.fn-jrow{display:grid;grid-template-columns:20px minmax(0,1fr) minmax(0,1.4fr) 180px;
  align-items:center;gap:9px;padding:8px 12px;background:var(--field);
  text-align:left;width:100%}
.fn-jrow+.fn-jrow{border-top:1px solid var(--rule)}
.fn-jrow:hover{background:var(--row-hover)}
.fj-role{font-size:12.5px;color:var(--t600);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.fn-head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.fn-head b{font-size:12.5px;font-weight:600}
.fn-head span{font-size:12px;color:var(--t600)}
#chart svg{width:100%;height:auto;display:block}
.sk-link{transition:opacity .15s;fill:none;stroke-opacity:var(--fn-band)}
/* The moving highlight. It is the band's own colour at a little more strength
   rather than a white sheen, so a ribbon looks like more of itself passing
   through rather than like something shining on top of it. */
.sk-flow{fill:none;pointer-events:none;stroke-width:1.7;
  stroke-opacity:var(--fn-flow);stroke-linecap:round;
  animation:sk-flow linear infinite}
/* One filter on the group rather than one per streamline: there are up to
   eight per band and a filter each would be paid for on every frame. */
.sk-flows{filter:blur(1.6px)}
@keyframes sk-flow{to{stroke-dashoffset:calc(var(--len) * -1)}}
@media(prefers-reduced-motion:reduce){ .sk-flow{display:none} }
.sk-hit{cursor:pointer}
.sk-hit:hover .sk-node{opacity:.8}
/* Dimming is meant to keep the rest of the chart as context. At .25 it took
   the labels with it -- a dimmed node's name measured 1.6:1 -- so selecting
   anything made every other stage unreadable, which is the opposite of
   context. Bands recede; text stays legible. */
.sk-dim{opacity:.4}
.sk-label.sk-dim{opacity:.72}
.sk-label{font:12px 'IBM Plex Sans',sans-serif;fill:var(--fn-label)}
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
.seg.paper{background:var(--seg-track);width:fit-content}
.seg.paper button{color:var(--t700);padding:4px 15px;font-size:12.5px}
.seg.paper button[aria-selected=true]{background:var(--seg-on);color:var(--t900);font-weight:500}
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
.themegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));
  gap:14px}
.thumbwrap{display:flex;flex-direction:column;gap:8px;align-items:center}
.thumb{width:100%;aspect-ratio:1.25;background:var(--page);border:1px solid var(--rule);
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
  border-radius:50%;background:var(--knob);box-shadow:0 1px 3px rgba(30,26,18,.45);margin-top:-5.5px;
  cursor:pointer}
.slider input[type=range]::-moz-range-thumb{width:14px;height:14px;border:0;border-radius:50%;
  background:var(--knob);box-shadow:0 1px 3px rgba(30,26,18,.45);cursor:pointer}
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
.set-wrap{flex:1;min-height:0;overflow-y:auto;padding:30px 34px 72px}
.set-inner{display:grid;grid-template-columns:160px minmax(0,1fr);gap:40px;
  max-width:980px;margin:0 auto}
.set-rail{display:flex;flex-direction:column;gap:1px;position:sticky;top:0;align-self:start}
.set-rail button{text-align:left;padding:6px 10px;font-size:13px;color:var(--t600);
  border-radius:5px}
.set-rail button:hover{background:var(--bar);color:var(--t900)}
.set-rail button[aria-selected=true]{color:var(--t900);font-weight:500;background:var(--bar)}
.sp h3{font-size:15px;font-weight:600;margin:0 0 4px}
.sp-lede{color:var(--t600);font-size:12.5px;line-height:1.6;margin:0 0 16px;max-width:62ch}
.sp-note{color:var(--t600);font-size:12px;line-height:1.6;margin:14px 0 0;max-width:62ch}
.srow{display:flex;align-items:center;gap:28px;padding:14px 0;border-top:1px solid var(--rule)}
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
.sp-sub{display:flex;align-items:center;gap:10px;font-size:11px;font-weight:600;
  color:var(--t500);margin:30px 0 12px;text-transform:uppercase;letter-spacing:.08em}
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
.client[data-client=claude] .badge{background:rgba(217,119,87,.14);color:#D97757}
.client[data-client=openai] .badge{color:var(--t900)}
.client[data-client=mistral] .badge{background:rgba(250,80,15,.12)}
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
  .rail-cvs{width:200px}.insp-cvs{width:270px}
  .peek{width:auto;left:0}
  .themegrid{grid-template-columns:repeat(3,1fr)}
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
try{var _p=JSON.parse(localStorage.getItem("cvstudio.prefs")||"{}");
    if(_p.appearance==="dark"||_p.appearance==="light")
      document.documentElement.dataset.theme=_p.appearance}catch(e){}
</script>

<!-- Marks for the AI clients. The Claude one is as published by Anthropic, and
     identifies that integration and nothing else: see THIRD-PARTY-NOTICES.md. -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
  <symbol id="claude-mark" viewBox="0 0 24 24"><path fill="currentColor"
    fill-rule="nonzero" d="M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z"/></symbol>

  <symbol id="openai-mark" viewBox="0 0 24 24"><path fill="currentColor"
    fill-rule="evenodd" d="M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z"/></symbol>
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
  <div class="seg" id="nav" role="tablist" aria-label="View">
    <button role="tab" data-view="cvs" aria-selected="true">CVs</button>
    <button role="tab" data-view="jobs" aria-selected="false">Jobs</button>
    <button role="tab" data-view="funnel" aria-selected="false">Funnel</button>
  </div>

  <div class="doctitle" id="doctitle"><span class="t"></span><span class="f mono"></span></div>
  <div class="grow" id="chrome-gap"></div>

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
  <button class="cbtn" id="btn-pdf" disabled>Export PDF&#8230;</button>
  <button class="pbtn" id="btn-render">Render</button>
  <button class="pbtn" id="btn-newjob" hidden>New job&#8230;</button>
  <button class="cbtn ai" id="btn-ai" title="AI clients" aria-label="AI clients">
    <span class="aic" data-client="claude" data-state="unknown"><svg width="13"
      height="13" viewBox="0 0 24 24" aria-hidden="true"
      ><use href="#claude-mark"/></svg><i class="dot"></i></span>
    <span class="aic" data-client="openai" data-state="unknown"><svg width="13"
      height="13" viewBox="0 0 24 24" aria-hidden="true"
      ><use href="#openai-mark"/></svg><i class="dot"></i></span>
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
        <div class="seg light" id="edtabs" role="tablist" aria-label="Preview mode">
          <button role="tab" data-tab="page" aria-selected="true">Page</button>
          <button role="tab" data-tab="form" aria-selected="false">Form</button>
          <button role="tab" data-tab="yaml" aria-selected="false">YAML</button>
        </div>
        <button class="prov" id="provchip" hidden></button>
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
      <div id="attentionwrap" hidden>
        <div class="rail-label mono attn">Attention</div>
        <div id="attentionlist"></div>
      </div>
      <div class="rail-label mono">Status</div>
      <div id="statuslist"></div>
      <div class="rail-label mono">Saved</div>
      <div id="savedlist"></div>
    </aside>
    <div class="tablewrap">
      <div class="thead"><span>Company</span><span>Role</span>
        <span>Documents</span><span>Status</span>
        <span>Applied</span><span>Follow-up</span></div>
      <div class="tbody" id="jobrows"></div>
    </div>
    <!-- A peek rather than a rail. The old 284px column was fixed at every
         window size, so a thirteen-field record was stacked into a sliver
         while the table beside it had a thousand pixels it did not need. This
         opens over the table at a width that fits the record, leaves the rows
         that identify the application visible to its left, and moves between
         them with the arrow keys without ever closing. -->
    <aside class="peek" id="jpeek" hidden aria-label="Application">
      <div class="peek-head">
        <div class="peek-who" aria-live="polite"><b id="jinsp-title"></b><span id="jinsp-sub"></span></div>
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

  <!-- ------------------------------------------------------------- Funnel -->
  <section class="view" id="v-funnel" hidden>
    <div class="fn-left">
      <div class="fn-bar"><div class="fn-head"><b id="fn-total"></b>
        <span id="fn-sub"></span></div></div>
      <div id="chart"></div>
      <div id="fn-jobs"></div>
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
      <div class="fg w88" id="dz-basics" style="max-width:560px"></div>
      <div id="dz-advanced" style="max-width:560px"></div>
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
            <p>The same list as Attention in the Jobs view, read out loud</p></div>
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
/* Which statuses still have somewhere to go -- used for the saved filters. */
const LIVE_STATUS=new Set(Object.keys(STATUS_TONE).filter(
  k=>STATUS_TONE[k]==="live"||STATUS_TONE[k]==="waiting"));
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
  /* The gap is what pins the action cluster to the right edge, and that has
     to hold on every tab or the gear moves when you switch. */
  $("#search").hidden = v!=="jobs";
  $("#range").hidden = v!=="funnel";
  $("#btn-design").hidden = v!=="cvs";
  $("#btn-pdf").hidden = v!=="cvs";
  $("#btn-render").hidden = v!=="cvs";
  $("#btn-newjob").hidden = v!=="jobs";
  if(v==="jobs"){ loadJobs(); loadAlerts() }
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
  if(c.state==="absent") return "Not set up. Two steps, below.";
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
      '<div class="go"><button class="obtn'+
        (st==="connected"&&c.last_seen?"":" primary")+
        '" data-connect="'+c.id+'">'+
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
  ai:"An AI client",you:"You"};
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
  return b?b.path.split("/").pop():"the base CV";
};

function setProv(prov){
  S.prov=prov||null;
  if(S.prov) S.prov.baseSet=new Set(S.prov.from_base||[]);
  paintProv();
}

/* The chip: the whole document's answer, on every tab. */
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

/* ---- the workspace changing underneath us -------------------------------
   Claude edits the same files this app has open, so the editor has to assume
   it is not the only writer. Polling one stat per document is cheap, and it is
   the difference between picking up the model's work and silently saving over
   it. */
let pulseTimer=null;
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

  /* A document appearing or disappearing means Claude created or removed one. */
  const names=o=>JSON.stringify(Object.keys(o.docs).sort());
  if(names(before)!==names(p)){
    try{ renderDocs((await api("/api/state")).documents) }catch(e){}
  }
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
  loadAlerts();
  if(d.documents.length) openDoc(d.documents[0].path);
  else{
    $("#pane-page").innerHTML='<div class="empty"><h3>No CVs yet</h3>'+
      '<p>Ask Claude or ChatGPT to write one, or start from a blank file here. '+
      'The model does the writing; this is where you see the page and fix what '+
      'it got wrong.</p>'+
      '<p>Either way it is a plain YAML file in your workspace, so you always own '+
      'it. No database, no account, nothing leaves your machine.</p>'+
      '<div class="cta"><button class="sbtn primary" id="firstcta">Create a CV</button>'+
      '<button class="sbtn" id="firstai">Connect an AI client</button></div></div>';
    $("#firstcta").onclick=()=>newDocumentSheet();
    $("#firstai").onclick=()=>$("#btn-ai").click();
    $("#btn-render").disabled=true;
    buildOutline();   /* nothing is open, so the Outline heading goes too */
  }
  loadAI();
  pulse();
  setInterval(pulse,2500);
  paintStatus();
  if(d.first_run) toast("Workspace created at "+d.workspace+
    ". Connect Claude or ChatGPT to it from Settings");
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
  /* Every RenderCV entry type keeps its headline under a different key, and
     a publication or a bullet reading "entry 3" in the outline is no use. */
  return it.company||it.institution||it.name||it.title||it.label||it.position||
    it.bullet||("entry "+(i+1));
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
    host.innerHTML=groups.map(g=>{
      const rows=docs.filter(d=>d.group===g).map(d=>{
        const pp=S.pages[d.path];
        const job=S.jobs.find(j=>j.cv_path===d.path||j.letter_path===d.path);
        return '<button class="row'+(d.path===S.path?" sel":"")+
          '" data-path="'+esc(d.path)+'" title="'+esc(d.path)+
          (d.base?"\ntailored from "+esc(d.base):"")+
          (d.ai?"\n"+esc(whoLabel(d.ai))+" worked on this "+
            ago(d.ai.at*1000)+" ago":"")+
          (job?"\n"+esc(job.title+" · "+job.company):"")+'">'+
          '<span class="mark"></span>'+
          '<span class="lbl">'+esc(d.label)+'</span>'+
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
  closeOverlays();
  setView("cvs");
  S.path=path; S.dirty=false; S.savedAt=null; S.sel=null; S.openSection=null;
  S.prov=null; $("#provchip").hidden=true;
  S.render=null; S.renderMs=null; S.fill=null; S.themePages={}; S.zoomAuto=true;
  hideExternalChange();
  /* The Design panel still holds the last document's controls, and its inputs
     are read straight into the patch list. Empty it until it is rebuilt. */
  $("#dz-basics").innerHTML=""; $("#dz-advanced").innerHTML="";
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
function select(sel){
  if(sel&&sel.kind==="section"){
    const list=((S.data.cv.sections||{})[sel.name])||[];
    S.openSection=sel.name;
    sel=list.length?{kind:"entry",name:sel.name,i:0}:{kind:"section",name:sel.name};
  }else if(sel&&sel.kind==="entry"){ S.openSection=sel.name }
  S.sel=sel;
  buildOutline();
  buildInspector();
  trackSelectionOnPage();
  if(S.tab==="form") buildForm();   /* carry the mark into the form */
  if(S.tab==="yaml") markYamlSelection();
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
  return '<label title="'+esc(label)+'">'+esc(String(label).replace(/_/g," "))+
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
  head.textContent=sectionLabel(sel.name)+" · "+entryTitle(it,sel.i);
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
  return '<div class="block"><span class="blabel mono">'+esc(label.replace(/_/g," "))+
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
  const show=body.querySelector("[data-show-job]");
  if(show) show.onclick=()=>{ selectJob(show.dataset.showJob); setView("jobs") };
  const link=body.querySelector("[data-link-job]");
  if(link) link.onclick=()=>linkJobSheet();
  const unlink=body.querySelector("[data-unlink-job]");
  if(unlink) unlink.onclick=async()=>{
    const j=linkedJob(); if(!j) return;
    const key=j.cv_path===S.path?"cv_path":"letter_path";
    try{
      await post("/api/jobs/update",{id:j.id,[key]:null});
      await loadJobs(); buildInspector(); renderDocs(S.state.documents);
      toast("Unlinked");
    }catch(e){ toast(e.message,true) }
  };
}

/* Attaching a document to an application it was written for, from the document
   side. The Jobs screen can already pick a document for an application; this is
   the same join made from the end you are more often standing at. */
function linkJobSheet(){
  const isLetter=(S.state.documents||[]).some(
    d=>d.path===S.path&&d.group==="Cover letters");
  const key=isLetter?"letter_path":"cv_path";
  const open=S.jobs.filter(j=>!j[key]);
  openSheet(
    '<div><h3>Link to an application</h3><p>'+esc(docLabel(S.path))+
    ' becomes the '+(isLetter?"cover letter":"CV")+' on the application you '+
    'pick. A document belongs to one application, and an application takes one '+
    'of each.</p></div>'+
    (open.length
      ? '<div class="fg w88"><label>Application</label><select id="lj-job">'+
        open.map(j=>'<option value="'+esc(j.id)+'">'+esc(j.title)+' · '+
          esc(j.company)+'</option>').join("")+'</select></div>'
      : '<div class="fg w88"><p class="note muted">Every application already has '+
        'one. Start a new application, or swap the document over from the Jobs '+
        'screen.</p></div>')+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn" id="lj-new">New application…</button>'+
    (open.length?'<button class="sbtn primary" id="lj-ok">Link</button>':"")+
    '</div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#lj-new").onclick=()=>{ closeSheet(); newJobSheet({[key]:S.path}) };
  const ok=$("#lj-ok");
  if(ok) ok.onclick=async()=>{
    ok.disabled=true;
    try{
      await post("/api/jobs/update",{id:$("#lj-job").value,[key]:S.path});
      await loadJobs(); closeSheet(); buildInspector();
      renderDocs(S.state.documents); paintTitle();
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
function linkedBlock(){
  const j=linkedJob();
  let inner;
  if(j){
    inner='<div class="card"><div class="drow">'+
      '<span style="display:flex;align-items:center;gap:8px;min-width:0">'+
      '<span class="dot '+statusTone(j.status)+'"></span>'+
      '<span style="overflow:hidden;text-overflow:ellipsis">'+esc(j.company)+' · '+
      esc(prettyStatus(j.status))+'</span></span>'+
      '<span style="display:flex;gap:10px;flex:none">'+
      '<button class="alink" data-show-job="'+esc(j.id)+'">Show in Jobs</button>'+
      '<button class="alink" data-unlink-job>Unlink</button></span>'+
      '</div></div>';
  }else if(S.jready){
    inner='<div class="card"><div class="drow"><span class="muted">Not linked to an '+
      'application</span><button class="alink" data-link-job>Link…</button></div></div>';
  }else inner='';
  return inner?'<div class="block ruled"><span class="blabel mono">Linked application</span>'+
    inner+'</div>':'';
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
      if(it===null||typeof it!=="object"){
        h+='<div class="fg wide formblock'+selMark({kind:"entry",name:name,i:i})+'" data-block="'+esc(name)+'" data-bi="'+i+'">'+fieldRow("text "+(i+1),["cv","sections",name,i],it,
          {multi:true})+'</div>';
      }else{
        h+='<div class="entry formblock'+selMark({kind:"entry",name:name,i:i})+'" data-block="'+esc(name)+'" data-bi="'+i+'"><div class="entry-hd"><b>'+esc(entryTitle(it,i))+'</b>'+
          '<button data-focus="'+esc(name)+'" data-i="'+i+'">Inspect</button></div>'+
          '<div class="fg wide">'+Object.keys(it).map(k=>
            fieldRow(k,["cv","sections",name,i,k],it[k],{mono:MONO_KEYS.test(k)})).join("")+
          '</div></div>';
      }
    });
    h+='</div></details>';
  }
  $("#pane-form").innerHTML=h;
  revealSelected($("#pane-form"));
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
  if(S.tab==="yaml") markYamlSelection();
  if(S.tab==="form") buildForm();   /* re-read the model, in case the inspector moved on */
  if(S.tab==="page") trackSelectionOnPage();  /* the selection may have moved while away */
});
$("#z-in").onclick=()=>setZoom(S.zoom+.1);
$("#z-out").onclick=()=>setZoom(S.zoom-.1);
/* The readout is also the way back: once you have zoomed, one click refits. */
$("#z-lvl").onclick=()=>{ S.zoomAuto=true; paintPage() };
$("#pg-prev").onclick=()=>setPage(S.page-1);
$("#pg-next").onclick=()=>setPage(S.page+1);
function setZoom(z){ S.zoom=Math.min(3,Math.max(.2,z)); S.zoomAuto=false; paintPage() }
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
    /* Our own write, so take its timestamp: the poll must not read it back as
       somebody else having changed the file. */
    S.docMtime=r.mtime; hideExternalChange();
    S.dirty=false; S.savedAt=Date.now();
    setProv(r.prov);
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
function fitZoom(host,img){
  const w=(host.clientWidth-PAGE_GUTTER*2)/pageCssWidth(img);
  return Math.max(.2,Math.min(1.5,w));
}
function paintPage(){
  const host=$("#pane-page"), r=S.render;
  if(!r||!r.pngs.length){ $("#pg-idx").textContent="–"; return }
  const url=r.pngs[S.page]+tok();
  const draw=img=>{
    if(S.zoomAuto) S.zoom=fitZoom(host,img);
    img.style.width=Math.round(pageCssWidth(img)*S.zoom)+"px";
    $("#z-lvl").textContent=Math.round(S.zoom*100)+"%";
    $("#z-lvl").classList.toggle("auto",!!S.zoomAuto);
    paintHits();
  };
  host.innerHTML='<div class="pgwrap"><img class="pg" src="'+url+'" alt="Page '+
    (S.page+1)+'"></div>';
  const img=host.querySelector(".pg");
  if(img.complete&&img.naturalHeight) draw(img);
  else img.onload=()=>{ if(host.querySelector(".pg")===img) draw(img) };
  $("#pg-idx").textContent=(S.page+1)+" / "+r.pngs.length;
  $("#pg-prev").disabled=S.page===0;
  $("#pg-next").disabled=S.page>=r.pngs.length-1;
}
/* Re-fit while the window is being resized, but only while nobody has chosen a
   zoom of their own. */
addEventListener("resize",()=>{ if(S.zoomAuto&&S.tab==="page") paintPage() });

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
  const pageH=img.naturalHeight/2, pageW=img.naturalWidth/2;  /* 144dpi: 2px/pt */
  const box=(S.render&&S.render.map_box)||null;
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
  if(S.tab!=="page"||!S.render||!S.render.map) return;
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
  if(S.view==="cvs"&&S.path){ paintTitle(); buildInspector() }
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

/* Nothing outside this app knows when an interview is. No event is written to
   any calendar, by design, so this notification is the only thing that reaches
   the user when the window is not in front of them. Hence the one place the
   app speaks without being spoken to.

   Once a day at most, and only ever one line. A notification per application
   would be four notifications on a bad Monday, which is how people learn to
   turn them off. */
async function notifyAlerts(){
  const a=S.alerts;
  if(!a||!a.total) return;
  const N=window.__TAURI__&&window.__TAURI__.notification;
  if(!N) return;                       /* a plain browser during development */
  const key="cvstudio-notified", today=isoToday();
  let seen=null;
  try{ seen=localStorage.getItem(key) }catch(e){ return }
  const stamp=today+":"+ATTENTION.map(([k])=>a.counts[k]).join(",");
  if(seen===stamp) return;

  try{
    /* Asked for on the first alert that would actually be shown, not at boot.
       A permission prompt before the app has anything to say is the kind of
       thing people refuse on principle. */
    let granted=await N.isPermissionGranted();
    if(!granted) granted=(await N.requestPermission())==="granted";
    if(!granted) return;
    const parts=ATTENTION.filter(([k])=>a.counts[k])
      .map(([k,label])=>a.counts[k]+" "+label.toLowerCase());
    N.sendNotification({
      title:a.total===1?"One application needs attention"
                       :a.total+" applications need attention",
      body:parts.join(", "),
    });
    localStorage.setItem(key,stamp);
  }catch(e){}
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
      '<span class="co">'+companyMark(j)+'<span class="con">'+
        esc(j.company)+'</span></span>'+
      '<span class="role"><b>'+esc(j.title)+'</b></span>'+
      '<span>'+(docs?'<span class="docs mono" data-open="'+esc(j.cv_path)+'">'+docs+'</span>'
                    :'<span class="docs none">no CV yet</span>')+'</span>'+
      '<span class="st"><span class="dot '+statusTone(j.status)+'"></span>'+
        esc(prettyStatus(j.status))+'</span>'+
      '<span class="when'+(ap?"":" none")+'">'+(ap?esc(shortDate(ap)):"–")+'</span>'+
      '<span class="when'+(j.followup_date?(due?" due":""):" none")+'">'+
        (j.followup_date?esc(shortDate(j.followup_date)):"–")+'</span>'+
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
  ["status","Status","status"],["followup_date","Follow-up","date"],
  ["score","Fit","fit"],["source","Source","text"],
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

function drawJobInspector(){
  const j=S.jobs.find(x=>x.id===S.jsel);
  const peek=$("#jpeek"), head=$("#jinsp-title"), body=$("#jinsp-body");
  $("#v-jobs").classList.toggle("peeking",!!j);
  if(!j){ peek.hidden=true; return }
  peek.hidden=false;
  head.textContent=j.company;
  head.title=j.company;
  $("#jinsp-sub").textContent=j.title;
  $("#jinsp-sub").title=j.title;

  const rows=peekRows(), at=rows.findIndex(x=>x.id===j.id);
  $("#jpk-idx").textContent=at<0?"":(at+1)+" of "+rows.length;
  $("#jpk-prev").disabled=at<=0;
  $("#jpk-next").disabled=at<0||at>=rows.length-1;

  const field=([k,label,kind])=>{
    let ctl;
    if(kind==="status") ctl='<span class="statusctl"><span class="dot '+statusTone(j.status)+'"></span><select data-j="status">'+S.statuses.map(s=>
      '<option value="'+s+'"'+(s===j.status?" selected":"")+'>'+esc(prettyStatus(s))+
      '</option>').join("")+'</select></span>';
    else if(kind==="fit") ctl='<div class="fit" role="group" aria-label="Fit">'+
      [1,2,3,4,5].map(n=>'<button data-fit="'+n+'"'+((j.score||0)>=n?' class="on"':"")+
      ' title="'+n+' of 5" aria-label="'+n+' of 5"></button>').join("")+
      '<span class="fitv">'+(j.score?j.score+" / 5":"not rated")+'</span></div>';
    else ctl='<input data-j="'+k+'"'+(kind==="date"?' type="date"':"")+
      (kind==="number"?' type="number" class="mono"':"")+' value="'+
      esc(j[k]==null?"":j[k])+'">';
    return '<label>'+esc(label)+'</label>'+ctl;
  };
  const grid=JOB_GRID.map(field).join("");
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
  /* The posting itself. The tools have been storing this since the tracker was
     opened up -- add_job's own description says to paste the whole thing,
     because it is what a model writes against when you later ask it to tailor
     a CV for this job, by which time the page is usually gone. Nothing in the
     app has ever shown it. There is room for it now. */
  const posting_block=j.description
    ? '<div class="block grow"><span class="blabel mono">The posting</span>'+
      '<div class="posting">'+esc(j.description)+'</div></div>'
    : '<div class="block grow"><span class="blabel mono">The posting</span>'+
      '<p class="note muted">Not saved. Paste it in when you add an application, '+
      'or ask a model to -- it is what a tailored CV gets written against once '+
      'the advert is gone.</p></div>';
  const posting=j.url?'<div class="drow"><span class="muted">'+
    esc(j.url.replace(/^https?:\/\//,"").slice(0,40))+'</span>'+
    '<a class="alink" href="'+esc(j.url)+'" target="_blank" rel="noreferrer">Open</a></div>':"";

  const hist=(j.status_history||[]);
  const timeline=hist.length?'<div class="tl">'+hist.map(h=>
    '<div class="tli"><div class="spine"><i></i><u></u></div>'+
    '<div class="ev"><span>'+esc(prettyStatus(h.status))+'</span>'+
    '<span class="when mono">'+esc(shortDate(h.at))+'</span></div></div>').join("")+'</div>'
    :'<p class="note muted">No history yet.</p>';

  /* Ordered by how often you touch it, not by the order the columns happen to
     sit in the table. Status and Follow-up drive the Attention rail, so they
     lead; Notes is what you write in every time, so it is above the fold
     rather than under a history block that grows without limit; and the nine
     fields you set once at creation are folded away. */
  /* Two columns that both run the full height, so the peek is filled rather
     than a short stack sitting on top of half a screen of nothing. Notes takes
     whatever height is left over: it is the one field with no natural size and
     the one you write the most in. */
  body.innerHTML=
    '<div class="peek-grid">'+
      '<div class="col">'+
        '<div class="block"><span class="blabel mono">Where it stands</span>'+
          '<div class="fg2">'+grid+'</div></div>'+
        '<div class="block"><span class="blabel mono">Documents</span><div class="card">'+
          docRow("CV","cv_path","My CVs")+docRow("Cover letter","letter_path","Cover letters")+
          posting+'</div></div>'+
        '<details class="fold"><summary>Company, role and the rest</summary>'+
          '<div class="fg2" style="margin-top:11px">'+more+'</div></details>'+
        '<div class="block ruled foot-del"><button class="sbtn danger" id="job-del">'+
          'Delete this application</button></div>'+
      '</div>'+
      '<div class="col">'+
        '<div class="block"><span class="blabel mono">Notes</span>'+
          '<textarea data-j="notes" class="notes">'+esc(j.notes||"")+'</textarea></div>'+
        '<div class="block"><span class="blabel mono">History</span>'+timeline+'</div>'+
        posting_block+
      '</div>'+
    '</div>';

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
  if(S.funnel){ drawFunnel(); return paintFunnelJobs() }
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
  paintFunnelJobs();
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
  /* Fill the pane rather than stopping at an arbitrary cap: the sankey was
     using little over half the canvas and pooling the rest at the bottom. */
  const avail=(host.clientHeight||520)-30;
  const H=Math.max(300,Math.min(avail,nodes.length*46));
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

  /* Each ribbon is drawn twice: the band itself, and a dashed copy of the
     same geometry that travels along it. Sankey paths already run source to
     target, so animating the dash offset negative moves the highlight the way
     the applications move -- left to right, down the funnel.

     The dash pattern is scaled to the band's own width so a thick ribbon gets
     long slow swells and a thin one gets short ones, which is what stops the
     whole chart pulsing in lockstep. A per-link delay staggers them further.
     Anyone who has asked their system not to animate gets none of it. */
  /* Streamlines, drawn along the flow rather than across it.
  
     A dashed stroke lays its dashes out along the path, but each one is drawn
     at the full stroke width -- so a short dash on a wide ribbon comes out as
     a bar standing across the current, and a row of them reads as rungs on a
     ladder, which is the opposite of moving water. The first version of this
     made exactly that mistake.
  
     Water wants marks elongated in the direction of travel. So each band gets
     a handful of thin streamlines instead: copies of the same path shifted
     vertically to sit inside the ribbon, stroked narrow, with dashes long
     enough to read as streaks. The flow is horizontal, so a vertical shift of
     the centreline is a parallel line within the band. */
  const DASH=150, GAP=175, LEN=DASH+GAP;
  const flow=(l,i)=>{
    const w=Math.max(1,l.width), d=path(l);
    /* Enough to read as a surface rather than as scratches on one. Capped at
       eight the wide bands came out with fifty pixels between streamlines,
       which is not a current, it is a scuff. */
    const n=Math.max(1,Math.min(14,Math.round(w/13)));
    let out="";
    for(let k=0;k<n;k++){
      const at=n===1?0:(k/(n-1))-0.5;          /* -0.5 .. 0.5 across the band */
      const dy=(at*(w-2.5)).toFixed(2);
      /* Each streamline drifts at its own rate, which is what keeps the band
         from sliding as one rigid sheet. */
      const dur=(3.4+((i*3+k)%6)*0.5).toFixed(2);
      const lag=(((i*7+k*5)%13)*0.42).toFixed(2);
      out+='<path class="sk-flow '+fnBand(l.tid)+(touches(l)?"":" sk-dim")+
        '" transform="translate(0,'+dy+')" d="'+d+
        '" stroke-dasharray="'+DASH+' '+GAP+'" style="--len:'+LEN+
        'px;animation-duration:'+dur+'s;animation-delay:-'+lag+'s"/>';
    }
    return out;
  };
  const bands=graph.links.map(l=>
    '<path class="sk-link '+fnBand(l.tid)+
    (touches(l)?"":" sk-dim")+'" d="'+path(l)+
    '" stroke-width="'+Math.max(1,l.width)+'"><title>'+
    esc(l.source.label)+' → '+esc(l.target.label)+': '+l.value+
    '</title></path>').join("")+
    '<g class="sk-flows">'+graph.links.map(flow).join("")+'</g>';

  /* The bar alone is a 9px target, so each node gets a hit area over its label
     too -- clicking a band is how you get to the jobs behind it. */
  const bars=graph.nodes.map(n=>{
    const h=Math.max(1,n.y1-n.y0), dim=S.fnode&&S.fnode!==n.id?" sk-dim":"";
    return '<g class="sk-hit'+dim+'" data-node="'+esc(n.id)+'" role="button" tabindex="0">'+
      '<title>'+esc(n.label)+': '+n.count+'. Click to list them</title>'+
      '<rect x="'+(n.x0-6)+'" y="'+(n.y0-8)+'" width="'+((n.x1-n.x0)+PAD)+'" height="'+
      (h+16)+'" fill="transparent"/>'+
      '<rect class="sk-node '+fnTone(n.id)+'" x="'+n.x0+'" y="'+n.y0+'" width="'+
      (n.x1-n.x0)+'" height="'+h+'"/></g>';
  }).join("");

  const labels=graph.nodes.map(n=>{
    const dim=S.fnode&&S.fnode!==n.id?"sk-dim":"";
    const text=esc(n.label)+" · "+n.count;
    /* The first column has no room to its right, so its label sits above. */
    /* Above the node's top edge, never beside it. Placing a label at the
       node's own vertical middle put it on top of that node's *first outgoing
       band* -- so "Applied · 54" sat on a ribbon worth 8, and "Offer · 5" on
       one worth 2. Every label was printed over a number that contradicted
       it. The first column already did the right thing. */
    return '<text class="sk-label '+dim+'" x="'+(n.x0<8?n.x0:n.x1+9)+'" y="'+
      (n.y0-7)+'">'+text+'</text>';
  }).join("");

  host.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet" '+
    'role="img" aria-label="Application funnel">'+bands+bars+
    '<g pointer-events="none">'+labels+'</g></svg>';
  host.querySelectorAll("[data-node]").forEach(g=>{
    const pick=()=>fnPick(g.dataset.node);
    g.onclick=pick;
    g.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); pick() } };
  });
}

/* Clicking a band used to throw you onto the Jobs screen, which answered the
   question and lost the chart that raised it. The answer belongs underneath it:
   the shape stays on screen while you read what is behind the part you touched. */
function fnPick(id){
  S.fnode=S.fnode===id?null:id;   /* clicking the same band again clears it */
  /* The list first, then the chart. The other way round sized the svg against
     the full-height pane and then opened a list under it that took 42% of
     that height -- the chart never re-laid out, the pane just scrolled, and
     the node you had clicked was the one that fell off the bottom. */
  paintFunnelJobs();
  drawFunnel();
}
function paintFunnelJobs(){
  const host=$("#fn-jobs");
  if(!host) return;
  if(!S.fnode){
    host.innerHTML='<p class="fn-hint">Click a band to see the applications behind it.</p>';
    return;
  }
  const want=new Set((S.nodes&&S.nodes[S.fnode])||[]);
  const rows=S.jobs.filter(j=>want.has(j.status));
  const label=(S.labels&&S.labels[S.fnode])||S.fnode;
  host.innerHTML='<div class="fn-jhead"><b>'+esc(label)+'</b>'+
    '<span>'+rows.length+" application"+(rows.length===1?"":"s")+'</span>'+
    '<div class="grow"></div>'+
    '<button class="alink" id="fn-clear">Clear</button>'+
    '<button class="alink" id="fn-open">Open in Jobs</button></div>'+
    (rows.length
      ? '<div class="fn-jlist">'+rows.map(j=>
          '<button class="fn-jrow" data-id="'+esc(j.id)+'">'+
          companyMark(j)+'<span class="con">'+esc(j.company)+'</span>'+
          '<span class="fj-role">'+esc(j.title)+'</span>'+
          '<span class="st"><span class="dot '+statusTone(j.status)+'"></span>'+
          esc(prettyStatus(j.status))+'</span></button>').join("")+'</div>'
      : '<p class="fn-hint">Nothing sits at this stage yet.</p>');
  $("#fn-clear").onclick=()=>{ S.fnode=null; drawFunnel(); paintFunnelJobs() };
  $("#fn-open").onclick=()=>{
    S.jfilter={kind:"node",value:S.fnode}; S.jsel=null;
    $("#jobq").value=""; setView("jobs");
  };
  /* Straight to the one you clicked, rather than to a filtered list of it. */
  host.querySelectorAll("[data-id]").forEach(b=>b.onclick=()=>{
    S.jfilter={kind:"all",value:""};
    setView("jobs"); selectJob(b.dataset.id);
  });
}

function drawRates(){
  const t=S.funnel.totals, c=S.funnel.by_status||{};
  const rate=(label,value,accent)=>
    '<div class="kv"><span>'+label+'</span><span class="v mono'+(accent?" acc":"")+'">'+
    value+'</span></div>';
  const reply=t.median_reply_days==null?"–"
    :t.median_reply_days+" day"+(t.median_reply_days===1?"":"s");
  /* A rate with nothing underneath it is not zero, it is unknown. Printing a
     confident "Offer → accepted 0%" at a range where no offer exists tells
     somebody they are fumbling a stage they have never reached. */
  const pct=(value,denom)=>denom?value+"%":"–";
  const thin=denom=>denom>0&&denom<10;   /* too few to read as a rate */
  /* The accent marks the stage that is actually leaking, not a fixed row.
     It used to sit on "Interview → offer" whenever a single offer existed,
     so its whole message was "you have had an offer" -- which the header
     already says. */
  const stages=[["Applied → interview",t.interview_rate,t.applied],
                ["Interview → offer",t.offer_rate,t.interviewed],
                ["Offer → accepted",t.accept_rate,t.offers]];
  const worst=stages.filter(([,,d])=>d>=10)
    .sort((a,b)=>a[1]-b[1])[0];
  $("#fn-rates").innerHTML=
    stages.map(([label,value,denom])=>
      rate(label,pct(value,denom),worst&&worst[0]===label)+
      (thin(denom)?'<div class="kv thin"><span></span><span class="v">of '+
        denom+' so far</span></div>':"")).join("")+
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
  if(ghost) out.push(ghost+" application"+(ghost===1?"":"s")+" went unanswered, "+
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
  /* A document with no font_family renders in whatever the theme picks. Saying
     so beats letting the browser show the first option and imply the CV uses a
     typeface it has never heard of. */
  let h='<label for="dz-face">Typeface</label>'+
    '<select id="dz-face"><option value=""'+(DZ.family?"":" selected")+
      '>Theme default</option>'+
    fonts.map(f=>'<option'+(f===DZ.family?" selected":"")+'>'+
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
  /* With no family chosen there is nothing to preview, and "'', serif"
     silently renders the control in a serif the app never uses. */
  const preview=f=>f?"'"+f+"', serif":"inherit";
  face.style.fontFamily=preview(DZ.family);
  face.onchange=()=>{ DZ.family=face.value||null;
    face.style.fontFamily=preview(DZ.family); touch() };
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
    note="Pick another theme to see what it does to the page count. Each one is "+
      "rendered for real, so the number is the number.";
  $("#dz-effect").innerHTML=
    '<div class="kv"><span>Pages</span><span class="v mono">'+r.pages+'</span></div>'+
    '<div class="kv"><span>Page '+r.pages+' fill</span><span class="v mono'+
      (pct!=null&&pct>=90?" acc":"")+'">'+(pct==null?"–":pct+"%")+'</span></div>'+
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
  ["workspace","editor","ai","api","updates","about"].forEach(k=>
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
    dt.innerHTML=(st.themes||[]).map(t=>"<option>"+esc(t)+"</option>").join("");
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
