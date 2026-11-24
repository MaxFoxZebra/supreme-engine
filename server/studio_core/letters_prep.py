"""Cover letters, interview prep, match scoring, application packs, and browser clip."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import threading
import time
import unicodedata
import zipfile
from pathlib import Path

from .context import (
    CLIP_PORT,
    cjkfonts,
    ctx,
    is_letter,
    jobstore,
    languages,
    letters,
    rel,
    safe_path,
    to_plain,
    yaml_rt,
)
from . import documents as _docs
from . import history_scans as _hist
from . import provenance as _prov


def letter_cv(meta: dict) -> Path | None:
    """The CV a letter looks like: the one it names, else the base CV."""
    for cand in (meta.get("looks_like"), (_prov.base_cv() or {}).get("path")):
        if cand:
            try:
                f = safe_path(str(cand))
                if f.exists():
                    return f
            except (PermissionError, ValueError):
                continue
    return None


def letter_head(meta: dict) -> dict:
    cvp = letter_cv(meta)
    data = {}
    if cvp:
        try:
            data = to_plain(yaml_rt.load(cvp.read_text(encoding="utf-8"))) or {}
        except Exception:
            data = {}
    theme = str(((data.get("design") or {}).get("theme")) or "classic")
    head = letters.letterhead(data, _docs.design_defaults(theme))
    head["cv"] = rel(cvp) if cvp else None
    return letters.override_head(head, meta.get("letterhead"))


def load_letter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    meta, body = letters.parse(text)
    head = letter_head(meta)
    return {"letter": True, "path": rel(path), "text": text, "meta": meta,
            "body": body, "head": head, "date_line": letters.date_line(dated(meta, path)),
            "sent_on": letter_sent_on(path),
            "words": letters.word_count(body), "target": letters.WORD_TARGET,
            "mtime": path.stat().st_mtime, "review": _prov.review_payload(path),
            # Writing prompts still in the body: shown as placeholders, never printed.
            "prompts": letters.prompts_left(body)}


def save_letter(path: Path, payload: dict, tool: str = "save") -> dict:
    if "text" in payload:
        text = str(payload["text"])
    else:
        meta, body = letters.parse(path.read_text(encoding="utf-8")) if path.exists() else ({}, "")
        meta.update({k: v for k, v in (payload.get("meta") or {}).items()})
        text = letters.dump(meta, payload.get("body", body))
    path.write_text(text, encoding="utf-8")
    return load_letter(path)




def letter_sent_on(path: Path) -> str | None:
    """The day the letter's application was sent, if it was."""
    try:
        job = next((j for j in jobstore.list_jobs(ctx.workspace) if j.get("letter_path") == rel(path)), None)
        sent = next((h["at"] for h in (job or {}).get("status_history") or [] if h.get("status") == "applied"), None)
        return str(sent)[:10] if sent else None
    except Exception:
        return None


def dated(meta: dict, path: Path) -> dict:
    """"today" on a letter already sent would redate it each export: once its
    application went out, the letter carries the day it was sent."""
    if meta.get("date") in (None, "", "today"):
        sent = letter_sent_on(path)
        if sent:
            return {**meta, "date": sent}
    return meta


def render_letter(path: Path) -> dict:
    meta, body = letters.parse(path.read_text(encoding="utf-8"))
    meta = dated(meta, path)
    head = letter_head(meta)
    cvp = letter_cv(meta)
    cjkfonts.ensure(path.read_text(encoding="utf-8") + head.get("name", ""))
    r = letters.render(meta, body, head, _docs.output_dir(path), letters.file_stem(head["name"]),
                       [p for p in ([cvp.parent / "fonts"] if cvp else []) + [path.parent / "fonts"]])
    if not r.get("ok"):
        return {"ok": False, "error": r.get("log"), "hint": "The letter did not lay out."}
    stamp = int(time.time() * 1000)
    return {"ok": True, "pages": r["pages"], "words": r["words"], "pdf": rel(Path(r["pdf"])),
            "pngs": [f"/api/asset?path={rel(Path(f))}&v={stamp}" for f in r["png_pages"]]}



