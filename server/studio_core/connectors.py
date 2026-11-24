"""AI client connectors, MCP binary copy management, skills, and MCP activity log."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

from .context import DEFAULT_WORKSPACE, SERVER_ROOT, cjkfonts, ctx, rel, review, to_plain, yaml_rt
from . import history_scans as _hist
from . import workspace as _ws


def _safe_path(raw: str) -> Path:
    from .context import safe_path
    return safe_path(raw)


def server_launch() -> dict:
    """How to launch this server, for the Claude Desktop config snippet.

    MCP needs the executable and its arguments separately -- a single string
    holding "python script.py" is not runnable. Frozen builds are the exe
    itself; a source checkout needs the interpreter plus the script path.
    """
    if getattr(sys, "frozen", False):
        own = mcp_copy_target()
        return {"command": str(own or Path(sys.executable).resolve()), "args": []}
    # server_main.py, not this file: --mcp is routed there, and studio.py's own
    # argument parser rejects it outright. Pointing a client at the wrong one
    # produces a server that exits before it says hello.
    entry = SERVER_ROOT / "server_main.py"
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


def _claude_appdata_config() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Claude" / "claude_desktop_config.json"


def _claude_package_configs() -> list[Path]:
    """Claude Desktop's config as its Windows package keeps it, newest first.

    Claude Desktop for Windows installs as an MSIX package, and Windows gives
    a packaged app a private AppData of its own, under
    %LOCALAPPDATA%\\Packages\\Claude_<publisher id>\\LocalCache\\Roaming. Once
    Claude has written its config there, that copy is the one it reads, and
    %APPDATA%\\Claude, the file its own "Edit config" opens, is never read
    again. Every update re-pointed that file only, so Claude went on starting
    the connector of whatever version was in its copy, which was too old to
    show the CV page in the chat. LOCALAPPDATA is only set on Windows.
    """
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    try:
        found = [f for d in (Path(local) / "Packages").glob("Claude_*")
                 if (f := d / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json").is_file()]
        return sorted(found, key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return []


def _claude_config_path() -> Path:
    """The config Claude Desktop reads: its package's own copy where it has
    one, else the one in AppData."""
    package = _claude_package_configs()
    return package[0] if package else _claude_appdata_config()


def _openai_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _gemini_config_path() -> Path:
    return Path.home() / ".gemini" / "settings.json"


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
    args = [*launch["args"], "--mcp", "--workspace", str(ctx.workspace)]
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
    "gemini": {
        "label": "Gemini CLI",
        "path": _gemini_config_path,
        "read": _json_read, "write": _json_write, "snippet": _json_snippet,
        "restart": "Start a new Gemini session, or run /mcp refresh, and the "
                   "tools are there.",
        "manual": "Gemini CLI and Gemini Code Assist share ~/.gemini/settings.json.",
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


def ai_config_paths(client: str) -> list[Path]:
    """Every config file of a client's that can start this app's connector,
    the one it reads first. One, except for Claude Desktop on Windows, where
    both its package's copy and the one in AppData can: an update re-points
    each, so the connector Claude starts is this version's whichever it reads."""
    first = ai_config_path(client)
    if client != "claude" or os.environ.get("CVSTUDIO_CLAUDE_CONFIG"):
        return [first]
    rest = [p for p in (*_claude_package_configs(), _claude_appdata_config())
            if p.is_file() and not _same_file(str(p), str(first))]
    return [first, *rest]


