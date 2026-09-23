"""Chinese, Japanese and Korean fonts, fetched the first time a CV needs them.

RenderCV ships Noto Sans in its Chinese, Japanese and Korean cuts. They are
two fifths of the fonts and a quarter of the download, for the few people who
write a CV in one of those languages, so the app is built without them and
fetches the pair a document needs once, into the user's app data folder, from
the same release of rendercv-fonts the build pins. Each file is checked
against the hash of the one it replaces before it is kept.

Nothing depends on the download succeeding. Typst falls back to the fonts
installed on the machine, and macOS and Windows both have fonts for all three,
so offline the page still prints, in a system face rather than Noto.

Standard library only: cv_render imports this from scripts that run on a bare
system Python.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import threading
import urllib.request
from pathlib import Path

# The rendercv-fonts release that `rendercv[full]==2.8` installs. Raise it with
# the pin in release.yml, and the hashes with it.
BASE = ("https://raw.githubusercontent.com/rendercv/rendercv-fonts/v0.5.1/"
        "rendercv_fonts/Noto%20Sans/")

# language code -> (the cut's suffix, its name, download in MB)
CUTS = {"zh": ("SC", "Chinese", 22), "ja": ("JP", "Japanese", 11), "ko": ("KR", "Korean", 12)}

SHA256 = {
    "NotoSansSC-Regular.ttf": "dc71173babc38dfd019912965f2b4b3421fb347ebd854e7b96f64ad63673924a",
    "NotoSansSC-Bold.ttf": "e414444c28bed174ec47f1d968fe8109ab935702022f4f082b2edde99607eb7d",
    "NotoSansJP-Regular.ttf": "7c8597677e9fac0f54d7848ad18bc6a708dfb5baa4ebf4bd91e66efcca313bf3",
    "NotoSansJP-Bold.ttf": "9e4e354e728cfc32e04bbc0bf686c7285a1cf47f64ce2ac27c1d813ad1dc9d63",
    "NotoSansKR-Regular.ttf": "9db318b65ee9c575a43e7efd273dbdd1afef26e467eea3e1073a50e1a6595f6d",
    "NotoSansKR-Bold.ttf": "a76c8dabfcfae31d67c86207373155718979ddcfcc1ce62d3129dd36530a5bdf",
}

HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")
KANA = re.compile(r"[぀-ヿㇰ-ㇿ]")
HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

_lock = threading.Lock()
_running: dict[str, threading.Thread] = {}
_failed: dict[str, str] = {}


def cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "CV Studio" / "fonts"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CV Studio" / "fonts"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "cv-studio" / "fonts"


def _files(code: str) -> list[str]:
    cut = CUTS[code][0]
    return [f"NotoSans{cut}-Regular.ttf", f"NotoSans{cut}-Bold.ttf"]


def _bundled(name: str) -> bool:
    try:
        import rendercv_fonts
    except Exception:
        return False
    return (Path(rendercv_fonts.__file__).parent / "Noto Sans" / name).is_file()


def have(code: str) -> bool:
    return all(_bundled(n) or (cache_dir() / n).is_file() for n in _files(code))


def needed(text: str) -> list[str]:
    """The cuts a document's text calls for. Kana make it Japanese, and Han
    characters on their own are read as Chinese."""
    out = []
    if HANGUL.search(text):
        out.append("ko")
    if KANA.search(text):
        out.append("ja")
    elif HAN.search(text):
        out.append("zh")
    return out


def register() -> None:
    """Put the download folder where every Typst compile looks for fonts."""
    d = cache_dir()
    if not any(d.glob("*.ttf")):
        return
    try:
        import rendercv_fonts
        if d not in rendercv_fonts.paths_to_font_folders:
            rendercv_fonts.paths_to_font_folders.append(d)
    except Exception:
        return
    # Compilers and font books are cached with the folders they were built on.
    try:
        from rendercv.renderer import pdf_png
        pdf_png.get_typst_compiler.cache_clear()
    except Exception:
        pass
    try:
        import cv_map
        cv_map._fonts_cache.clear()
    except Exception:
        pass


def _fetch(code: str) -> None:
    d = cache_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        for name in _files(code):
            if _bundled(name) or (d / name).is_file():
                continue
            with urllib.request.urlopen(BASE + name, timeout=60) as r:
                data = r.read()
            if hashlib.sha256(data).hexdigest() != SHA256[name]:
                raise ValueError(f"{name} did not match the file it replaces")
            part = d / (name + ".part")
            part.write_bytes(data)
            part.replace(d / name)
        _failed.pop(code, None)
        register()
    except Exception as exc:
        _failed[code] = f"{type(exc).__name__}: {exc}"
    finally:
        with _lock:
            _running.pop(code, None)


def fetch(codes: list[str]) -> list[str]:
    """Start fetching whichever of `codes` are missing. Returns those."""
    started = []
    for code in codes:
        if code not in CUTS or have(code):
            continue
        with _lock:
            if code not in _running:
                t = threading.Thread(target=_fetch, args=(code,), daemon=True)
                _running[code] = t
                t.start()
        started.append(code)
    return started


def ensure(text: str, timeout: float = 90) -> list[str]:
    """Before a render: fetch what the text needs and wait for it. Returns the
    cuts still missing afterwards, which print in a system font instead."""
    for code in fetch(needed(text)):
        t = _running.get(code)
        if t:
            t.join(timeout)
    return [c for c in needed(text) if not have(c)]


def status() -> dict:
    return {code: ("ready" if have(code) else "downloading" if code in _running
                   else "failed" if code in _failed else "missing")
            for code in CUTS}
