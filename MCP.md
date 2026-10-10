# Connecting CV Studio to an AI client

CV Studio ships an MCP server, so Claude Desktop, the ChatGPT app, Codex or any
other MCP client can read, edit and render the CVs in your workspace, and keep
your job applications up to date. It is the same binary the app uses, just
started in a different mode, so a CV rendered through a model is identical to
one rendered by clicking Save.

## The documents

| Tool | What it does |
|---|---|
| `list_cvs` | List every CV in your workspace |
| `read_cv` | Read a CV's YAML source |
| `edit_cv_fields` | Change individual fields, **keeping your comments** |
| `write_cv` | Replace a whole file (blunt; prefer `edit_cv_fields`) |
| `create_cv` | New blank CV or cover letter, or a duplicate of one. This is how you tailor per application |
| `render_cv` | Render to PDF and **return the page as an image** |
| `design_options` | Available themes, fonts and page sizes |
| `workspace_info` | Where the workspace is and what is in it |

## The applications

| Tool | What it does |
|---|---|
| `list_jobs` | Your applications, trimmed to what identifies them |
| `read_job` | One in full, including the posting text you saved |
| `find_job` | Which application a message or event belongs to. Reports what it found and refuses to choose |
| `job_alerts` | The same list the Jobs view shows under Attention |
| `set_job_status` | Move one along the funnel |
| `update_job_tracking` | Interview time, follow-up date, who is writing to you, which CV or letter was sent, and the posting (link, text, place, source) |
| `save_person` | Record someone you are talking to about an application (recruiter, hiring manager, interviewer, referral), from an email thread or an invitation. Completes someone already listed, never removes anyone |
| `get_interview_prep` | The prep for an application's next round as the user sees it: likely questions (with where each comes from and the user's notes), stories that back the posting's asks, questions to ask. `local` means the app made it from the posting alone |
| `save_interview_prep` | Write better questions, stories and asks for that round. The user's notes, rehearsal marks and own questions are kept |
| `ats_check` | Reads a CV's PDF the way an applicant tracking system does: what fails to parse, and which of the posting's keywords it uses |
| `add_job` | Add one, refusing a likely duplicate unless you confirm. Given the posting's link, it reads the title and full text from the job board itself; given the company's website, it fetches the company's logo too |
| `read_posting` | Read a posting from its link exactly as published: Lever, Greenhouse, Ashby and SmartRecruiters from their public feeds, any other page from the job it describes for search engines |
| `set_company_logo` | Give a company a logo, from its website or an image on disk, and use it on every application to that company |

`render_cv` returning an image is the point of the whole thing. The model can
*look* at the rendered page and catch what only shows up visually: a bullet
stranded alone on page two, a heading orphaned at a page break, a lopsided final
page. None of that is visible in the YAML.

Everything the AI does lands in the same files the app is showing, in the same
places: a CV rendered here writes the PDF the app's **Export PDF** opens, and a
cover letter created with `create_cv(kind="letter")` appears in the app's
document list as a letter. There is nothing to sync.

**And the app is watching.** The client starts the MCP server as its own
process, so the app cannot talk to it directly; what the two share is the
workspace folder. Every tool call appends to `.cvstudio-mcp.json` there: tool
name, file, timestamp, last twenty. The app polls the file timestamps it
has open a few times a minute, and asks the server for a fingerprint of the
applications table on the same poll. So a CV edited here reloads in front of
you rather than going stale, a status moved from a chat window updates the open
Jobs table within a couple of seconds, and if you had unsaved edits of your own it asks
instead of letting your next save quietly overwrite the model's work. That file
is local bookkeeping between the two halves; delete it whenever you like.

It records the tool, the file and **which client called**, so the app names
the client rather than saying "an AI client". Two answers, because each covers
what the other cannot: the app writes `--client` into the config it installs,
since it wrote the config and therefore knows; and the server reads `clientInfo`
off the MCP handshake for a config written by hand. A client that names itself
something unrecognised still gets its own title recorded, and is never
attributed to you.

That is also what the card in **Settings → AI clients** reports. "Configured"
means a config file points at this build; "Connected, last heard from 4 minutes
ago" means the client actually started the server and called something. Only
the second is evidence.

## What it cannot do

Worth knowing before you ask for something and get a refusal, or assume
something is safe that is not. This is the whole of it, checked against the
tools rather than remembered.

