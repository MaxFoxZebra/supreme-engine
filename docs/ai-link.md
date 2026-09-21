# The link between the AI clients and the app

An investigation into what is still clunky about the two halves of CV Studio,
and what to do about it.

**Phases 0 and 1 are built.** Sections 2 to 5 describe what shipped; sections 6
to 8 are still a plan. What each phase covers, and what is left, is in section 8.

The app and the MCP server share a workspace folder and nothing else. That
split is right and should stay. What is missing is not a channel between them;
it is that the app cannot say *who* changed something, *what* they changed, or
*what it used to be* -- and that the model spends most of its tokens
re-reading files to work out where to write.

---

## 1. What is actually in the way

### The app cannot name who did it

`note_mcp_activity()` records the tool, the file and a timestamp. It does not
record the client. So:

- `loneClient()` returns a name only when exactly one client is connected.
  Configure two and every message degrades to "AI" or "An AI client".
- `whoChanged()` has the same limit, so the reload toast and the conflict bar
  both go vague as soon as the setup is realistic.
- There is no way to draw a Claude mark, because nothing knows it was Claude.

`MCP.md` explains this as a protocol limit:

> It records the tool and the file, but not which client called: MCP does not
> pass that down to the server here.

**That is no longer true.** The pinned `mcp==2.2.0` passes `clientInfo` at
initialize, and a tool reaches it through `ctx.session.client_params.client_info`
(`.name`, `.version`). A `Context`-annotated parameter is detected by
`find_context_parameter` and stripped from the published JSON schema, so it
costs nothing in tokens.

There is an even cheaper route that works alongside it. The app writes these
config files itself, in `ai_entry()`, so it already knows which client each
entry is for. Adding `"--client", "claude"` to the args means the server knows
its own identity before any handshake, with no MCP plumbing at all.

Do both: `--client` for configs the app wrote, `clientInfo` as the fallback for
hand-written ones. `ai_status()` only inspects `command` and `--workspace`, so
an extra pair of args does not disturb connection detection.

### There is nowhere to record what changed

The workspace holds CV YAML, `applications.db`, and `.cvstudio-mcp.json` (the
last twenty tool calls: tool, path, time). Nothing records which *fields* were
touched, by whom, or what they held before.

The app already computes a field-level diff -- `changedFields(mine, theirs)`
walks two parsed documents and returns paths -- but only between the editor's
in-memory copy and the file on disk, and only for as long as the conflict bar
is up. It is thrown away on resolve.

### There is no link back to the base CV

`create_cv(copy_from=...)` and `/api/new` with `from` both copy the bytes and
forget where they came from. The New document sheet even has a **Base on**
picker whose answer is discarded the moment the file is written. So "what did
this change from the base resume" is unanswerable today, for the app and for
the model alike.

### The model reads whole files to find out where to write

To change one bullet, the model calls `read_cv` and takes the entire YAML into
context -- roughly 1,500-2,000 tokens for a real two-page CV -- purely to learn
that the bullet it wants is `experience[2].highlights[1]`. It then gets back
`"Applied 3 edit(s) to …"`, which confirms nothing about what actually landed,
so it reads or renders again to check.

### Every render round costs a full-page image

`render_cv` always returns the page as a PNG. A4 at 144dpi is 1190x1684,
resized by the API to 1109x1568, which is about **2,300 tokens per call**.
That is the right price for the final look -- it is the whole point of the tool
-- and the wrong price for the four intermediate iterations that only needed to
know the page count and how full the last page is.

The app already measures last-page fill, in `measureFill()`, by pixel-scanning
the PNG in JavaScript. The model cannot get that number without the picture.

### Skills reach one client out of three

The packaging flow is the clunkiest thing in the app: press **Package for
Claude Desktop**, find the folder, open Customize, open Skills, press `+`,
upload seven zips, four of which have had a section appended explaining that
their local scripts will not run. Codex and Vibe get nothing at all.

MCP has a channel for exactly this -- **prompts** -- and the server registers
none. `mcp==2.2.0` supports them.

---

## 2. Know who: the foundation

Everything visual below depends on this, and it is the smallest change here.

1. `ai_entry()` appends `"--client", <id>` to the args it writes.
2. `server_main.py` accepts `--client` and sets `studio.CLIENT_ID`.
3. The tool decorator in `mcp_server.py` takes a `ctx: Context` and falls back
   to `ctx.session.client_params.client_info` when `--client` was absent,
   mapping the handshake names (`claude-ai`, `claude-code`, `codex`, `vibe`)
   onto the ids the app already uses.
