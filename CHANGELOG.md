# Changelog

What changed in each release, written for the person installing it. The commit
history has the reasoning; this has the effect.

The release workflow reads this file: the section matching the tag becomes the
release notes on GitHub and the text the app shows when an update is waiting.
A version with no section here falls back to the commit subjects since the last
tag, so a release is never published with nothing said about it.

## 0.14.0

- **One workbench instead of two.** Only the title bar is dark now. The
  sidebars and the footer sit on the same paper as the work, so the window no
  longer reads as a dark frame bolted around a light page.
- **The title bar stays the same shape on every screen.** It carries the new
  mark, the three tabs and the AI clients, and nothing else. Search and New
  application live in the Applications header, the date range lives on the
  Funnel, and the editor keeps the tabs, with a crumb back to the list you
  came from.
- **The AI clients show their logos.** Connected ones carry a green dot on
  the corner; ones you have not set up are faded.
- **Ochre means one thing at a time.** It marks the primary button, the
  selected row and a follow-up that is due. An application with no CV says
  "Not tailored" quietly and offers **Tailor a CV** when you are on the row,
  rather than every row shouting it in orange.
- **An open application gets the room.** The table narrows to a list of
  companies and roles on the left, and the record takes the rest of the
  window, instead of the other way round.
- **The base CV is a card at the foot of the filters**, not a band across
  the top of the list.
- **The Funnel reads at a glance.** The rates are four tiles across the top,
  the bands are flat colour, and clicking a stage lists its applications
  beside the chart instead of underneath it.
- **Settings opens over the app** rather than replacing it.

## 0.13.0

- **Your documents have a screen.** Until now the only list of them lived
  inside the editor, so finding a CV meant already having one open — and a CV
  written for nothing, a master copy or an old version, was reachable from
  nowhere at all. **Documents** sits beside Applications and lists everything:
  the ones written for an application, each showing which one, and the ones
  written for nothing.
- **The base CV can be seen.** It was a thin band the colour of the table
  header, its name set smaller than the company names below it, with its
  buttons a screen-width away at the other edge. It is now its own surface,
  the name outranks the rows it is the parent of, and Open sits next to it.
- **Back knows where you came from.** Leaving a document returns you to the
  application it was written for, as before — or to Documents, if that is
  where you opened it and it belongs to no application. The button says which.
- **Jobs is called Applications**, in the tab, the search box, the footer and
  everywhere else the app had been saying both.

- **Hermes Agent is a fourth AI client.** One button in Settings → AI clients,
  the same as the others, and it covers Hermes Desktop, the TUI and the CLI at
  once, because they share one config. Run `/reload-mcp` and the tools are
  there without leaving the conversation you are in.
- That config is YAML you are likely to have hand-written, so it is **edited
  rather than rewritten**: your comments, your ordering, your other servers and
  whatever you set on this one — a timeout, an environment variable, a tool
  filter — all stay exactly as they were. Only the command and the workspace
  are ours to write.
- Edits that arrive through Hermes are **marked as Hermes'**, beside the field,
  in the outline and in the page margin, like every other client's.

## 0.12.3

- **Release notes say what changed.** Every release used to carry the same
  paragraph about Apple quarantine and SmartScreen and nothing about the
  version you were installing. The notes now come from this file; the install
  note is still there, folded away at the bottom where it belongs.
- **An update comes to you.** When a new version is waiting the app shows it,
  with what changed in it, and installs and restarts in one step. It used to be
  a toast telling you to go and look in Settings, which is a notification about
  a notification. Saying **Later** means later — it will not ask again for that
  version, and Settings still has it whenever you want it.

## 0.12.2

- Claude can now record **which CV was sent for which application**, not only
  read it. Tailoring end to end — copy your base CV, edit it, attach it to the
  job — is one conversation instead of a conversation and then a click.
- A wrong path is refused rather than written: it has to be inside the
  workspace, exist, and be the right kind of document.
- `MCP.md` gained a **What it cannot do** section — a straight map of what an
  AI client can and cannot reach in your workspace, checked against the tools
  rather than remembered.

## 0.12.1

- The editor is **no longer a third tab**. You reach a document by opening it
  from an application or from your base CV, and **← Applications** — or `Esc` —
  takes you back to the application it was written for, with its row selected.
  If a filter was hiding that row, going back widens the filter to find it.
- The top bar **stops rearranging itself** when you switch screens.

## 0.12.0

- **The app opens on your applications.** The work is applying for jobs; a CV
  is something an application either has or has not got yet.
- **A base CV.** One document every tailored copy starts from, pinned above the
  list. It lives in the workspace rather than in the browser, so the AI clients
  you have connected can see it too.
- **Tailor in one click.** An application with no CV offers to make one: it
  copies the base, names it after the company and role, attaches it and opens
  it. What it was copied from is recorded, so every field you then change is
  marked as differing from the base.
- A row with a cover letter and no CV used to hide the letter. It doesn't now.

## 0.11.0

- **Add and remove sections and entries from the Form tab.** Every section has
  an Add under it, every entry a ×, and there is an Add a section at the end. A
  new entry copies the shape of its neighbours; a new section asks which kind
  once.
- Clearing a date no longer breaks the render. An empty date has to be absent,
  not empty, and it was being written as empty.

## 0.11.1

- **Clicking a block on the rendered page works again.** It had been silently
  broken on seven of the nine themes RenderCV ships: the page was a picture and
  nothing said why. Entries with no dates were the trigger.
- The page's click targets now hug the text column exactly, measured from the
  document rather than assumed from the image.

## 0.10.0

- **Form and YAML sit beside the page** instead of replacing it, so the render
  is on screen while you type rather than a tab away. Drag the divider to
  change the share; it stays where you put it.
- Put the caret in a field, or on a line of YAML, and the page highlights that
  entry and scrolls to it. Click a block on the page and the left pane goes to
  it.

## 0.9.0

- **Edit the CV on the page itself.** Click any block and a card opens beside
  it with that entry's fields; the page steps aside to make room. Arrow keys
  walk it from block to block. The form column that used to stand there
  permanently is gone.
- When the page cannot be made clickable, it now says so and why, instead of
  looking broken.

## 0.8.1

- The release build refuses to run when the tag and the app version disagree.
  v0.8.0 shipped binaries that called themselves 0.7.0, so the updater offered
  0.7.0 to machines already running it and nothing ever arrived.
