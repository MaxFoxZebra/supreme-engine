#!/usr/bin/env python3
"""Development loop: run the server and restart it whenever any code changes.

Almost all of CV Studio is Python and served static HTML/CSS/JS, so day-to-day
work needs no build at all.

    python dev.py                 # against ~/Documents/CV Studio
    python dev.py --workspace X   # against any other workspace
    python dev.py --no-open       # do not steal focus with a browser window

Edit a file, save, refresh the browser. Roughly two seconds.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKIP_DIRS = {".venv", "__pycache__", "build", "dist", "checks", "i18n"}
WATCH_EXTS = {".py", ".js", ".css", ".html"}


def _reexec_in_venv() -> None:
    """Re-run under the project venv if the current interpreter cannot serve."""
    try:
        import ruamel.yaml  # noqa: F401
        return
    except ImportError:
        pass
    for candidate in (HERE / ".venv" / "Scripts" / "python.exe",
                      HERE / ".venv" / "bin" / "python"):
        if candidate.exists() and str(candidate) != sys.executable:
            proc = subprocess.run([str(candidate), str(HERE / "dev.py"), *sys.argv[1:]])
            raise SystemExit(proc.returncode)
    sys.stderr.write(
        "This interpreter cannot run the server: ruamel.yaml is missing and no "
        "project venv was found next to dev.py." + os.linesep + os.linesep +
        "Create one with:" + os.linesep +
        "    uv venv .venv --python 3.14" + os.linesep +
        "    uv pip install --python .venv \"rendercv[full]==2.8\" ruamel.yaml mcp pypdf tzdata" + os.linesep)
    raise SystemExit(1)


def stamps() -> dict[str, float]:
    """Recursively snapshot mtimes of every server Python module and static asset."""
    out: dict[str, float] = {}
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        root_p = Path(root)
        for name in files:
            p = root_p / name
            if p.suffix not in WATCH_EXTS:
                continue
            try:
                out[p.relative_to(HERE).as_posix()] = p.stat().st_mtime
            except OSError:
                pass
    return out


def spawn(args) -> subprocess.Popen:
    cmd = [sys.executable, str(HERE / "studio.py"), "--port", str(args.port)]
    if args.workspace:
        cmd += ["--workspace", args.workspace]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen(cmd, env=env, cwd=str(HERE))


def main() -> int:
    _reexec_in_venv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8722)
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}/"
    proc = spawn(args)
    last = stamps()
    print(f"CV Studio dev  -> {url}", flush=True)
    print(f"watching       : {len(last)} source & static files under {HERE.name}/")
    print("save a file to restart; Ctrl+C to stop\n", flush=True)
    if not args.no_open:
        time.sleep(1.2)
        webbrowser.open(url)

    try:
        while True:
            time.sleep(0.6)
            now = stamps()
            changed = [k for k, v in now.items() if last.get(k) != v]
            if changed:
                print(f"changed: {', '.join(changed)} -> restarting", flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc = spawn(args)
                last = now
            elif proc.poll() is not None:
                print("server exited; fix the error and save to restart", flush=True)
                while proc.poll() is not None:
                    time.sleep(0.6)
                    now = stamps()
                    if [k for k, v in now.items() if last.get(k) != v]:
                        last = now
                        proc = spawn(args)
                        print("restarted", flush=True)
                        break
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
