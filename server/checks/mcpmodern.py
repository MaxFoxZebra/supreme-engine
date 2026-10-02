"""The MCP server on a 2026-07-28 connection, driven by the SDK's own client.

mcpclient.py speaks the handshake-era protocol by hand, the way most clients
still do. This is the other half: a client that negotiates 2026-07-28, where
questions to the user come back as "input required" and the call is repeated
with the answer, and change events arrive on a subscriptions/listen stream.

    python checks/mcpmodern.py [python] [workspace]
"""

from __future__ import annotations

import json
import sys
import tempfile
import warnings
from pathlib import Path

import anyio

warnings.simplefilter("ignore")
from mcp import Client, StdioServerParameters, types  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
fails = 0
asked: list[str] = []
answers: list = []          # what the next question gets: a callable of the schema's properties


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + str(detail) if detail else ''}")
    fails += not ok


async def on_elicit(ctx, params):
    asked.append(params.message)
    props = params.requested_schema["properties"]
    return answers.pop(0)(props) if answers else types.ElicitResult(action="decline")


yes = lambda props: types.ElicitResult(action="accept", content={"confirm": True})
no = lambda props: types.ElicitResult(action="decline")
second = lambda props: types.ElicitResult(action="accept", content={"choice": props["choice"]["enum"][1]})


async def main(py: str, ws: Path) -> int:
    import os
    home = Path(tempfile.mkdtemp())     # a ~/.claude of its own, for the skill added below
    params = StdioServerParameters(command=py, args=["server_main.py", "--mcp", "--workspace", str(ws)],
                                   cwd=str(HERE), env={**os.environ, "HOME": str(home)})
    async with Client(params, elicitation_callback=on_elicit) as c:
        check("the connection is 2026-07-28", c.protocol_version == "2026-07-28", c.protocol_version)

        # What a client shows: titles, icons, the shapes results come in, and
        # how long it may keep the lists.
        tl = await c.list_tools()
        tools = {t.name: t for t in tl.tools}
        check("every tool has a title and an icon",
              all(t.title and t.icons for t in tools.values()),
              [n for n, t in tools.items() if not (t.title and t.icons)])
        check("structured results say their shape",
              "candidates" in json.dumps(tools["find_job"].output_schema or {})
              and tools["render_cv"].output_schema is None)
        check("the tool list may be cached for an hour", getattr(tl, "ttl_ms", None) == 3_600_000,
              getattr(tl, "ttl_ms", None))
        await c.call_tool("add_job", {"company": "Monzo", "title": "SRE", "status": "interviewing"})
        r = await c.complete(types.PromptReference(type="ref/prompt", name="interview-prep"),
                             {"name": "application", "value": "mon"})
        check("a prompt's application completes from the applications",
              r.completion.values == ["Monzo – SRE"], r.completion.values)
        r = await c.complete(types.ResourceTemplateReference(type="ref/resource", uri="cvstudio://documents/{+path}"),
                             {"name": "path", "value": "prof"})
        check("a resource address completes the documents", "profile/my-cv.yaml" in r.completion.values)
        seen = []

        async def on_progress(p, total, message):
            seen.append(message)
        await c.call_tool("render_cv", {"path": "profile/my-cv.yaml"}, progress_callback=on_progress)
        check("a render reports its progress", len(seen) >= 2, seen)
        body = lambda r: json.loads(r.content[0].text) if not r.is_error else r.content[0].text

        # Resources
        res = (await c.list_resources()).resources
        uris = [str(r.uri) for r in res]
        doc = "cvstudio://documents/profile/my-cv.yaml"
        check("the base CV and its PDF are resources", doc in uris
              and "cvstudio://pdf/profile/my-cv.yaml" in uris, uris[:3])
        text = (await c.read_resource(doc)).contents[0]
        check("a CV reads as its YAML", "cv:" in text.text and text.mime_type == "application/yaml")
        pdf = (await c.read_resource("cvstudio://pdf/profile/my-cv.yaml")).contents[0]
        check("its PDF reads as a PDF", pdf.mime_type == "application/pdf" and pdf.blob.startswith("JVBER"))

        # Questions, as input_required rounds
        a = body(await c.call_tool("add_job", {"company": "Acme", "title": "Engineer", "status": "applied"}))
        answers.append(no)
        r = await c.call_tool("set_job_status", {"job_id": a["id"], "status": "interviewing"})
        check("a status change is asked about, and a no changes nothing",
              r.is_error and "declined" in r.content[0].text and "Move Acme" in asked[-1])
        answers.append(yes)
        r = body(await c.call_tool("set_job_status", {"job_id": a["id"], "status": "interviewing"}))
        check("a yes makes it", r.get("status") == "interviewing")
        answers.append(yes)
        body(await c.call_tool("add_job", {"company": "Acme", "title": "Analyst"}))
        answers.append(second)
        f = body(await c.call_tool("find_job", {"company": "Acme", "about": "Invite, 2 Oct"}))
        check("several matches become a list to pick from", f["confident"] and len(f["candidates"]) == 1)
        await c.call_tool("update_job_tracking", {"job_id": a["id"], "interview_at": "2099-01-02T10:00:00",
                                                  "description": "First text."})
        n = len(asked)
        answers.extend([yes, yes])
        r = body(await c.call_tool("update_job_tracking", {"job_id": a["id"],
                                                           "interview_at": "2099-01-03T10:00:00",
                                                           "description": "Second text."}))
        check("a call with two questions asks each once, then makes both changes",
              len(asked) - n == 2 and r.get("interview_at") == "2099-01-03T10:00:00"
              and r.get("description") == "Second text.", asked[n:])

        # Change events
        got = []
        async with c.listen(resources_list_changed=True, resource_subscriptions=[doc]) as sub:
            async def edit_outside():
                await anyio.sleep(2.5)
                f = ws / "profile" / "my-cv.yaml"
                f.write_text(f.read_text(encoding="utf-8").replace("Your Role", "Edited in the app"),
                             encoding="utf-8")
                (ws / "profile" / "second.yaml").write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            async with anyio.create_task_group() as tg:
                tg.start_soon(edit_outside)
                with anyio.move_on_after(12):
                    async for ev in sub:
                        got.append(type(ev).__name__)
                        if {"ResourceUpdated", "ResourcesListChanged"} <= set(got):
                            break
                tg.cancel_scope.cancel()
        check("an edit made outside is announced to a subscriber", "ResourceUpdated" in got, got)
        check("and a new document changes the list", "ResourcesListChanged" in got, got)

        # A skill added while connected becomes a prompt, and the client is told.
        got = []
        async with c.listen(prompts_list_changed=True) as sub:
            async def add_skill():
                await anyio.sleep(2.5)
                d = home / ".claude" / "skills" / "cv-studio-salary"
                d.mkdir(parents=True)
                (d / "SKILL.md").write_text("---\nname: cv-studio-salary\ndescription: Negotiate an "
                                            "offer.\n---\n\n# Negotiating\n", encoding="utf-8")
            async with anyio.create_task_group() as tg:
                tg.start_soon(add_skill)
                with anyio.move_on_after(12):
                    async for ev in sub:
                        got.append(type(ev).__name__)
                        break
                tg.cancel_scope.cancel()
        names = [p.name for p in (await c.list_prompts()).prompts]
        check("a new skill becomes a prompt, and the client is told",
              got == ["PromptsListChanged"] and "salary" in names, (got, names))

    print(f"\n{fails} failure(s)" if fails else "\nevery 2026-07-28 check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    py = sys.argv[1] if len(sys.argv) > 1 else sys.executable
    ws = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp())
    sys.exit(anyio.run(main, py, ws))
