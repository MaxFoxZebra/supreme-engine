# CV Studio

A local CV editor with live PDF preview, built on [RenderCV](https://github.com/rendercv/rendercv)
and [Typst](https://typst.app). Everything runs on your machine, with no account, no
server, no telemetry.

Your CVs are plain YAML files in a folder you own. There is no database, so you
can read, grep, diff, back up and version them without this app, and take them
somewhere else whenever you like.

## Install

Download the installer for your platform from Releases.

Windows installs per-user, so there is no admin prompt.

Both builds are unsigned, so the first launch needs one extra step:

- **macOS**: right-click the app and choose **Open**, then confirm. Double-clicking
  an unsigned app makes Gatekeeper refuse it outright.
- **Windows**: SmartScreen may show "Windows protected your PC". Choose
  **More info → Run anyway**.

Nothing else is required. Python, RenderCV, Typst and the fonts are all bundled.

## Using it

On first launch it creates a workspace at `~/Documents/CV Studio` with a starter
CV, and opens it.

| | |
|---|---|
| **Form tab** | Edit fields directly, no YAML knowledge needed |
| **YAML tab** | The raw file, with syntax highlighting, for full control |
| **Preview** | Re-renders on save. Shows page count and the word count an ATS reads |
| **Theme / Font / Page** | Five themes, fourteen font families, A4 or US Letter |
| **Live** | On by default. The preview re-renders as you type, without saving |
| **Save & Render** | Or `Ctrl`/`Cmd` + `S` |
| **+ New** | Blank CV, or duplicate the one you have open, one file per application |

Panels are draggable. The preview zooms. Comments you write in the YAML survive
edits made through the form.

Live preview renders a scratch copy, so your file is only written when you
actually save. While you are mid-edit and the YAML is momentarily invalid, the
last good page stays on screen instead of flashing an error at every keystroke.

## Connect it to Claude Desktop

CV Studio ships an MCP server, so an AI client can read, edit and render your
CVs. `render_cv` returns the rendered page as an *image*, so the model can
actually look at the result rather than guessing from the source.

See [MCP.md](MCP.md) for the config. It is the same bundled binary run with
`--mcp`, so nothing extra to install.

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
explained in the preview pane when they happen.

## Building from source

See [BUILD.md](BUILD.md). In short: `pip install "rendercv[full]" pyinstaller`,
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

MIT. Bundles RenderCV (MIT), Typst (Apache-2.0), and the RenderCV font set
(SIL Open Font License / Apache-2.0). See [LICENSE](LICENSE).
