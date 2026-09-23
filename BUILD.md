# CV Studio desktop builds

Tauri v2. The app is a native shell that supervises a PyInstaller-frozen Python
server and points the OS webview at it.

**Why this shape.** Rendering a CV means running RenderCV, which is Python. The
alternative was shipping Chromium (Electron, ~150MB on top of everything else)
for a UI layer that gains nothing from it. Supervising a child process instead
keeps the Rust shell under 4MB and preserves the comment-preserving YAML
round-trip: a Rust YAML crate would have silently dropped the comments in
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
`studio.py`, `cv_render.py`, `cv_map.py`, `jobs.py`, `mcp_server.py`, or the
interface in `static/app.js` and `static/app.css`. Edit, save, refresh: about
two seconds. A syntax error is reported and it waits for
the next save rather than restart-looping.

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
render a CV**, because a build that ships but cannot render is worse than a
failed build, then builds, signs with the updater key, and publishes a GitHub release
including `latest.json`.

Existing installs pick it up through the updater, which polls
`releases/latest/download/latest.json` and installs in `passive` mode.

### Releasing without pushing a tag

The workflow also runs from **Actions → Build desktop apps → Run workflow**,
which takes a **tag** input. Type `v0.5.2` there and CI creates that tag and
releases under it, no tag push and no local clone needed. Leave the input empty
and it builds all three platforms and publishes nothing, which is the way to
test the pipeline.

Only a tag push sets the tag from the ref; a manual run reads the input, because
`github.ref_name` on a manual run is the branch and would publish a release
called "main".

### Signing

Releases are signed with the Tauri updater key, held as the repository secrets
`TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. The
password is deliberately empty: the key was generated without one, GitHub will
not accept an empty secret value, and setting it explicitly stops the signer
prompting in CI. The matching public key sits in `tauri.conf.json`.

Without those secrets the build still succeeds, but the artifacts cannot be used
as updates.

That key is only the updater's. It is unrelated to platform code signing, which
is a separate axis and covered next.

#### Apple signing and notarization

The macOS jobs sign in two passes, because Tauri copies `server-dist` in as an
opaque resource tree and never walks it. The workflow signs every Mach-O in the
frozen Python first, inside out, then Tauri seals the `.app` around it with the
same identity. `src-tauri/entitlements.plist` carries the three exceptions a
frozen CPython needs once the hardened runtime is on, and the smoke test runs
*after* signing so a bundle that builds and then refuses to start fails the
build rather than the release.

With no secrets set, the identity is ad-hoc (`-`). That produces a valid seal,
which is what lets the app run at all once quarantine is cleared, but it is not
a Developer ID and does nothing for Gatekeeper on a downloaded copy.

To make downloads open with no extra step, set these six repository secrets:

| Secret | What it is |
|---|---|
| `APPLE_CERTIFICATE` | Developer ID Application `.p12`, base64 encoded |
| `APPLE_CERTIFICATE_PASSWORD` | its export password |
| `APPLE_SIGNING_IDENTITY` | e.g. `Developer ID Application: Name (TEAMID)` |
| `APPLE_ID` | the Apple ID that owns the account |
| `APPLE_PASSWORD` | an app-specific password, not the account password |
| `APPLE_TEAM_ID` | the ten-character team ID |

Present, they switch the identity from ad-hoc to the real one, add a trusted
timestamp, and turn on notarization. Absent, the build behaves exactly as it
does today. Nothing else has to change.

Set them as a complete set. The last three are exported to the bundler only when
all three are non-empty, and only alongside a certificate, because Tauri decides
to notarize on whether those names exist rather than on what they contain, and
an unset repository secret reaches a workflow as an empty string rather than as
nothing at all. Listing them unconditionally is what made v0.5.1 fail with
"Team ID must be at least 3 characters" after signing perfectly well.

## Building locally

Only worth it to inspect an installer before tagging. The result is unsigned by
the updater key, so installed copies will refuse it as an update.

`src-tauri/server-dist/` is gitignored and produced by PyInstaller, so it has to
be built first, because the Tauri bundle config lists it as a resource.

```bash
cd server
pip install "rendercv[full]==2.8" "ruamel.yaml==0.19.1" "mcp==2.2.0" \
  "pypdf==6.19.0" "pyinstaller==6.22.3"

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
  --hidden-import cv_map \
  --hidden-import studio \
  --hidden-import mcp_server \
  --hidden-import ats \
  --paths . \
  server_main.py

