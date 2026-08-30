#!/usr/bin/env python3
"""Shared RenderCV rendering, usable both as a normal script and inside a
frozen (PyInstaller) app bundle.

The distinction matters: a frozen app has no external Python interpreter to
shell out to -- `sys.executable` is the bundle itself -- so the packaged build
must drive RenderCV in-process. Outside a bundle, shelling out to the installed
CLI keeps this script dependency-free for the skills that call it with the
system Python.

Both paths return the same dict, so callers never care which ran.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def rendercv_importable() -> bool:
    try:
        import rendercv.cli.entry_point  # noqa: F401
        return True
    except Exception:
        return False


def find_rendercv_exe() -> str | None:
    """Locate the CLI, including uv's install dir which is often not yet on PATH."""
    found = shutil.which("rendercv")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "rendercv.exe",
        Path.home() / ".local" / "bin" / "rendercv",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _render_in_process(yaml_path: Path, out_dir: Path) -> tuple[bool, str]:
    """Drive RenderCV's CLI entry point without spawning a process."""
    from rendercv.cli.entry_point import entry_point

    argv, cwd = sys.argv, Path.cwd()
    buf = io.StringIO()
    try:
        os.chdir(yaml_path.parent)
        sys.argv = ["rendercv", "render", str(yaml_path), "--output-folder", str(out_dir)]
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                entry_point()
            code = 0
        except SystemExit as exc:
            code = exc.code or 0
    except Exception as exc:  # a crash inside rendercv should read like a failure
        return False, f"{buf.getvalue()}\n{type(exc).__name__}: {exc}"
    finally:
        sys.argv = argv
        os.chdir(cwd)
    return code == 0, buf.getvalue()


def _render_subprocess(yaml_path: Path, out_dir: Path) -> tuple[bool, str]:
    exe = find_rendercv_exe()
    if exe is None:
        return False, (
            "rendercv not found. Install it with:\n"
            '    uv tool install "rendercv[full]"\n'
            "(the [full] extra is required -- plain `rendercv` refuses to run)"
        )
    env = dict(os.environ)
    # Without this, rendercv dies printing its success tick on a Windows console.
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [exe, "render", str(yaml_path), "--output-folder", str(out_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(yaml_path.parent),
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def render_file(yaml_path: str | Path, out_dir: str | Path) -> dict:
    """Render a RenderCV YAML file and describe the result.

    Returns {ok, pages, png_pages, pdf, markdown, ats_word_count, pdf_kb, log}.
    `pages` is exact: RenderCV emits one PNG per page.
    """
    yaml_path = Path(yaml_path).resolve()
    out_dir = Path(out_dir)
    if not out_dir.is_absolute():
        out_dir = yaml_path.parent / out_dir

    if not yaml_path.exists():
        return {"ok": False, "log": f"{yaml_path} does not exist"}

    # Prefer in-process when RenderCV is importable: it is faster and it is the
    # only path that works inside a frozen bundle.
    if rendercv_importable():
        ok, log = _render_in_process(yaml_path, out_dir)
    else:
        ok, log = _render_subprocess(yaml_path, out_dir)

    log = strip_ansi(log)
    pdfs = sorted(out_dir.glob("*.pdf"))
    pngs = sorted(out_dir.glob("*.png"))
    mds = sorted(out_dir.glob("*.md"))

    if not ok or not pdfs:
        return {"ok": False, "log": log}

    words = 0
    if mds:
        words = len(mds[0].read_text(encoding="utf-8", errors="replace").split())

    return {
        "ok": True,
        "pdf": str(pdfs[0]),
        "pages": len(pngs),
        "png_pages": [str(p) for p in pngs],
        "markdown": str(mds[0]) if mds else None,
        "ats_word_count": words,
        "pdf_kb": round(pdfs[0].stat().st_size / 1024, 1),
        "log": log,
    }