**It cannot destroy anything.** There is no delete tool for an application and
none for a document, and no tool renames a file. Nothing it can do loses work.

**It cannot rewrite what you wrote.** No tool takes `company`, `title` or
`notes`. It can *append* a dated line to notes — `append_note`, on two of the
tools — and that is all. Your notes are yours; the row stays findable under the
name you know it by. These are not rules it is asked to follow: the parameters
do not exist, so a model misreading its instructions still cannot do it.

**Some fields are yours alone.** Fit score, both salary figures and their
currency, and country are on the record but on no tool. Nothing an AI reads in
a mailbox should be setting what you think a job is worth.

**The posting can be saved at any time.** `add_job` takes the posting URL,
location, source and description, and `update_job_tracking` sets them later,
when the model finds the link a week on or you ask it to "save the posting".
A posting already saved is treated as yours, since you may have edited it:
replacing it takes `replace_posting=True`, which the model should only pass
when you asked for it. A link has to be `http://` or `https://`.

**Titles come from the posting.** A model reading a web page often gets a
summary of it, and a summary can name the job wrongly. So `add_job` with a
link reads the posting itself and uses its title and text, saying so when they
differ from what the model passed. `update_job_tracking` takes a `title` only
to correct a row to the title its posting gives, checked against the link;
any other rename is yours, in the app.

**It cannot see the funnel or export.** The rates, the drop-off, the CSV and
JSON exports are app-side only. It can list the applications and count them
itself.

**It cannot choose your base CV.** `workspace_info` reports which document the
tailored copies start from, so it can copy the right one, but nominating a
different one is a decision left with you.

Two things it *can* do that the tool names hide:

- **Change the design.** `edit_cv_fields` writes anywhere in the file, not only
  under `cv`, so a patch at `["design", "theme"]` switches the theme. Ask
  `design_options` first for the legal values.
- **Attach a document to an application**, via `cv_path` or `letter_path` on
  `update_job_tracking`. That is the second half of tailoring: copy the base,
  edit the copy, then say what it was for. It checks the path exists and is
  the right kind of document before writing it, because the link is a plain
  string in the database with nothing behind it to catch a typo.

## The application tracker

The tracker used to be out of reach here, on the grounds that the documents were
the model's job and the record of what you sent was yours. That changed when the
useful thing turned out to be keeping the record in step with a mailbox, which
only something that can read the mail can do.

So a connected model can list your applications, work out which one an email or
a calendar invitation belongs to, move its status, record an interview, and add
one you never got round to logging.

What it cannot do: delete an application, rename the company or the role, or
overwrite notes you typed. Notes are append-only from this side, a dated line at
a time. There is no delete tool at all, deliberately, because a model misreading
a rejection must not be able to destroy the record. None of those are promises
about good behaviour; they are parameters that do not exist.

**The Google half is not in here.** This app has no integration with anything,
and makes one kind of network request only (company logos, below). Gmail and Calendar reach the model through its own
connectors, and everything flows one way: it reads them, and writes what it
learned in here. Nothing goes back, no events created, no invitations answered,
no mail sent.

One consequence worth knowing. Because no event is ever written to your
calendar, the interview time stored here is the only one this app knows, and it
is a snapshot from the last time you asked. Move an interview in Google and CV
Studio will not notice until the next sweep. Asking the model to check again is
what fixes that, and the skill below tells it to re-read every interview it has
already recorded.

A status change appends to the permanent history the funnel is drawn from, and
this app has no undo. The model is told to show you every change and wait for
you. That one is a rule in prose, not a lock in the code.

### Applying, as a skill