cd ..
rm -rf src-tauri/server-dist
cp -r server/dist/cv-studio-server src-tauri/server-dist
npx @tauri-apps/cli build --bundles nsis
```

Output: `src-tauri/target/release/bundle/nsis/CV Studio_<version>_x64-setup.exe`.

Needs Python 3.12 or newer, Rust, MSVC Build Tools and WebView2 on Windows.
CI builds on 3.14, which is the newest every dependency here claims: RenderCV
requires 3.12 at minimum and lists 3.14 as its ceiling.

The versions are pinned deliberately. RenderCV changes its design schema inside
a major version and its models forbid unknown keys, so the starter CV that
renders under 2.8 is rejected outright by 2.3. Unpinned, the day upstream
renames another design key is the day a tagged release fails its smoke test.
Raising them should be an edit you make and test, not something a build picks
up on its own.

The shell also falls back to `server/dist/cv-studio-server/` relative to the
executable, so `cargo run` finds a freshly frozen server without staging it.

### macOS

CI covers both architectures, so building on a Mac by hand is rarely needed. A
`.app` needs a Mach-O binary and cannot be cross-compiled from Windows. On the
Mac, after the PyInstaller and staging steps above:

```bash
# one-time
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# stage with ditto, not cp, so signatures and symlinks survive the copy
rm -rf src-tauri/server-dist
ditto server/dist/cv-studio-server src-tauri/server-dist

# what CI does before bundling; without it the .app is sealed around unsigned
# nested code and macOS calls the result damaged even after quarantine is cleared
find src-tauri/server-dist -type f \
  \( -perm -u+x -o -name '*.so' -o -name '*.dylib' \) -print0 |
  while IFS= read -r -d '' f; do
    file -b "$f" | grep -q 'Mach-O' &&
      codesign --force --options runtime --sign - "$f"
  done
codesign --force --options runtime --entitlements src-tauri/entitlements.plist \
  --sign - src-tauri/server-dist/cv-studio-server

npx @tauri-apps/cli build --bundles app,dmg
```

Icons are already generated, `src-tauri/icons/icon.icns` included, so nothing
needs regenerating.

A locally built `.app` is ad-hoc signed, same as CI's. It runs on the machine
that built it and, copied anywhere by hand, on any other. It is only a download
that picks up the quarantine flag, and only that needs the `xattr` step under
First launch.

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

**macOS**: a downloaded copy is refused on first open with "CV Studio.app is
damaged and can't be opened. You should move it to the Bin." The app is not
damaged. That is the dialog macOS shows for any quarantined app that Apple has
not notarized, and it looks identical whether the app is unsigned, ad-hoc
signed, or genuinely corrupt.

Clearing the quarantine flag is the way in:

```bash
xattr -dr com.apple.quarantine "/Applications/CV Studio.app"
```

Right-click → Open used to substitute for this and no longer does: macOS 15
removed it as a Gatekeeper bypass. The System Settings → Privacy & Security
"Open Anyway" button is the other route, but it only appears for about an hour
after a failed launch, and it does not appear at all for the "damaged" case, so
the command is what the release notes tell people to run.

Only notarization removes the step, and notarization needs a paid Apple
Developer account. The workflow is wired for it already; see Signing above.

**Windows**: SmartScreen may warn on first run. Choose More info, then Run anyway. The
installer is `currentUser` mode, so there is no admin prompt.

## Project layout

```
.
├── app-icon.png            source icon, 1024px
├── dist/index.html         loading screen shown while the server starts
├── server/                 the Python server, frozen into the bundle
│   ├── server_main.py      PyInstaller entry point; --mcp switches to MCP
│   ├── studio.py           the API, and the page's markup (INDEX_HTML)
│   ├── cv_render.py        RenderCV wrapper
│   ├── cv_map.py           where each block landed on the page
│   ├── jobs.py             applications.db
│   ├── mcp_server.py       MCP surface (see MCP.md)
│   ├── dev.py              hot-restarting dev server
│   ├── i18n/               the translation catalogue; build.py writes static/i18n.js
│   └── static/             the interface (app.js, app.css), i18n.js, vendored d3,
│                           the interface fonts and the app mark
└── src-tauri/
    ├── Cargo.toml          release profile tuned for size (opt-level z, LTO, strip)
    ├── tauri.conf.json     bundle config, updater endpoint and public key
    ├── icons/              generated for every platform incl. .icns
    ├── entitlements.plist  hardened-runtime exceptions the frozen Python needs
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
