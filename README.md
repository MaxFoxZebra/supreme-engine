# CV Studio

The eyes of a CV written by an AI.

Claude or ChatGPT reads the posting and writes the YAML, through an MCP server
that ships inside this app. Point it at your mail as well and it keeps the
application tracker in step with your inbox, without this app ever touching a
Google account. CV Studio renders it, shows you the page and what it
costs in space, and lets you fix by hand what is quicker pointed at than
described: click a block on the page and you are editing it. When the model
changes a file you have open, the app notices and offers you its version.

It works perfectly well on its own, too: a local CV editor with live PDF
preview, built on [RenderCV](https://github.com/rendercv/rendercv) and
[Typst](https://typst.app).

Everything runs on your machine, with no account, no server, no telemetry,
which matters more once a model is editing the files, not less. Your CVs are
plain YAML in a folder you own, with no database between you and them, so you
can read, grep, diff, back up and version them without this app, and take them
somewhere else whenever you like. Applications are rows in `applications.db`
beside them, which exports to JSON or CSV for the same reason. Both halves only
ever touch that folder.

Connecting a client is one button in **Settings → AI clients**: Claude Desktop;
OpenAI, where the same config file covers the ChatGPT app, the Codex CLI and the
Codex IDE extension; or Mistral Vibe. See [MCP.md](MCP.md) for what the model
can and cannot do.

## Install

Download the installer for your platform from Releases.

Windows installs per-user, so there is no admin prompt.

Neither build is signed by its platform vendor, so the first launch needs one
extra step:

- **macOS**: drag CV Studio to Applications, then run this once:

  ```bash
  xattr -dr com.apple.quarantine "/Applications/CV Studio.app"
  ```

  It opens normally after that. Until you do, macOS says the app is **damaged
  and can't be opened**, which is what it reports for anything downloaded that
  Apple has not notarized. The app is not damaged and the command disables
  nothing system-wide: it clears the flag macOS put on that one download.
  Right-click → **Open** was the old way around this and macOS 15 removed it.
- **Windows**: SmartScreen may show "Windows protected your PC". Choose
  **More info → Run anyway**.

Nothing else is required. Python, RenderCV, Typst and the fonts are all bundled.

## Using it

On first launch it creates a workspace at `~/Documents/CV Studio` with a starter
CV, and opens it.

There are three screens, switched from the control at the top left.

**CVs** is the editor. The left rail lists your documents and, under them, an
outline of the open one. The rendered page is always on screen; the tabs choose
what stands beside it.

**Page** gives it the whole pane and you edit the document on the document:
click any block and a card opens beside it with that entry's fields, the page
stepping aside to make room the way a word processor makes room for a comment.
Arrow keys walk the card from block to block.

**Form** and **YAML** put the fields, or the source, on the left with the page
on the right, and the page re-renders as you type. Put the caret in a field or
on a line and the page highlights that entry and scrolls to it; click a block
on the page and the left pane goes to it. Drag the divider to change the share,
and it stays where you put it. Selecting an entry in one view selects it in all
of them.

| | |
|---|---|
| **Page / Form / YAML** | The page on its own, or the page beside every field at once, or beside the raw file with syntax highlighting |
| **Live** | Edits re-render behind you, so the page beside the form is the page you are making, not the one you started with |
| **Click the page** | Every block on the rendered page is a target: click the job you are reading and its fields open beside it, with `+` and `−` to add or drop a bullet. `↑` `↓` walk to the next block, `Esc` closes |
| **Page budget** | Page count, the word count an ATS reads, and how full the last page is, measured off the render |
| **Render** | Or `Ctrl`/`Cmd` + `S`. The status bar reports how long it took |
| **Design** | Theme, typeface, body size and page size, with every other RenderCV option under them, and what each costs in pages |
| **Appearance** | Light or dark, or follow the system. In Settings. The rendered CV page stays white either way: it is a document, not a surface |
| **AI clients** | Whether Claude, OpenAI and Mistral Vibe are wired up to this workspace, in the title bar, one mark and one dot each. Clicking sets them up, and shows what the model has been doing in here |
| **Skills** | The job-search skills, packaged as uploads for the Claude Desktop app. Claude Code reads them off disk already |
| **When the model edits** | The app watches the files it has open. No unsaved work: it reloads and says so. Unsaved work: it asks, rather than saving over what the model wrote |
| **What the model changed** | A mark beside every field it wrote, in the outline, the block editor, the bullet and the page margin, with what the line said before. A second mark for every field that no longer matches the CV this one was tailored from. Neither is in the YAML, so neither prints |

**Jobs** is `applications.db`: filter by status down the left, six columns of
what matters across the middle, and one application's details on the right:
status, source, fit, its documents, its history and your notes. A filename in
the Documents column opens that CV in the editor.

**Funnel** shows where the applications went, cumulatively: how many reached an
interview, how many converted, and where the rest dropped out. Rejections and
ghostings are split by whether they happened before or after an interview,
because those say very different things. Clicking a band opens Jobs filtered to
it.

Live preview is on by default: the preview re-renders as you type, without
saving. Comments you write in the YAML survive edits made through the form.

Live preview renders a scratch copy, so your file is only written when you
actually save. While you are mid-edit and the YAML is momentarily invalid, the
last good page stays on screen instead of flashing an error at every keystroke.

## Connect it to Claude Desktop

CV Studio ships an MCP server, so an AI client can read, edit and render your
CVs. `render_cv` returns the rendered page as an *image*, so the model can
actually look at the result rather than guessing from the source.

It can also keep the tracker up to date. Given a mail or calendar connector of
its own, it reads the replies, works out which application each belongs to, and
moves the status once you have agreed. CV Studio itself never touches a Google
account and makes no network calls; everything arrives through the client. It
cannot delete an application, rename one, or overwrite your notes, because no
tool takes those arguments.

See [MCP.md](MCP.md) for the config and the full tool list. It is the same
bundled binary run with `--mcp`, so nothing extra to install.

## Why it looks the way it does

**Page count is shown because it is the constraint that matters.** A CV that
spills onto a third page gets skimmed differently, and you cannot see that in a
text editor.

**The ATS word count** reflects what an applicant tracking system actually
extracts from the PDF, not what you see on screen. RenderCV emits tagged PDFs
with a real text layer, which is what makes them parse correctly.

**Errors come with an explanation.** The two that catch everyone: a colon
followed by a space inside a bullet (YAML reads it as a new field), and a phone
number that is well-formed but not actually dialable in its country. Both are
explained where they happen, with the line number when there is one.

**One accent colour, used once per region.** Ochre marks the selected item, the
primary action, or the live metric, never three things at once. Everything else
is neutral, so what it marks is never in doubt.

## Building from source

See [BUILD.md](BUILD.md). In short: `pip install "rendercv[full]==2.8" pyinstaller`,
run PyInstaller to produce the server, copy it to `src-tauri/server-dist`, then
`npx @tauri-apps/cli build`. The GitHub Actions workflow in `.github/workflows`
does all of this for Windows, Apple Silicon and Intel Macs.

## Architecture

A Tauri v2 shell (Rust, ~4 MB, uses the OS webview rather than bundling Chromium)
supervises a PyInstaller-frozen Python server that drives RenderCV. That split
exists because rendering is genuinely Python work, while shipping Electron or an
embedded interpreter in the UI layer would have cost 100 MB+ for no benefit.

The Rust side deliberately does no YAML parsing: round-tripping through ruamel
preserves the comments in your files, which a Rust YAML crate would silently
discard.

## Licence

MIT. Bundles RenderCV (MIT), Typst (Apache-2.0), the RenderCV font set and
IBM Plex (SIL Open Font License / Apache-2.0). See [LICENSE](LICENSE) and
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
