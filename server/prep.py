"""Interview prep made on this computer, without an AI client.

From three things the app already has: the posting's requirement lines, the
CV that was sent, and the kind of round coming up. It gives the likely
questions (each saying where it comes from), the stories (what the posting
asks for, next to the line of the CV that shows it, or nothing), and a few
questions to ask them. An AI client connected over MCP can replace the
questions, stories and asks with better ones; the notes stay yours.
"""
from __future__ import annotations

import re
import uuid

# The posting, the CV and the round, in the application's language.
T = {
    "en": {
        "posting": "The role asks you to “{req}”. Where have you done that, and what came of it?",
        "cv": "Your CV says “{line}”. Walk us through it: what did you do, and how do you know it worked?",
        "why": "Why this role, and why now?",
    },
    "fr": {
        "posting": "Le poste demande de « {req} ». Où l'avez-vous déjà fait, et avec quel résultat ?",
        "cv": "Votre CV dit « {line} ». Racontez-nous : qu'avez-vous fait, et comment savez-vous que ça a marché ?",
        "why": "Pourquoi ce poste, et pourquoi maintenant ?",
    },
    "es": {
        "posting": "El puesto pide «{req}». ¿Dónde lo has hecho ya, y con qué resultado?",
        "cv": "Tu CV dice «{line}». Cuéntanos: ¿qué hiciste y cómo sabes que funcionó?",
        "why": "¿Por qué este puesto, y por qué ahora?",
    },
    "pt": {
        "posting": "A vaga pede “{req}”. Onde você já fez isso, e com que resultado?",
        "cv": "Seu CV diz “{line}”. Conte para nós: o que você fez e como sabe que funcionou?",
        "why": "Por que esta vaga, e por que agora?",
    },
}

# Two questions each kind of round tends to bring, and three to ask in it.
ROUND = {
    "en": {
        "Recruiter screen": (["Walk me through your background in two minutes.",
                              "What are you looking for in your next role, and what is your notice period?"],
                             ["What does the rest of the process look like, and how long does it take?",
                              "What made the last person in this role successful?",
                              "Why is the role open now?"]),
        "Technical": (["Take a recent problem you solved end to end: what was hard about it?",
                       "How do you decide something is ready to ship?"],
                      ["What does a normal week look like for the team?",
                       "How are technical decisions made and written down?",
                       "What would you want changed after six months?"]),
        "System design": (["Design the core flow of this product. What would you measure first?",
                           "Where does your design fail first at ten times the load?"],
                          ["What is the hardest part of the current system?",
                           "How is on-call shared, and how often does it page?",
                           "What would you want this person to have changed after six months?"]),
        "Take-home": (["Walk us through your solution: what did you leave out, and why?",
                       "What would you change with another day?"],
                      ["What did the strongest submissions have in common?",
                       "Who will I work with most closely?",
                       "What comes after this round, and when will I hear?"]),
        "Team fit": (["Tell us about a disagreement with a colleague and how it ended.",
                      "How do you like to receive feedback?"],
                     ["What does the team do well that you would not want to lose?",
                      "How do people here grow?",
                      "What is a hard week like on this team?"]),
        "Final": (["Why this company, and not the others you are talking to?",
                   "What would you do in your first ninety days?"],
                  ["What would make this hire a success a year from now?",
                   "What is the biggest risk the team faces this year?",
                   "What comes next, and by when do you decide?"]),
        "": (["Tell us about yourself.",
              "What are you proudest of in your last role?"],
             ["What would you want this person to have changed after six months?",
              "What does a hard week look like on this team?",
              "What comes after this round, and when will I hear?"]),
    },
    "fr": {
        "": (["Présentez-vous.", "De quoi êtes-vous le plus fier dans votre dernier poste ?"],
             ["Qu'attendez-vous de cette personne au bout de six mois ?",
              "À quoi ressemble une semaine difficile dans l'équipe ?",
              "Quelle est la suite du processus, et quand aurai-je un retour ?"]),
        "System design": (["Concevez le parcours principal de ce produit. Que mesureriez-vous en premier ?",
                           "Où votre conception casse-t-elle en premier à dix fois la charge ?"],
                          ["Quelle est la partie la plus difficile du système actuel ?",
                           "Comment les astreintes sont-elles partagées ?",
                           "Qu'attendez-vous de cette personne au bout de six mois ?"]),
    },
    "es": {
        "": (["Háblanos de ti.", "¿De qué estás más orgulloso en tu último puesto?"],
             ["¿Qué esperáis de esta persona a los seis meses?",
              "¿Cómo es una semana difícil en el equipo?",
              "¿Qué viene después de esta ronda, y cuándo tendré noticias?"]),
    },
    "pt": {
        "": (["Fale um pouco sobre você.", "Do que você mais se orgulha no seu último cargo?"],
             ["O que vocês esperam desta pessoa depois de seis meses?",
              "Como é uma semana difícil no time?",
              "Qual é o próximo passo, e quando terei um retorno?"]),
    },
}

STOP = set("""a an and are as at be by for from in into is it of on or our the their to we with you your
this that will who what how have has more than across around about all any each
le la les de des du un une et en au aux pour par sur dans avec nos vos votre notre est sont
el los las del un una y en con por para nuestro nuestra su sus es son
o os as do da dos das um uma e em com por para nosso nossa seu sua""".split())


def _stem(w: str) -> str:
    """Plural and singular alike: "reviews" meets "review"."""
    w = w.rstrip(".")
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-zà-ÿ0-9+#.]{2,}", text.lower()) if w not in STOP and not w.isdigit()}


