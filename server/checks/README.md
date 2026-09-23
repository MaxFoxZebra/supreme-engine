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
| `prefstest.py`, `langtest.py`, `lettertest.py`, `importtest.py`, `phototest.py`, `maptest.py`, `sampletest.py` | One area each, end to end in a scratch folder: preferences, languages, cover letters, importing, the photo, the page map, sample data. |
| `mcpclient.py` | Speaks MCP over stdio exactly as Claude Desktop does. |
| `shot.js` | Drives Chromium over the DevTools Protocol: click through to a state, then photograph it. |
| `flow.js` | User flows against the running app, with assertions. |
| `i18nscan.js` | Every screen in English and French; lists text left untranslated. |
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
