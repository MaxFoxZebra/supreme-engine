"""Workspace management, preferences, app presence, photo, logos, discovery, and pulse."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlparse

from .context import (
    PHOTO_FILE,
    PHOTO_MAX,
    STARTER_CV,
    STARTER_CV_BEFORE,
    cjkfonts,
    ctx,
    is_cv_yaml,
    is_letter,
    jobstore,
    languages,
    letters,
    rel,
    review,
    safe_path,
    sample,
    to_plain,
    yaml_rt,
)
from . import connectors as _conn
from . import documents as _docs
from . import history_scans as _hist
from . import letters_prep as _lp
from . import provenance as _prov
from . import translations as _trans


# --------------------------------------------------------------------------
# Photo
#
# One photo per workspace, photo.jpg at its root, already cropped square and
# shrunk by the app before it arrives. RenderCV prints cv.photo from a path
# relative to the YAML, so each CV that shows it points at it relatively
# (../photo.jpg from profile/): tailored copies land beside their source and
# keep working, and the folder still moves as one.
# --------------------------------------------------------------------------



def photo_info() -> dict | None:
    f = ctx.workspace / PHOTO_FILE
    if not f.is_file():
        return None
    st = f.stat()
    return {"path": PHOTO_FILE, "bytes": st.st_size,
            "url": f"/api/asset?path={PHOTO_FILE}&v={int(st.st_mtime * 1000)}"}


def photo_ref(doc: Path) -> str:
    """The workspace photo's path as this document has to write it."""
    return Path(os.path.relpath(ctx.workspace / PHOTO_FILE, doc.parent)).as_posix()


def save_photo(data: bytes) -> dict:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("The photo has to arrive as a JPEG.")
    if len(data) > PHOTO_MAX:
        raise ValueError("That photo is too large. Crop it again.")
    (ctx.workspace / PHOTO_FILE).write_bytes(data)
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
            if ref and (f.parent / str(ref)).resolve() == (ctx.workspace / PHOTO_FILE).resolve():
                _docs.apply_patches(f, [{"path": ["cv", "photo"], "value": None}], "photo")
                cleared.append(rel(f))
        except Exception:
            continue
    try:
        (ctx.workspace / PHOTO_FILE).unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "cleared": cleared}




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
    return cjkfonts.app_data_dir() / "prefs.json"


def app_file() -> Path:
    """Where the desktop app says it is open: beside the preferences. The app
    starts its server on a free port and nothing else would know which, so
    the connector, a process an AI client runs, reads this to tell whether the
    user has the app at all and whether it is open now."""
    return cjkfonts.app_data_dir() / "app.json"


def note_app_open(port: int) -> None:
    """Written by the server the desktop app starts, never by a connector."""
    try:
        f = app_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        review.write_whole(f, json.dumps({"port": port, "version": ctx.version, "pid": os.getpid(),
                                          "started": int(time.time())}))
    except OSError:
        pass


def clear_app_open() -> None:
    """Remove app.json on clean shutdown if it still belongs to this process."""
    try:
        f = app_file()
        info = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(info, dict) and info.get("pid") == os.getpid():
            f.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def app_presence() -> dict:
    """{"installed", "running"}: whether the desktop app has been used on
    this computer, and whether it answers now. Running is its port taking a
    connection: the app can close without a word (a crash, a shutdown), so the
    file alone says only that it was."""
    import socket
    try:
        info = json.loads(app_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"installed": False, "running": False}
    port = info.get("port") if isinstance(info, dict) else None
    running = False
    if isinstance(port, int) and 0 < port < 65536:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                running = True
        except OSError:
            pass
    return {"installed": True, "running": running}


# --------------------------------------------------------------------------
# Files the desktop app hands you
#
# A browser tab downloads an export itself. The desktop webview does not: it
# drops window.open on the floor as it does target=_blank, so Export PDF and
# every other export did nothing there at all. The page fetches the file and
# hands it here, and it goes where a browser would have put it, in Downloads,
# under a name that takes nothing already there, and is shown. Only the kinds
# of file the app exports, since this writes outside the workspace.
# --------------------------------------------------------------------------

DOWNLOAD_KINDS = (".pdf", ".docx", ".txt", ".md", ".zip", ".csv", ".json", ".ics")


def downloads_dir() -> Path:
    """The Downloads folder, wherever Windows has been told it is: it can be
    moved, to another drive or into OneDrive."""
    override = os.environ.get("CVSTUDIO_DOWNLOADS")
    if override:
        return Path(override)
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                where, _ = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")
            p = Path(os.path.expandvars(where))
            if p.is_dir():
                return p
        except OSError:
            pass
    return Path.home() / "Downloads"


def save_download(name: str, data: bytes) -> Path:
    """`data` in Downloads as `name`, or `name (2)` and on if that is taken."""
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(name or "")).strip(" .") or "export"
    stem, suffix = os.path.splitext(clean)
    if suffix.lower() not in DOWNLOAD_KINDS:
        raise ValueError(f"CV Studio only saves its own exports, not {suffix or 'that'} files.")
    folder = downloads_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        target = folder / (clean if n == 1 else f"{stem} ({n}){suffix}")
        try:
            with open(target, "xb") as f:
                f.write(data)
            return target
        except FileExistsError:
            continue
    raise ValueError(f"Downloads already has a thousand files called {clean}.")


