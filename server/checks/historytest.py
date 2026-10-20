"""History: every version of every CV, letter and application, who made it,
and a way back to any of them.

    python checks/historytest.py

In the process first, the way the app's writes reach it; then through the MCP
server as a client, the way an AI client's do.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "checks"))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]

import studio  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + str(detail) if detail else ''}")
    fails += not ok


def bump(p: Path, text: str) -> None:
    """Write, and move the clock on so the change is seen even on a file
    system that keeps mtimes coarsely."""
    p.write_text(text, encoding="utf-8")
    t = time.time() + 2 + bump.n
    bump.n += 2
    os.utime(p, (t, t))


bump.n = 0


def in_process() -> None:
    ws = Path(tempfile.mkdtemp()) / "My CVs"
    studio.WORKSPACE = ws
    studio.bootstrap(ws)
    cv = next((ws / "profile").glob("*.yaml"))
    rel = studio.rel(cv)
    original = cv.read_text(encoding="utf-8")

    print("Where history begins")
    studio.note_history("elsewhere")
    h = studio.history_of(rel)
    check("the first look keeps every file as it is", len(h["versions"]) == 1
          and h["versions"][0]["who"] == "start", h["versions"])
    check("it is kept outside the workspace", not str(studio.history_dir()).startswith(str(ws)),
          studio.history_dir())
    check("nothing changed is nothing kept", studio.note_history("you", tool="save") == 0)

    print("Who changed what")
    bump(cv, original.replace("Your Name", "Max Bidault"))
    studio.note_history("claude", agent="Claude Desktop", tool="write_cv")
    bump(cv, original.replace("Your Name", "Maxime Bidault"))
    studio.note_history("you", tool="save")
    bump(cv, original.replace("Your Name", "Maxime B."))
    studio.note_history("you", tool="save")
    h = studio.history_of(rel)
    who = [(v["who"], v["tool"]) for v in h["versions"]]
    check("an AI client's change is its own version, named", ("claude", "write_cv") in who, who)
    check("your saves a moment apart are one version", who.count(("you", "save")) == 1, who)
    check("newest first", h["versions"][0]["who"] == "you" and h["versions"][-1]["who"] == "start", who)
    mine = h["versions"][0]
    check("and it says when it began", bool(mine.get("since")), mine)
    check("and how many lines it moved", mine["lines"] == {"added": 1, "removed": 1}, mine["lines"])

    bump(cv, cv.read_text(encoding="utf-8").replace("Maxime B.", "Maxime Bidault"))
    studio.note_history("elsewhere")
    check("a change made outside CV Studio is kept as such",
          studio.history_of(rel)["versions"][0]["who"] == "elsewhere")

    print("What a version changed")
    claude = next(v for v in studio.history_of(rel)["versions"] if v["who"] == "claude")
    d = studio.history_diff(rel, claude["id"])
    adds = [ln["text"] for ln in d["lines"] if ln["t"] == "add"]
    dels = [ln["text"] for ln in d["lines"] if ln["t"] == "del"]
    check("against the version before it", any("Max Bidault" in x for x in adds)
          and any("Your Name" in x for x in dels), (adds, dels))
    check("long unchanged runs are cut short", any(ln["t"] == "skip" for ln in d["lines"]))
    now = studio.history_diff(rel, claude["id"], "now")
    check("and against the file now", any("Maxime Bidault" in ln["text"] for ln in now["lines"]
                                          if ln["t"] == "add"), now["summary"])

    print("Restoring")
    studio.restore_version(rel, claude["id"])
    check("puts the file back as that version", "Max Bidault" in cv.read_text(encoding="utf-8")
          and "Maxime" not in cv.read_text(encoding="utf-8"))
    vs = studio.history_of(rel)["versions"]
    check("is a version of its own, saying so", vs[0]["tool"] == "restore"
          and "Restored the version of" in (vs[0]["note"] or ""), vs[0])
    check("and loses nothing in between", any(v["who"] == "elsewhere" for v in vs))
    try:
        studio.restore_version(rel, "nope")
        check("an unknown version is refused", False)
    except ValueError:
        check("an unknown version is refused", True)

    print("Deleted, and back")
    letter = studio.new_letter(None, "plain")["path"]
    studio.note_history("you", tool="letter/new")
    lp = ws / letter
    bump(lp, lp.read_text(encoding="utf-8") + "\nA sentence worth keeping.\n")
    studio.note_history("you", tool="save")
    lp.unlink()
    studio.note_history("you", tool="doc/delete")
    vs = studio.history_of(letter)["versions"]
    check("a deleted file says so", vs[0]["deleted"] and not studio.history_of(letter)["exists"], vs[0])
    studio.restore_version(letter, vs[1]["id"])
    check("and its last version brings it back", lp.is_file()
          and "worth keeping" in lp.read_text(encoding="utf-8"))

    print("Applications")
    job = studio.jobstore.add_job(ws, {"company": "Scaleway", "title": "AI Evangelist"})
    studio.note_history("claude", agent="Claude Desktop", tool="add_job")
    studio.jobstore.update_job(ws, job["id"], {"status": "applied", "notes": "Sent on Monday"})
    studio.note_history("you", tool="jobs/update")
    name = studio.jobstore._find(ws, job["id"])[0]
    path = f"tracker/{name}"
    vs = studio.history_of(path)["versions"]
    check("each change to an application is kept", [v["who"] for v in vs] == ["you", "claude"],
          [v["who"] for v in vs])
    recent = studio.history_recent()
    lab = next((c.get("label") for c in recent if c["path"] == path), None)
    check("recent changes name it by company and role", lab == "Scaleway, AI Evangelist", lab)
    studio.restore_version(path, vs[1]["id"])
    back = studio.jobstore.list_jobs(ws)[0]
    check("and one can be restored", back["status"] == "pending" and not back.get("notes"), back)

    print("Sample data")
    sample_ws = studio.sample.folder()
    studio.WORKSPACE = sample_ws
    sample_ws.mkdir(parents=True, exist_ok=True)
    check("is not kept", studio.note_history("you") == 0)
    studio.WORKSPACE = ws


def through_mcp() -> None:
    from mcpclient import Client
    print("Through an AI client")
    ws = Path(tempfile.mkdtemp()) / "ws"
    c = Client(os.environ.get("PYTHON") or sys.executable,
               [str(HERE / "server_main.py"), "--mcp", "--workspace", str(ws), "--client", "claude"])
    try:
        c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "claude-ai", "version": "1"}})
        c.send("notifications/initialized", {}, notify=True)
        tools = {t["name"]: t for t in c.send("tools/list", {})["result"]["tools"]}
        check("the history tools are listed", {"file_history", "restore_version"} <= set(tools))
        check("file_history only reads",
              (tools.get("file_history", {}).get("annotations") or {}).get("readOnlyHint") is True)
        call = lambda name, args: c.send("tools/call", {"name": name, "arguments": args})["result"]
        cvp = "profile/my-cv.yaml"
        r = call("edit_cv_fields", {"path": cvp, "edits": [{"path": ["cv", "name"], "value": "Maxime Bidault"}]})
        check("an edit goes through", not r.get("isError"), r)
        text = call("file_history", {"path": cvp})["content"][0]["text"]
        check("and is in the history as the client's", "edit_cv_fields" in text and "by" in text, text)
        vid = next(line.split()[1] for line in text.splitlines() if "as it was when history began" in line)
        diff = call("file_history", {"path": cvp, "version_id": vid})["content"][0]["text"]
        check("a version's changes can be read", "lines against the version before" in diff, diff[:200])
        r = call("restore_version", {"path": cvp, "version_id": vid})
        check("a CV can be restored", not r.get("isError")
              and "Your Name" in (ws / cvp).read_text(encoding="utf-8"), r)
        call("add_job", {"company": "Acme", "title": "Engineer"})
        path = next(f"tracker/{p.name}" for p in (ws / "tracker").glob("*.yaml"))
        jtext = call("file_history", {"path": path})["content"][0]["text"]
        vid = jtext.splitlines()[1].split()[1]
        r = call("restore_version", {"path": path, "version_id": vid})
        check("an application is not restored by a model", bool(r.get("isError")), r)
    finally:
        c.close()


if __name__ == "__main__":
    in_process()
    through_mcp()
    print()
    print("every history check passes" if not fails else f"{fails} history check(s) failed")
    sys.exit(1 if fails else 0)
