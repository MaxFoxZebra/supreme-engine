# Checks

The interface is a single served HTML string and the MCP server is a separate
process, so most of what can break here is invisible to a Python test: a CSS
rule that never applies, an element the script addresses that no longer exists,
a tool whose schema changed shape over the wire. These drive the real thing and
measure what it actually produced.

They are not unit tests and they need nothing installed. The Python ones run on
their own; `shot.js` and `flow.js` need a server you have already started.
`.github/workflows/checks.yml` runs all of them on every push.

```bash
cd server
./.venv/Scripts/python.exe studio.py --port 8750 --token t --workspace /tmp/ws
```

| | |
|---|---|
| `jscheck.py` | Every script in the served page parsed with `node --check`: a stray quote leaves a page that loads and then does nothing. |
| `audit.py` | Static audit of the served interface. No browser needed. |
| `prefstest.py`, `langtest.py`, `lettertest.py`, `reviewtest.py`, `importtest.py`, `phototest.py`, `maptest.py`, `sampletest.py` | One area each, end to end in a scratch folder: preferences, languages, cover letters, reviewing what an AI client changed, importing, the photo, the page map, sample data. |
| `trackertest.py` | Applications as files in `tracker/`: what a file looks like, an edit made in another editor, a sync client's conflicted copy, two processes writing at once, the index being only a copy, and moving out of `applications.db`, including on a second computer that has not synced yet. |
| `connectortest.py` | The connector an AI client runs: a copy per version under its own name (so an update never closes it), clients re-pointed after an update, old copies removed; the skills installed into Codex, Vibe and Hermes and kept current, packed as one plugin for Claude Desktop, and the repository as a plugin marketplace; tool titles, hints and prompts. |
| `viewdev.js` | Not a check: the preview for working on the page view. `node checks/viewdev.js` renders CVs (sample data, plus a two-page one, or `--workspace`) and serves the view as Claude frames it, inline in light and dark and full screen, reloading when `static/mcp-page.html` changes and showing what it would send to the chat. `--shots dir` saves screenshots of each frame, with a block selected and after sending, and exits. `viewhost.js` is the stand-in for Claude that it and `pageview.js` share. |
| `jobview.js` | The job views in the conversation (MCP Apps), with the check as the client: job_stats gives the model a few lines and the view the funnel and the applications behind every number; in Chrome a stage opens its applications and one opens its card; a status moved from the card asks first, is saved as the user's with its history, and is told to the model; a company name is only ever text. |
| `i18nviews.js` | The views in the conversation in French (standing for the three languages): starts `viewdev.js` over the sample data, opens every view in each frame and what a click opens in it, and lists text the translator sees that is still in English. Add what it lists to `i18n/views.py`, then `python i18n/build.py`. |
| `pageview.js` | The CV page view an AI client shows inside the conversation (MCP Apps), with the check as the client: render_cv names its view and returns every page and the block map only to a client that can show views; in Chrome the view does the handshake, shows the page with a click target per block, tells the model which block is selected, and sends the change to the chat as the user. `SHOT=file.png` keeps a screenshot. |
| `mcpclient.py` | Speaks MCP over stdio exactly as Claude Desktop does. |
| `shot.js` | Drives Chromium over the DevTools Protocol: click through to a state, then photograph it. |
| `flow.js` | User flows against the running app, with assertions. |
| `editortest.js` | The letter and CV editors under real key and mouse input: the caret stays in its field and its paragraph, and the panes stay put, through autosave, the live preview, Ctrl+S and the poll after it; bold, italic, strikethrough and code keep the selection; what was typed reaches the file where it was typed; undo works after a save. |
| `a11yscan.js` | Every screen in light and dark: text below WCAG AA contrast, things you can click but not reach with Tab, controls with no name, Tab stops with no visible ring. |
| `scaletest.js` | Sample data with 500 applications; times the list, opening one, search, the funnel and the calendar, and fails on anything over 250 ms (`SCALE_BUDGET` to change it). |
| `i18nscan.js` | Every screen in English and French; lists text left untranslated. |
| `i18nserver.py` | Every message the server can send (errors, render hints) has a translation. The catalogue is `i18n/catalogue.py`; `python i18n/build.py` writes `static/i18n.js` from it. |
| `clipflow.js` | Save to CV Studio in headless Chrome on simulated job pages (Greenhouse drawn by script, a company page with job data, thin job data on Lever, a board page with nothing and no company in its record, Indeed, LinkedIn, a page with only selected text): what the window reads and what is saved. Starts its own server with the boards' feeds simulated (`clipsim.py`); needs Chrome or Chromium, or `CHROME=`. |
| `crop.py` | Crops and zooms a PNG using only the stdlib, so a screenshot can be read at a size where design decisions are visible. |

## audit.py

```bash
python checks/audit.py
```

Reports, and exits non-zero on: CSS custom properties referenced but never
defined; **a rule opened inside an unclosed rule** (the browser silently drops
the whole block, so the class is in the DOM and the style simply never
applies. This has bitten twice); stray or unbalanced braces; duplicate ids;
ids the
script addresses that are not in the markup; classes used but never styled;
functions defined and never called; `S.<prop>` read but never assigned; and
routes implemented but missing from the OpenAPI spec, or the reverse.

## mcpclient.py

```bash
python checks/mcpclient.py ./.venv/Scripts/python.exe server_main.py --mcp --workspace /tmp/ws
```

Handshake, `tools/list`, then every tool called for real, including that
`render_cv` returns a decodable PNG and that a path outside the workspace is
refused. Point it at `server_main.py`, never `studio.py`: only the former
routes `--mcp`.

## shot.js and flow.js

```bash
node checks/shot.js "http://127.0.0.1:8750/?token=t" ./shots \
  '01-editor::setPref("appearance","dark");applyAppearance()' \
  '02-jobs::document.querySelector("#nav button[data-view=jobs]").click()'

node checks/flow.js "http://127.0.0.1:8750/?token=t"
```

`flow.js` needs Chromium listening on port 9333 and an empty workspace behind
the server: it makes its own fixture (a two-page CV, six applications and a
cover letter) through the app's API.

Each `shot.js` argument is `name::javascript`: the script runs in the page,
then the frame is saved as `name.png`. `flow.js` exercises clicking a block on
the rendered page, the selection following into the other views, linking a CV to
an application, and the document rail's grouping.

**Assert on computed style, not on class presence.** A class can be applied
while its rule was discarded as malformed; that exact bug shipped once because a
check confirmed the class and stopped there.
