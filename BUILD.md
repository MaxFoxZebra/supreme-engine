# CV Studio desktop builds

Tauri v2. The app is a native shell that supervises the Python studio server and
points the OS webview at it.

**Why this shape.** Rendering a CV means running RenderCV, which is Python. The
alternatives were embedding a Python runtime (PyInstaller: 80–150MB) or shipping
Chromium (Electron: ~150MB). Supervising a child process instead keeps the binary
at 3.7MB and preserves the comment-preserving YAML round-trip. A Rust YAML crate
would have silently dropped the comments in `master-profile.yaml`.

## Developing

Almost nothing here needs a build. The interface, the renderer wrapper, the job
store and the API are all Python and served HTML.

### The everyday loop

```bash
cd server
python dev.py
```

Serves on 127.0.0.1:8722, opens a browser, and restarts whenever you save
`studio.py`, `cv_render.py`, `jobs.py`, `mcp_server.py` or the vendored JS. Edit,
save, refresh: about two seconds. If you introduce a syntax error it says so and
waits for the next save rather than restart-looping.

Useful flags: `--no-open` to keep it from stealing focus, `--workspace DIR` to
work against a scratch workspace instead of your real CVs, `--port N`.

### Testing the shell itself

The browser cannot exercise the frameless title bar, the window controls or the
updater. Attach the real shell to the dev server instead of repackaging:

```bash
python server/dev.py --no-open          # terminal 1
CVSTUDIO_DEV_URL=http://127.0.0.1:8722 cargo run   # terminal 2, in src-tauri
```

The shell skips spawning its own packaged server and points the webview at the
live one, so Python still hot-restarts underneath it.

### When you actually need to build

| Change | Command | Roughly |
|---|---|---|
| Python or the interface | nothing, `dev.py` restarts | 2s |
| `main.rs` | `cargo build` | 15s incremental |
| Verify the frozen bundle | PyInstaller, then run `dist/cv-studio-server` | 60s |
| Produce an installer | `npx @tauri-apps/cli build --bundles nsis` | 3min |
| Signed release, all platforms | `git tag vX.Y.Z && git push --tags` | ~15min in CI |

Repackage when something outside the Python source changes: a new dependency,
new files under `server/static`, or anything added with `--add-data`. A pure
edit to existing Python never needs it.

## Sizes

| Artifact | Size |
|---|---|
| `cv-studio.exe` | 3.7 MB |
| Windows installer (NSIS) | 1.3 MB |
| macOS `.app` / `.dmg` | expect ~5–8 MB |

## Prerequisites on the target machine

The app is small because it does not carry a language runtime. It needs:

- **uv**: https://astral.sh/uv (the app finds it on PATH or in `~/.local/bin`)
- **RenderCV**: `uv tool install "rendercv[full]"` (the `[full]` extra is required)

If `uv` is missing the app says so in its own window rather than failing silently.
Python itself does not need installing separately, because `uv` provisions it.

## Windows

Already built and tested. To rebuild:

```bash
cd ~/Documents/Career/desktop
npx @tauri-apps/cli build --bundles nsis
```

Output: `src-tauri/target/release/bundle/nsis/CV Studio_0.1.0_x64-setup.exe`

Requires Rust, MSVC Build Tools, and WebView2, all present on this machine.

## macOS (must be built on a Mac)

A macOS bundle needs a Mach-O binary, so this cannot be cross-compiled from
Windows. Copy this `desktop/` directory to the Mac and run:

```bash
# one-time, if not already present
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install "rendercv[full]"

cd desktop
npx @tauri-apps/cli build --bundles app,dmg
```

Output: `src-tauri/target/release/bundle/macos/CV Studio.app` and a `.dmg`.

The icons are already generated. `src-tauri/icons/icon.icns` exists, so nothing
needs regenerating on the Mac.

### Apple Silicon vs Intel

The above builds for the Mac's own architecture. For a universal binary:

```bash
rustup target add aarch64-apple-darwin x86_64-apple-darwin
npx @tauri-apps/cli build --target universal-apple-darwin --bundles app,dmg
```

### Gatekeeper

An unsigned `.app` will be refused on first open with "cannot be opened because
the developer cannot be verified". Right-click → Open, once, clears it. Proper
signing needs a paid Apple Developer account; for a personal tool it is not worth
it.

## Project layout

```
desktop/
├── app-icon.png            source icon, 1024px
├── dist/index.html         loading screen shown while the server starts
└── src-tauri/
    ├── Cargo.toml          release profile tuned for size (opt-level z, LTO, strip)
    ├── tauri.conf.json     bundle config
    ├── icons/              generated for every platform incl. .icns
    ├── resources/          studio.py, cv_render.py, render_cv.py (bundled)
    └── src/main.rs         process supervision + webview
```

`resources/` holds copies of the skill scripts. After editing the originals in
`~/.claude/skills/`, re-copy them before building or the bundle ships stale code:

```bash
cp ~/.claude/skills/cv-studio/scripts/studio.py src-tauri/resources/
cp ~/.claude/skills/cv-studio-resume/scripts/cv_render.py src-tauri/resources/
cp ~/.claude/skills/cv-studio-resume/scripts/render_cv.py src-tauri/resources/
```

Outside a bundle the app falls back to reading those scripts directly from
`~/.claude/skills/`, so a development build tracks edits without repackaging.

## Behaviour worth knowing

**The window opens without taking focus** (`.focused(false)` in `main.rs`). A
utility that activates itself will pull a fullscreen game or video back to the
desktop. The window appears and waits to be clicked.

**The server port is chosen at random** from the free range, so multiple
instances and leftover processes do not collide.

**The child process is killed on window close.** Without that the Python server
outlives the window and holds its port.
