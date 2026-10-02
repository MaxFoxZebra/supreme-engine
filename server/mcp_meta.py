"""What an AI client shows for CV Studio's tools and prompts: a title a person
reads ("Render CV" rather than render_cv) and a small icon per kind of tool.

Every icon is a data: URI, so a client draws it without fetching anything:
the server makes no request to show its own face.
"""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import quote

from mcp.types import Icon

TITLES = {
    "list_cvs": "List documents",
    "read_cv": "Read a document",
    "cv_outline": "Outline a CV",
    "write_cv": "Rewrite a CV",
    "edit_cv_fields": "Edit CV fields",
    "create_cv": "New CV or letter",
    "render_cv": "Render and look at the page",
    "design_options": "Themes, fonts and sizes",
    "save_theme": "Save a look as a theme",
    "ats_check": "ATS check",
    "add_language": "Translate a CV",
    "translation_status": "What a translation is missing",
    "mark_translation_current": "Mark a translation up to date",
    "create_letter": "Start a cover letter",
    "write_letter": "Write a cover letter",
    "workspace_info": "Workspace overview",
    "list_jobs": "List applications",
    "read_job": "Read an application",
    "find_job": "Find the application",
    "job_alerts": "What needs attention",
    "calendar": "Interviews and follow-ups ahead",
    "add_job": "Add an application",
    "read_posting": "Read a job posting",
    "set_job_status": "Move an application",
    "update_job_tracking": "Record dates, contacts and documents",
    "save_person": "Record a contact",
    "get_interview_prep": "Read interview prep",
    "save_interview_prep": "Write interview prep",
    "set_company_logo": "Set a company logo",
    "review_changes": "Review changes by AI",
    "show_themes": "Show the CV in other themes",
}

# 24x24 outline glyphs, one per kind of tool, in a neutral ink that reads on
# light and dark alike.
_PATHS = {
    "doc": "M7 3h7l5 5v13H7z M14 3v5h5 M9.5 13h7 M9.5 17h5",
    "page": "M5 3h11l3 3v15H5z M8 8h8 M8 11h8 M8 14h5 M14 17l2 2 3-4",
    "theme": "M12 3a9 9 0 1 0 0 18c1.1 0 1.6-.9 1.2-1.8-.5-1-.1-2.2 1.1-2.2H17a4 4 0 0 0 4-4c0-5-4-10-9-10z M7.5 11.5h.01 M10 7.5h.01 M14.5 7.5h.01",
    "job": "M4 8h16v11H4z M9 8V5h6v3 M4 13h16",
    "person": "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6",
    "calendar": "M4 6h16v15H4z M4 10h16 M8 3v4 M16 3v4",
    "check": "M5 12l4 4 10-10",
    "web": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M3 12h18 M12 3c3 3.5 3 14.5 0 18 M12 3c-3 3.5-3 14.5 0 18",
}
KIND = {
    "list_cvs": "doc", "read_cv": "doc", "cv_outline": "doc", "write_cv": "doc",
    "edit_cv_fields": "doc", "create_cv": "doc", "create_letter": "doc", "write_letter": "doc",
    "add_language": "doc", "translation_status": "doc", "mark_translation_current": "doc",
    "workspace_info": "doc", "render_cv": "page", "ats_check": "check", "review_changes": "check",
    "design_options": "theme", "save_theme": "theme", "show_themes": "theme",
    "list_jobs": "job", "read_job": "job", "find_job": "job", "job_alerts": "job",
    "add_job": "job", "set_job_status": "job", "update_job_tracking": "job",
    "set_company_logo": "job", "save_person": "person", "calendar": "calendar",
    "get_interview_prep": "person", "save_interview_prep": "person", "read_posting": "web",
}


def _svg(path: str) -> str:
    """A glyph as a data: URI. Each " M" in `path` starts a separate stroke."""
    strokes = [seg if seg.startswith("M") else "M" + seg for seg in path.split(" M")]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           'stroke="#6b6b6b" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
           + "".join(f'<path d="{d}"/>' for d in strokes) + "</svg>")
    return "data:image/svg+xml," + quote(svg, safe=":/=,; ")


def tool_icons(name: str) -> list[Icon] | None:
    kind = KIND.get(name)
    return [Icon(src=_svg(_PATHS[kind]), mime_type="image/svg+xml", sizes=["any"])] if kind else None


def server_icons(static_dir: Path) -> list[Icon] | None:
    """CV Studio's own mark, as the server's icon."""
    f = static_dir / "brand-mark.png"
    try:
        data = base64.b64encode(f.read_bytes()).decode("ascii")
    except OSError:
        return None
    return [Icon(src=f"data:image/png;base64,{data}", mime_type="image/png", sizes=["64x64"])]


PROMPT_TITLES = {"apply": "Apply to a job", "inbox": "Catch the tracker up from my mail",
                 "interview-prep": "Prepare an interview"}


def _hints():
    from mcp.server.caching import CacheHint
    hour, five = 3_600_000, 300_000
    return {"tools/list": CacheHint(ttl_ms=hour), "resources/templates/list": CacheHint(ttl_ms=hour),
            "server/discover": CacheHint(ttl_ms=hour), "prompts/list": CacheHint(ttl_ms=five),
            "resources/list": CacheHint(ttl_ms=0), "resources/read": CacheHint(ttl_ms=0)}


CACHE_HINTS = _hints()