def claude_extension() -> dict | None:
    """CV Studio installed in Claude Desktop as an extension (pack_mcpb.py),
    if it is: {path, version, enabled, workspace}.

    Claude Desktop unpacks extensions next to its config file, one folder
    each with the manifest inside, and keeps each one's settings (enabled,
    and the folder the user picked) in a JSON file named after it. Read
    defensively: none of this is a published contract.
    """
    root = ai_config_path("claude").parent
    try:
        folders = [d for d in (root / "Claude Extensions").iterdir() if d.is_dir()]
    except OSError:
        return None
    for d in folders:
        try:
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict) or m.get("name") != "cv-studio":
            continue
        settings = {}
        try:
            settings = json.loads((root / "Claude Extensions Settings" / f"{d.name}.json")
                                  .read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            pass
        config = settings.get("userConfig") if isinstance(settings, dict) else None
        ws = (config or {}).get("workspace") if isinstance(config, dict) else None
        return {"path": str(d), "version": m.get("version"),
                "enabled": settings.get("isEnabled", True) is not False if isinstance(settings, dict) else True,
                "workspace": str(ws) if isinstance(ws, str) and ws else None}
    return None


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
           # The connector it last ran, when that was not this version's: it
           # has not restarted since the update, or it reads a config the
           # update did not reach. None for a connector from before versions
           # were noted, which is older than this one too.
           "stale": bool(heard) and heard.get("version") != ctx.version,
           "connector_version": heard.get("version"),
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
    # An older copy of this same app (the installed server, or the copy of a
    # version before) is not "another CV Studio": the app moves it to this
    # version's copy when it starts.
    if not _same_file(out["command"], want["command"]) and not _ours(out["command"]):
        out["state"] = "elsewhere"
    elif not _same_file(workspace, str(ctx.workspace)):
        out["state"] = "other-workspace"
    else:
        out["state"] = "connected"
    return out


def _ai_status(client: str, seen: dict) -> dict:
    """ai_status, and for Claude Desktop the extension: installed, it is how
    Claude reaches CV Studio, and an entry in the config as well would give
    Claude every tool twice."""
    out = ai_status(client, seen)
    if client != "claude":
        return out
    ext = claude_extension()
    if not ext or not ext["enabled"]:
        return out
    out["extension"] = ext
    out["also_in_config"] = out["state"] != "absent"
    out["state"] = "extension"
    if ext["workspace"]:
        out["workspace"] = ext["workspace"]
    out["other_workspace"] = bool(ext["workspace"]) and not _same_file(ext["workspace"], str(ctx.workspace))
    return out


def ai_clients() -> list[dict]:
    seen = mcp_activity().get("seen") or {}
    return [_ai_status(client, seen) for client in AI_CLIENTS]


def _write_entry(client: str, want: dict, path: Path | None = None) -> dict | None:
    """Put `want` in a client's config (`path`, else the one it reads),
    leaving the rest alone, and return what was there before.

    People keep other servers in these files, so each is read, merged and
    written back rather than replaced -- and backed up first. A file that is
    already broken is refused rather than rewritten: overwriting it would
    silently throw away whatever else was configured. And because the TOML edit
    is made in the text, the result is read back and checked before it is
    allowed to stand.
    """
    spec = AI_CLIENTS[client]
    path = path or ai_config_path(client)
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
        return before

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
    return before


def ai_connect(client: str) -> dict:
    """Add this workspace to a client's config, and the skills to the
    client's skills folder when it reads them from one."""
    if client not in AI_CLIENTS:
        raise ValueError(f"unknown client: {client}")
    ext = claude_extension() if client == "claude" else None
    if ext and ext["enabled"]:
        raise ValueError("CV Studio is installed in Claude Desktop as an extension, so it is "
                         "connected already. Adding it to the config too would give Claude "
                         "every tool twice.")
    ensure_mcp_copy()
    want = ai_entry(client)
    before = _write_entry(client, want)
    skills = install_skills(client) if client_skills_dir(client) else None
    action = "unchanged" if before == want else "updated" if before else "added"
    return {"ok": True, "action": action, "skills": skills, **ai_status(client)}


# --------------------------------------------------------------------------
# The connector's own copy
#
# An AI client starts the connector itself and keeps it running for as long as
# the client is open, which can be days. On Windows a running program's files
# cannot be replaced, so the installer closes every cv-studio-server.exe before
# an update -- and that used to include the one Claude Desktop was talking to,
# which then stayed dead until Claude was restarted. So on Windows the
# connector runs from a copy of its own, one folder per version beside the
# app's data, under a name of its own (cv-studio-mcp.exe, which the
# installer's taskkill does not match). An update never touches it. When the
# app starts after one, it makes the new version's copy and points each
# client at it, which the client picks up the next time it starts; a copy
# nothing is running any more is deleted.
#
# Elsewhere a running program's files can be replaced under it, so the
# connector runs from the app as it always did.
# --------------------------------------------------------------------------

MCP_COPY_EXE = "cv-studio-mcp.exe"
_mcp_copy_lock = threading.Lock()


def mcp_copy_root() -> Path:
    return _hist.backup_dir() / "mcp"


def _mcp_copy_wanted() -> bool:
    if os.environ.get("CVSTUDIO_MCP_COPY") == "1":         # the checks, anywhere
        return True
    return getattr(sys, "frozen", False) and sys.platform == "win32"


def mcp_copy_target() -> Path | None:
    """Where this version's connector runs from, or None where it runs from
    the app itself."""
    if not _mcp_copy_wanted():
        return None
    return mcp_copy_root() / ctx.version / MCP_COPY_EXE


def ensure_mcp_copy() -> Path | None:
    """This version's copy of the connector, made if it is not there yet.

    Made beside where it goes and renamed into place, with a marker written
    last, so a copy cut short (the app closed halfway) is never mistaken for a
    whole one.
    """
    target = mcp_copy_target()
    if target is None:
        return None
    done = target.parent / ".complete"
    if done.is_file() and target.is_file():
        return target
    with _mcp_copy_lock:
        if done.is_file() and target.is_file():
            return target
        exe = Path(sys.executable).resolve()
        part = target.parent.with_name(f".{target.parent.name}.part-{os.getpid()}")
        shutil.rmtree(part, ignore_errors=True)
        shutil.copytree(exe.parent, part)
        (part / exe.name).replace(part / MCP_COPY_EXE)
        (part / ".complete").write_text(ctx.version, encoding="utf-8")
        if target.parent.exists():
            shutil.rmtree(target.parent, ignore_errors=True)
        part.replace(target.parent)
    return target


def _ours(command: str) -> bool:
    """Whether a configured command is this app's connector: the app's own
    server, or one of the per-version copies."""
    if _same_file(command, sys.executable):
        return True
    try:
        root = os.path.normcase(os.path.realpath(mcp_copy_root()))
        return os.path.normcase(os.path.realpath(command)).startswith(root + os.sep)
    except OSError:
        return False


def refresh_connectors() -> dict:
    """After an update: point every client that runs one of this app's
    connectors at this version's copy, keeping its workspace and arguments,
    bring the skills installed for it up to date, and delete the copies
    nothing points at or runs."""
    target = ensure_mcp_copy()
    moved, skills = [], {}
    for client, spec in AI_CLIENTS.items():
        ours = False
        for path in ai_config_paths(client):
            try:
                entry = spec["read"](path) if path.is_file() else None
            except Exception:
                continue
            if not entry or not entry.get("command") or not _ours(str(entry["command"])):
                continue
            ours = True
            if target is not None and not _same_file(str(entry["command"]), str(target)):
                try:
                    _write_entry(client, {"command": str(target),
                                          "args": [str(a) for a in entry.get("args") or []]}, path)
                    if client not in moved:
                        moved.append(client)
                except ValueError as exc:
                    print(f"could not re-point {client}: {exc}", file=sys.stderr)
        if ours and client_skills_dir(client):
            skills[client] = install_skills(client, only_ours=True)
    pruned = []
    root = mcp_copy_root()
    if target is not None and root.is_dir():
        for d in root.iterdir():
            if d == target.parent or not d.is_dir():
                continue
            # A folder with a running program in it cannot be renamed on
            # Windows, so a rename that works is the proof nothing runs from
            # it: deleting file by file could leave a running connector
            # without the fonts it renders with.
            gone = d.with_name(f".gone-{d.name}-{os.getpid()}")
            try:
                d.replace(gone)
            except OSError:
                continue
            shutil.rmtree(gone, ignore_errors=True)
            pruned.append(d.name)
    return {"moved": moved, "pruned": pruned, "skills": skills}


def start_connector_refresh() -> None:
    def run() -> None:
        # Out of the way of the start: after an update this copies the whole
        # server, which a virus scanner reads file by file, and that is
        # nothing the window should be waiting behind.
        time.sleep(45)
        try:
            r = refresh_connectors()
            if r["moved"] or r["pruned"]:
                print(f"connectors: re-pointed {r['moved'] or 'none'}, "
                      f"removed old copies {r['pruned'] or 'none'}")
        except Exception as exc:        # never take the app down over this
            print(f"connector refresh failed: {exc}", file=sys.stderr)
    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------
# Skills
#
# The procedures around the tools: from a job link to an application, the
# tracker caught up from the inbox, an interview prepared. They ship with the
# app as a Claude plugin (plugin/ in the repository, bundled beside the
# server), and reach each client the way that client reads skills:
#
# - Codex, Mistral Vibe and Hermes read a skills folder beside their config,
#   so connecting one copies them there, and the app keeps that copy current
#   after an update. Only folders it put there itself are ever replaced or
#   removed; each carries a marker saying so.
# - Claude Desktop keeps skills in the account, not on disk. It takes them as
#   one plugin, uploaded once, or follows this repository as a plugin
#   marketplace and updates itself. The connector stays in its config rather
#   than in the plugin: a local server bundled in a plugin runs in Cowork and
#   Claude Code but not in chat.
# --------------------------------------------------------------------------

MARKETPLACE = "MaxFoxZebra/supreme-engine"
SKILL_MARK = ".installed-by-cv-studio"
PLUGIN_FILE = "cv-studio.plugin"
SKILL_JUNK = ("__pycache__", ".pyc", ".pyo", ".DS_Store")


def plugin_dir() -> Path | None:
    """The bundled plugin: beside the frozen server, or in the checkout."""
    here = SERVER_ROOT
    for base in (getattr(sys, "_MEIPASS", None), here, here.parent):
        if base and (Path(base) / "plugin" / ".claude-plugin" / "plugin.json").is_file():
            return Path(base) / "plugin"
    return None


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


def skill_folders() -> list[Path]:
    d = plugin_dir()
    if d is None or not (d / "skills").is_dir():
        return []
    return sorted(f for f in (d / "skills").iterdir() if (f / "SKILL.md").is_file())


def skill_texts() -> list[dict]:
    """Each skill's name, description and body, for the list and for the
    MCP prompts that offer them to clients without skills."""
    out = []
    for d in skill_folders():
        text = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        fm = _front_matter(text)
        end = text.find("\n---", 3) if text.startswith("---") else -1
        out.append({"name": fm.get("name") or d.name, "folder": d.name,
                    "description": fm.get("description") or "",
                    "body": text[end + 4:].lstrip() if end >= 0 else text})
    return out


def _skills_digest() -> str:
    h = hashlib.sha1()
    for d in skill_folders():
        for f in sorted(d.rglob("*")):
            if f.is_file() and not any(j in str(f) for j in SKILL_JUNK):
                h.update(f.relative_to(d.parent).as_posix().encode())
                h.update(f.read_bytes())
    return h.hexdigest()[:12]


def client_skills_dir(client: str) -> Path | None:
    """The folder a client reads skills from, or None for Claude Desktop,
    which keeps them in the account.

    Codex reads ~/.agents/skills (~/.codex/skills is its legacy place, still
    read but on the way out), Vibe ~/.vibe/skills, and Hermes the skills/
    folder in its home, which is where its config is.
    """
    if client not in ("openai", "mistral", "hermes", "gemini"):
        return None
    override = os.environ.get(f"CVSTUDIO_{client.upper()}_SKILLS")
    if override:
        return Path(override)
    if client == "openai":
        return Path.home() / ".agents" / "skills"
    return ai_config_path(client).parent / "skills"


def skills_state(client: str) -> str | None:
    """installed, outdated, absent, or None for a client without a folder.
    A folder the user made under one of the names counts as theirs, not as
    missing."""
    d = client_skills_dir(client)
    if d is None:
        return None
    want = _skills_digest()
    seen = []
    for f in skill_folders():
        mark = d / f.name / SKILL_MARK
        if mark.is_file():
            seen.append("ok" if mark.read_text(encoding="utf-8").strip() == want else "old")
        else:
            seen.append("yours" if (d / f.name).exists() else "missing")
    if not seen or all(x in ("missing", "yours") for x in seen) and "missing" in seen:
        return "absent"
    return "outdated" if any(x in ("old", "missing") for x in seen) else "installed"


def install_skills(client: str, only_ours: bool = False) -> dict:
    """Copy the skills into a client's skills folder.

    A folder of the same name the user made themselves is left alone, and
    said so. `only_ours` is the update after an app update: it refreshes
    copies this app made and adds none, so a client someone removed them
    from stays without.
    """
    d = client_skills_dir(client)
    if d is None:
        return {"state": None}
    want = _skills_digest()
    names = {f.name for f in skill_folders()}
    done, kept = [], []
    for src in skill_folders():
        dest = d / src.name
        if dest.exists() and not (dest / SKILL_MARK).is_file():
            kept.append(src.name)
            continue
        if only_ours and not dest.exists():
            continue
        if (dest / SKILL_MARK).is_file() and \
                (dest / SKILL_MARK).read_text(encoding="utf-8").strip() == want:
            continue
        d.mkdir(parents=True, exist_ok=True)
        part = d / f".{src.name}.part-{os.getpid()}"
        shutil.rmtree(part, ignore_errors=True)
        shutil.copytree(src, part, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.pyo", ".DS_Store"))
        (part / SKILL_MARK).write_text(want + "\n", encoding="utf-8")
        if dest.exists():
            shutil.rmtree(dest)
        part.replace(dest)
        done.append(src.name)
    # A skill this app installed that it no longer ships.
    if d.is_dir():
        for old in d.iterdir():
            if old.name.startswith("cv-studio") and old.name not in names \
                    and (old / SKILL_MARK).is_file():
                shutil.rmtree(old, ignore_errors=True)
    return {"state": skills_state(client), "installed": done, "kept_yours": kept,
            "dir": str(d)}


def skills_list() -> dict:
    return {"skills": [{"name": k["name"], "description": k["description"][:220]}
                       for k in skill_texts()],
            "clients": {c: {"state": skills_state(c), "dir": str(client_skills_dir(c))}
                        for c in AI_CLIENTS if client_skills_dir(c)},
            "plugin": rel(ctx.workspace / "assets" / "claude" / PLUGIN_FILE)
            if (ctx.workspace / "assets" / "claude" / PLUGIN_FILE).is_file() else None,
            "out_dir": rel(ctx.workspace / "assets" / "claude"),
            "marketplace": MARKETPLACE}


def package_skills() -> dict:
    """The plugin as one file for Claude Desktop: Customize, Plugins, Add,
    Upload plugin. Stamped with this version, so a new upload replaces the
    old one rather than sitting beside it."""
    src = plugin_dir()
    if src is None or not skill_folders():
        raise ValueError("This build has no skills to package.")
    dest = ctx.workspace / "assets" / "claude"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / PLUGIN_FILE
    manifest = json.loads((src / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    manifest["version"] = ctx.version
    part = target.with_name(target.name + ".part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(".claude-plugin/plugin.json", json.dumps(manifest, indent=2) + "\n")
        for f in sorted(src.rglob("*")):
            arc = f.relative_to(src).as_posix()
            if not f.is_file() or arc.startswith(".claude-plugin/") \
                    or any(j in arc for j in SKILL_JUNK):
                continue
            z.write(f, arc)
    part.replace(target)
    return {"ok": True, "path": rel(target), "dir": rel(dest),
            "skills": [k["name"] for k in skill_texts()],
            "kb": round(target.stat().st_size / 1024, 1)}


def note_mcp_activity(tool: str, path: str | None = None, ok: bool = True,
                      error: str | None = None) -> None:
    """Record what the AI just did, so the app can say so.

    Called from the MCP process, read by the app's. Bookkeeping must never be
    the thing that breaks a tool call, so every failure here is swallowed.

    `by` and `agent` are what let the app name the client rather than saying
    "an AI client". Until they existed the app could only name one when exactly
    one was configured, which stopped being true the moment anybody set up two.
    `version` is this connector's: a client that has not restarted since an
    update still runs the old one, and that is how the app can tell.
    """
    try:
        f = ctx.workspace / ACTIVITY_FILE
        try:
            log = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log = []
        if not isinstance(log, list):
            log = []
        entry = {"tool": tool, "path": path, "at": time.time()}
        entry["by"] = ctx.client_id or "ai"
        entry["version"] = ctx.version
        if ctx.client_agent:
            entry["agent"] = ctx.client_agent
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
        log = json.loads((ctx.workspace / ACTIVITY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log = []
    if not isinstance(log, list) or not log:
        return {"last": None, "recent": [], "seen": {}}
    seen: dict[str, dict] = {}
    for entry in log:
        who = entry.get("by")
        if who:
            seen[who] = {"at": entry.get("at"), "agent": entry.get("agent"),
                         "tool": entry.get("tool"), "version": entry.get("version")}
    return {"last": log[-1], "recent": log[-8:][::-1], "seen": seen}



