"""MCP server for CV Studio.

Lets an AI client (Claude Desktop, or anything else speaking MCP) read, edit and
render the CVs in a CV Studio workspace. It shares the same code as the desktop
app, so a CV rendered here is byte-identical to one rendered by clicking Save.

The important design choice is that `render_cv` returns the rendered page as an
*image*, not just a file path. That lets the model actually look at the result
and catch what only shows up visually: a bullet stranded alone on page two, a
heading orphaned at a page break, a lopsided final page. Those are invisible in
the YAML and obvious in the picture.

Run it with:  cv-studio-server --mcp
"""

from __future__ import annotations

import base64
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent

import studio
from cv_render import render_file

mcp = MCPServer(
    name="cv-studio",
    instructions=(
        "Read, edit and render CVs in the user's CV Studio workspace.\n\n"
        "CVs are RenderCV YAML files. Two rules save most failures:\n"
        "1. A colon followed by a space inside a bullet turns it into a YAML "
        "field. Quote such text or use a >- block.\n"
        "2. Phone numbers are validated against real numbering plans, not just "
        "their format.\n\n"
        "After editing, always call render_cv and look at the returned page "
        "image before telling the user it is done. Page-break problems are "
        "invisible in the source."
    ),
)


def _ws() -> Path:
    return studio.WORKSPACE


@mcp.tool()
def list_cvs() -> list[dict]:
    """List every CV in the workspace, with its path and where it lives."""
    return studio.list_documents()


@mcp.tool()
def read_cv(path: str) -> str:
    """Read a CV's YAML source. `path` is relative to the workspace."""
    return studio.safe_path(path).read_text(encoding="utf-8")


@mcp.tool()
def write_cv(path: str, content: str) -> str:
    """Overwrite a CV's YAML source with `content`.

    Prefer edit_cv_fields for small changes: this replaces the whole file and
    will drop any comments the user wrote that are not in `content`.
    """
    p = studio.safe_path(path)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}"


@mcp.tool()
def edit_cv_fields(path: str, edits: list[dict]) -> str:
    """Change individual fields, preserving the rest of the file and its comments.

    Each edit is {"path": ["cv", "headline"], "value": "Solutions Engineer"}.
    List positions are integers: ["cv","sections","experience",0,"company"].
    """
    p = studio.safe_path(path)
    studio.apply_patches(p, edits)
    return f"Applied {len(edits)} edit(s) to {path}"


@mcp.tool()
def create_cv(name: str, copy_from: str | None = None) -> str:
    """Create a CV, either blank or duplicated from an existing one.

    Duplicating is the normal way to tailor: copy the base CV, then edit the
    copy for a specific job so the original stays intact.
    """
    safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe:
        raise ValueError("Give the CV a name.")
    dest = studio.safe_path(f"profile/{safe}.yaml")
    if dest.exists():
        raise ValueError(f"{safe}.yaml already exists.")
    dest.write_text(
        studio.safe_path(copy_from).read_text(encoding="utf-8") if copy_from
        else studio.STARTER_CV,
        encoding="utf-8",
    )
    return f"Created profile/{safe}.yaml"


@mcp.tool()
def render_cv(path: str, page: int = 1) -> list:
    """Render a CV to PDF and return the page as an image to look at.

    Returns the page count, the word count an ATS would extract, the PDF's
    location, and an image of the requested page. Check the image before
    reporting success: page-break damage does not show up in the YAML.
    """
    p = studio.safe_path(path)
    out = (p.parent / "output") if p.parent.name != "profile" \
        else (_ws() / "assets" / p.stem)
    result = render_file(p, out)

    if not result.get("ok"):
        log = (result.get("log") or "render failed")[-2500:]
        hint = studio.friendly(log)
        return [f"RENDER FAILED\n\n{('Likely cause: ' + hint) if hint else ''}\n\n{log}"]

    pages = result["pages"]
    summary = (
        f"Rendered {path}\n"
        f"Pages: {pages}\n"
        f"Words an ATS reads: {result['ats_word_count']}\n"
        f"PDF: {result['pdf']}"
    )
    out_blocks: list = [summary]

    idx = max(1, min(page, pages)) - 1
    if result.get("png_pages"):
        data = Path(result["png_pages"][idx]).read_bytes()
        out_blocks.append(ImageContent(
            type="image",
            data=base64.b64encode(data).decode("ascii"),
            mime_type="image/png",
        ))
    return out_blocks


@mcp.tool()
def design_options() -> dict:
    """The themes, fonts and page sizes available for the design block."""
    return {
        "themes": studio.THEMES,
        "fonts": studio.font_families(),
        "page_sizes": studio.PAGE_SIZES,
        "note": "Set these under design.theme, design.typography.font_family.body "
                "and design.page.size.",
    }


@mcp.tool()
def workspace_info() -> dict:
    """Where the workspace is and what is in it."""
    ws = _ws()
    return {
        "workspace": str(ws),
        "cv_count": len(studio.list_documents()),
        "storage": "Plain YAML files. No database, so the user owns these files.",
    }


def main(workspace: str | None = None) -> int:
    studio.WORKSPACE = Path(workspace).resolve() if workspace else studio.DEFAULT_WORKSPACE
    studio.bootstrap(studio.WORKSPACE)
    mcp.run(transport="stdio")
    return 0
