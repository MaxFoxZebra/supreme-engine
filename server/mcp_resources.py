"""The workspace as MCP resources, kept fresh with subscriptions.

Tools are what a model calls. Resources are what a person, or a client on
their behalf, attaches: "my base CV", "the Monzo posting", "the PDF I sent".
Clients that show resources list these in their attach menu, and read one
only when it is attached, so nothing reaches the model that nobody picked.

    cvstudio://documents/<path>          a CV (YAML) or a letter (Markdown)
    cvstudio://pdf/<path>                its PDF, rendered if it is out of date
    cvstudio://applications/<id>         an application, as JSON
    cvstudio://applications/<id>/posting its posting, as Markdown

The list is read from the workspace on every request, so a document made a
minute ago is there without a restart. And the workspace is watched while a
client is connected: when the user edits a CV in the app, or a status
changes, every client that subscribed to that resource is told, and every
client is told when something is added or removed. Both ways of saying so are
served: `resources/subscribe` with notifications on the session (clients
from before 2026-07-28), and events on `subscriptions/listen` streams (from
then on), published to the server's subscription bus.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import quote, unquote

import anyio
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import PromptsListChanged, ResourcesListChanged, ResourceUpdated
from mcp.types import (Annotations, BlobResourceContents, EmptyResult, ListResourcesResult,
                       ListResourceTemplatesResult, PaginatedRequestParams, ReadResourceRequestParams,
                       ReadResourceResult, Resource, ResourceTemplate, SubscribeRequestParams,
                       TextResourceContents, UnsubscribeRequestParams)

import studio

log = logging.getLogger("cvstudio.resources")

SCHEME = "cvstudio://"
DOC, PDF, APP = SCHEME + "documents/", SCHEME + "pdf/", SCHEME + "applications/"
POLL_SECONDS = 2.0
INVALID_PARAMS = -32602


def _doc_uri(rel: str, prefix: str = DOC) -> str:
    return prefix + quote(rel, safe="/")


def catalog() -> dict[str, dict]:
    """Every resource the workspace has now, by URI, with what to list and a
    stamp that changes whenever its content does."""
    out: dict[str, dict] = {}
    for f, label, group in studio.document_files():
        rel = studio.rel(f)
        try:
            stamp = f.stat().st_mtime
        except OSError:
            continue
        letter = studio.is_letter(f)
        kind = "Cover letter" if group == "Cover letters" else "CV"
        out[_doc_uri(rel)] = {
            "name": rel, "title": f"{kind}: {label}", "stamp": stamp,
            "mime": "text/markdown" if letter else "application/yaml",
            "description": f"The {kind.lower()} source, as the user keeps it.",
            "priority": 0.8 if rel == (studio.base_cv() or {}).get("path") else 0.5,
        }
        out[_doc_uri(rel, PDF)] = {
            "name": rel.rsplit(".", 1)[0] + ".pdf", "title": f"{kind} PDF: {label}",
            "stamp": stamp, "mime": "application/pdf",
            "description": "As it prints, rendered first if the source changed since.",
            "priority": 0.4,
        }
    if studio.jobstore is not None:
        for job in studio.jobstore.list_jobs(studio.WORKSPACE):
            who = f"{job['company']} – {job['title']}"
            out[APP + job["id"]] = {
                "name": f"application-{job['id'][:8]}", "title": f"Application: {who}",
                "stamp": job.get("updated_at"), "mime": "application/json",
                "description": f"{who}: status, dates, people, documents and history.",
                "priority": 0.6,
            }
            if job.get("description"):
                out[APP + job["id"] + "/posting"] = {
                    "name": f"posting-{job['id'][:8]}", "title": f"Posting: {who}",
                    "stamp": job.get("updated_at"), "mime": "text/markdown",
                    "description": "The job posting as saved with the application.",
                    "priority": 0.6,
                }
    return out


def read(uri: str) -> tuple[str, str | bytes]:
    """(mime type, content) for one resource."""
    if uri.startswith(DOC) or uri.startswith(PDF):
        rel = unquote(uri[len(DOC) if uri.startswith(DOC) else len(PDF):])
        p = studio.safe_path(rel)
        if not p.is_file():
            raise MCPError(code=INVALID_PARAMS, message=f"There is no document at {rel}.")
        if uri.startswith(DOC):
            return ("text/markdown" if studio.is_letter(p) else "application/yaml",
                    p.read_text(encoding="utf-8"))
        if studio.is_letter(p):
            return "application/pdf", studio.letter_export(p, "pdf")[0]
        pdf, failed = studio.current_pdf(p)
        if pdf is None:
            raise MCPError(code=INVALID_PARAMS, message=(failed or {}).get("hint")
                           or f"{rel} does not render; render_cv says why.")
        return "application/pdf", pdf.read_bytes()
    if uri.startswith(APP):
        job_id, _, part = uri[len(APP):].partition("/")
        job = next((j for j in studio.jobstore.list_jobs(studio.WORKSPACE) if j["id"] == job_id), None)
        if job is None:
            raise MCPError(code=INVALID_PARAMS, message="No such application.")
        if part == "posting":
            return "text/markdown", job.get("description") or ""
        if not part:
            return "application/json", json.dumps(job, ensure_ascii=False, indent=2, default=str)
    raise MCPError(code=INVALID_PARAMS, message=f"Unknown resource {uri}. resources/list lists them.")


class Live:
    """The resource handlers, the subscriptions and the watcher, for one
    server process. A process serves one client over stdio, but nothing here
    assumes that."""

    def __init__(self, mcp, prompts_changed=None) -> None:
        self.mcp = mcp
        # Called on every tick, in a worker thread: True when the prompts were
        # re-read from changed skills, so clients are told to list them again.
        self.prompts_changed = prompts_changed
        # Each request hands over its own session object around one shared
        # connection, so both are keyed by the connection: one client, one
        # notification.
        self.sessions: dict[int, object] = {}
        self.subscribed: dict[str, dict[int, object]] = {}   # uri -> sessions (pre-2026 clients)
        self.last: dict[str, object] = {}

    # ---- handlers --------------------------------------------------------

    @staticmethod
    def _key(session) -> int:
        return id(getattr(session, "_connection", session))

    def _seen(self, ctx) -> None:
        try:
            if ctx.session is not None:
                self.sessions[self._key(ctx.session)] = ctx.session
        except Exception:
            pass

    async def list_resources(self, ctx, params: PaginatedRequestParams | None) -> ListResourcesResult:
        self._seen(ctx)
        items = await anyio.to_thread.run_sync(catalog)
        return ListResourcesResult(resources=[
            Resource(uri=uri, name=m["name"], title=m["title"], description=m["description"],
                     mime_type=m["mime"],
                     annotations=Annotations(audience=["user", "assistant"], priority=m["priority"]))
            for uri, m in items.items()])

    async def list_templates(self, ctx, params: PaginatedRequestParams | None) -> ListResourceTemplatesResult:
        return ListResourceTemplatesResult(resource_templates=[
            ResourceTemplate(uri_template=DOC + "{+path}", name="document",
                             title="A CV or a letter, by its path in the workspace",
                             mime_type="application/yaml"),
            ResourceTemplate(uri_template=PDF + "{+path}", name="pdf",
                             title="A document's PDF, as it prints", mime_type="application/pdf"),
            ResourceTemplate(uri_template=APP + "{id}", name="application",
                             title="An application, by its id", mime_type="application/json"),
            ResourceTemplate(uri_template=APP + "{id}/posting", name="posting",
                             title="An application's saved posting", mime_type="text/markdown"),
        ])

    async def read_resource(self, ctx, params: ReadResourceRequestParams) -> ReadResourceResult:
        self._seen(ctx)
        uri = str(params.uri)
        try:
            mime, content = await anyio.to_thread.run_sync(read, uri)
        except MCPError:
            raise
        except Exception as exc:
            raise MCPError(code=INVALID_PARAMS, message=f"{uri} could not be read: {exc}")
        studio.note_mcp_activity("read_resource", uri.removeprefix(SCHEME))
        if isinstance(content, bytes):
            import base64
            return ReadResourceResult(contents=[BlobResourceContents(
                uri=uri, mime_type=mime, blob=base64.b64encode(content).decode("ascii"))])
        return ReadResourceResult(contents=[TextResourceContents(uri=uri, mime_type=mime, text=content)])

    async def subscribe(self, ctx, params: SubscribeRequestParams) -> EmptyResult:
        self._seen(ctx)
        self.subscribed.setdefault(str(params.uri), {})[self._key(ctx.session)] = ctx.session
        return EmptyResult()

    async def unsubscribe(self, ctx, params: UnsubscribeRequestParams) -> EmptyResult:
        self.subscribed.get(str(params.uri), {}).pop(self._key(ctx.session), None)
        return EmptyResult()

    async def _note_session(self, ctx, call_next):
        """Middleware: every request names its session, so list changes reach
        a client that only ever calls tools or lists prompts."""
        self._seen(ctx)
        return await call_next(ctx)

    def install(self) -> None:
        low = self.mcp._lowlevel_server
        low.middleware.append(self._note_session)
        low.add_request_handler("resources/list", PaginatedRequestParams, self.list_resources)
        low.add_request_handler("resources/templates/list", PaginatedRequestParams, self.list_templates)
        low.add_request_handler("resources/read", ReadResourceRequestParams, self.read_resource)
        low.add_request_handler("resources/subscribe", SubscribeRequestParams, self.subscribe)
        low.add_request_handler("resources/unsubscribe", UnsubscribeRequestParams, self.unsubscribe)

    # ---- the watcher -----------------------------------------------------

    async def check_prompts(self) -> None:
        if self.prompts_changed is None or not await anyio.to_thread.run_sync(self.prompts_changed):
            return
        await self.mcp._subscriptions.publish(PromptsListChanged())
        for key, session in list(self.sessions.items()):
            try:
                await session.send_prompt_list_changed()
            except Exception:
                self.sessions.pop(key, None)

    async def check(self) -> None:
        """Compare the workspace with the last look, and say what changed."""
        now = {uri: m["stamp"] for uri, m in (await anyio.to_thread.run_sync(catalog)).items()}
        before, self.last = self.last, now
        if not before:
            return
        changed = [u for u, s in now.items() if u in before and before[u] != s]
        listing = set(now) != set(before)
        bus = self.mcp._subscriptions
        for uri in changed:
            await bus.publish(ResourceUpdated(uri=uri))
            for key, session in list(self.subscribed.get(uri, {}).items()):
                try:
                    await session.send_resource_updated(uri)
                except Exception:
                    self.subscribed[uri].pop(key, None)
        if listing:
            await bus.publish(ResourcesListChanged())
            for key, session in list(self.sessions.items()):
                try:
                    await session.send_resource_list_changed()
                except Exception:
                    self.sessions.pop(key, None)

    async def watch(self) -> None:
        while True:
            try:
                await self.check()
                await self.check_prompts()
            except Exception:
                log.debug("resource watch failed", exc_info=True)
            await anyio.sleep(POLL_SECONDS)


async def run_stdio(mcp, prompts_changed=None) -> None:
    """mcp.run(transport="stdio"), with the resources served and watched,
    and list changes advertised to pre-2026 clients."""
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.server.stdio import stdio_server

    live = Live(mcp, prompts_changed)
    live.install()
    low = mcp._lowlevel_server
    async with stdio_server() as (read_stream, write_stream), anyio.create_task_group() as tg:
        tg.start_soon(live.watch)
        await low.run(read_stream, write_stream, low.create_initialization_options(
            NotificationOptions(resources_changed=True, prompts_changed=True)))
        tg.cancel_scope.cancel()
