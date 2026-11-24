#!/usr/bin/env python3
"""CV Studio: local server behind the desktop app.

Thin entrypoint and backwards-compatible facade over the modular `studio_core`
package. Runs entirely on 127.0.0.1 with plain YAML files on disk.
"""

from __future__ import annotations

import argparse
import atexit
import html  # noqa: F401 - re-exported for legacy callers
import secrets
import sys
import threading
import time
import types
import webbrowser
from pathlib import Path

from studio_core import (
    connectors,
    context,
    documents,
    history_scans,
    http_server,
    letters_prep,
    openapi,
    provenance,
    translations,
    workspace,
)
from studio_core.context import (  # noqa: F401
    ALLOWED_HOSTS,
    CLIP_HTML,
    CLIP_PORT,
    DEFAULT_WORKSPACE,
    DOCS_HTML,
    EDITS_FILE,
    EDITS_KEEP,
    EDITS_MAX_AGE,
    EDITS_VALUE_CHARS,
    INDEX_HTML,
    LOOPBACK_NAMES,
    PAGE_SIZES,
    PHOTO_FILE,
    PHOTO_MAX,
    SAFE_ASSET,
    STARTER_CV,
    STARTER_CV_BEFORE,
    STATIC_DIR,
    THEMES,
    ats,
    backups,
    cjkfonts,
    ctx,
    cv_map,
    history,
    importer,
    is_cv_yaml,
    is_letter,
    jobstore,
    languages,
    letters,
    posting,
    rel,
    render_file,
    review,
    safe_path,
    sample,
    scans,
    themes,
    to_plain,
    yaml_rt,
)
from studio_core.connectors import *  # noqa: F401, F403
from studio_core.connectors import _claude_appdata_config  # noqa: F401
from studio_core.documents import *  # noqa: F401, F403
from studio_core.history_scans import *  # noqa: F401, F403
from studio_core.history_scans import _all_jobs  # noqa: F401
from studio_core.http_server import Handler, Server, ServerConfig, start_embedded_server  # noqa: F401
from studio_core.letters_prep import *  # noqa: F401, F403
from studio_core.letters_prep import _base_sig, _match_sig  # noqa: F401
from studio_core.openapi import openapi_spec  # noqa: F401
from studio_core.provenance import *  # noqa: F401, F403
from studio_core.provenance import _edits_read, _edits_write  # noqa: F401
from studio_core.translations import *  # noqa: F401, F403
from studio_core.workspace import *  # noqa: F401, F403
from studio_core.workspace import _logo_stem  # noqa: F401


# Module-level attributes that live on `studio_core.context.ctx` so tests and
# callers reading or assigning `studio.WORKSPACE = ...` stay 100% synchronized
# with every `studio_core` submodule.
_CTX_FIELDS = {
    "WORKSPACE": "workspace",
    "REAL_WORKSPACE": "real_workspace",
    "FIRST_RUN": "first_run",
    "API_TOKEN": "api_token",
    "VERSION": "version",
    "STARTED": "started",
    "OPEN_NETWORK": "open_network",
    "OWN_PORTS": "own_ports",
    "CLIP_STATE": "clip_state",
    "IS_MCP": "is_mcp",
    "CLIENT_ID": "client_id",
    "CLIENT_AGENT": "client_agent",
}


class _StudioModule(types.ModuleType):
    def __getattribute__(self, name: str):
        field = _CTX_FIELDS.get(name)
        if field is not None:
            return getattr(ctx, field)
        if name == "_claude_appdata_config":
            return connectors._claude_appdata_config
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value):
        field = _CTX_FIELDS.get(name)
        if field is not None:
            setattr(ctx, field, value)
            return
        if name == "_claude_appdata_config":
            connectors._claude_appdata_config = value
            return
        super().__setattr__(name, value)


if __name__ != "__main__":
    sys.modules[__name__].__class__ = _StudioModule


def main() -> int:
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
    ap.add_argument("--spawned-at", type=int, default=None,
                    help="When the desktop app launched this, in ms since the epoch.")
    ap.add_argument("--embedded", action="store_true",
                    help="Serve the page without desktop-only background jobs.")
    args = ap.parse_args()
    imported = time.time()

    if args.app_version:
        ctx.version = args.app_version
    if args.parent_pid:
        if not args.embedded:
            workspace.log_to_file()
        workspace.watch_parent(args.parent_pid)

    ctx.workspace = Path(args.workspace).resolve() if args.workspace else DEFAULT_WORKSPACE
    ctx.first_run = workspace.bootstrap(ctx.workspace)
    cjkfonts.register()
    if not args.embedded:
        history_scans.start_backups()
    threading.Thread(target=history_scans.note_history, args=("elsewhere",), daemon=True).start()
    if not args.embedded:
        connectors.start_connector_refresh()
    ready = time.time()

    ctx.api_token = args.token
    if args.host not in LOOPBACK_NAMES and not ctx.api_token:
        ctx.api_token = secrets.token_urlsafe(24)
        print(f"Generated API token: {ctx.api_token}")

    url = f"http://{args.host}:{args.port}/"
    ctx.own_ports.add(args.port)
    if args.host not in LOOPBACK_NAMES:
        ctx.open_network = True
    if args.host in LOOPBACK_NAMES and not args.embedded:
        letters_prep.start_clip_listener()
    if args.parent_pid and not args.embedded:
        workspace.note_app_open(args.port)
        atexit.register(workspace.clear_app_open)

    with Server((args.host, args.port), Handler) as httpd:
        spawned = args.spawned_at / 1000 if args.spawned_at else None
        started = ctx.started
        print("startup    : " + ", ".join(filter(None, [
            f"system {started - spawned:.1f}s" if spawned and started else None,
            f"imports {imported - started:.1f}s" if started else None,
            f"workspace {ready - imported:.1f}s",
            f"listening after {time.time() - (spawned or started or imported):.1f}s"])))
        print(f"CV Studio  -> {url}")
        if ctx.api_token:
            print("Auth       : X-API-Key required")
        print(f"Workspace  : {ctx.workspace}")
        print(f"Documents  : {len(workspace.list_documents())}")
        if args.open:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
