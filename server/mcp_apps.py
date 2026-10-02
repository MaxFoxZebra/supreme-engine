"""CV Studio's MCP Apps views: interactive pages an AI client shows in the chat.

A tool names its view in `_meta.ui.resourceUri`; a client that supports MCP
Apps (extension io.modelcontextprotocol/ui) reads that ui:// resource and
shows it beside the tool's result, sandboxed. Every tool still answers in
text and images for a client that does not, so nothing depends on a view.

    render_cv       ui://cv-studio/page.html    the page, its fill, each page, the PDF
    show_themes     ui://cv-studio/themes.html  the CV in several themes, one click to switch
    review_changes  ui://cv-studio/review.html  what an AI client changed, Keep or Undo

The views are plain HTML with their script inline and fetch nothing: they work
under the host's default content policy, which allows no network at all.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.apps import Apps

HERE = Path(__file__).resolve().parent / "mcp_apps"

VIEWS = {
    "render_cv": ("ui://cv-studio/page.html", "page.html", "The page"),
    "show_themes": ("ui://cv-studio/themes.html", "themes.html", "Themes"),
    "review_changes": ("ui://cv-studio/review.html", "review.html", "Changes to review"),
}
# Tools only a view calls: the model never sees them in its list, and the
# host refuses them from anything but a CV Studio view. resolve_change is the
# Keep and Undo buttons: the decision stays with the person.
APP_ONLY = {"preview_image", "resolve_change"}


def _html(name: str) -> str:
    shared = (HERE / "_shared.html").read_text(encoding="utf-8")
    return (HERE / name).read_text(encoding="utf-8").replace("{{SHARED}}", shared)


def build() -> Apps:
    apps = Apps()
    for uri, file, title in VIEWS.values():
        apps.add_html_resource(uri, _html(file), name=file.removesuffix(".html"),
                               title=f"CV Studio: {title.lower()}", prefers_border=True)
    return apps


def tool_meta(name: str) -> dict | None:
    """The `_meta.ui` a tool carries, or None."""
    if name in VIEWS:
        return {"ui": {"resourceUri": VIEWS[name][0]}}
    if name in APP_ONLY:
        return {"ui": {"visibility": ["app"]}}
    return None