4. `note_mcp_activity()` records `by` and `agent` alongside the tool.

One wrinkle: the decorator wraps `fn` and `functools.wraps` copies its
signature, so `find_context_parameter` inspects the wrapper and sees no
`Context`. The wrapper needs its own `ctx` parameter with the annotation, and
`__signature__` set to the original's parameters plus `ctx`.

Two things fall out for free:

- **Every "an AI client" string becomes a name.** Status bar, reload toast,
  conflict bar, activity log.
- **Proof of connection.** The client card says "connected" on the strength of
  having read a config file; it has no idea whether the server was ever
  actually reached. The activity log now knows. A card that reads
  *Configured -- not heard from yet* until the first tool call lands, then
  *Connected -- last heard from 4 minutes ago*, removes the single most common
  "did that actually work?" moment in the setup.

---

## 3. Provenance: a sidecar, not a comment

Marks need somewhere to live. It must not be the YAML: comments there would
pollute files the user owns, fight `edit_cv_fields`' ruamel round-trip, and put
provenance one bad theme away from printing.

So: `.cvstudio-edits.json`, beside `.cvstudio-mcp.json`, under the same promise
the README already makes about that file -- local bookkeeping between the two
halves, delete it whenever you like.

```json
{
  "version": 1,
  "docs": {
    "applications/datadog/se-datadog.yaml": {
      "base": { "path": "profile/my-cv.yaml", "at": 1758400000, "hash": "9f2c…" },
      "edits": [{
        "at": 1758401234,
        "by": "claude",
        "agent": "Claude Desktop 0.9.3",
        "tool": "edit_cv_fields",
        "field": ["cv", "sections", "experience", 0, "highlights", 1],
        "anchor": { "section": "experience", "entry": "Datadog",
                    "key": "highlights", "i": 1 },
        "from": "Built the metrics pipeline",
        "to": "Cut p99 ingest latency 40% across 3B points/day",
        "to_hash": "a71b…"
      }]
    }
  }
}
```

Four decisions worth stating:

**`by` is an id, not a sentence.** `claude` / `openai` / `mistral` / `you`. It
is what picks the mark to draw.

**`anchor` is how a mark survives reordering.** A path like
`sections.experience.2.highlights.0` is index-based and wrong the moment an
entry is inserted above it. Matching on section plus entry title plus key, with
the index as a fallback, re-anchors through a reorder. When nothing matches,
drop the mark. A mark on the wrong line is worse than no mark, so the failure
mode is silence.

**`to_hash` is what keeps it honest.** If the field no longer hashes to `to_hash`,
someone has edited it since, and the mark either clears or flips to *you*.
That is what makes "last edited by Claude" true rather than "Claude edited this
at some point".

**It is bounded.** `from` and `to` truncated to a few hundred characters, a cap
of roughly 200 edits per document, and anything older than a month dropped. The
file stays small enough to read on every poll.

### Where it gets written

`apply_patches()` is the choke point for field edits from both halves -- the
app's `/api/save` with patches and the MCP server's `edit_cv_fields` both go
through it. Recording there covers both with one change.

The whole-file paths (`write_cv`, `/api/save` with `yaml`) need the diff
computed: parse before, parse after, walk. That is `changedFields` ported from
the frontend to Python, which is worth doing anyway so both halves agree on
what "changed" means.

---

## 4. Lineage: what changed from the base

Separate question, separate answer. Record `base` when a document is created
from another -- `create_cv(copy_from=…)` and `/api/new` with `from` both know
it and both currently throw it away.

With the pointer stored, "what differs from the base resume" is a live
computation over two parsed documents, not a history: walk the tailored CV
against the base and collect the paths that differ. No sidecar entry needed
beyond the pointer, and it stays correct no matter who made the change or when.

These are genuinely two different marks and should look different:

| | Question | Source | Mark |
|---|---|---|---|
| **vs base** | is this line different from the master CV? | computed live | a quiet rule in the gutter |
| **by whom** | who last wrote this line, and when? | sidecar | the client's mark |

A line is often both. The first is the one that answers "is this CV actually
tailored"; the second answers "did I write that or did Claude".

---

## 5. Where the marks go

The repo's own principle is that the selection is the thread through every
view. Provenance should follow it, on all five surfaces, and they should agree.

**Under the document title.** One line: *Tailored from my-cv.yaml · Claude
changed 7 fields · last 12 minutes ago*, clicking through to the list. This is
the direct answer to "last edits by Claude and stuff like that", and it is the
cheapest of the five to build.

