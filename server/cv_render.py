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
import os
import re
import shutil
import subprocess
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


# Where RenderCV keeps the callable its `rendercv` console script points at.
# It has moved: 2.3 publishes rendercv.cli:app, 2.8 rendercv.cli.entry_point:
# entry_point. Hard-coding one turned a RenderCV upgrade into "rendercv not
# found" -- the import fails, the in-process renderer is declared unavailable,
# and a frozen bundle falls back to hunting for an executable it does not ship.
# Newest first, so the version we pin costs one import.
CLI_PATHS = (
    "rendercv.cli.entry_point:entry_point",
    "rendercv.cli:app",
)


def rendercv_cli():
    """RenderCV's CLI callable, or None if RenderCV is not importable."""
    import importlib

    for path in CLI_PATHS:
        module, _, attr = path.partition(":")
        try:
            return getattr(importlib.import_module(module), attr)
        except Exception:
            continue
    # Last resort: ask the installed distribution where its script points.
    # Left last on purpose -- a PyInstaller bundle carries the package without
    # necessarily carrying its metadata, so this is the path most likely to be
    # missing in the one build that matters most.
    try:
        from importlib.metadata import distribution

        for entry in distribution("rendercv").entry_points:
            if entry.name == "rendercv":
                return entry.load()
    except Exception:
        pass
    return None


def rendercv_importable() -> bool:
    return rendercv_cli() is not None


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


# In-process rendering changes the working directory, argv and stdout, all of
# which belong to the whole process, and the server answers requests on
# threads. Two renders at once -- the base card catching up while the editor
# previews -- would each run in the other's folder and read the other's
# output, and both fail. One at a time; they take well under a second.
_IN_PROCESS = threading.Lock()


def _render_in_process(yaml_path: Path, out_dir: Path) -> tuple[bool, str]:
    """Drive RenderCV's CLI entry point without spawning a process."""
    with _IN_PROCESS:
        return _render_in_process_locked(yaml_path, out_dir)


def _render_in_process_locked(yaml_path: Path, out_dir: Path) -> tuple[bool, str]:
    entry_point = rendercv_cli()
    if entry_point is None:
        return False, "rendercv is installed but its CLI could not be found"

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


def _newest(out_dir: Path, pattern: str) -> list[Path]:
    """The most recently written match, as a 0- or 1-item list."""
    found = [f for f in out_dir.glob(pattern) if f.is_file()]
    if not found:
        return []
    return [max(found, key=lambda f: f.stat().st_mtime)]


def _page_index(png: Path, stem: str) -> int | None:
    """The page number off a `<stem>_<n>.png`, or None if it is not one."""
    tail = png.stem[len(stem) + 1:]
    return int(tail) if tail.isdigit() else None


def _pages_of(out_dir: Path, stem: str | None) -> list[Path]:
    """This document's page images, ordered by page rather than by name.

    Sorting them as text would put page 10 between pages 1 and 2.
    """
    if not stem:
        return []
    return [f for f in out_dir.glob(f"{stem}_*.png")
            if _page_index(f, stem) is not None]


def render_file(yaml_path: str | Path, out_dir: str | Path) -> dict:
    """Render a RenderCV YAML file and describe the result.

    Returns {ok, pages, png_pages, pdf, markdown, typ, ats_word_count, pdf_kb,
    log}. `pages` is exact: RenderCV emits one PNG per page.
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
    # Newest, not alphabetically first: the run that just finished is the one
    # being described, and a leftover from another document could otherwise
    # sort ahead of it.
    pdfs = _newest(out_dir, "*.pdf")

    # In-process is preferred, not infallible. A RenderCV whose CLI is not
    # shaped the way CLI_PATHS expects can be imported, called, exit cleanly
    # and write nothing at all -- so "it ran" is not evidence, the PDF is. When
    # that happens and there is an executable to ask instead, ask it rather
    # than reporting a failure we have a second route around.
    if not pdfs and rendercv_importable() and find_rendercv_exe():
        ok, second = _render_subprocess(yaml_path, out_dir)
        log = f"{log}\n{strip_ansi(second)}"
        pdfs = _newest(out_dir, "*.pdf")
    mds = _newest(out_dir, "*.md")
    typs = _newest(out_dir, "*.typ")

    # Only this document's pages. RenderCV names its output after `cv.name`,
    # and the preview folder is shared by every document, so globbing "*.png"
    # counts whatever an earlier render or an earlier name left behind --
    # which reads as extra pages and pages you into someone else's CV.
    stem = pdfs[0].stem if pdfs else None
    pngs = sorted(_pages_of(out_dir, stem), key=lambda f: _page_index(f, stem))

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
        # The Typst source RenderCV compiled. It is an ordered transcript of the
        # document, which is what cv_map reads to make the page clickable.
        "typ": str(typs[0]) if typs else None,
        "ats_word_count": words,
        "pdf_kb": round(pdfs[0].stat().st_size / 1024, 1),
        "log": log,
    }
