"""Clean in-process bridge for opening CV Studio beside an AI chat (`open_studio`).

Instead of spawning a duplicate Python subprocess and polling for up to 60s,
this starts CV Studio's HTTP server on a lightweight daemon thread inside the
existing MCP process (sharing the already-loaded RenderCV/Typst/YAML runtime).
HTTP requests run inside `ctx.actor(is_mcp=False)` so edits the user makes in
the embedded app are always attributed to "you", while concurrent MCP tool
calls stay attributed to the AI client. When the MCP process exits, the daemon
thread exits immediately with it.
"""

from __future__ import annotations

import threading
from urllib.parse import quote

from mcp.types import CallToolResult, TextContent

import studio
from studio_core import http_server
from .applications import _job
from .runtime import STUDIO_KEY, STUDIO_PORT, STUDIO_VIEW, _app_note, _doc_path, _one_line, _shows_views, tool

_STUDIO: dict = {"server": None}
_STUDIO_LOCK = threading.Lock()


def _studio_up() -> None:
    """Start CV Studio's embedded HTTP server in-process if it is not up yet."""
    with _STUDIO_LOCK:
        if _STUDIO["server"] is not None:
            return
        _STUDIO["server"] = http_server.start_embedded_server(STUDIO_PORT, STUDIO_KEY)


@tool(view=STUDIO_VIEW)
def open_studio(path: str | None = None, job_id: str | None = None) -> CallToolResult:
    """Open CV Studio itself beside the chat: the whole app, where the user
    edits by hand, changes the design, goes back through the history, or works
    through their applications. Use it when they ask to open CV Studio, the
    app or the editor, or want to do something themselves that the tools here
    do not.

    `path` opens it on that CV or letter, `job_id` on that application, and
    with neither it opens on their applications. What they do there is theirs,
    saved to the same files: read a document again before changing it after
    they have used it.
    """
    if path:
        p = _doc_path(path)
        if not p.is_file():
            raise ValueError(f"There is no document at {path}.")
        rp = studio.rel(p)
        where = ("#/letter/" if studio.is_letter(p) else "#/doc/") + quote(rp, safe="/")
        label = next((d.get("label") for d in studio.list_documents() if d.get("path") == rp), None) or rp
    elif job_id:
        job = _job(job_id)
        where = "#/jobs/" + quote(job["id"], safe="")
        label = ", ".join(x for x in (_one_line(job.get("company"), 60), _one_line(job.get("title"), 80)) if x)
    else:
        where, label = "#/jobs", "Your applications"
    if not _shows_views():
        return CallToolResult(content=[TextContent(type="text", text=(
            "This client does not show views, so CV Studio cannot open beside the chat. "
            + _app_note()["app_note"]))])
    _studio_up()
    return CallToolResult(
        content=[TextContent(type="text", text=(
            f"CV Studio is open beside the chat, on {label}. What the user does there is saved "
            "to the same files these tools read."))],
        # The key is the view's alone: it never reaches the model.
        structured_content={"view": "studio", "label": label,
                            "url": f"http://127.0.0.1:{STUDIO_PORT}/?token={STUDIO_KEY}{where}"})