**Inspector.** A mark in each field's label gutter, `title` carrying "Claude,
12 minutes ago -- was: …". This is the one that matters most, because it sits
where you are actually reading the text.

**Outline.** A mark beside a section holding touched fields. The count slot is
taken by the entry count, so this goes left of the label.

**YAML tab.** `line_map()` already returns `[start, end]` per block, so a
gutter mark per changed block is very nearly free.

**The rendered page.** `cv_map` gives every entry a band: `{k, name, i, page,
y0, y1}` plus `box.x0`, the left edge of the text column. A mark absolutely
positioned at `y0` and *outside* `box.x0` sits in the page margin, which is
empty on the render -- track-changes in the margin, next to the entry it
belongs to.

Granularity caveat, stated plainly: bands exist at entry level, not bullet
level. `_inject()` only probes lines at column 0, and RenderCV nests highlights
inside the entry call, so they are indented and unprobed. A mark next to *the
job* is buildable today; a mark next to *one bullet* needs extra probes inside
the entry, which is a real but separate piece of work.

### It cannot print

Worth being explicit, since it was an explicit requirement. The PDF is produced
by RenderCV from the YAML alone. Nothing in any of this writes to the YAML: the
lineage and the edits live in a dot-prefixed sidecar, and `document_files()`
already skips dot-prefixed files. The marks are DOM overlays in the app. There
is no code path by which one could reach Typst.

---

## 6. Token efficiency

Roughly what one "tailor my CV for this posting" session costs today:

| Step | Now | Note |
|---|---|---|
| tool schemas, resident | ~2,600 | 16 tools + instructions, every turn |
| `read_cv` | 1,500-2,000 | whole file, to find one index |
| `list_jobs` | 1,500-2,500 | unbounded, 12 fields per row |
| `read_job` | 1,000-3,000 | whole posting plus full status history |
| `render_cv` x5 | ~11,500 | ~2,300 each, image every time |

Seven changes, most valuable first.

**1. An outline tool.** `cv_outline(path)` returning a compact skeleton --
section keys, entry titles, dates, bullet counts, word counts -- so the model
can address an edit without reading the file. Against the starter CV that is
roughly 60 tokens versus 489; against a real two-page CV, roughly 200 versus
2,000. This sits on the hot path of every single task and is the largest single
win available.

**2. Make `edit_cv_fields` return the diff.** `{path, from, to}` per edit, plus
the new page and word counts if they are cheap to hand back. The model stops
re-reading to find out what it did.

This also fixes a real bug. `apply_patches` does `continue` on a bad path, so a
model that mis-indexes gets `"Applied 3 edit(s)"` and believes it. Silent
no-ops are worse than errors here -- report which edits missed.

**3. Make the image opt-in.** `render_cv(path)` returns numbers only: pages,
ATS words, last-page fill, and a `layout_warnings` list computed server-side
(move `measureFill` out of the frontend so both halves can use it).
`render_cv(path, look=True)` returns the picture, for the final check or when
the numbers look wrong. Keep the "look before you report success" instruction;
make looking deliberate. Five rounds saves roughly 9,000 tokens.

While there: `page: int = 1` defaults to the first page, but the page worth
looking at is almost always the last. Accept `page="last"`.

**4. Crop.** `server/checks/crop.py` already exists. For "is the last page
lopsided", the bottom half is enough -- about 600 tokens instead of 2,300.

**5. Bound the job tools.** `list_jobs` takes no limit and returns twelve
fields per row when the model usually wants three. Add `limit`, default around
20, and tighten the default projection. `read_job` returns the whole posting
*and* the full status history every time; let it take a `fields` list.

Also: `_brief()` drops `cv_path` and `letter_path`, so the model cannot see
which CV was sent to an application without a second call -- while the app
draws its link marker from exactly those fields. Cheap to add, and it is the
join the provenance work wants anyway.

**6. Trim the resident schema.** 16 tools is about 2,600 tokens on every turn.
`set_job_status` alone is ~300, because the status vocabulary is prose in the
docstring; as an `enum` on the parameter it is shorter *and* enforced rather
than merely requested. `write_cv` can fold into `edit_cv_fields` (its own
docstring already steers away from it), `workspace_info` into `list_cvs`, and
`design_options` can become a resource -- resources are not in the tool budget.
Sixteen tools to about eleven, ~2,600 tokens to ~1,500.

**7. `readOnlyHint` annotations** on `list_cvs`, `read_cv`, `render_cv`,
`list_jobs`, `read_job`, `find_job`, `job_alerts`, `design_options`,
`workspace_info`. Clients use these to skip approval prompts on reads. Not a
token saving, but the most direct available fix for the everyday clunk of
confirming a read.

