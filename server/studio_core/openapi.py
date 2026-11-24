"""OpenAPI 3.1 specification for CV Studio's local HTTP API."""

from __future__ import annotations

from . import connectors as _conn
from .context import ctx


def openapi_spec() -> dict:
    """Describe the local API so it can be driven by other tools."""
    def body(props):
        return {"required": True, "content": {"application/json": {
            "schema": {"type": "object", "properties": props}}}}
    ok = {"200": {"description": "OK"}}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "CV Studio local API",
            "version": ctx.version,
            "description": (
                "Read, edit and render CVs. Binds to 127.0.0.1 by default with no "
                "authentication. Start the server with --token to require an "
                "X-API-Key header; a token is generated automatically when "
                "binding beyond loopback."),
        },
        "paths": {
            "/api/state": {"get": {"summary": "Workspace, documents, themes, fonts",
                                   "responses": ok}},
            "/api/doc": {"get": {"summary": "Read one CV",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}}], "responses": ok}},
            "/api/save": {"post": {"summary": "Save a CV (whole yaml, or field patches)",
                "requestBody": body({
                    "path": {"type": "string"},
                    "yaml": {"type": "string", "description": "Replace the whole file"},
                    "patches": {"type": "array", "description":
                        "Field edits; preserves comments",
                        "items": {"type": "object", "properties": {
                            "path": {"type": "array", "items": {}},
                            "value": {}}}}}), "responses": ok}},
            "/api/render": {"post": {"summary":
                "Render to PDF and PNG, with a map of where each block landed",
                "requestBody": body({"path": {"type": "string"}}), "responses": ok}},
            "/api/review": {"post": {"summary":
                "Keep or undo changes an AI client made to a document, one unit or all ('*')",
                "requestBody": body({"path": {"type": "string"},
                                     "ids": {"type": "array", "items": {"type": "string"}},
                                     "action": {"type": "string", "enum": ["keep", "undo"]},
                                     "sig": {"type": "string"}}),
                "responses": ok}},
            "/api/preview": {"post": {"summary":
                "Render unsaved content without writing the file",
                "requestBody": body({"path": {"type": "string"},
                                     "yaml": {"type": "string"},
                                     "patches": {"type": "array", "items": {}}}),
                "responses": ok}},
            "/api/letter/new": {"post": {"summary":
                "Write a cover letter scaffold for an application, linked to it",
                "requestBody": body({"job_id": {"type": "string"}, "name": {"type": "string"}}),
                "responses": ok}},
            "/api/letter/export": {"get": {"summary":
                "A cover letter as PDF, Word (.docx) or plain text",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}},
                               {"name": "format", "in": "query",
                                "schema": {"type": "string", "enum": ["pdf", "docx", "txt"]}}],
                "responses": ok}},
            "/api/photo": {"post": {"summary":
                "Save the workspace photo, a square JPEG the app has already cropped",
                "requestBody": body({"data": {"type": "string", "description": "base64 JPEG"}}),
                "responses": ok}},
            "/api/photo/remove": {"post": {"summary":
                "Delete the workspace photo and take it off every CV that showed it",
                "responses": ok}},
            "/api/language/add": {"post": {"summary":
                "Write a translation scaffold of a CV in another language",
                "requestBody": body({"path": {"type": "string"},
                                     "language": {"type": "string",
                                                  "description": "code, e.g. fr"}}),
                "responses": ok}},
            "/api/language/done": {"post": {"summary":
                "Mark a translation as covering everything its source says",
                "requestBody": body({"path": {"type": "string"}}),
                "responses": ok}},
            "/api/import": {"post": {"summary":
                "Read a CV from a PDF or a LinkedIn data archive, without writing anything",
                "requestBody": body({"name": {"type": "string"},
                                     "data": {"type": "string", "description": "base64"}}),
                "responses": ok}},
            "/api/theme-preview": {"post": {"summary":
                "Render page one of a document in another theme",
                "requestBody": body({"path": {"type": "string"},
                                     "theme": {"type": "string"},
                                     "patches": {"type": "array", "items": {}}}),
                "responses": ok}},
            "/api/new": {"post": {"summary": "Create a CV, blank or duplicated",
                "requestBody": body({"name": {"type": "string"},
                                     "kind": {"type": "string"},
                                     "from": {"type": "string"}}), "responses": ok}},
            "/api/language/set": {"post": {"summary":
                "Set the language a CV prints in (its locale); the text is not changed",
                "requestBody": body({"path": {"type": "string"}, "language": {"type": "string"}}),
                "responses": ok}},
            "/api/backups": {"get": {"summary":
                "This workspace's backups, newest first, and the folder they are in",
                "responses": ok}},
            "/api/backups/new": {"post": {"summary": "Back the workspace up now",
                "responses": ok}},
            "/api/history": {"get": {"summary":
                "Every version of a document or application file (path, or job id), newest first, with who made it",
                "parameters": [{"name": "path", "in": "query", "schema": {"type": "string"}},
                               {"name": "job", "in": "query", "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/history/diff": {"get": {"summary":
                "What a version changed, line by line: against the version before it, or against the file now",
                "parameters": [{"name": "path", "in": "query", "schema": {"type": "string"}},
                               {"name": "id", "in": "query", "schema": {"type": "string"}},
                               {"name": "against", "in": "query", "schema": {"type": "string",
                                                                           "enum": ["before", "now"]}}],
                "responses": ok}},
            "/api/history/recent": {"get": {"summary":
                "The latest changes across the workspace, newest first", "responses": ok}},
            "/api/history/restore": {"post": {"summary":
                "Put a file back as one of its versions; the restore is a version of its own",
                "requestBody": body({"path": {"type": "string"}, "id": {"type": "string"}}),
                "responses": ok}},
            "/api/scans": {"get": {"summary":
                "What scheduled job searches found: the folder, its files, and the jobs still to look at",
                "responses": ok}},
            "/api/scans/record": {"post": {"summary":
                "Hand over what a search found: one row per job not already in the scans spreadsheet, "
                "decided, or an application",
                "requestBody": body({"jobs": {"type": "array", "items": {"type": "object", "properties": {
                    "company": {"type": "string"}, "role": {"type": "string"}, "link": {"type": "string"},
                    "location": {"type": "string"}, "why": {"type": "string"}, "posted": {"type": "string"},
                    "scan": {"type": "string"}, "notes": {"type": "string"}, "description": {"type": "string"},
                    "details": {"type": "object"}}}},
                    "scan": {"type": "string"}, "found": {"type": "string"}}),
                "responses": ok}},
            "/api/scans/add": {"post": {"summary":
                "Add a job a scan found to the applications, its posting read from its link",
                "requestBody": body({"key": {"type": "string"}}), "responses": ok}},
            "/api/scans/decide": {"post": {"summary":
                "Mark jobs a scan found as New, Added or Skipped",
                "requestBody": body({"keys": {"type": "array", "items": {"type": "string"}},
                                     "status": {"type": "string", "enum": ["New", "Added", "Skipped"]}}),
                "responses": ok}},
            "/api/scans/undo": {"post": {"summary":
                "Take a decision back: the job is waiting again, and the application a yes made goes to the trash",
                "requestBody": body({"key": {"type": "string"}, "job_id": {"type": "string"}}),
                "responses": ok}},
            "/api/scans/create": {"post": {"summary":
                "Make the scans spreadsheet, with its header row, in the scans folder",
                "responses": ok}},
            "/api/backups/restore": {"post": {"summary":
                "Put a backup's files back into the workspace (what is there is backed up first)",
                "requestBody": body({"name": {"type": "string"}}), "responses": ok}},
            "/api/sample": {"post": {"summary":
                "Switch to freshly made sample data (on: true) or back to your own "
                "workspace (on: false). Your workspace is never written to. "
                "applications sets how many to make (default 64, up to 2000)",
                "requestBody": body({"on": {"type": "boolean"}, "applications": {"type": "integer"}}),
                "responses": ok}},
            "/api/prefs": {"post": {"summary":
                "Save the interface's preferences (theme, language, time zone, notifications) "
                "on this machine. set merges; replace swaps them all",
                "requestBody": body({"set": {"type": "object"}, "replace": {"type": "object"}}),
                "responses": ok}},
            "/api/base": {"post": {"summary":
                "Nominate the CV that tailored copies start from; null clears it",
                "requestBody": body({"path": {"type": "string"}}),
                "responses": ok}},
            "/api/jobs": {
                "get": {"summary": "List job applications", "responses": ok},
                "post": {"summary": "Create a job application",
                         "requestBody": body({"title": {"type": "string"},
                                              "company": {"type": "string"},
                                              "status": {"type": "string"}}),
                         "responses": ok}},
            "/api/jobs/update": {"post": {"summary":
                "Update a job; a status change appends to its history",
                "requestBody": body({"id": {"type": "string"},
                                     "status": {"type": "string"}}), "responses": ok}},
            "/api/funnel": {"get": {"summary":
                "Application funnel: node counts, flows and conversion rates",
                "responses": ok}},
            "/api/posting": {"get": {"summary":
                "The posting at a link (?url=), read from its job board's own "
                "record: title, company, location, description as Markdown",
                "responses": ok}},
            "/api/clip": {"get": {"summary":
                "Save to CV Studio, the bookmark: the fixed port its window is "
                "served on, whether it is listening, and the bookmarklet itself",
                "responses": ok}},
            "/api/jobs/prep": {"get": {"summary":
                "Interview prep for an application's next round: likely questions "
                "(each with its source and your notes), stories that back the "
                "posting's asks, and questions to ask them; made here from the "
                "posting and the CV sent until an AI client writes better ones",
                "parameters": [{"name": "id", "in": "query", "schema": {"type": "string"}}],
                "responses": ok},
                "post": {"summary": "Save the prep whole: {id, prep}", "responses": ok}},
            "/api/jobs/draft": {"get": {"summary":
                "What an email about an application can be written from: your "
                "name (from its CV, else the base CV), the dates you applied and "
                "were interviewed, and the posting's first two duties",
                "parameters": [{"name": "id", "in": "query", "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/pack": {"get": {"summary":
                "An application's CV and cover letter as one PDF (format=pdf, CV "
                "first) or a zip of separate files (format=zip, with posting=1 to "
                "add the posting as Markdown). name sets the file name",
                "parameters": [{"name": n, "in": "query", "schema": {"type": "string"}}
                               for n in ("job", "format", "name", "posting")],
                "responses": {"200": {"description": "The file"}}}},
            "/api/pack/info": {"get": {"summary":
                "What /api/pack would put together: the suggested file name and "
                "whether there is a CV, a letter and a posting",
                "parameters": [{"name": "job", "in": "query", "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/search": {"get": {"summary":
                "Documents whose text holds every word of q (accents and case "
                "ignored), with the line where the first word was found",
                "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/alerts": {"get": {"summary":
                "Applications needing attention: interviews due, follow-ups "
                "due, interviews with no outcome, and silence since applying",
                "responses": ok}},
            "/api/calendar.ics": {"get": {"summary":
                "Interviews and follow-ups as an iCalendar file; ?id= for one application",
                "responses": ok}},
            "/api/jobs/trash": {"get": {"summary":
                "Deleted applications still in the trash, newest first; each can be restored",
                "responses": ok}},
            "/api/jobs/export": {"get": {"summary": "Export every job as JSON or CSV",
                "parameters": [{"name": "format", "in": "query",
                                "schema": {"type": "string", "enum": ["json", "csv"]}}],
                "responses": ok}},
            "/api/basediff": {"get": {"summary":
                "What a tailored CV changed from the document it was copied from",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/basediff/revert": {"post": {"summary":
                "Put back what the base CV says for some of the ways a tailored CV differs from it",
                "requestBody": body({"path": {"type": "string"},
                                     "ids": {"type": "array", "items": {"type": "string"}},
                                     "sig": {"type": "string"}}),
                "responses": ok}},
            "/api/ats": {"post": {"summary":
                "What an ATS reads from a CV's PDF, and which of a posting's keywords it uses",
                "requestBody": body({"path": {"type": "string"},
                                     "job_id": {"type": "string"},
                                     "posting": {"type": "string"}}),
                "responses": ok}},
            "/api/ats/fix": {"post": {"summary":
                "Apply one of the design changes an ATS check offers, then check again",
                "requestBody": body({"path": {"type": "string"},
                                     "fix": {"type": "string",
                                             "enum": ["icons", "urls"]}}),
                "responses": ok}},
            "/api/open": {"post": {"summary":
                "Open a web link in the system browser",
                "requestBody": body({"url": {"type": "string"}}),
                "responses": ok}},
            "/api/thumb": {"get": {"summary":
                "A document's first page as last rendered, and whether it is current",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}}],
                "responses": ok}},
            "/api/ai": {"get": {"summary":
                "Whether each AI client is wired up to this build and workspace",
                "responses": ok}},
            "/api/ai/connect": {"post": {"summary":
                "Add this workspace to one AI client's MCP config",
                "requestBody": body({"client": {"type": "string",
                                                "enum": list(_conn.AI_CLIENTS)}}),
                "responses": ok}},
            "/api/skills": {"get": {"summary":
                "The skills that ship with CV Studio, and where each client has them",
                "responses": ok}},
            "/api/skills/package": {"post": {"summary":
                "The skills as one plugin file, for Claude Desktop's Upload plugin",
                "responses": ok}},
            "/api/skills/install": {"post": {"summary":
                "Copy the skills into a client's skills folder (Codex, Vibe, Hermes)",
                "requestBody": body({"client": {"type": "string",
                                                "enum": ["openai", "mistral", "hermes"]}}),
                "responses": ok}},
            "/api/pulse": {"get": {"summary":
                "Document timestamps and recent AI activity, cheap to poll",
                "responses": ok}},
            "/api/asset": {"get": {"summary": "Fetch a rendered PDF or PNG",
                "parameters": [{"name": "path", "in": "query", "required": True,
                                "schema": {"type": "string"}}], "responses": ok}},
            "/api/jobs/delete": {"post": {"summary":
                "Move one job application to the trash (kept 30 days)",
                "requestBody": body({"id": {"type": "string"}}), "responses": ok}},
            "/api/jobs/restore": {"post": {"summary":
                "Put a deleted application back, with its id and history",
                "requestBody": body({"id": {"type": "string"}}), "responses": ok}},
            "/api/doc/rename": {"post": {"summary":
                "Rename a CV or a letter in place; applications, the base CV and "
                "translation links follow it",
                "requestBody": body({"path": {"type": "string"}, "name": {"type": "string"}}),
                "responses": ok}},
            "/api/doc/delete": {"post": {"summary":
                "Move a CV or a letter to the workspace's .trash folder. Refuses the base CV "
                "and a document others are translated from",
                "requestBody": body({"path": {"type": "string"}}), "responses": ok}},
            "/api/download": {"post": {"summary":
                "Save an export (base64 `data`, as `name`) in the Downloads folder, and show it",
                "requestBody": body({"name": {"type": "string"}, "data": {"type": "string"}}),
                "responses": ok}},
            "/api/reveal": {"post": {"summary":
                "Open the workspace, or one path inside it, in the file manager",
                "requestBody": body({"path": {"type": "string"}}), "responses": ok}},
            "/api/docs": {"get": {"summary": "This reference, as a page",
                "responses": ok}},
        },
        "components": {"securitySchemes": {"apiKey": {
            "type": "apiKey", "in": "header", "name": "X-API-Key"}}},
    }


