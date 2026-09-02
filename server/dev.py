#!/usr/bin/env python3
"""Development loop: run the server and restart it whenever the code changes.

Almost all of CV Studio is Python and served HTML, so day-to-day work needs no
build at all. PyInstaller and the installer only matter when you want to test
the frozen bundle or ship it; the CI matrix only runs when you push a tag.

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
WATCH = ["studio.py", "cv_render.py", "jobs.py", "mcp_server.py"]


def _reexec_in_venv() -> None:
    """Re-run under the project venv if the current interpreter cannot serve.

    Running `python dev.py` with a system interpreter fails later and less
    clearly, when studio.py cannot import ruamel.yaml. Detect it here and switch
    interpreters, so the obvious command works whichever Python is on PATH.
    """
    try:
        import ruamel.yaml  # noqa: F401
        return
    except ImportError:
        pass
    for candidate in (HERE / ".venv" / "Scripts" / "python.exe",
                      HERE / ".venv" / "bin" / "python"):
        if candidate.exists() and str(candidate) != sys.executable:
            # Not os.execv: on Windows it flattens argv into one command line
            # without quoting, so a checkout under a path containing a space
            # ("AI Projects") splits the interpreter path and the child tries to
            # run the tail of it as a script. subprocess quotes properly.
            proc = subprocess.run([str(candidate), str(HERE / "dev.py"), *sys.argv[1:]])
            raise SystemExit(proc.returncode)
    sys.stderr.write(
        "This interpreter cannot run the server: ruamel.yaml is missing and no "
        "project venv was found next to dev.py." + os.linesep + os.linesep +
        "Create one with:" + os.linesep +
        "    uv venv .venv --python 3.13" + os.linesep +
        "    uv pip install --python .venv \"rendercv[full]\" ruamel.yaml mcp" + os.linesep)
    raise SystemExit(1)


def stamps() -> dict[str, float]:
    out = {}
    for name in WATCH:
        f = HERE / name
        if f.exists():
            out[name] = f.stat().st_mtime
    for f in (HERE / "static").glob("*.js"):
        out[f.name] = f.stat().st_mtime
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
    print(f"watching       : {', '.join(WATCH)}")
    print("save a file to restart; Ctrl+C to stop\n", flush=True)
    if not args.no_open:
        # Give the server a moment to bind before the browser asks for it.
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
                # The server died on its own, usually a syntax error. Wait for
                # the next save rather than restart-looping on a broken file.
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