def show_file(p: Path) -> None:
    """The file, selected in the file manager, as a browser's "show in
    folder" does."""
    try:
        if sys.platform == "win32":
            # One string: explorer reads /select,"path" itself, and the quoting
            # subprocess would put around the whole argument breaks it.
            subprocess.Popen(f'explorer /select,"{p}"')  # noqa: S603
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
    except OSError:
        pass


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
    f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} CV Studio {ctx.version} ---\n")
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
    return ctx.workspace.resolve() == sample.folder().resolve()


def open_sample(applications: int = 64) -> dict:
    """Rebuild the sample folder and switch the app to it."""
    if not in_sample():
        ctx.real_workspace = ctx.workspace
    ctx.workspace = sample.folder()
    try:
        return sample.build(sys.modules.get("studio") or __import__("studio"), applications)
    except Exception:
        ctx.workspace = ctx.real_workspace or ctx.workspace
        raise


def close_sample() -> None:
    if in_sample() and ctx.real_workspace is not None:
        ctx.workspace = ctx.real_workspace


def bootstrap(workspace: Path) -> bool:
    """Create and seed the workspace. Returns True when this was a first run."""
    created = not workspace.exists()
    (workspace / "profile").mkdir(parents=True, exist_ok=True)
    (workspace / "applications").mkdir(exist_ok=True)
    (workspace / "tracker").mkdir(exist_ok=True)
    (workspace / "letters").mkdir(exist_ok=True)
    (workspace / "assets").mkdir(exist_ok=True)
    if not any((workspace / "profile").glob("*.y*ml")):
        (workspace / "profile" / "my-cv.yaml").write_text(STARTER_CV, encoding="utf-8")
        # The one moment the base is not a guess: this is the only CV there is,
        # and the app put it there. Nominating it now means a new workspace has
        # something to tailor from before anybody has been asked anything.
        _prov.set_base_cv("profile/my-cv.yaml")
        created = True
    return created


def starter_untouched() -> bool:
    """Whether the base CV is still the placeholder the app wrote.

    First run is one launch; setup is not. Quit halfway through it and the next
    launch is no longer a first run, but the CV still says Your Name, and that
    is the fact the welcome is really about.
    """
    base = _prov.base_cv()
    if not base or base.get("missing"):
        return False
    try:
        return safe_path(base["path"]).read_text(encoding="utf-8") in (STARTER_CV, STARTER_CV_BEFORE)
    except OSError:
        return False


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
    return ctx.workspace / LOGO_DIR


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
    for folder, group in ((ctx.workspace / "profile", "My CVs"),
                          (ctx.workspace / "letters", "Cover letters")):
        if folder.is_dir():
            found += [(f, f.stem, group) for f in sorted(folder.glob("*.y*ml"))
                      if not f.name.startswith(".")]
            if group == "Cover letters":
                found += [(f, f.stem, group) for f in sorted(folder.glob("*" + letters.EXT))
                          if not f.name.startswith(".")]
    apps = ctx.workspace / "applications"
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
    docs = _prov._edits_read()["docs"]
    waiting = review.pending(ctx.workspace)

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
            except (OSError, ValueError):     # ValueError: not UTF-8
                meta = {}
            out.append({"path": path, "label": label, "group": group, "letter": True,
                        "mtime": f.stat().st_mtime, "ai": last_ai(path), "base": None,
                        "lang": languages.code_of(meta.get("language")),
                        "translation_of": None, "looks_like": meta.get("looks_like"),
                        "review": waiting.get(path)})
            continue
        if not is_cv_yaml(f):
            continue
        path = rel(f)
        try:
            lang = languages.file_language(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):         # ValueError: not UTF-8
            lang = "en"
        trans = (docs.get(path, {}).get("translation") or {})
        if trans.get("of") and label.endswith("." + str(trans.get("lang"))):
            label = label[: -len(str(trans.get("lang"))) - 1]
        out.append({"path": path, "label": label, "group": group,
                    "mtime": f.stat().st_mtime, "ai": last_ai(path),
                    "base": (docs.get(path, {}).get("base") or {}).get("path"),
                    "lang": lang,
                    "translation_of": trans.get("of"),
                    "review": waiting.get(path)})
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
    return {"docs": stamps, "mcp": _conn.mcp_activity(), "jobs": jobs_stamp(),
            "edits": _prov.edits_stamp(), "review": review.stamp(ctx.workspace), "scans": _hist.scans_stamp()}


def jobs_stamp() -> str | None:
    """A fingerprint of the applications, for the same reason as `docs`.

    An AI client changing a status is another process writing the workspace,
    and so is a sync client bringing in what was done on another computer.
    Without this the open Jobs table had no way to find out: it reloaded on
    boot, on a view switch, and after its own edits, so a status moved from a
    chat sat there stale until the user happened to navigate. Cheap enough to
    ask for every couple of seconds.
    """
    if jobstore is None:
        return None
    try:
        return jobstore.stamp(ctx.workspace)
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


