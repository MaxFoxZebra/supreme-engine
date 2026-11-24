"""Pack the frozen server as a Claude Desktop extension (.mcpb).

    python pack_mcpb.py --server dist/cv-studio-server --platform darwin --out ../dist-mcpb

A .mcpb is a zip with a manifest.json at its root (MCPB manifest 0.3). Claude
Desktop installs it with a double-click, asks for the one setting it declares
(the workspace folder), and starts the server itself from the folder it
unpacked it into. That folder is Claude's, not the app's, so an update of the
desktop app never closes the connector under Claude: no per-version copy is
needed. An updated extension replaces the old one when it is installed.

The server is the same PyInstaller build the desktop app ships, so the bundle
is per platform. Unix modes are kept (the binary must stay executable).

The bundle holds no symlinks. Claude Desktop's unpacker does not restore them:
it writes each one as a small text file holding the link's target. The macOS
build has links in Python.framework, and PyInstaller's bootloader loads
_internal/Python, which is one of them. Unpacked as text, that fails with "slice
is not valid mach-o file" and the server never starts. So every link is stored
as a copy of what it points to: a file for a link to a file, the folder's
contents for a link to a folder. Nothing in the build loads libpython by name,
so a copy behaves exactly like the link did. The cost is a few duplicate
megabytes in the zip.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE = "cv-studio-server"


def manifest(version: str, platform: str) -> dict:
    exe = EXE + (".exe" if platform == "win32" else "")
    return {
        "manifest_version": "0.3",
        "name": "cv-studio",
        "display_name": "CV Studio",
        "version": version,
        "description": "Tailor, render and check your CVs and cover letters, and keep your job "
                       "applications up to date, from the chat.",
        "long_description": (
            "CV Studio keeps your CVs, cover letters and job applications as plain files in a "
            "folder on this computer, and lets Claude read, tailor, render and check them. "
            "Rendered pages show in the conversation: click a part of the page to ask for a "
            "change, fix a typo yourself, see exactly what Claude changed, and keep or undo "
            "each change.\n\nNothing leaves your computer except what you send to Claude, and a "
            "job posting's page when you ask Claude to read it. The CV Studio desktop app is "
            "optional: it opens the same folder."),
        "author": {"name": "Maxime Bidault", "url": "https://github.com/MaxFoxZebra/supreme-engine"},
        "repository": {"type": "git", "url": "https://github.com/MaxFoxZebra/supreme-engine"},
        "homepage": "https://github.com/MaxFoxZebra/supreme-engine",
        "support": "https://github.com/MaxFoxZebra/supreme-engine/issues",
        "icon": "icon.png",
        "license": "MIT",
        "keywords": ["cv", "resume", "cover letter", "job search", "rendercv"],
        "server": {
            "type": "binary",
            "entry_point": f"server/{exe}",
            "mcp_config": {
                "command": "${__dirname}/server/" + exe,
                "args": ["--mcp", "--workspace", "${user_config.workspace}", "--client", "claude"],
                "env": {"CVSTUDIO_EXTENSION": "1"},
            },
        },
        "user_config": {
            "workspace": {
                "type": "directory",
                "title": "CV Studio folder",
                "description": "Where your CVs, letters and applications are kept. The desktop "
                               "app uses Documents/CV Studio; pick the same folder to share it.",
                "required": True,
                "default": "${DOCUMENTS}/CV Studio",
            },
        },
        "tools_generated": True,
        "prompts_generated": True,
        "compatibility": {"platforms": [platform]},
    }


def _icon() -> bytes:
    # The app's own 512 px icon, the size Claude Desktop asks for.
    for f in (HERE.parent / "src-tauri" / "icons" / "icon.png", HERE.parent / "app-icon.png",
              HERE / "static" / "brand-mark.png"):
        if f.is_file():
            return f.read_bytes()
    raise SystemExit("no icon found")


def _check_link(link: Path, server: Path) -> None:
    """A link is stored as what it points to, so it must point inside the
    build, and a link to a folder must not point at one of its own parents (a
    walk that follows it would never end)."""
    target = link.resolve()
    root = server.resolve()
    if not target.exists() or not target.is_relative_to(root):
        raise SystemExit(f"{link} -> {os.readlink(link)} does not point inside {server}")
    if target.is_dir() and link.parent.resolve().is_relative_to(target):
        raise SystemExit(f"{link} -> {os.readlink(link)} points at one of its own parents")


def pack(server: Path, platform: str, out: Path, version: str) -> Path:
    exe = server / (EXE + (".exe" if platform == "win32" else ""))
    if not exe.is_file():
        raise SystemExit(f"{exe} is not there: point --server at PyInstaller's onedir output")
    out.mkdir(parents=True, exist_ok=True)
    import platform as pf
    arch = os.environ.get("MCPB_ARCH") or ("arm64" if pf.machine().lower() in ("arm64", "aarch64") else "x64")
    name = {"darwin": "macos", "win32": "windows", "linux": "linux"}[platform]
    target = out / f"CV-Studio-{version}-{name}-{arch}.mcpb"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.json", json.dumps(manifest(version, platform), indent=2))
        z.writestr("icon.png", _icon())
        # Links are followed, folders included, so each one is stored as a copy
        # of its target (see the module's docstring for why).
        for root, dirs, files in os.walk(server, followlinks=True):
            dirs.sort()
            for name_ in dirs + files:
                if (Path(root) / name_).is_symlink():
                    _check_link(Path(root) / name_, server)
            for name_ in sorted(files):
                f = Path(root) / name_
                arc = "server/" + f.relative_to(server).as_posix()
                st = f.stat()
                info = zipfile.ZipInfo(arc, date_time=(2024, 1, 1, 0, 0, 0))
                info.create_system = 3
                mode = st.st_mode & 0o777
                if f == exe or platform != "win32" and mode & 0o111:
                    mode |= 0o755
                info.external_attr = (stat.S_IFREG | (mode or 0o644)) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(f, "rb") as src, z.open(info, "w") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--server", required=True, type=Path, help="PyInstaller's onedir output")
    ap.add_argument("--platform", default={"win32": "win32", "darwin": "darwin"}.get(sys.platform, "linux"),
                    choices=["darwin", "win32", "linux"])
    ap.add_argument("--out", default=Path("dist-mcpb"), type=Path)
    ap.add_argument("--version", default=None)
    a = ap.parse_args()
    version = a.version
    if not version:
        import re
        version = re.search(r'^VERSION = "([^"]+)"', (HERE / "studio.py").read_text(encoding="utf-8"),
                            re.M).group(1)
    print(pack(a.server, a.platform, a.out, version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
