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
| `cv_outline` | A CV's sections, entries and bullets, each with the exact path `edit_cv_fields` takes, so the model finds what to change without reading the whole file |
| `edit_cv_fields` | Change individual fields, **keeping your comments**. Lists take `append`, `insert` and `remove`, so a bullet can be added or dropped without rewriting the file |
| `write_cv` | Replace a whole file (blunt; prefer `edit_cv_fields`) |
| `create_cv` | New blank CV or cover letter, or a duplicate of one. This is how you tailor per application |
| `render_cv` | Render a CV or a letter to PDF and **return a page as an image**: the last page unless asked for another, with the page count, how full the last page is, and a warning when a few lines spilled over |
| `create_letter` | Start the cover letter for an application, with the subject, greeting, closing and date filled in, and attach it |
| `write_letter` | Replace a letter's body (and subject) |
| `add_language` | Start a translation of a CV, with dates and common section titles already translated |
| `translation_status` | What a translation is missing from the CV it was translated from |
| `mark_translation_current` | Record that a translation has caught up |
| `design_options` | Available themes (the user's own first), fonts and page sizes |
| `save_theme` | Keep a CV's design as a theme of the user's own, in `themes/<name>/` |
| `workspace_info` | Where the workspace is and what is in it, including the base CV to tailor from |

## Resources, and what changed

Tools are what the model calls; resources are what you attach. A client that
shows resources lists these in its attach menu, and reads one only when you
pick it:

| Resource | What it is |
|---|---|
| `cvstudio://documents/<path>` | A CV (YAML) or a cover letter (Markdown) |
| `cvstudio://pdf/<path>` | Its PDF, rendered first if the source changed since |
| `cvstudio://applications/<id>` | An application, as JSON |
| `cvstudio://applications/<id>/posting` | Its saved posting, as Markdown |

The list is read fresh, so a CV made a minute ago is there. And the server
watches the workspace while a client is connected: edit a CV in the app, or
move an application along, and a client that subscribed to it is told, so the
model is not working from the copy it read an hour ago. A client is also told
when something is added or removed. Both protocol generations are served:
`resources/subscribe` with notifications (clients from before 2026-07-28) and
`subscriptions/listen` streams (from then on).

## The applications

| Tool | What it does |
|---|---|
| `list_jobs` | Your applications, trimmed to what identifies them |
| `read_job` | One in full, including the posting text you saved |
| `find_job` | Which application a message or event belongs to. Reports what it found and refuses to choose |
| `job_alerts` | The same list the Applications view shows under Attention |
| `calendar` | Interviews and follow-ups ahead, optionally as an `.ics` file |
| `set_job_status` | Move one along the funnel. Takes the status code (`applied`) or the app's label (`Awaiting reply`) |
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
(That is the MCP server. The app's own HTTP API on `127.0.0.1`, which its
interface uses, can delete; a model with a shell on your machine is not bound by
any of this, and should be told to go through the MCP tools.)

**It is told why when it is refused.** A duplicate, an unknown status, a date
that is not a date, a path outside the workspace: each refusal reaches the model
as the tool's own explanation, so it can correct itself rather than retry
blindly. Dates are ISO (`2026-10-14`, `2026-10-14T15:00:00`); "next Tuesday" is
refused with an example, rather than stored and then silently missing from the
calendar.

**Clients know which tools only read.** Every tool carries MCP annotations:
reads are marked read-only, so a client can skip asking before them, and
`write_cv` and `set_job_status` are marked destructive.

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

A status change appends to the history the funnel is drawn from. The model is
told to show you every change and wait for you.

**And the server asks you itself, where your client lets it.** A client that
supports MCP elicitation lets the server put a small form in front of you,
mid-call, that the model cannot answer. CV Studio uses it where a model is
most often wrong, or a mistake costs you most:

| When | You see | Your answer |
|---|---|---|
| `set_job_status` moves an application | "Move Acme – Engineer from Awaiting reply to Interviewing?" | No: nothing changes |
| `add_job` finds a likely duplicate | What the company already has, and "Add it as a separate application?" | Yes adds it; no keeps the one you have |
| `find_job` matches several applications | A list to pick from, with what the mail was (`about`), and "None of these" | The one you pick is the only candidate |
| `update_job_tracking` moves or clears an interview | "Move the interview from … to …?" | No keeps the time you had |
| `update_job_tracking` replaces a posting you saved | Both lengths, and a warning it may have your edits | Yes replaces it with no flag needed |
| `update_job_tracking` corrects a title | "Rename it to the title its posting gives?" | No keeps your title |
| `write_cv` would drop comments you wrote | How many, and one of them | No keeps the file |
| `add_job` cannot read a posting (a sign-in wall) | A box to paste the posting into | What you paste is saved; empty is fine |

On a 2026-07-28 connection a server no longer sends a question in the middle
of a call. The call comes back "input required" with the question, your
client asks you, and calls again with your answer. CV Studio does both: it
asks mid-call on older connections and this way on newer ones, and a call
that has two questions asks each once.

A refusal tells the model you declined and that nothing changed, and the
instructions tell it to ask you rather than try again. In a client without
elicitation, every one of these behaves as before: the guard refuses, or the
change is made and waits for you under Changes by AI to review.

**Every change to an application can be undone.** Around each tool that writes
the tracker, the server records each application's fields as they were and as
they became. The app lists these under **Attention → Changes by AI to review**,
one card per tool call, field by field, newest first. Keep puts it away; Undo
puts back exactly those fields, status history and note included, and refuses
when any of them has changed again since, so it never overwrites a later edit.
Undoing an application the model added moves it to the trash. A logo on its own
is not listed.

### Applying, as a skill

`skills/cv-studio-apply/SKILL.md` takes a job link to an application ready to
send: it adds the job with the posting read from the board, copies the base CV
in the posting's language and tailors it (never adding experience), checks it
renders and passes the ATS check, attaches it, then writes and attaches a
cover letter. Adding a job without the skill gets the same steps from the
server's instructions and from `add_job`'s `next`, but the skill carries the
judgement: what to reorder, what to cut, what a letter says. Copy the folder
into `~/.claude/skills/`, or package it for Claude Desktop under Settings.

### Interview prep, as a skill

`skills/cv-studio-interview-prep/SKILL.md` is how a coach would prepare a round:
read the posting and the CV that was actually sent, write the questions this
round will bring, match every ask of the posting to a line of the CV (or say it
is a gap), and suggest questions to ask back, all saved with
`save_interview_prep` so the user rehearses them in the app. Copy it into
`~/.claude/skills/` and ask "prepare my Monzo interview".

### The sweep, as a skill

`skills/cv-studio-inbox/SKILL.md` in this repository is the procedure: what to
read, how to match a message to an application, what each kind of reply means,
and when to ask rather than write. Copy the folder into `~/.claude/skills/` and
ask Claude to catch your applications up.

Without it the tools still work and the server's own instructions still carry
the three rules that matter. The skill is what saves you explaining the workflow
every time.

## Setting it up

**Open CV Studio, go to Settings → AI clients (or click the AI clients button in
the title bar), and press Set up next to the client you want.** The title bar
shows only the clients installed on this computer, or already set up. It writes the entry into that client's config for you,
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
immediately. Each change then waits under **Review** in the app, where you keep
it or undo it (see below), and the files are plain YAML, so keeping the
workspace in git gives you a full history as well.

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
counting a mis-indexed entry as a success. Applications are rows in
`applications.db` rather than files, so git does not cover those; Settings
exports them to JSON or CSV.

**Comments survive `edit_cv_fields`** because it round-trips through ruamel.
`write_cv` replaces the file wholesale and will drop anything not in the new
content, which is why the tool description steers toward the former.

**It is local.** The server talks to your filesystem, and goes online for three
things only. It reads a posting from the link a model passes (`add_job`,
`read_posting`, a title check in `update_job_tracking`), straight from the job
board. It fetches a company's icon from the company's own website, when a model
passes one -- there is no logo service in between, so nothing learns the list
of companies you apply to that the companies do not already know. The Typst
packages a theme needs ship with the app, so rendering works offline from the
first launch; only a CV in Chinese, Japanese or Korean downloads its font, once. The interface draws logos from the workspace, and checks
for updates. There is no telemetry. The mail and calendar an AI client
reads reach it through *its* connectors, and this app never sees them. The AI
client sees only what the tools return.

**Only one workspace per configured server.** Add a second entry with a
different `--workspace` if you keep separate sets of CVs.

**Both clients can be connected at once.** They are separate config files and
separate entries; nothing is shared but the workspace, and the app shows the
state of each.

## Skills

The MCP tools are the *doing*; the skills are the judgement around it: reading
a posting, tailoring from the base CV, letters, interview prep. They are
delivered differently in each place, which is worth knowing before you go
looking for them:

| | MCP tools | Skills |
|---|---|---|
| **Claude Code** | configure as above | read straight off disk from `~/.claude/skills/` |
| **Claude Desktop** | configure as above | uploaded to your account, not read from disk |

**Every client also gets them as MCP prompts**, with nothing to install: the
server offers `apply`, `inbox` and `interview-prep`, each the skill word for
word, with an optional `request` argument. In a client that shows prompts
(Claude Desktop lists them under the attachment menu), pick one and add the
job link or the name of the application.

There is no folder you can drop a skill into for the desktop app, so CV Studio
cannot install them for you. What it can do is package them: **Settings → AI
clients → Skills → Package for Claude Desktop** writes one upload-ready `.zip`
per skill into `assets/skills/` in your workspace. Then, in the desktop app,
Customize → Skills → **+** and upload each one.

There are three, in `skills/` in this repository and inside the app:
`cv-studio-apply`, `cv-studio-interview-prep` and `cv-studio-inbox`. Packaging
uses the copies that ship with the app, or yours in `~/.claude/skills/` if you
have edited one. All three work through the MCP tools rather than local
scripts, so they work the same in the desktop app's sandbox.

## On the command line

The same server works with Claude Code and the Codex CLI. The Codex CLI shares
the ChatGPT config the app writes. Claude Code is set up by hand, with the
command and arguments the Claude Desktop entry above uses:

```bash
claude mcp add cv-studio -- "<path to cv-studio-server>" --mcp --workspace "<your workspace>" --client claude
```

Claude Code gets the skills as prompts from the server (`/mcp__cv-studio__apply`
and so on). To have them trigger on their own, copy the three folders under
`skills/` into `~/.claude/skills/` as well.

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
