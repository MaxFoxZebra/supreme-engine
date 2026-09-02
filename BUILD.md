# CV Studio desktop builds

Tauri v2. The app is a native shell that supervises a PyInstaller-frozen Python
server and points the OS webview at it.

**Why this shape.** Rendering a CV means running RenderCV, which is Python. The
alternative was shipping Chromium (Electron, ~150MB on top of everything else)
for a UI layer that gains nothing from it. Supervising a child process instead
keeps the Rust shell under 4MB and preserves the comment-preserving YAML
round-trip — a Rust YAML crate would have silently dropped the comments in
`master-profile.yaml`.

## Developing

Almost nothing here needs a build. The interface, the renderer wrapper, the job
store and the API are all Python and served HTML.

### The everyday loop

```bash
cd server
python dev.py
```

Serves on 127.0.0.1:8722, opens a browser, and restarts whenever you save
`studio.py`, `cv_render.py`, `jobs.py` or `mcp_server.py`. Edit, save, refresh:
about two seconds. A syntax error is reported and it waits for the next save
rather than restart-looping.

Useful flags: `--no-open` to keep it from stealing focus, `--workspace DIR` to
work against a scratch workspace instead of your real CVs, `--port N`.

### Testing the shell itself

The browser cannot exercise the frameless title bar, the window controls or the
updater. Attach the real shell to the dev server instead of repackaging:

```bash
python server/dev.py --no-open                     # terminal 1
CVSTUDIO_DEV_URL=http://127.0.0.1:8722 cargo run   # terminal 2, in src-tauri
```

The shell skips spawning its own packaged server and points the webview at the
live one, so Python still hot-restarts underneath it.

### When you actually need to build

| Change | Command | Roughly |
|---|---|---|
| Python or the interface | nothing, `dev.py` restarts | 2s |
| `main.rs` | `cargo build` | 15s incremental |
| Verify the frozen server | PyInstaller, then run `server/dist/cv-studio-server/cv-studio-server` | 60s |
| Produce an installer | freeze, stage, `npx @tauri-apps/cli build --bundles nsis` | ~5min |
| Signed release, all platforms | `git tag vX.Y.Z && git push --tags` | ~15min in CI |

Repackage when something outside the Python source changes: a new dependency,
new files under `server/static`, or anything added with `--add-data`. A pure
edit to existing Python never needs it.

## Releasing

This is the normal path, and the only one that produces artifacts an installed
copy will accept as an update.

1. Bump the version in **both** `src-tauri/tauri.conf.json` and
   `src-tauri/Cargo.toml`. They must agree.
2. Commit, then tag and push:

```bash
git tag v0.4.0
git push origin v0.4.0
```

`.github/workflows/release.yml` then runs on Windows, Apple Silicon and Intel
macOS runners. Each freezes the server with PyInstaller, stages it at
`src-tauri/server-dist`, **smoke-tests that the packaged server can actually
render a CV** — a build that ships but cannot render is worse than a failed
build — then builds, signs with the updater key, and publishes a GitHub release
including `latest.json`.

Existing installs pick it up through the updater, which polls
`releases/latest/download/latest.json` and installs in `passive` mode.

The workflow also accepts `workflow_dispatch` if you need a build without cutting
a tag.

### Signing

Releases are signed with the Tauri updater key, held as the repository secrets
`TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. The
password is deliberately empty — the key was generated without one, GitHub will
not accept an empty secret value, and setting it explicitly stops the signer
prompting in CI. The matching public key sits in `tauri.conf.json`.

Without those secrets the build still succeeds, but the artifacts cannot be used
as updates. Neither Apple nor Microsoft code signing is in play; see First
launch below.

## Building locally

Only worth it to inspect an installer before tagging. The result is unsigned by
the updater key, so installed copies will refuse it as an update.

`src-tauri/server-dist/` is gitignored and produced by PyInstaller, so it has to
be built first — the Tauri bundle config lists it as a resource.

```bash
cd server
pip install "rendercv[full]" "ruamel.yaml" mcp pyinstaller

