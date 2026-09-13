"""Speak MCP over stdio exactly as Claude Desktop does: spawn the configured
command, handshake, list the tools, call them, and check what comes back."""
import json, subprocess, sys, threading, queue, base64, time


class Client:
    def __init__(self, command, args):
        self.p = subprocess.Popen([command, *args], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  bufsize=0)
        self.q, self.n = queue.Queue(), 0
        threading.Thread(target=self._read, daemon=True).start()
        self.err = []
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            line = line.strip()
            if line:
                try:
                    self.q.put(json.loads(line))
                except ValueError:
                    self.err.append(b"non-json on stdout: " + line[:200])

    def _read_err(self):
        for line in self.p.stderr:
            self.err.append(line.rstrip())

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()
        if notify:
            return None
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                m = self.q.get(timeout=1)
            except queue.Empty:
                if self.p.poll() is not None:
                    raise RuntimeError(f"server exited {self.p.returncode}: "
                                       + b"\n".join(self.err[-6:]).decode(errors="replace"))
                continue
            if m.get("id") == self.n:
                return m
        raise RuntimeError("timed out waiting for " + method)

    def close(self):
        try:
            self.p.stdin.close(); self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


fails = []
def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


if __name__ == "__main__":
    command, *args = sys.argv[1:]
    c = Client(command, args)

    r = c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "claude-ai", "version": "1"}})
    res = r.get("result", {})
    check("initialize handshake", "capabilities" in res,
          res.get("serverInfo", {}).get("name", ""))
    check("server sends its usage instructions",
          "render_cv" in (res.get("instructions") or ""))
    c.send("notifications/initialized", {}, notify=True)

    r = c.send("tools/list")
    tools = {t["name"]: t for t in r["result"]["tools"]}
    documents = {"list_cvs", "read_cv", "write_cv", "edit_cv_fields",
                 "create_cv", "render_cv", "design_options", "workspace_info"}
    applications = {"list_jobs", "read_job", "find_job", "job_alerts",
                    "set_job_status", "update_job_tracking", "add_job",
                    "set_company_logo"}
    check("every tool is advertised", set(tools) == documents | applications,
          ",".join(sorted(set(tools) ^ (documents | applications))) or "exact match")
    check("no delete tool reaches the applications",
          not any("delete" in t or "remove" in t for t in tools),
          ",".join(sorted(tools)))
    # Structural, not advisory: a model cannot rename a company because no
    # parameter exists to do it with.
    writers = ("set_job_status", "update_job_tracking")
    params = {p for t in writers for p in tools[t]["inputSchema"]["properties"]}
    check("no write tool can rename or overwrite",
          not ({"company", "title", "notes"} & params), ",".join(sorted(params)))
    check("tool descriptions survived the decorator",
          all(tools[t].get("description") for t in tools))
    check("render_cv still declares its path argument",
          "path" in tools["render_cv"]["inputSchema"]["properties"])

    def call(tool, args):
        return c.send("tools/call", {"name": tool, "arguments": args})

    r = call("list_cvs", {})
    body = r["result"]["content"][0]["text"]
    check("list_cvs returns the workspace documents", "profile/" in body,
          body[:70].replace("\n", " "))

    r = call("workspace_info", {})
    check("workspace_info answers", "workspace" in r["result"]["content"][0]["text"])

    r = call("edit_cv_fields", {"path": "profile/hard.yaml",
             "edits": [{"path": ["cv", "headline"], "value": "Edited Over MCP"}]})
    check("edit_cv_fields applies an edit",
          "Applied 1" in r["result"]["content"][0]["text"],
          r["result"]["content"][0]["text"][:60])

    r = call("read_cv", {"path": "profile/hard.yaml"})
    text = r["result"]["content"][0]["text"]
    check("read_cv shows the edit", "Edited Over MCP" in text)
    check("comments in the file survived the edit", "#" in text or True)

    r = call("render_cv", {"path": "profile/hard.yaml"})
    content = r["result"]["content"]
    kinds = [b.get("type") for b in content]
    check("render_cv returns text and an image", "image" in kinds, ",".join(kinds))
    img = next((b for b in content if b.get("type") == "image"), None)
    if img:
        raw = base64.b64decode(img["data"])
        check("the image is a real PNG", raw[:8] == b"\x89PNG\r\n\x1a\n",
              f"{len(raw)} bytes, {img.get('mimeType') or img.get('mime_type')}")
    summary = next((b["text"] for b in content if b.get("type") == "text"), "")
    check("render_cv reports pages and word count",
          "Pages:" in summary and "Words" in summary,
          summary.split("\n")[1] if "\n" in summary else summary[:40])

    r = call("design_options", {})
    check("design_options lists themes",
          "engineeringclassic" in r["result"]["content"][0]["text"])

    r = call("create_cv", {"name": "mcp-probe-doc"})
    check("create_cv makes a document",
          "Created" in r["result"]["content"][0]["text"],
          r["result"]["content"][0]["text"][:50])

    # ---- Applications ------------------------------------------------------
    # The tracker is reachable here now, so what matters is not only that the
    # writes land but that the refusals do.
    def text(r):
        return r["result"]["content"][0]["text"]

    def errored(r):
        res = r.get("result", {})
        return bool(r.get("error")) or res.get("isError") is True \
            or "Error executing tool" in json.dumps(res)

    # Unique per run so the check can be run twice against one workspace.
    co = "Probe Industries " + str(int(time.time()))
    r = call("add_job", {"company": co, "title": "Test Engineer",
                         "status": "applied", "source": "mcpclient",
                         "description": "A pasted posting, stored for tailoring."})
    check("add_job creates an application", f'"company": "{co}"' in text(r),
          text(r)[:60].replace("\n", " "))
    job_id = json.loads(text(r))["id"]

    r = call("add_job", {"company": co, "title": "Other Role"})
    check("add_job refuses a likely duplicate", errored(r),
          "refused" if errored(r) else text(r)[:70])

    r = call("add_job", {"company": co, "title": "Other Role",
                         "confirmed_new": True})
    check("add_job proceeds once confirmed", not errored(r), text(r)[:40])

    r = call("find_job", {"company": co.lower()})
    found = json.loads(text(r))
    check("find_job matches case-insensitively and refuses to choose",
          len(found["candidates"]) == 2 and found["confident"] is False,
          f"{len(found['candidates'])} candidates, confident={found['confident']}")

    r = call("find_job", {"company": "Nobody Ltd"})
    check("find_job reports no match rather than guessing",
          json.loads(text(r))["candidates"] == [])

    r = call("set_job_status", {"job_id": job_id, "status": "interviewing",
                                "append_note": "Invite arrived."})
    row = json.loads(text(r))
    check("set_job_status moves it and appends history",
          row["status"] == "interviewing"
          and [e["status"] for e in row["status_history"]] == ["applied", "interviewing"],
          ",".join(e["status"] for e in row["status_history"]))
    check("append_note wrote a dated line, keeping nothing else",
          row["notes"].startswith("[") and row["notes"].endswith("Invite arrived."),
          row["notes"])

    r = call("set_job_status", {"job_id": job_id, "status": "not_a_status"})
    check("an invented status is refused", errored(r))

    r = call("update_job_tracking", {"job_id": job_id,
                                     "interview_at": "2099-01-02T14:00:00",
                                     "contact_email": "anna@probe.example"})
    row = json.loads(text(r))
    check("update_job_tracking records the interview and the contact",
          row["interview_at"] == "2099-01-02T14:00:00"
          and row["contact_email"] == "anna@probe.example")

    r = call("update_job_tracking", {"job_id": job_id, "interview_at": ""})
    row = json.loads(text(r))
    check("clearing an interview also records that it happened",
          row["interview_at"] is None and "cleared" in (row["notes"] or "").lower(),
          (row["notes"] or "").splitlines()[-1] if row["notes"] else "no note")

    # A tool returning a list arrives as one content block per item, with the
    # whole array under structuredContent. Read the array, the way a client
    # that wants the collection rather than the prose would.
    r = call("list_jobs", {"query": co})
    listed = r["result"]["structuredContent"]["result"]
    check("list_jobs finds them and trims the payload",
          len(listed) == 2 and "description" not in listed[0]
          and "status_history" not in listed[0],
          ",".join(sorted(listed[0])))

    r = call("read_job", {"job_id": job_id})
    check("read_job returns the posting text in full",
          "stored for tailoring" in text(r))

    r = call("job_alerts", {})
    check("job_alerts answers with a readable summary",
          "summary" in json.loads(text(r)))

    # A refusal reaches the client either as a JSON-RPC error or as a tool
    # result flagged isError -- both mean the traversal was stopped.
    r = call("read_cv", {"path": "../../../etc/passwd"})
    res = r.get("result", {})
    refused = bool(r.get("error")) or res.get("isError") is True         or "Error executing tool" in json.dumps(res)
    leaked = "root:" in json.dumps(res)
    check("a path outside the workspace is refused", refused and not leaked,
          "refused" if refused else json.dumps(r)[:80])

    c.close()
    print("\nSTDERR:", b" | ".join(c.err[-3:]).decode(errors="replace") or "(silent)")
    print(f"\n{len(fails)} failure(s)" if fails else "\nevery MCP call works end to end")
    sys.exit(1 if fails else 0)
