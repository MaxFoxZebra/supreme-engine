"""Interview prep: made here from the posting and the CV, saved with your
notes, and rewritten by an AI client without losing them."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import prep

fails = 0
def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + ("" if ok else "  -> " + str(detail)))
    fails += 0 if ok else 1

posting = "## What you will do\n\n- Run and improve our Kubernetes clusters across two regions\n- Own on-call for payment systems\n- Improve incident response and SLOs\n"
cv = {"sections": {"experience": [{"company": "Northwind", "highlights": [
        "Moved 140 services from VMs to Kubernetes with no downtime",
        "Led the incident review practice; pages per week fell from 14 to 3"]}],
      "skills": [{"label": "Infrastructure", "details": "Kubernetes, Terraform, Go"}]}}
job = {"language": "en", "description": posting}
p = prep.build(job, cv, "System design")
check("three posting questions", sum(q["src"] == "posting" for q in p["questions"]) == 3, p["questions"])
check("CV claims with numbers become questions", any(q["src"] == "cv" and "140" in q["q"] for q in p["questions"]))
check("round questions for the kind", any("ten times" in q["q"] for q in p["questions"]))
st = {s["req"]: s for s in p["stories"]}
check("Kubernetes is backed by the CV", "Kubernetes" in st["Run and improve our Kubernetes clusters across two regions"]["proof"])
check("payment on-call has no proof", st["Own on-call for payment systems"]["proof"] == "", st)
check("asks, two kept", len(p["asks"]) == 3 and sum(a["keep"] for a in p["asks"]) == 2)
fr = prep.build({"language": "fr", "description": posting}, cv, "")
check("French questions", fr["questions"][0]["q"].startswith("Le poste demande"))

# An AI client's questions keep the user's notes on the same question.
p["questions"][0]["note"] = "my story"; p["questions"][0]["state"] = "got"
p["questions"].append({"id": "x", "q": "My own question", "src": "you", "why": "", "cv": "", "note": "", "state": ""})
new = {"questions": [{"q": p["questions"][0]["q"], "src": "posting"}, {"q": "Something new", "src": "round"}]}
m = prep.merge(p, new)
qs = {q["q"]: q for q in m["questions"]}
check("note kept on a question asked again", qs[p["questions"][0]["q"]]["note"] == "my story")
check("rehearsal mark kept", qs[p["questions"][0]["q"]]["state"] == "got")
check("own question kept", "My own question" in qs)
check("new question added", "Something new" in qs)

# Stored through the job store and the studio API.
import jobs as J
ws = Path(tempfile.mkdtemp())
j = J.add_job(ws, {"company": "Acme", "title": "SRE", "status": "interviewing", "description": posting})
J.update_job(ws, j["id"], {"prep": m})
back = next(x for x in J.list_jobs(ws) if x["id"] == j["id"])["prep"]
check("prep round-trips through the store", back and len(back["questions"]) == len(m["questions"]), back)
J.update_job(ws, j["id"], {"prep": {"questions": [{"q": "", "src": "bad"}], "asks": "no"}})
back = next(x for x in J.list_jobs(ws) if x["id"] == j["id"])["prep"]
check("junk is cleaned", back["questions"] == [] and back["asks"] == [], back)

print("\nevery prep check passes" if not fails else f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
