"""MCP server for CV Studio.

Thin entrypoint and backwards-compatible facade over the modular `mcp_app`
package. Shares `studio_core` with the desktop app so every rendered page is
byte-identical, and runs `open_studio` in-process on a daemon thread with
contextvar-isolated actor attribution.

Run it with:  cv-studio-server --mcp
"""

from __future__ import annotations

from pathlib import Path

import studio
from studio_core.context import ctx

from mcp_app.runtime import (  # noqa: F401
    JOBS_VIEW,
    OPEN_WORLD,
    PAGE_VIEW,
    READ_ONLY,
    STUDIO_KEY,
    STUDIO_PORT,
    STUDIO_VIEW,
    TITLES,
    _checkpoint,
    _doc_path,
    _snapshot,
    apps,
    mcp,
    tool,
)
from mcp_app.applications import *  # noqa: F401, F403
from mcp_app.documents import *  # noqa: F401, F403
from mcp_app.job_views import *  # noqa: F401, F403
from mcp_app.page_view import *  # noqa: F401, F403
from mcp_app.studio_bridge import open_studio  # noqa: F401


def main(workspace: str | None = None, client: str | None = None) -> int:
    ctx.workspace = Path(workspace).resolve() if workspace else studio.DEFAULT_WORKSPACE
    ctx.is_mcp = True
    # The app writes the client into the config it installs, so this is known
    # before any handshake. _identify() fills it from clientInfo otherwise.
    if client:
        ctx.client_id = client
        ctx.client_agent = studio.AI_CLIENTS.get(client, {}).get("label")
    studio.bootstrap(ctx.workspace)
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