def letter_map(path: Path, prefix: str = "") -> dict | None:
    """Where each paragraph of a rendered letter is on its pages (letters.page_map)."""
    meta, body = letters.parse(path.read_text(encoding="utf-8"))
    meta = dated(meta, path)
    cvp = letter_cv(meta)
    return letters.page_map(meta, body, letter_head(meta), _docs.output_dir(path),
                            [p for p in ([cvp.parent / "fonts"] if cvp else []) + [path.parent / "fonts"]],
                            prefix)

def _slug_name(*parts: str) -> str:
    """"Alex Moreau", "Mistral AI", "Senior Platform Engineer" ->
    Alex-Moreau-Mistral-AI-Senior-Platform-Engineer: what a recruiter's
    download folder can tell apart."""
    words = []
    for p in parts:
        words += re.findall(r"[\w]+", unicodedata.normalize("NFKD", str(p or ""))
                            .encode("ascii", "ignore").decode())
    return "-".join(words)[:120] or "application"


def pack_info(job_id: str) -> dict:
    """What Export both would put together for an application."""
    job = next((j for j in jobstore.list_jobs(ctx.workspace) if j["id"] == job_id), None) \
        if jobstore is not None else None
    if job is None:
        raise ValueError("No such job.")
    have = {}
    for key in ("cv_path", "letter_path"):
        p = job.get(key)
        if p:
            try:
                f = safe_path(p)
            except (ValueError, PermissionError):
                continue
            if f.is_file():
                have[key] = f
    person = ""
    if "cv_path" in have:
        data = to_plain(yaml_rt.load(have["cv_path"].read_text(encoding="utf-8"))) or {}
        person = str(((data.get("cv") or {}).get("name")) or "")
    if not person and "letter_path" in have:
        meta, _ = letters.parse(have["letter_path"].read_text(encoding="utf-8"))
        person = str(letter_head(meta).get("name") or "")
    return {"job": job, "files": have,
            "name": _slug_name(person, job.get("company"), job.get("title")),
            "person": _slug_name(person) if person else "",
            "cv": "cv_path" in have, "letter": "letter_path" in have,
            "posting": bool((job.get("description") or "").strip())}



# Runs on the job page when the bookmark is clicked. It reads the job the page
# describes for search engines (schema.org JobPosting), or the text selected,
# and hands it to CV Studio's own window, which it opens. The page never sees
# the app's key: the window is served by the app and saves from there.
BOOKMARKLET = r"""(()=>{const O="http://127.0.0.1:__PORT__";
const F=o=>{if(!o||typeof o!=="object")return null;if(Array.isArray(o)){for(const x of o){const r=F(x);if(r)return r}return null}
if(o["@graph"])return F(o["@graph"]);return [].concat(o["@type"]||[]).includes("JobPosting")?o:null};
let J=null;for(const s of document.querySelectorAll('script[type="application/ld+json"]')){try{J=F(JSON.parse(s.textContent));if(J)break}catch(e){}}
const q=l=>{for(const x of l.split("|")){const e=document.querySelector(x);if(e&&e.innerText.trim())return e}return null};
const tx=l=>{const e=q(l);return e?e.innerText.trim().split("\n")[0].trim():""};
const P=new URLSearchParams(location.search);
const B=[[/indeed\./,'[data-testid="jobsearch-JobInfoHeader-title"]|.jobsearch-JobInfoHeader-title','[data-testid="inlineHeader-companyName"]|[data-company-name]','[data-testid="inlineHeader-companyLocation"]|[data-testid="job-location"]|#jobLocationText','#jobDescriptionText',()=>{const k=P.get("vjk")||P.get("jk");return k?location.origin+"/viewjob?jk="+k:""}],
[/linkedin\./,'.job-details-jobs-unified-top-card__job-title|.jobs-unified-top-card__job-title|.top-card-layout__title','.job-details-jobs-unified-top-card__company-name|.jobs-unified-top-card__company-name|.topcard__org-name-link','.job-details-jobs-unified-top-card__primary-description-container .tvm__text|.job-details-jobs-unified-top-card__bullet|.topcard__flavor--bullet','#job-details|.jobs-description__content|.show-more-less-html__markup',()=>{const k=P.get("currentJobId");return k?"https://www.linkedin.com/jobs/view/"+k+"/":""}],
[/glassdoor\./,'[data-test="job-title"]|[id^="jd-job-title"]','[data-test="employer-name"]|[class*="EmployerProfile_employerName"]','[data-test="location"]','[class*="JobDetails_jobDescription"]|#JobDescriptionContainer',()=>""]];
for(const [re,t,c,l,ds,u] of B){if(!re.test(location.hostname))continue;const T=tx(t).replace(/\s+-\s+(job post|offre d.emploi|emploi)$/i,""),D=q(ds);if(!T)break;
J={"@type":"JobPosting",title:T,hiringOrganization:{name:tx(c)},jobLocation:{address:tx(l)},description:D?D.innerHTML:"",url:u()||location.href};break}
const m=n=>{const e=document.querySelector('meta[property="'+n+'"],meta[name="'+n+'"]');return e?e.content:""};
const d={url:location.href,host:location.hostname,title:document.title,site:m("og:site_name"),job:J,
selection:String(getSelection()||"").slice(0,40000)};
const w=window.open(O+"/clip","cvstudio_clip","width=500,height=760");
if(!w){alert("CV Studio: allow pop-ups for this site, then click again.");return}
const h=e=>{if(e.origin===O&&e.data==="cvstudio-clip-ready"){w.postMessage(d,O);removeEventListener("message",h)}};
addEventListener("message",h)})();"""