---

## 7. Prompts: the real fix for the setup

`mcp==2.2.0` supports `@mcp.prompt()`, and the server registers none.

A prompt arrives through the same stdio pipe the tools already come down. It
appears in Claude Desktop as a slash command. It needs no zip, no upload, no
`~/.claude/skills/`, and it works identically in Claude Code, Codex and Vibe --
none of which can use the packaged skills at all.

Three to start, mirroring the skills that are pure judgement:

- `tailor` -- read the posting, duplicate the base CV, tailor it, check the page
- `sweep-inbox` -- what `skills/cv-studio-inbox/SKILL.md` already says
- `interview-prep`

They cost nothing at rest: prompts are listed, not injected, until invoked.

This does not replace skills for Claude Code, which reads them off disk and is
better served that way. It replaces the packaging dance for everyone else.

**Resources** are the other unused half: `cv://<path>` per document,
`cv://outline/<path>`, `cv://design-options`. A client can attach and cache a
resource rather than spending a tool round-trip on it.

---

## 8. What to build, in what order

**Phase 0 -- know who. Built.** `--client` in the written config, `clientInfo`
off the handshake as the fallback, both recorded in the activity log. Every
"an AI client" is a name now: the status bar, the reload toast, the conflict
bar, the activity log. The client card reports "last heard from 4 minutes ago"
rather than claiming a connection on the strength of having read a config file.

One wrinkle worth remembering: `find_context_parameter` inspects the function
MCPServer is handed, which is the decorator's wrapper, and `functools.wraps`
copies the wrapped signature over it. The wrapper has to set `__signature__`
and `__annotations__` itself. The parameter also cannot start with an
underscore -- `func_metadata` rejects that outright.

**Phase 1 -- provenance and lineage. Built.** `.cvstudio-edits.json` beside the
activity log, the base pointer recorded by `create_cv(copy_from=)` and
`/api/new`, `changed_fields` in Python so both halves measure a change the same
way, and marks on six surfaces: the document rail, the outline, the inspector's
field labels, its individual bullets, a chip in the subbar, and the page margin.

Two things came out differently from the plan. Marks needed a third state:
`last` (newest edit by anyone) and `last_ai` (newest by anyone who is not you)
diverge the moment you type in a document a model worked on, and the summary
wants the second -- "Claude, an hour ago" stays true and stays interesting
after you have edited since. And the page draws marks for entries and the
header only, not sections: a section's mark comes from its entries and sits
directly above the first of them, so marking both put two marks a few
millimetres apart saying the same thing.

Still open from this phase: **per-bullet marks on the rendered page**, which
need probes inside the entry call in `cv_map._inject` (section 5). The
inspector has them per bullet already.

**Phase 2 -- tokens.** `cv_outline`, the diff-returning edit, render modes, the
job-tool limits, `readOnlyHint`, schema trim. Independent of Phase 1 and could
go first if the token cost is the more pressing pain.

**Phase 3 -- prompts and resources.** The largest reduction in setup clunk per
line of code in this document.

**Phase 4 -- optional.** Margin marks on the rendered page at bullet
granularity, which needs finer Typst probes. And elicitation
(`session.elicit_form`) for status changes: the instructions currently carry
"show the user every change and wait", enforced by nothing, and `MCP.md` is
candid that it is "a rule in prose, not a lock in the code". Elicitation would
make it a lock, which is the posture the rest of this codebase takes. It costs
a round trip and client support varies, so it needs a capability check and a
fallback to the prose rule.

---

## 9. Found on the way

- **`apply_patches` swallowed failed patches.** A mis-indexed edit was a silent
  no-op reported as a success, to the model and to the user. *Fixed:* it now
  returns what it applied and what it missed, and `edit_cv_fields` names each
  path it could not write.
- **`MCP.md` was stale on `clientInfo`.** It stated the protocol does not pass
  the client down. Under the pinned `mcp==2.2.0` it does. *Fixed.*
- **`checks/mcpclient.py` pointed at a `profile/hard.yaml` that nothing
  creates**, so every assertion below it failed against a missing file.
  *Fixed:* the check now creates the document it edits.
- **`_brief()` hides `cv_path` / `letter_path`** from every job tool, so the
  model cannot answer "which CV did I send to Acme?" without a second call.
  Still open, and wanted by the provenance work: it is the join between a
  tailored CV and the application it was tailored for.
- **`render_cv`'s failure string** builds `('Likely cause: ' + hint) if hint
  else ''` inside an f-string, leaving a stray blank line when there is no
  hint. Cosmetic.
