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
| `update_job_tracking` | Interview time, follow-up date, who is writing to you |
| `add_job` | Add one, refusing a likely duplicate unless you confirm |
| `set_company_logo` | Point every application at one company to the same logo |

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

**The Google half is not in here.** This app makes no network calls and has no
integration with anything. Gmail and Calendar reach the model through its own
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

### The sweep, as a skill

`skills/cv-studio-inbox/SKILL.md` in this repository is the procedure: what to
read, how to match a message to an application, what each kind of reply means,
and when to ask rather than write. Copy the folder into `~/.claude/skills/` and
ask Claude to catch your applications up.

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
Mistral, start a new Vibe session.

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

None of it is written into the YAML, so **none of it prints**: the sidecar is
local bookkeeping, the marks are drawn in the app, and the PDF comes from
RenderCV out of the YAML alone.

**A patch that lands nowhere is reported.** `edit_cv_fields` answers with how
many of the edits it applied and names each one it could not, rather than
counting a mis-indexed entry as a success. Applications are rows in
`applications.db` rather than files, so git does not cover those; the Jobs view
exports them to JSON or CSV.

**Comments survive `edit_cv_fields`** because it round-trips through ruamel.
`write_cv` replaces the file wholesale and will drop anything not in the new
content, which is why the tool description steers toward the former.

**It is local.** The server talks to your filesystem and nothing else. There are
no network calls, no telemetry. That is still true now the tracker is exposed:
the mail and calendar an AI client reads reach it through *its* connectors, and
this app never sees them. The AI client sees only what the tools return.

**Only one workspace per configured server.** Add a second entry with a
different `--workspace` if you keep separate sets of CVs.

**Both clients can be connected at once.** They are separate config files and
separate entries; nothing is shared but the workspace, and the app shows the
state of each.

## Skills

The MCP tools are the *doing*; the skills are the judgement around it: reading
a posting, tailoring from a master profile, letters, interview prep. They are
delivered differently in each place, which is worth knowing before you go
looking for them:

| | MCP tools | Skills |
|---|---|---|
| **Claude Code** | configure as above | read straight off disk from `~/.claude/skills/` |
| **Claude Desktop** | configure as above | uploaded to your account, not read from disk |

There is no folder you can drop a skill into for the desktop app, so CV Studio
cannot install them for you. What it can do is package them: **Settings → AI
clients → Skills → Package for Claude Desktop** writes one upload-ready `.zip`
per skill into `assets/skills/` in your workspace. Then, in the desktop app,
Customize → Skills → **+** and upload each one.

**A skill that runs a local script cannot work in the desktop app.** Skills
there execute in Claude's sandbox: no workspace on disk, no Python, no
`127.0.0.1:8722`. Four of the seven are built that way, and the packaged copy of
each gets a section appended pointing at the MCP tool that does the same job:
`render_cv` instead of a render script, `edit_cv_fields` instead of writing
YAML, and so on. The originals in `~/.claude/skills/` are never modified. The
other three are pure judgement and travel unchanged.

## On the command line

The same server works with Claude Code and the Codex CLI, configured the same
way. Claude Code users get more than the MCP tools: the `~/.claude/skills/`
directory in this project holds skills for the whole job-search workflow:
analysing a posting, tailoring a CV from a master profile, writing cover
letters, tracking applications and interview prep. The MCP server covers CV
editing and rendering; the skills cover the judgement around it.

## Updates

The app checks for updates on launch and under Settings, About. Updates are
signed: the installed copy verifies each package against the public key baked
into its own build, so a compromised release host still cannot push a package
it will install.

Publishing an update:

1. Bump `version` in `src-tauri/tauri.conf.json` **and** `src-tauri/Cargo.toml`.
   They must agree.
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
