"""The connector an AI client runs, and the skills that go with it.

    python checks/connectortest.py

On Windows the installer closes every cv-studio-server.exe before an update,
which used to include the one Claude Desktop was running. So the connector runs
from a copy per version under a name of its own, and the app points each client
at the new copy after an update. This builds a pretend frozen app in a scratch
folder and checks that: the copy, the re-pointing (only of entries that are
ours, keeping their workspace and every other server in the file), and the
removal of old copies. Then the skills: installed into the folder Codex, Vibe
and Hermes read, kept current, never over a folder the user made; packed as one
plugin for Claude Desktop; and the repository as a plugin marketplace. Last,
what the MCP server tells a client about its tools and prompts. And the
server as a Claude Desktop extension (.mcpb): what its bundle holds, and the
app recognising it once Claude Desktop has installed it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
SCRATCH = Path(tempfile.mkdtemp())
os.environ["XDG_DATA_HOME"] = str(SCRATCH / "data")
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]
os.environ["CVSTUDIO_MCP_COPY"] = "1"
for client, rel in (("claude", "claude/claude_desktop_config.json"),
                    ("openai", ".codex/config.toml"),
                    ("mistral", ".vibe/config.toml"),
                    ("hermes", "hermes/config.yaml")):
    os.environ[f"CVSTUDIO_{client.upper()}_CONFIG"] = str(SCRATCH / rel)
    os.environ[f"CVSTUDIO_{client.upper()}_SKILLS"] = str(SCRATCH / client / "skills")

import studio  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def fake_install() -> Path:
    """An installed app as PyInstaller lays it out: the exe and _internal/."""
    dist = SCRATCH / "Program" / "CV Studio" / "server-dist"
    (dist / "_internal" / "fonts").mkdir(parents=True)
    (dist / "cv-studio-server.exe").write_bytes(b"MZ pretend")
    (dist / "_internal" / "fonts" / "Noto.ttf").write_bytes(b"font")
    return dist / "cv-studio-server.exe"


def copies() -> None:
    print("The connector's own copy")
    exe = fake_install()
    sys.executable = str(exe)
    sys.frozen = True
    studio.VERSION = "1.0.0"
    ws = SCRATCH / "CV Studio"
    studio.WORKSPACE = ws
    studio.bootstrap(ws)

    target = studio.ensure_mcp_copy()
    check("this version's copy is made", target is not None and target.is_file(), str(target))
    check("under a name the installer's taskkill does not match",
          target.name == "cv-studio-mcp.exe" and not (target.parent / "cv-studio-server.exe").exists())
    check("with everything beside it", (target.parent / "_internal" / "fonts" / "Noto.ttf").is_file())
    check("in a folder per version", target.parent.name == "1.0.0")
    check("a client is pointed at the copy", studio.ai_entry("claude")["command"] == str(target))

    # Claude's config as an older version left it: pointing at the installed
    # server itself, beside a server of someone else's.
    cfg = Path(os.environ["CVSTUDIO_CLAUDE_CONFIG"])
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {
        "cv-studio": {"command": str(exe), "args": ["--mcp", "--workspace", str(ws / "elsewhere")]},
        "other": {"command": "npx", "args": ["other-server"]}}}), encoding="utf-8")
    codex = Path(os.environ["CVSTUDIO_OPENAI_CONFIG"])
    codex.parent.mkdir(parents=True)
    codex.write_text('[mcp_servers.cv-studio]\ncommand = "C:/someone/else.exe"\nargs = []\n',
                     encoding="utf-8")

    r = studio.refresh_connectors()
    data = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]
    check("an update re-points the client at the new copy", data["cv-studio"]["command"] == str(target),
          data["cv-studio"]["command"])
    check("keeping its workspace and arguments",
          data["cv-studio"]["args"] == ["--mcp", "--workspace", str(ws / "elsewhere")])
    check("and the other servers in the file", data.get("other") == {"command": "npx", "args": ["other-server"]})
    check("a command that is not ours is left alone",
          "C:/someone/else.exe" in codex.read_text(encoding="utf-8") and "openai" not in r["moved"])

    studio.VERSION = "1.1.0"
    r = studio.refresh_connectors()
    new = studio.mcp_copy_target()
    data = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]
    check("the next version gets its own copy", new.is_file() and new.parent.name == "1.1.0")
    check("and the client follows it", data["cv-studio"]["command"] == str(new))
    check("the copy nothing uses any more is removed",
          not (studio.mcp_copy_root() / "1.0.0").exists() and r["pruned"] == ["1.0.0"], str(r["pruned"]))
    check("nothing half-made is left", [p.name for p in studio.mcp_copy_root().iterdir()] == ["1.1.0"],
          str([p.name for p in studio.mcp_copy_root().iterdir()]))
    st = studio.ai_status("claude")
    check("and the app shows it as this copy", st["state"] in ("connected", "other-workspace"), st["state"])


def skills() -> None:
    print("Skills")
    names = [d.name for d in studio.skill_folders()]
    check("the app finds the skills it ships", len(names) >= 3, str(names))
    for k in studio.skill_texts():
        if not (k["name"] and k["description"] and k["body"].strip()):
            check(f"{k['folder']} has a name, a description and a body", False)

    r = studio.ai_connect("openai")
    d = studio.client_skills_dir("openai")
    check("connecting Codex puts the skills in its folder",
          d == SCRATCH / "openai" / "skills"
          and all((d / n / "SKILL.md").is_file() for n in names), str(d))
    check("and says so", sorted(r["skills"]["installed"]) == sorted(names))
    check("each marked as the app's", all((d / n / studio.SKILL_MARK).is_file() for n in names))
    check("the state reads installed", studio.skills_state("openai") == "installed")

    (d / names[0] / studio.SKILL_MARK).write_text("older\n", encoding="utf-8")
    (d / names[0] / "SKILL.md").write_text("old text", encoding="utf-8")
    check("an older copy reads as outdated", studio.skills_state("openai") == "outdated")
    studio.refresh_connectors()
    check("an update of the app brings it up to date",
          studio.skills_state("openai") == "installed"
          and (d / names[0] / "SKILL.md").read_text(encoding="utf-8") != "old text")

    vibe = studio.client_skills_dir("mistral")
    (vibe / names[1]).mkdir(parents=True)
    (vibe / names[1] / "SKILL.md").write_text("mine", encoding="utf-8")
    r = studio.install_skills("mistral")
    check("a folder the user made is left as it was",
          (vibe / names[1] / "SKILL.md").read_text(encoding="utf-8") == "mine"
          and r["kept_yours"] == [names[1]], str(r))
    check("and counts as theirs, not as out of date", r["state"] == "installed", r["state"])
    gone = d / "cv-studio-retired"
    gone.mkdir()
    (gone / studio.SKILL_MARK).write_text("x", encoding="utf-8")
    studio.install_skills("openai")
    check("a skill the app no longer ships is removed", not gone.exists())
    check("the update adds none where there were none",
          not studio.install_skills("hermes", only_ours=True)["installed"])
    check("Claude Desktop has no skills folder", studio.client_skills_dir("claude") is None)
    del os.environ["CVSTUDIO_OPENAI_SKILLS"]
    check("Codex's is ~/.agents/skills, not the legacy ~/.codex/skills",
          studio.client_skills_dir("openai") == Path.home() / ".agents" / "skills")
    os.environ["CVSTUDIO_OPENAI_SKILLS"] = str(SCRATCH / "openai" / "skills")

    p = studio.package_skills()
    f = studio.WORKSPACE / p["path"]
    with zipfile.ZipFile(f) as z:
        files = z.namelist()
        manifest = json.loads(z.read(".claude-plugin/plugin.json"))
    check("Claude Desktop gets one plugin file", f.name == "cv-studio.plugin", f.name)
    check("with one manifest, stamped with this version",
          files.count(".claude-plugin/plugin.json") == 1 and manifest.get("version") == studio.VERSION
          and manifest.get("name") == "cv-studio")
    check("and every skill in it", all(f"skills/{n}/SKILL.md" in files for n in names), str(files))

    root = HERE.parent
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entry = market["plugins"][0]
    check("the repository is a plugin marketplace",
          re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", market["name"]) is not None
          and (root / entry["source"] / ".claude-plugin" / "plugin.json").is_file())
    check("whose plugin is the one the app ships",
          (root / entry["source"]).resolve() == studio.plugin_dir().resolve())


def extension() -> None:
    print("The Claude Desktop extension")
    import stat as st_
    import pack_mcpb
    frozen = SCRATCH / "frozen" / "cv-studio-server"
    (frozen / "_internal" / "lib").mkdir(parents=True)
    (frozen / "cv-studio-server").write_bytes(b"#!/bin/sh\n")
    (frozen / "cv-studio-server").chmod(0o755)
    (frozen / "_internal" / "lib" / "real.so").write_bytes(b"lib")
    os.symlink("lib/real.so", frozen / "_internal" / "link.so")
    out = pack_mcpb.pack(frozen, "darwin", SCRATCH / "mcpb", studio.VERSION)
    with zipfile.ZipFile(out) as z:
        m = json.loads(z.read("manifest.json"))
        infos = {i.filename: i for i in z.infolist()}
    cfg = m["server"]["mcp_config"]
    check("one file, named for the version and the computer",
          out.suffix == ".mcpb" and studio.VERSION in out.name and "macos" in out.name, out.name)
    check("a manifest Claude Desktop reads: MCPB 0.3, this version, a binary server",
          m["manifest_version"] == "0.3" and m["version"] == studio.VERSION and m["name"] == "cv-studio"
          and m["server"]["type"] == "binary" and m["server"]["entry_point"] in infos, m["server"]["entry_point"])
    check("started as the connector, on the folder the user picks",
          cfg["command"] == "${__dirname}/server/cv-studio-server"
          and cfg["args"][:3] == ["--mcp", "--workspace", "${user_config.workspace}"]
          and m["user_config"]["workspace"]["type"] == "directory"
          and m["user_config"]["workspace"]["default"] == "${DOCUMENTS}/CV Studio", json.dumps(cfg))
    exe = infos["server/cv-studio-server"]
    check("the server stays executable", (exe.external_attr >> 16) & 0o111 == 0o111, oct(exe.external_attr >> 16))
    link = infos.get("server/_internal/link.so")
    check("and a symlink stays a symlink", link is not None and st_.S_ISLNK(link.external_attr >> 16))
    check("with the icon", "icon.png" in infos and m["icon"] == "icon.png")
    win = SCRATCH / "frozen-win"
    win.mkdir()
    (win / "cv-studio-server.exe").write_bytes(b"MZ")
    with zipfile.ZipFile(pack_mcpb.pack(win, "win32", SCRATCH / "mcpb", studio.VERSION)) as z:
        wm = json.loads(z.read("manifest.json"))
    check("the Windows one runs the .exe, on Windows only",
          wm["server"]["mcp_config"]["command"].endswith("cv-studio-server.exe")
          and wm["compatibility"]["platforms"] == ["win32"])

    # Claude Desktop, having installed it: a folder per extension beside its
    # config, and that extension's settings in a file named after it.
    root = Path(os.environ["CVSTUDIO_CLAUDE_CONFIG"]).parent
    ext = root / "Claude Extensions" / "local.mcpb.maxime-bidault.cv-studio"
    ext.mkdir(parents=True)
    (ext / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    (root / "Claude Extensions Settings").mkdir()
    settings = root / "Claude Extensions Settings" / f"{ext.name}.json"
    settings.write_text(json.dumps({"isEnabled": True, "userConfig": {"workspace": str(studio.WORKSPACE)}}),
                        encoding="utf-8")
    found = studio.claude_extension()
    check("the app finds CV Studio installed as an extension, and its folder",
          found and found["version"] == studio.VERSION and found["workspace"] == str(studio.WORKSPACE), str(found))
    claude = next(c for c in studio.ai_clients() if c["id"] == "claude")
    check("and says Claude is connected through it", claude["state"] == "extension"
          and not claude["other_workspace"], claude["state"])
    check("noticing the config entry too, which would give Claude every tool twice",
          claude["also_in_config"] is True)
    try:
        studio.ai_connect("claude")
        refused = False
    except ValueError:
        refused = True
    check("so Connect does not add a second one", refused)
    settings.write_text(json.dumps({"isEnabled": True, "userConfig": {"workspace": str(SCRATCH / "other")}}),
                        encoding="utf-8")
    claude = next(c for c in studio.ai_clients() if c["id"] == "claude")
    check("an extension on another folder is said to be", claude["other_workspace"] is True)
    settings.write_text(json.dumps({"isEnabled": False}), encoding="utf-8")
    claude = next(c for c in studio.ai_clients() if c["id"] == "claude")
    check("and a disabled one is not counted", claude["state"] != "extension", claude["state"])


def mcp() -> None:
    print("What the connector tells a client")
    import mcp_server
    tools = asyncio.run(mcp_server.mcp.list_tools())
    check("every tool has a title", all(t.title for t in tools),
          str([t.name for t in tools if not t.title]))
    check("every tool says whether it only reads",
          all(t.annotations and t.annotations.read_only_hint == (t.name in mcp_server.READ_ONLY)
              for t in tools))
    by = {t.name: t.annotations for t in tools}
    check("replacing a CV is marked destructive", by["write_cv"].destructive_hint is True)
    check("adding an application is not", by["add_job"].destructive_hint is False)
    check("reading a posting reaches the web", by["read_posting"].open_world_hint is True)
    prompts = asyncio.run(mcp_server.mcp.list_prompts())
    check("the skills are offered as prompts",
          sorted(p.name for p in prompts) == sorted(k["name"] for k in studio.skill_texts()))
    r = asyncio.run(mcp_server.mcp.get_prompt(prompts[0].name, {"request": "the Monzo one"}))
    text = r.messages[0].content.text
    check("a prompt carries the skill and what was asked",
          text.rstrip().endswith("the Monzo one") and len(text) > 500)


def main() -> int:
    copies()
    skills()
    extension()
    mcp()
    print()
    print(f"{fails} failure(s)" if fails else "every connector check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