def requirements(posting: str, limit: int = 6) -> list[str]:
    """The posting's bullet lines, as written, without Markdown."""
    out = []
    for m in re.findall(r"(?m)^\s*[-*•]\s+(.+)$", posting or ""):
        line = re.sub(r"[*_`]", "", m).strip().rstrip(".;")
        if 8 <= len(line) <= 200:
            out.append(line)
    return out[:limit]


def cv_lines(cv: dict) -> list[tuple[str, str]]:
    """(line, where) for every highlight and skill in a RenderCV cv block."""
    out = []
    for title, entries in ((cv or {}).get("sections") or {}).items():
        for e in entries or []:
            if isinstance(e, str):
                out.append((e, str(title).replace("_", " ").title()))
                continue
            if not isinstance(e, dict):
                continue
            where = str(e.get("company") or e.get("institution") or e.get("name") or e.get("label")
                        or str(title).replace("_", " ").title())
            for h in e.get("highlights") or []:
                out.append((str(h), where))
            if e.get("label") and e.get("details"):
                out.append((f"{e['label']}: {e['details']}", str(title).replace("_", " ").title()))
    return [(re.sub(r"[*_`]", "", t).strip(), w) for t, w in out if str(t).strip()]


def _weights(req: str) -> dict[str, int]:
    """Each word of a requirement, weighted: a name (Prometheus, SLOs, Go) or
    anything with a digit or symbol counts double, a plain word once."""
    out = {}
    for k, m in enumerate(re.finditer(r"[A-Za-zÀ-ÿ0-9+#.]{2,}", req)):
        w = m.group(0)
        low = _stem(w.lower())
        if low in STOP or low.isdigit():
            continue
        named = (k > 0 and w[0].isupper()) or any(c.isupper() for c in w[1:]) or re.search(r"[\d+#]", w)
        out[low] = max(out.get(low, 0), 2 if named else 1)
    return out


def match(req: str, lines: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The CV line that best backs a requirement, if any really does: two
    plain words in common, or one name. Something you did wins a tie over a
    skill you only list."""
    want = _weights(req)
    listed = lambda l: bool(re.match(r"[^:]{1,40}:\s", l))
    best, score = None, 0
    for line, where in lines:
        have = _words(line)
        sc = sum(v for k, v in want.items() if k in have) + (0 if listed(line) else 0.5)
        if sc > score:
            best, score = (line, where), sc
    return best if score >= 2 else None


def build(job: dict, cv: dict | None, round_kind: str = "", person_role: str = "") -> dict:
    """Prep for the next round, made here: questions, stories, asks."""
    lang = job.get("language") if job.get("language") in T else "en"
    tx = T[lang]
    reqs = requirements(job.get("description") or "")
    lines = cv_lines(cv or {})
    qid = lambda: uuid.uuid4().hex[:8]
    questions = [{"id": qid(), "q": tx["posting"].format(req=r[0].lower() + r[1:]), "src": "posting",
                  "why": r, "cv": (match(r, lines) or ("", ""))[0], "note": "", "state": ""}
                 for r in reqs[:3]]
    # Claims with a number in them are the ones an interviewer tests.
    claims = [(l, w) for l, w in lines if re.search(r"\d", l) and ":" not in l[:24]]
    questions += [{"id": qid(), "q": tx["cv"].format(line=l.rstrip(".")), "src": "cv", "why": "",
                   "cv": f"{l} · {w}", "note": "", "state": ""} for l, w in claims[:2]]
    kinds = ROUND.get(lang, {})
    qs, asks = kinds.get(round_kind) or kinds.get("") or ROUND["en"].get(round_kind) or ROUND["en"][""]
    questions += [{"id": qid(), "q": q, "src": "round", "why": "", "cv": "", "note": "", "state": ""}
                  for q in qs]
    questions.append({"id": qid(), "q": tx["why"], "src": "round", "why": "", "cv": "", "note": "",
                      "state": ""})
    stories = []
    for r in reqs:
        m = match(r, lines)
        stories.append({"req": r, "proof": m[0] if m else "", "where": m[1] if m else ""})
    return {"questions": questions, "stories": stories,
            "asks": [{"id": qid(), "q": a, "keep": i < 2} for i, a in enumerate(asks)],
            "by": "", "at": "", "cv_read": False}


def merge(old: dict | None, new: dict) -> dict:
    """New questions from an AI client, keeping what the user wrote: notes and
    rehearsal state on a question asked again, and their own questions."""
    old = old or {}
    norm = lambda s: re.sub(r"\W+", " ", str(s or "").lower()).strip()
    kept = {norm(q.get("q")): q for q in old.get("questions") or []}
    out = []
    for q in new.get("questions") or []:
        prev = kept.get(norm(q.get("q")))
        if prev:
            q = {**q, "id": prev.get("id"), "note": prev.get("note", ""), "state": prev.get("state", "")}
        out.append(q)
    seen = {norm(q.get("q")) for q in out}
    out += [q for q in old.get("questions") or []
            if (q.get("src") == "you" or q.get("note")) and norm(q.get("q")) not in seen]
    asks_old = {norm(a.get("q")): a for a in old.get("asks") or []}
    asks = [{**a, "keep": asks_old.get(norm(a.get("q")), a).get("keep", a.get("keep", False))}
            for a in new.get("asks") or old.get("asks") or []]
    return {**old, **new, "questions": out, "asks": asks,
            "stories": new.get("stories") or old.get("stories") or [],
            "cv_read": old.get("cv_read", False)}