def bookmarklet() -> str:
    code = re.sub(r"\n", "", BOOKMARKLET.replace("__PORT__", str(CLIP_PORT)))
    # Some browsers end a bookmark's address at "#", and read "%" as the
    # start of an escape: both are written escaped, which the browser undoes
    # before running it.
    return "javascript:" + code.replace("%", "%25").replace("#", "%23")


def start_clip_listener() -> None:
    """Listen on CLIP_PORT as well, for the bookmark. Taken (another copy of
    the app, or something else): the bookmark will not work, and Settings
    says so, but nothing else changes."""
    from . import http_server as _http
    try:
        srv = _http.make_clip_server(CLIP_PORT)
    except OSError as exc:
        ctx.clip_state.update(ok=False, why=f"port {CLIP_PORT} is in use ({exc.strerror or exc})")
        return
    ctx.clip_state.update(ok=True, why="")
    threading.Thread(target=srv.serve_forever, daemon=True, name="clip").start()


def draft_context(job_id: str) -> dict:
    """What an email about an application can say without asking: whose name
    to sign, when you applied and met, and the posting's first duties."""
    job = next((j for j in jobstore.list_jobs(ctx.workspace) if j["id"] == job_id), None) \
        if jobstore is not None else None
    if job is None:
        raise ValueError("No such job.")
    you = ""
    for p in (job.get("cv_path"), (_prov.base_cv() or {}).get("path")):
        if not p:
            continue
        try:
            data = to_plain(yaml_rt.load(safe_path(p).read_text(encoding="utf-8"))) or {}
        except Exception:
            continue
        you = str(((data.get("cv") or {}).get("name")) or "")
        if you:
            break
    when = {}
    for h in job.get("status_history") or []:
        when.setdefault(h.get("status"), str(h.get("at") or "")[:10])
    duties = [re.sub(r"[*_`]", "", m).strip().rstrip(".")
              for m in re.findall(r"(?m)^\s*[-*•]\s+(.+)$", job.get("description") or "")][:2]
    return {"you": you, "applied": when.get("applied"), "interviewed": when.get("interviewing"),
            "duties": duties}


def next_round(job: dict) -> dict | None:
    """The round being prepared for: the first one without an outcome."""
    return next((r for r in job.get("rounds") or [] if not r.get("outcome")), None)


def interview_prep(job_id: str) -> dict:
    """The prep for an application's next round: what was saved (by you or an
    AI client), else made here from the posting, the CV sent and the round."""
    import prep as prepmod
    job = next((j for j in jobstore.list_jobs(ctx.workspace) if j["id"] == job_id), None) \
        if jobstore is not None else None
    if job is None:
        raise ValueError("No such job.")
    rnd = next_round(job) or {}
    stored = job.get("prep")
    if stored and stored.get("questions"):
        return {**stored, "local": False, "round": rnd}
    cv = None
    for p in (job.get("cv_path"), (_prov.base_cv() or {}).get("path")):
        if not p:
            continue
        try:
            cv = (to_plain(yaml_rt.load(safe_path(p).read_text(encoding="utf-8"))) or {}).get("cv")
            break
        except Exception:
            continue
    made = prepmod.build(job, cv, rnd.get("kind") or "")
    # What you already did with it (notes, the CV re-read) survives a rebuild.
    if stored:
        made = prepmod.merge(stored, {k: made[k] for k in ("questions", "stories", "asks")})
    return {**made, "local": True, "round": rnd}