`plugin/skills/cv-studio-apply/SKILL.md` takes a job link to an application ready to
send: it adds the job with the posting read from the board, copies the base CV
in the posting's language and tailors it (never adding experience), checks it
renders and passes the ATS check, attaches it, then writes the motivation text
the application asks for: a cover letter, attached; a short answer when the
form asks a question instead; nothing when it has no place for one. Adding a
job without the skill gets the same steps from the server's instructions and
from `add_job`'s `next`, but the skill carries the judgement: what to reorder,
what to cut, what a letter says, and where honest framing stops. See
[Skills](#skills) for how it reaches each client.

### Interview prep, as a skill

`plugin/skills/cv-studio-interview-prep/SKILL.md` is how a coach would prepare a round:
read the posting and the CV that was actually sent, write the questions this
round will bring, match every ask of the posting to a line of the CV (or say it
is a gap), and suggest questions to ask back, all saved with
`save_interview_prep` so the user rehearses them in the app. Ask "prepare my
Monzo interview".

### The sweep, as a skill

`plugin/skills/cv-studio-inbox/SKILL.md` in this repository is the procedure: what to
read, how to match a message to an application, what each kind of reply means,
and when to ask rather than write. Ask Claude to catch your applications up.

Without it the tools still work and the server's own instructions still carry
the three rules that matter. The skill is what saves you explaining the workflow
every time.

## Setting it up

**Open CV Studio, click the marks in the title bar, and press Set up next to
the client you want.** It writes the entry into that client's config for you,
keeping whatever else is already in there (other servers, your own comments),
and backing the file up first. The same panel afterwards tells you whether
each
client is pointed at this build and this workspace.

Then restart the client: Claude Desktop shows the tools under the connectors
icon; for OpenAI, restart the ChatGPT app or start a new Codex session; for
Hermes, start it or run `/reload-mcp` in a session already open; for Mistral,
start a new Vibe session.

The rest of this section is for doing it by hand.

### Claude Desktop

Settings → Developer → Edit Config, and add:

**Windows**

The installer is per-user (NSIS `currentUser` mode, so it never asks for admin).
Tauri's template installs that to `%LOCALAPPDATA%\CV Studio`, not `Program
Files`, and not under `Programs`:

```json
{
  "mcpServers": {
    "cv-studio": {
      "command": "C:\\Users\\YOU\\AppData\\Local\\CV Studio\\server-dist\\cv-studio-server.exe",
      "args": ["--mcp"]
    }
  }
}
```

Replace `YOU` with your Windows username. JSON has no environment-variable
expansion, so `%LOCALAPPDATA%` will not work here: the path has to be literal.

**macOS**

```json
{
  "mcpServers": {
    "cv-studio": {
      "command": "/Applications/CV Studio.app/Contents/Resources/server-dist/cv-studio-server",
      "args": ["--mcp"]
    }
  }
}
```

Restart Claude Desktop. The tools appear under the connectors icon.

To point it at a workspace other than the default `~/Documents/CV Studio`:

```json
"args": ["--mcp", "--workspace", "C:\\Users\\you\\Documents\\Career"]
```

### OpenAI

The ChatGPT desktop app, the Codex CLI and the Codex IDE extension share one
MCP configuration, at `~/.codex/config.toml`, so setting it up once covers all
three. Add:

```toml
[mcp_servers.cv-studio]
command = "C:\\Users\\YOU\\AppData\\Local\\CV Studio\\server-dist\\cv-studio-server.exe"
args = ["--mcp", "--workspace", "C:\\Users\\YOU\\Documents\\CV Studio"]
```

On macOS the command is
`/Applications/CV Studio.app/Contents/Resources/server-dist/cv-studio-server`.
TOML basic strings take the same backslash escaping as JSON, so Windows paths
need doubling here too.

`codex mcp add cv-studio -- <command> --mcp` does the same thing from the
command line, and `codex mcp list` shows what is configured.

### Hermes Agent

Hermes Desktop, the `hermes` TUI and the CLI all read one config, so setting it
up once covers all three. It is YAML, and `mcp_servers` is a mapping keyed by
server name — the same shape as Claude Desktop's, in a different language:

```yaml
mcp_servers:
  cv-studio:
    command: /Applications/CV Studio.app/Contents/Resources/server-dist/cv-studio-server
    args:
      - --mcp
      - --workspace
      - /Users/you/Documents/CV Studio
```

The file lives in Hermes' home, which is `~/.hermes/config.yaml` on macOS and
Linux and `%LOCALAPPDATA%\hermes\config.yaml` on native Windows. If you have set
`HERMES_HOME`, or you are using a named profile — which is a home of its own, at
`<home>/profiles/<name>` — the config is in there instead, and setting it up
from the app follows the environment variable.

Hermes picks up a changed config on start, or with `/reload-mcp` in a session
that is already open, so you do not have to lose the conversation you are in.

Everything else in that file is left exactly as it was. It is YAML, the app is
already carrying a round-trip YAML parser for the CVs themselves, and this is
the one client config it can edit without reflowing the comments and ordering
around it. Only `command` and `args` are written — a `timeout`, an `env`, a
tool filter you have set on this server stay untouched. The one exception is
`enabled: false`: setting up a server and leaving it switched off would not be
setting it up, so pressing **Set up** switches it back on.

### Mistral Vibe

`~/.vibe/config.toml`. TOML again, but a different shape: Vibe keeps an array of
tables and puts the name inside each one rather than in its header, so this is
an entry appended to a list rather than a table of its own.

```toml
[[mcp_servers]]
name = "cv-studio"
transport = "stdio"
command = "/Applications/CV Studio.app/Contents/Resources/server-dist/cv-studio-server"
args = ["--mcp", "--workspace", "/Users/you/Documents/CV Studio"]
```

A fresh Vibe config contains the line `mcp_servers = []`. Delete it. TOML will
not accept that key and `[[mcp_servers]]` tables in the same file, and the error
you get if you leave it in does not say so. Setting it up from the app removes
the line for you, and refuses rather than guessing if you keep your servers as a
populated inline array instead.

**Le Chat is not here, and cannot be.** Its custom connectors take an https URL
to a remote MCP server. This server is local and speaks stdio over a pipe, so
the only way to reach it from Le Chat would be to expose your CVs and your job
applications to the public internet. That is the opposite of the point.

## Verifying it works

The server speaks MCP over stdio, so you can probe it without a client:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' | cv-studio-server --mcp
```

A healthy server answers with its name and protocol version.

## Things worth knowing

**It edits real files.** `edit_cv_fields` and `write_cv` write to disk
immediately. There is no undo inside the app, but the files are plain YAML, so
keeping the workspace in git gives you a real history.

**Every edit is marked in the app.** Which fields a model changed, what they
said before, and which of them no longer match the CV this one was copied from,
are recorded in `.cvstudio-edits.json` beside the activity log, and drawn in the
app next to the lines themselves. `create_cv(copy_from=...)` is what records the
lineage, so duplicating rather than writing a new file from scratch is what
makes "how does this differ from the base" answerable afterwards.

**And kept for review until the user has seen it.** The first time a tool
writes a document, what the document said before is kept in
`.cvstudio-review.json`. Later writes leave that alone, so the app can show
everything a model changed since the user last looked, as changes to keep or
undo one at a time: a paragraph of a letter, a line of its header, an entry of a
CV, a header field. A document a tool created is one change, which undoing sends
to the trash. Nothing a tool does needs to change for this; it is the app's
bookkeeping, like the marks.

None of it is written into the YAML, so **none of it prints**: the sidecar is
local bookkeeping, the marks are drawn in the app, and the PDF comes from
RenderCV out of the YAML alone.

**A patch that lands nowhere is reported.** `edit_cv_fields` answers with how
many of the edits it applied and names each one it could not, rather than
counting a mis-indexed entry as a success. Applications are files
too, one YAML file each in `tracker/`, so git covers those as well; the Jobs
view also exports them to JSON or CSV. The CV tools refuse to write them: a
model changes an application through `update_job_tracking`, which keeps its
status history and cannot erase notes.

**Comments survive `edit_cv_fields`** because it round-trips through ruamel.
`write_cv` replaces the file wholesale and will drop anything not in the new
content, which is why the tool description steers toward the former.

**It is local.** The server talks to your filesystem, and makes one kind of
network request: when a model adds an application and passes the company's
website, or calls `set_company_logo` with one, the server fetches that
company's icon from that website -- from the page, and from wherever the page
says its icon lives. There is no logo service in between, so nothing learns the
list of companies you apply to that the companies do not already know. The
interface itself makes no request at all: the logo is saved in the workspace
and drawn from there. There is no telemetry. The mail and calendar an AI client
reads reach it through *its* connectors, and this app never sees them. The AI
client sees only what the tools return.

**Only one workspace per configured server.** Add a second entry with a
different `--workspace` if you keep separate sets of CVs.

**Both clients can be connected at once.** They are separate config files and
separate entries; nothing is shared but the workspace, and the app shows the
state of each.

## Skills

The MCP tools are the *doing*; the skills are the procedure around it: from a
job link to an application, the tracker caught up from the inbox, an interview
prepared. They ship with the app as a Claude plugin (`plugin/` in this
repository, bundled beside the server), and reach each client the way that
client reads skills:

| | Skills |
|---|---|
| **Claude Desktop** | One plugin, added once. Customize → Plugins → Add → **Add marketplace** and enter `MaxFoxZebra/supreme-engine` (new versions arrive on their own), or **Upload plugin** with the `cv-studio.plugin` file **Settings → AI clients → Skills → Make the plugin file** writes. |
| **Codex** | Copied into `~/.agents/skills/` when you connect it. |
| **Mistral Vibe** | Copied into `~/.vibe/skills/` when you connect it. |
| **Hermes Agent** | Copied into the `skills/` folder in Hermes' home when you connect it. |
| **Claude Code** | `/plugin marketplace add MaxFoxZebra/supreme-engine`, then `/plugin install cv-studio@cv-studio`. |

The app keeps the copies it made current after an update, and never replaces a
folder of the same name you made yourself; each of its own carries an
`.installed-by-cv-studio` marker. The plugin carries only the skills: the
connector stays in Claude Desktop's config, because a local server bundled in a
plugin runs in Cowork and Claude Code but not in chat.

If you uploaded CV Studio skills to Claude one at a time before, remove them
under Customize → Skills: the plugin replaces them.

**The skills are prompts too.** The server offers each one as an MCP prompt
(*Apply to a job*, *Catch up from my inbox*, *Prepare an interview*), so a
client without skills still has them: in Claude Desktop under the **+** menu,
and wherever else a client lists a server's prompts.

## The page, in the conversation

In a client that shows views (MCP Apps: Claude, and VS Code's chat), `render_cv`
also shows the rendered CV inside the conversation: every page (up to twelve), with each
block on it clickable, the same way the page is in the app's editor. Click a
block and:

- Claude is told which one you mean (its name, and where it is in the YAML),
  so "make this shorter" typed in the chat means that block.
- A box opens under the page: say what should change, or pick *Make it
  shorter*, *Make it stronger*, *Match the posting more closely* or *Remove
  it*, and it goes to the chat as your message, naming the file and the block.
  Claude edits it and renders again, and the new page shows under the old one.

**Keep or undo, from the page.** The changes an AI client made that nobody
has kept or undone yet (the same ones the app's review shows, kept in
`.cvstudio-review.json`) are marked on the page and listed under *Review*.
A changed block's box shows the lines that went and the lines that came,
with *Undo* and *Keep*; *Keep all* and *Undo all* settle everything. The page
does it through `review_change`, a tool only views can call: clients hide it
from the model, and it is not recorded as an AI change or as the model's
activity. It returns the page as it is afterwards, and the view tells the
model what was undone so it does not redo it.

A render that follows a change to the same CV in the same conversation says
what changed since the last one (which blocks, how many entries went, whether
the design did), marks those blocks in the margin and in the outline, and
keeps the page before it: *Before* and *After* switch between the two. The
connector remembers the last render of each document for as long as the
client keeps it running, which is the conversation's lifetime in practice.

The bar over the page says how it lays out: the page count, the words, and
whether it fits on one page or leaves a last page mostly empty. With several
pages, the others sit beside it to click through. Full screen fits the page to
the window and lists every block beside it, to select from there.

**What changed, marked on the page.** After Claude changes a CV or a
letter, the page opens on *Changes*: the real page with Claude's new words
highlighted in green and a thin red stroke where words were cut, like
tracked changes. Typst lays the page out again with a show rule per new
phrase (`server/marks.py`); highlighting takes no room, and the marked page
is used only if every block sits exactly where it does on the page that
prints. *After* is the page without the marks, *Before* the page before the
change. A changed block's box, and each row of *Review*, show the change in
its sentence: the words that went struck through, the words that came in
green, unchanged lines left out.

**Letters too.** A cover letter's paragraphs are blocks like a CV's
entries, and its header (to, date, subject) is one: click one to ask for a
change, and keep or undo Claude's changes paragraph by paragraph.

**Fix it yourself.** Double-click a block, or press *Edit* in its box, and
its own text opens there: the fields of a job (company, title, dates,
bullets one per line), a line of the summary, a paragraph of a letter. Save
writes it through `edit_on_page`, a tool only views can call, as your edit
rather than Claude's: it is not added to what you review, and a change
Claude made stays reviewable with your words in it. A change that would
stop the page rendering (a phone number that is not a real one) is not
saved. Claude is told what you edited, so it keeps your wording.

**Against the posting.** In a client that shows views, `ats_check` shows
the page with the posting beside it: its requirements, each met, partly met
or not shown in the CV, and its keywords, found or missing. A found one
lights the blocks it is in. A missing one opens the box on the job that
already says most of what the posting asks, with *Add evidence for …, only
what I have really done* to finish and send; nothing is sent until you do.
The keywords are a heuristic, and the view says so.

**Made for the chat.** In the conversation the page is as wide as the chat
allows, with most of its side margins cut and the pages sharper (200 ppi), so
it reads there. After a change, the view opens on the change: each changed
block is a card cut from the page as it prints, with the new words marked and
its own *Keep* and *Undo*, and the whole page is folded under them until
asked for. A click on a card opens the page on that block. Earlier renders
fold to one line. The writing tools show no view of their own, since each view
is a new frame in the conversation; `render_cv` asks Claude to make every edit
a request needs first and render once.

**Full screen** is for looking at the finished page. Each new render opens a
new view in the chat, so a full-screen view follows the file's latest render
by itself, and says *Claude is changing it…* when a render of it starts.

**Pinned beside the chat.** In a client that offers it (MCP Apps' `pip`
display mode), a pin button keeps the page in a small window while you go on
talking: the page, its pages, and *Keep all* / *Undo all* for what is waiting.
Pinned or full screen, the view follows the file: each later render of it
replaces what it shows (`page_view_data` with `what="latest"`), so you watch
the CV change as Claude works.

**In the chat's language.** The views' own words follow the locale the
client gives them: French, Spanish and Brazilian Portuguese, else English;
dates and numbers are written that locale's way. Your CV, the posting,
names and notes are never translated. The words are in `server/i18n/views.py`;
`python i18n/build.py` writes them into both views, and
`node checks/i18nviews.js` lists anything new left untranslated.

**Download the PDF.** In a client that can hand a view's file to you
(`ui/download-file`), the bar has a *PDF* button: the file you send with the
application, without opening the app or the folder.

**Earlier renders fold away.** Each render of a CV leaves a page in the
conversation. When the CV has been rendered again since, the earlier view
shrinks to one line, *Earlier version · 2 changes since*, which opens again on
a click. A view finds out by asking the connector, when it loads and when it
is scrolled back into sight; the connector keeps a small record of each render
for that, outside the workspace, for two months.

**Light results.** A render's result carries only the page the view opens on.
The others are stored once by content, outside the workspace, and the view
fetches them through `page_view_data` (another tool only views can call)
while you look at the first, so a long CV does not make a heavy result and a
conversation opened again later still has its pages.

To work on the view, `node checks/viewdev.js` in `server/` shows it the way
Claude frames it, inline in light and dark, full screen and pinned, and reloads when
the file changes; `--shots dir` saves screenshots instead. To see it in Claude
itself without a release, point Claude Desktop's config at the checkout
(`python server_main.py --mcp --workspace <your workspace> --client claude`,
run from `server/`) and restart the connector after each change: a client may
keep the view it loaded.

The model still gets the same summary and page image as before, and a client
without views gets exactly that and nothing more: the pages and the block map
for the view travel in the result's structured content, which the server only
sends to a client that said it can show views. The view is
`server/static/mcp-page.html`, one file with no network access, which follows
the client's light or dark theme.

## The job search, in the conversation

Two more tools show a view (`static/mcp-jobs.html`):

- **`job_stats`** shows how the search is going. It opens with a sentence or
  two on where applications are being lost, worked out from the numbers (the
  weakest step, counting only applications with an outcome there, and the
  source that gets most replies), then: applications sent, the share
  that got a reply and an interview, the median days to a reply, the funnel
  (applied, replied, interviewed, offer, accepted, each as a share of the
  stage before, with who stopped between each and how), every application
  as a dot in the week or month it went out, shaded by how far it got, and
  which sources got replies. 30 days, 90 days, a year or
  everything, switched on the view. Every number opens the applications
  behind it, and each of those opens its card, in the same view. The model
  gets the same numbers as a few lines of text, and is asked to say what they
  mean; *What do these numbers say?* asks it from the view.
- **`show_application`** shows one application as a card: where it is on its
  way from applied to offer, with the day it reached each stage, and the
  one thing to do next, with a countdown and the matching action (a follow-up due, an interview coming, an outcome to
  record, an offer to decide on), when it was applied to and how fast they
  replied compared with your other replies, its history, interview rounds,
  people and documents, and questions that fit where it is (*Draft a
  follow-up email*, *Prepare me for the interview*).

- **`today`** shows what needs the user today, most pressing first:
  interviews coming up (with a countdown and *Prepare me for it*),
  interviews with no outcome recorded, offers to decide on, follow-ups due
  and applications gone quiet (with *Draft a follow-up*), from the tracker's
  own alert rules. Each opens its card; the numbers open from it too.

From a card the user can move an application to its next status. The view
asks first, because the history is permanent and the funnel is drawn from it,
then writes it through `job_view_data` (a tool only views can call) as the
user's own change, with a dated line in the notes, and tells the model so it
does not change it back. Only the statuses that can come next are offered.

The app keeps its own funnel screen: the chat is for the numbers with a
reading of them, and for acting on one application where the conversation is.

**Only for clients that show views.** The views' own tools (`review_change`,
`page_view_data`, `edit_on_page`, `job_view_data`) act as the user: an edit
saved as theirs, a status moved as if they had clicked it. A client that
shows views keeps them from its model. A client that does not (Codex, Vibe,
Hermes) is not told about them at all, and a call to one from it is
refused.

## What a client is told about each tool

Every tool has a title and the standard hints: whether it only reads, whether
it can overwrite something (`write_cv`, a status change) or only adds (a new
CV, a new application), whether calling it twice changes anything more, and
whether it reaches a website (`add_job`, `read_posting`, `set_company_logo`).
Clients use these to decide what to ask permission for, so the read-only tools
can be allowed once and the ones that rewrite a file keep asking. The server
also gives its name, version and the app's icon.

## The connector keeps running through an update

An AI client starts the connector itself and keeps it running as long as the
client is open. On Windows the installer has to close everything running from
the app's folder before it can replace the files, and that used to include the
connector Claude Desktop was using: it stayed disconnected until Claude was
restarted. Now the connector runs from a copy of its own, one per version, in
`%LOCALAPPDATA%\CV Studio\mcp\<version>\cv-studio-mcp.exe`. An update never
touches it. When the app starts after an update it makes the new version's
copy, points every client that runs one of its connectors at it (keeping each
one's workspace and every other server in the file), and deletes old copies
once nothing runs from them. A client picks up the new version the next time
it starts. On macOS and Linux a running program's files can be replaced, so the
connector runs from the app as it always did.

## As a Claude Desktop extension

Each release also packs the connector as a Claude Desktop extension (MCPB),
one per platform: `CV-Studio-<version>-macos-arm64.mcpb`, `-macos-x64` and
`-windows-x64`. It is the same frozen server the app ships, signed the same
way, with a `manifest.json` that tells Claude Desktop how to start it
(`--mcp --workspace <folder> --client claude`) and asks for the one setting
it has, the workspace folder (default `Documents/CV Studio`). A double-click
installs it; installing a newer one replaces it.

Claude Desktop runs it from its own folder (`Claude Extensions/` beside its
config), which the app's installer never touches, so the per-version copy
above is not needed there. The app reads that folder: with the extension
installed, *Settings → AI clients* shows Claude as connected through it,
warns if the config file has a `cv-studio` entry as well (Claude would see
every tool twice) or if the extension points at another folder, and *Connect*
will not add a second copy.

To build one from a local build: `python server/pack_mcpb.py --server
server/dist/cv-studio-server --out dist-mcpb`, then `npx @anthropic-ai/mcpb
validate` on its `manifest.json`.

## Updates

The app checks for updates on launch and under Settings, About. Updates are
signed: the installed copy verifies each package against the public key baked
into its own build, so a compromised release host still cannot push a package
it will install.

Publishing an update:

1. Bump the version in all four places that carry it, which must agree:
   `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml`, the `cv-studio`
   package entry in `src-tauri/Cargo.lock`, and `VERSION` in
   `server/studio.py`. The shell passes its own version to the server with
   `--app-version` so the two cannot drift at runtime, but the constant is
   what a server started by hand reports.
2. Tag and push: `git tag v0.4.0 && git push origin v0.4.0`.
3. CI builds Windows, Apple Silicon and Intel, signs them, and attaches
   `latest.json` to the GitHub release. Installed copies pick it up from there.

Two things must be set up once for this to work:

- **`TAURI_SIGNING_PRIVATE_KEY`** and **`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`**
  as repository secrets. The private key is at `~/.tauri/cvstudio.key` and must
  never be committed. Losing it means no installed copy can ever be updated
  again, so back it up somewhere safe.
- **A public repository.** The updater fetches the release asset without
  credentials, so a private repo returns 404 and updates silently never arrive.
