# Connecting CV Studio to an AI client

CV Studio ships an MCP server, so Claude Desktop (or any MCP client) can read,
edit and render the CVs in your workspace. It is the same binary the app uses,
just started in a different mode, so a CV rendered through Claude is identical
to one rendered by clicking Save.

## What the AI can do

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

`render_cv` returning an image is the point of the whole thing. The model can
*look* at the rendered page and catch what only shows up visually: a bullet
stranded alone on page two, a heading orphaned at a page break, a lopsided final
page. None of that is visible in the YAML.

Everything the AI does lands in the same files the app is showing, in the same
places: a CV rendered here writes the PDF the app's **Export PDF** opens, and a
cover letter created with `create_cv(kind="letter")` appears in the app's
document list as a letter. There is nothing to sync.

The job tracker is deliberately not exposed here. Applications are the user's
record of what they sent and when; the AI's job is the documents.

## Setting it up in Claude Desktop

Open Claude Desktop → Settings → Developer → Edit Config, and add:

**Windows**

The installer is per-user (NSIS `currentUser` mode, so it never asks for admin),
which means the app lands under `%LOCALAPPDATA%`, not in `Program Files`:

```json
{
  "mcpServers": {
    "cv-studio": {
      "command": "C:\\Users\\YOU\\AppData\\Local\\Programs\\CV Studio\\server-dist\\cv-studio-server.exe",
      "args": ["--mcp"]
    }
  }
}
```

Replace `YOU` with your Windows username. JSON has no environment-variable
expansion, so `%LOCALAPPDATA%` will not work here — the path has to be literal.

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

**Comments survive `edit_cv_fields`** because it round-trips through ruamel.
`write_cv` replaces the file wholesale and will drop anything not in the new
content, which is why the tool description steers toward the former.

**It is local.** The server talks to your filesystem and nothing else. There are no
network calls, no telemetry. The AI client sees only what the tools return.

**Only one workspace per configured server.** Add a second entry with a
different `--workspace` if you keep separate sets of CVs.

## Claude Code

Claude Code users get more than the MCP tools: the `~/.claude/skills/` directory
in this project holds skills for the whole job-search workflow: analysing a
posting, tailoring a CV from a master profile, writing cover letters, tracking
applications and interview prep. The MCP server covers CV editing and rendering;
the skills cover the judgement around it.

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