def save_interview_prep(job_id: str, prep_data: dict, by: str = "") -> dict:
    """Store the prep whole: the app saving your notes, or an AI client its
    questions (merged so your notes stay)."""
    import prep as prepmod
    if by:
        cur = interview_prep(job_id)
        prep_data = prepmod.merge({k: v for k, v in cur.items() if k not in ("local", "round")},
                                  {**prep_data, "by": by, "at": time.strftime("%Y-%m-%d")})
    jobstore.update_job(ctx.workspace, job_id, {"prep": prep_data})
    return interview_prep(job_id)


def _match_sig(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _base_sig() -> tuple[str | None, str | None]:
    """The base CV and a fingerprint of its text, or (None, None)."""
    base = _prov.base_cv()
    if not base or base.get("missing"):
        return None, None
    try:
        return base["path"], _match_sig(safe_path(base["path"]).read_text(encoding="utf-8"))
    except OSError:
        return base["path"], None


def match_stale(match: dict | None, job: dict, base: tuple[str | None, str | None]) -> list[str]:
    """Why a match no longer describes the application: the base CV is
    another one or was edited ("cv"), or the posting changed ("posting")."""
    if not match:
        return []
    why = []
    path, sig = base
    if path and (match.get("cv") != path or (sig and match.get("cv_sig") and match["cv_sig"] != sig)):
        why.append("cv")
    if match.get("posting_sig") and match["posting_sig"] != _match_sig(job.get("description") or ""):
        why.append("posting")
    return why


def save_match(job_id: str, data: dict, by: str = "") -> dict:
    """An AI client's reading of how the base CV matches one posting, stored
    with fingerprints of both so the app can say when either has moved on."""
    job = next((j for j in jobstore.list_jobs(ctx.workspace) if j["id"] == job_id), None) \
        if jobstore is not None else None
    if job is None:
        raise ValueError("No such job.")
    if not (job.get("description") or "").strip():
        raise ValueError("This application has no posting saved to score against. Save it first "
                         "(update_job_tracking with description, or read_posting on its link).")
    path, sig = _base_sig()
    if not path:
        raise ValueError("There is no base CV to score against. Ask the user which CV it is, "
                         "then set_base_cv.")
    match = {**data, "by": by, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "cv": path,
             "cv_sig": sig or "", "posting_sig": _match_sig(job["description"])}
    saved = jobstore.update_job(ctx.workspace, job_id, {"match": match})["match"]
    return {**saved, "stale": []}


def application_pack(job_id: str, fmt: str = "pdf", name: str | None = None,
                     posting: bool = False) -> tuple[bytes, str, str]:
    """The CV and the letter for one application as one PDF (CV first) or a
    zip of separate files, each rendered from what is saved now."""
    info = pack_info(job_id)
    if not (info["cv"] or info["letter"]):
        raise ValueError("This application has no CV or letter yet.")
    parts = []
    if info["cv"]:
        pdf, failed = _docs.current_pdf(info["files"]["cv_path"])
        if pdf is None:
            raise ValueError((failed or {}).get("hint") or "The CV did not render.")
        parts.append(("CV", pdf.read_bytes()))
    if info["letter"]:
        data, _, _ = letter_export(info["files"]["letter_path"], "pdf")
        parts.append(("Cover-letter", data))
    stem = _slug_name(re.sub(r"\.(pdf|zip)\s*$", "", name.strip(), flags=re.I)) if name and name.strip() \
        else info["name"]
    if fmt == "zip":
        buf = io.BytesIO()
        who = info["person"] or "Application"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for label, data in parts:
                z.writestr(f"{who}-{label}.pdf", data)
            if posting and info["posting"]:
                z.writestr("posting.md", info["job"]["description"])
        return buf.getvalue(), "application/zip", stem + ".zip"
    if len(parts) == 1:
        return parts[0][1], "application/pdf", stem + ".pdf"
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for _, data in parts:
        w.append(PdfReader(io.BytesIO(data)))
    out = io.BytesIO()
    w.write(out)
    return out.getvalue(), "application/pdf", stem + ".pdf"


def letter_export(path: Path, fmt: str) -> tuple[bytes, str, str]:
    """(bytes, content type, file name) for a letter in another form."""
    meta, body = letters.parse(path.read_text(encoding="utf-8"))
    head = letter_head(meta)
    base = letters.file_stem(head["name"])
    if fmt == "docx":
        return (letters.docx(meta, body, head),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                base + ".docx")
    if fmt == "txt":
        return letters.plain_text(meta, body, head).encode("utf-8"), \
            "text/plain; charset=utf-8", base + ".txt"
    pdf = _docs.last_pdf(path)
    if not pdf or pdf.stat().st_mtime < path.stat().st_mtime:
        r = render_letter(path)
        if not r.get("ok"):
            raise ValueError(r.get("error") or "The letter did not lay out.")
        pdf = ctx.workspace / r["pdf"]
    return pdf.read_bytes(), "application/pdf", base + ".pdf"


def new_letter(job_id: str | None = None, name: str | None = None) -> dict:
    """A letter with everything filled in but the words: from the application,
    its company, role and language; from its CV (else the base CV), the look
    and the place it is written from. Linked to the application."""
    job = None
    if job_id and jobstore is not None:
        job = next((j for j in jobstore.list_jobs(ctx.workspace) if j["id"] == job_id), None)
    company = (job or {}).get("company") or ""
    role = (job or {}).get("title") or ""
    lang = (job or {}).get("language") or languages.detect(
        f"{role} {(job or {}).get('description') or ''}") or "en"
    looks = (job or {}).get("cv_path") or (_prov.base_cv() or {}).get("path")
    place = ""
    try:
        data = to_plain(yaml_rt.load(safe_path(looks).read_text(encoding="utf-8"))) if looks else {}
        place = str(((data or {}).get("cv") or {}).get("location") or "").split(",")[0].strip()
    except Exception:
        pass
    stem = re.sub(r"[^a-z0-9]+", "-", (name or f"cover-{company or 'letter'}").lower()).strip("-") or "cover-letter"
    folder = ctx.workspace / "letters"
    folder.mkdir(parents=True, exist_ok=True)
    dest, n = folder / f"{stem}{letters.EXT}", 2
    while dest.exists():
        dest, n = folder / f"{stem}-{n}{letters.EXT}", n + 1
    dest.write_text(letters.scaffold(company=company, role=role, lang=lang, place=place,
                                     looks_like=looks, application=job_id if job else None),
                    encoding="utf-8")
    if job and jobstore is not None:
        jobstore.update_job(ctx.workspace, job["id"], {"letter_path": rel(dest)})
    return {"ok": True, "path": rel(dest)}


def convert_legacy_letters() -> list[str]:
    """Every letter still written as a RenderCV document, as a Markdown letter.
    The old file is renamed to .yaml.bak beside it, and an application that
    pointed at it points at the new one."""
    done = []
    folder = ctx.workspace / "letters"
    if not folder.is_dir():
        return done
    jobs = jobstore.list_jobs(ctx.workspace) if jobstore is not None else []
    for f in sorted(folder.glob("*.y*ml")):
        if f.name.startswith("."):
            continue
        try:
            data = to_plain(yaml_rt.load(f.read_text(encoding="utf-8"))) or {}
            if not isinstance(data.get("cv"), dict):
                continue
            meta, body = letters.from_legacy(data)
            dest = f.with_suffix(letters.EXT)
            if dest.exists():
                continue
            job = next((j for j in jobs if j.get("letter_path") == rel(f)), None)
            if job:
                meta.update({"application": job["id"], "company": job.get("company"),
                             "looks_like": job.get("cv_path") or (_prov.base_cv() or {}).get("path")})
            else:
                meta["looks_like"] = (_prov.base_cv() or {}).get("path")
            dest.write_text(letters.dump(meta, body), encoding="utf-8")
            f.rename(f.with_name(f.name + ".bak"))
            if job:
                jobstore.update_job(ctx.workspace, job["id"], {"letter_path": rel(dest)})
            done.append(rel(dest))
        except Exception:
            continue
    return done