# rendercv_fonts, typst and mcp ship binaries and package data PyInstaller does
# not discover on its own. server/static holds the vendored d3 modules and the
# interface fonts, which the server reads from _MEIPASS at runtime -- without
# --add-data they are simply absent, so the funnel chart fails to load its
# scripts and the interface falls back to a system face.
SEP=";"   # ":" on macOS and Linux
PYTHONIOENCODING=utf-8 pyinstaller --onedir --noconfirm --clean \
  --name cv-studio-server \
  --add-data "static${SEP}static" \
  --collect-all rendercv \
  --collect-all rendercv_fonts \
  --collect-all typst \
  --collect-all ruamel.yaml \
  --collect-all mcp \
  --collect-all pydantic \
  --hidden-import cv_render \
  --hidden-import studio \
  --hidden-import mcp_server \
  --paths . \
  server_main.py

cd ..
rm -rf src-tauri/server-dist
cp -r server/dist/cv-studio-server src-tauri/server-dist
npx @tauri-apps/cli build --bundles nsis
```

Output: `src-tauri/target/release/bundle/nsis/CV Studio_<version>_x64-setup.exe`.

Needs Rust, MSVC Build Tools and WebView2 on Windows.

The shell also falls back to `server/dist/cv-studio-server/` relative to the
executable, so `cargo run` finds a freshly frozen server without staging it.

### macOS

CI covers both architectures, so building on a Mac by hand is rarely needed. A
`.app` needs a Mach-O binary and cannot be cross-compiled from Windows. On the
Mac, after the PyInstaller and staging steps above:

```bash
# one-time
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

npx @tauri-apps/cli build --bundles app,dmg
```

Icons are already generated, `src-tauri/icons/icon.icns` included, so nothing
needs regenerating.

## Sizes

The installers carry a frozen Python, RenderCV, Typst and the full font set, so
they are not small. The Rust shell is a rounding error inside them.

| Artifact | Size |
|---|---|
| Windows installer (NSIS) | ~72 MB |
| macOS `.dmg`, per architecture | ~103 MB |
| Rust shell binary, before resources | ~4 MB |

Nothing has to be installed on the target machine. RenderCV is inside the
bundle and is driven in-process; the `uv tool install "rendercv[full]"` path in
`cv_render.py` is the development fallback for when it is not importable.

## First launch

Neither platform's code signing is paid for, so first launch takes one extra
step. Both are spelled out in the release notes the workflow writes.

**macOS**: an unsigned `.app` is refused on first open with "cannot be opened
because the developer cannot be verified". Right-click → Open, once, clears it.
Proper signing needs a paid Apple Developer account; for a personal tool it is
not worth it.

**Windows**: SmartScreen may warn on first run — More info, then Run anyway. The
installer is `currentUser` mode, so there is no admin prompt.

## Project layout

```
.
├── app-icon.png            source icon, 1024px
├── dist/index.html         loading screen shown while the server starts
├── server/                 the Python server, frozen into the bundle
│   ├── server_main.py      PyInstaller entry point; --mcp switches to MCP
│   ├── studio.py           interface and API
│   ├── cv_render.py        RenderCV wrapper
│   ├── jobs.py             applications.db
│   ├── mcp_server.py       MCP surface (see MCP.md)
│   ├── dev.py              hot-restarting dev server
│   └── static/             vendored d3 and the interface fonts
└── src-tauri/
    ├── Cargo.toml          release profile tuned for size (opt-level z, LTO, strip)
    ├── tauri.conf.json     bundle config, updater endpoint and public key
    ├── icons/              generated for every platform incl. .icns
    ├── installer/          NSIS header and sidebar art, install hooks
    ├── server-dist/        gitignored; PyInstaller output, staged before bundling
    └── src/main.rs         process supervision + webview
```

## Behaviour worth knowing

**The window opens without taking focus** (`.focused(false)` in `main.rs`). A
utility that activates itself will pull a fullscreen game or video back to the
desktop. The window appears and waits to be clicked.

**The server port is chosen at random** from the free range, so multiple
instances and leftover processes do not collide.

**The child process is killed on window close.** Without that the Python server
outlives the window and holds its port.

**The packaged server is also the MCP server.** The same binary answers `--mcp`
over stdio. `MCP.md` has the client configuration and the installed paths.
