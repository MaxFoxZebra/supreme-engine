"""The application lifecycle: one table (jobs.LIFECYCLE), and everything that
names a status agreeing with it.

    python checks/lifecycletest.py

A status is known in several places that cannot import each other: the store,
the MCP server, the app's script, the view in the chat, the translations. The
table is the store's; this says what a change to it has left behind, by name,
so adding a status is the row and then whatever this lists.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "i18n"))

import jobs  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail and not ok else ''}")
    fails += not ok


def js_map(src: str, opener: str) -> dict[str, str]:
    """The `key: "value"` pairs of the object literal that follows `opener`."""
    at = src.index(opener) + len(opener)
    body = src[at:src.index("}", at)]
    return dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', body))


def main() -> int:
    L, S = jobs.LIFECYCLE, set(jobs.STATUSES)

    # --- the table itself --------------------------------------------------
    check("every move goes to a status there is",
          all(set(v["next"]) <= S for v in L.values()),
          str({s: v["next"] for s, v in L.items() if not set(v["next"]) <= S}))
    check("no status lists itself as its next", all(s not in v["next"] for s, v in L.items()))
    seen, todo = {"pending"}, ["pending"]
    while todo:
        for n in L[todo.pop()]["next"]:
            if n not in seen:
                seen.add(n)
                todo.append(n)
    check("every status can be reached from a draft by the usual moves", seen == S, str(S - seen))
    check("a usual move never goes back a stage",
          all(L[n]["stage"] >= v["stage"] for v in L.values() for n in v["next"]))
    check("a draft is the only status at stage 0",
          [s for s, v in L.items() if v["stage"] == 0] == ["pending"])
    check("live, over and draft are every status, once each",
          jobs.LIVE | jobs.TERMINAL | {"pending"} == S
          and not jobs.LIVE & jobs.TERMINAL and "pending" not in jobs.LIVE | jobs.TERMINAL)
    check("what is live has somewhere to go", all(L[s]["next"] for s in jobs.LIVE))
    check("only giving up on a reply can be taken back",
          {s for s in jobs.TERMINAL if L[s]["next"]} == {s for s in S if s.startswith("ghosted")})

    # --- the funnel, drawn from it ------------------------------------------
    N = jobs.NODE_STATUSES
    check("the funnel's cumulative nodes are the stages",
          N["applied_s"] == jobs.reached(1) and N["interview_s"] == jobs.reached(2)
          and N["offer_s"] == jobs.reached(3) and N["all"] == jobs.STATUSES)
    check("every node holds statuses there are, and has a label",
          all(set(v) <= S for v in N.values()) and set(N) == set(jobs.LABELS))
    leaves = [n for n in N if not any(src == n for src, _, _ in jobs.FLOWS)]
    held = [s for n in leaves for s in N[n]]
    check("every status ends in exactly one leaf of the funnel",
          sorted(held) == sorted(S), str(sorted(S ^ set(held))))
    check("every flow carries statuses its two ends both hold",
          all(set(ss) <= set(N[a]) and set(ss) <= set(N[b]) for a, b, ss in jobs.FLOWS))

    # --- the store ----------------------------------------------------------
    ws = Path(tempfile.mkdtemp())
    j = jobs.add_job(ws, {"company": "Acme", "title": "SRE", "status": "applied"})
    j = jobs.update_job(ws, j["id"], {"status": "rejected_interviewing"})
    check("a move that is not usual is still saved, with its history",
          j["status"] == "rejected_interviewing"
          and [h["status"] for h in j["status_history"]] == ["applied", "rejected_interviewing"])
    check("and is said to be unusual, naming the usual ones",
          "interviewing, rejected, ghosted" in (jobs.unexpected_move("applied", "rejected_interviewing") or ""))
    check("a usual move, or none at all, is said nothing about",
          jobs.unexpected_move("applied", "interviewing") is None
          and jobs.unexpected_move("offer", "offer") is None
          and jobs.unexpected_move(None, "applied") is None)

    # --- the MCP server -----------------------------------------------------
    try:
        import mcp_server as M
    except Exception as e:                      # the SDK is not installed here
        M = None
        print(f"  skip  the MCP server ({type(e).__name__}: {e})")
    if M is not None:
        check("the card in the chat offers the table's moves", M.NEXT is jobs.NEXT)
        check("the chat has a label for every status", set(M.STATUS_LABEL) == S,
              str(S ^ set(M.STATUS_LABEL)))
        check("where each one stands covers everything sent, once",
              sorted(s for s, _ in M.OUTCOMES) == sorted(S - {"pending"}))
        stopped = [s for group in M.STOPPED_AT for s in group]
        check("where applications stop names statuses there are, with a word each",
              set(stopped) <= S and set(stopped) == set(M.DROP_LABEL), str(set(stopped) ^ set(M.DROP_LABEL)))
        view = (HERE / "static" / "mcp-jobs.html").read_text(encoding="utf-8")
        check("the view's labels are the server's", js_map(view, "const LABEL = {") == M.STATUS_LABEL)

    # --- the app ------------------------------------------------------------
    app = "\n".join(p.read_text(encoding="utf-8") for p in sorted((HERE / "static" / "js").glob("*.js")))
    pretty = js_map(app, "const map={pending:")
    pretty["pending"] = re.search(r'const map=\{pending:"([^"]*)"', app).group(1)
    tone = js_map(app, "const STATUS_TONE={")
    check("the app has a name for every status", set(pretty) == S, str(S ^ set(pretty)))
    check("and a colour", set(tone) == S, str(S ^ set(tone)))
    check("nothing over is coloured as in play, and nothing in play as over",
          all((tone[s] in ("waiting", "live", "offer")) == (s in jobs.LIVE) for s in S - {"pending"}),
          str({s: tone[s] for s in S}))
    check("the app keeps no list of its own of what is live or over",
          not re.search(r'(DEAD_ST|LIVE_ST|IV_STATUSES)\s*=\s*new Set\(\[', app))
    check("and reads the lifecycle that comes with the applications",
          "adoptLifecycle(d)" in app and '"lifecycle"' in (HERE / "studio_core" / "http_server.py").read_text(encoding="utf-8"))

    from catalogue import C  # noqa: E402
    missing = sorted({v for v in pretty.values() if v not in C} | {v for v in jobs.LABELS.values() if v not in C})
    check("every name the app shows for one is translated", not missing, ", ".join(missing))

    print("every lifecycle check passes" if not fails else f"{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
