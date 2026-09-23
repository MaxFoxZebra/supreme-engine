"""A workspace full of made-up data, to see what the app looks like in use.

Settings -> Workspace -> Sample data switches the app to a separate folder in
the app data directory, rebuilt from scratch each time: a base CV, its French,
Spanish and Brazilian Portuguese translations (a little behind the English
one, so the drift shows), CVs
tailored for five applications, four cover letters, and about sixty
applications spread over five months in every status, with postings, notes,
follow-ups and interviews coming up. Dates are relative to today, so the
funnel's weeks and the alerts always have something current to show.

Your own workspace is never written to, and the MCP server AI clients talk to
keeps using it: it is a separate process with its own workspace.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import shutil
import sqlite3
from pathlib import Path

import cjkfonts


def folder() -> Path:
    return cjkfonts.cache_dir().parent / "sample"


BASE_CV = """cv:
  name: Alex Moreau
  headline: Senior Platform Engineer
  location: Lyon, France
  email: alex.moreau@example.com
  phone: +33 6 12 34 56 78
  website: https://alexmoreau.example.com
  social_networks:
    - network: LinkedIn
      username: alex-moreau-example
    - network: GitHub
      username: alexmoreau-example
  sections:
    summary:
      - Platform engineer with nine years of building the infrastructure product
        teams ship on. I like boring deploys, fast feedback loops and on-call
        rotations nobody dreads.
    experience:
      - company: Northwind Logistics
        position: Senior Platform Engineer
        location: Lyon, France
        start_date: 2022-03
        end_date: present
        highlights:
          - Moved 140 services from hand-run VMs to Kubernetes with no customer-facing
            downtime, and cut the monthly cloud bill by 31%.
          - Built the internal deploy pipeline used by 60 engineers; median time from
            merge to production went from 3 hours to 11 minutes.
          - Led the incident review practice; pages per on-call week fell from 14 to 3.
      - company: Fabrikam Payments
        position: Backend Engineer
        location: Paris, France
        start_date: 2019-01
        end_date: 2022-02
        highlights:
          - Owned the ledger service processing 2 million transactions a day, in Go
            and PostgreSQL.
          - Designed the idempotency layer that ended double charges after retries.
      - company: Contoso Games
        position: Software Engineer
        location: Montpellier, France
        start_date: 2016-09
        end_date: 2018-12
        highlights:
          - Wrote the matchmaking service for a live game with 400,000 daily players.
    education:
      - institution: INSA Lyon
        area: Computer Science
        degree: MSc
        start_date: 2011-09
        end_date: 2016-06
    skills:
      - label: Infrastructure
        details: Kubernetes, Terraform, AWS, GCP, Prometheus, Grafana
      - label: Languages
        details: Go, Python, TypeScript, SQL
      - label: Practices
        details: Incident response, SLOs, CI/CD, capacity planning
    languages:
      - label: French
        details: native
      - label: English
        details: fluent (C1)
design:
  theme: classic
  page:
    size: a4
"""

POSTINGS = {
    "platform": """## About the role

We are looking for a **Platform Engineer** to join the infrastructure team that
keeps our product running for 2 million patients a month.

## What you will do

- Run and improve our Kubernetes clusters across two regions
- Build the self-service tooling product teams deploy with
- Take part in a calm, well-staffed on-call rotation

## What we are looking for

- 5+ years building and running production infrastructure
- Strong Terraform and Kubernetes
- Go or Python for tooling
- You write things down

## Nice to have

- Experience with PostgreSQL at scale
- Healthcare or other regulated environments

**Location:** Paris or remote in France · **Salary:** €70–85k · Visa sponsorship: no
""",
    "sre": """## The team

Site Reliability at a company whose customers notice every minute of downtime.

## Responsibilities

- Own SLOs for the core API and the paths that lead to it
- Lead incident response and the reviews that follow
- Make capacity planning a routine, not a fire drill

## Requirements

- Production experience with Prometheus and Grafana
- Comfortable in Go
- Clear written communication in English

**Location:** London, hybrid (2 days) · **Salary:** £85–100k · Visa sponsorship available
""",
    "fr": """## Le poste

Nous recrutons un·e **Ingénieur·e Plateforme** pour rejoindre l'équipe
Infrastructure à Lyon.

## Vos missions

- Faire évoluer notre plateforme Kubernetes
- Industrialiser les déploiements des équipes produit
- Participer aux astreintes

## Profil recherché

- 5 ans d'expérience minimum en infrastructure
- Maîtrise de Terraform et d'AWS
- Anglais professionnel

**Lieu :** Lyon, 2 jours de télétravail · **Salaire :** 60–72 k€
""",
    "es": """## Sobre el puesto

Buscamos un/a **Ingeniero/a de Plataforma** para el equipo de infraestructura.

## Qué harás

- Operar y mejorar nuestros clústeres de Kubernetes
- Construir herramientas de despliegue para los equipos de producto
- Participar en las guardias

## Qué buscamos

- 5+ años en infraestructura en producción
- Terraform, AWS y Go
- Inglés profesional

**Ubicación:** Madrid, híbrido · **Salario:** 55–70 k€
""",
    "pt": """## A vaga

Procuramos uma pessoa **Engenheira de Plataforma** para o time de infraestrutura.

## O que você vai fazer

- Operar e evoluir nossos clusters Kubernetes
- Criar ferramentas de deploy para os times de produto
- Participar do plantão

## O que buscamos

- 5+ anos com infraestrutura em produção
- Terraform, AWS e Go
- Inglês fluente

**Local:** São Paulo, híbrido · **Salário:** R$ 25–32 mil
""",
}

# Every one of these has a logo in static/sample-logos: its mark from Simple
# Icons (CC0, simpleicons.org) on a tile in its brand colour.
COMPANIES = [
    ("Dailymotion", "Paris", "fr"), ("Brevo", "Paris", "fr"), ("Qwant", "Paris", "fr"),
    ("Datadog", "Paris", "en"), ("Deliveroo", "London", "en"), ("Mistral AI", "Paris", "en"),
    ("Revolut", "London", "en"), ("SNCF", "Paris", "fr"), ("Airbus", "Toulouse", "fr"),
    ("Figma", "London", "en"), ("Algolia", "Paris", "en"), ("Adyen", "Amsterdam", "en"),
    ("Wise", "London", "en"), ("Deezer", "Paris", "fr"), ("Ubisoft", "Lyon", "fr"),
    ("N26", "Berlin", "en"), ("Malt", "Lyon", "fr"), ("Personio", "Munich", "en"),
    ("Aircall", "Paris", "en"), ("Lydia", "Paris", "fr"), ("Klarna", "Stockholm", "en"),
    ("Dataiku", "Paris", "en"), ("Renault", "Paris", "fr"), ("Vinted", "Remote", "en"),
    ("Booking.com", "Amsterdam", "en"), ("Zalando", "Berlin", "en"), ("Stripe", "London", "en"),
    ("GitLab", "Remote", "en"), ("Scaleway", "Paris", "fr"), ("OVHcloud", "Lyon", "fr"),
    ("Hugging Face", "Remote", "en"), ("Shopify", "Remote", "en"), ("Monzo", "London", "en"),
    ("Spotify", "Stockholm", "en"), ("Cloudflare", "Lisbon", "en"),
    ("Typeform", "Barcelona", "es"), ("Glovo", "Barcelona", "es"), ("Movistar", "Madrid", "es"),
    ("Nubank", "São Paulo", "pt"), ("iFood", "São Paulo", "pt"), ("VTEX", "Rio de Janeiro", "pt"),
]
ZONES = {"London": "Europe/London", "Amsterdam": "Europe/Amsterdam", "Berlin": "Europe/Berlin",
         "Munich": "Europe/Berlin", "Madrid": "Europe/Madrid", "Barcelona": "Europe/Madrid",
         "São Paulo": "America/Sao_Paulo", "Rio de Janeiro": "America/Sao_Paulo",
         "Stockholm": "Europe/Stockholm", "Lisbon": "Europe/Lisbon"}
TITLES = ["Platform Engineer", "Senior Backend Engineer", "Staff Engineer", "Site Reliability Engineer",
          "Infrastructure Engineer", "Backend Engineer (Go)", "DevOps Lead", "Senior Platform Engineer"]
SOURCES = (["LinkedIn"] * 9 + ["Welcome to the Jungle"] * 6 + ["Indeed"] * 4 + ["Glassdoor"] * 2 +
           ["Company’s careers page"] * 4 + ["Referral"] * 3 + ["Recruiter"] * 3)
INTERVIEW_ODDS = {"Referral": .6, "Recruiter": .5, "Company’s careers page": .3,
                  "Welcome to the Jungle": .28, "Glassdoor": .15, "LinkedIn": .16, "Indeed": .07}
NOTES = [
    "Spoke to the hiring manager, Claire. Team of six, mostly Go.",
    "Recruiter said the process is three rounds: screen, system design, team fit.",
    "They asked about on-call load twice. Prepare numbers from Northwind.",
    "Salary band confirmed on the first call.",
    "Referred by Julien from INSA.",
]


def _iso(d: dt.datetime) -> str:
    return d.isoformat(timespec="seconds")


def build(studio, applications: int = 64) -> dict:
    """Rebuild the sample folder and fill it. `studio` is the studio module,
    whose WORKSPACE must already point at folder(). `applications` is how
    many to make at random, beside the two interviews abroad: more shows how
    the app holds up with a long search behind it."""
    ws = folder()
    assert studio.WORKSPACE.resolve() == ws.resolve()
    if ws.exists():
        shutil.rmtree(ws)
    for sub in ("profile", "applications", "letters", "assets"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    R = random.Random(20260923)
    now = dt.datetime.now().replace(microsecond=0)

    base = ws / "profile" / "my-cv.yaml"
    base.write_text(BASE_CV, encoding="utf-8")
    studio.set_base_cv("profile/my-cv.yaml")

    # Translated bases in French, Spanish and Brazilian Portuguese. Then the
    # English one moves on, so each has something to catch up on.
    SUMMARY = ("Platform engineer with nine years of building the infrastructure product\n"
               "        teams ship on. I like boring deploys, fast feedback loops and on-call\n"
               "        rotations nobody dreads.")
    TR = {
        "fr": ("Ingénieur plateforme senior",
               "Ingénieur plateforme depuis neuf ans, je construis l'infrastructure sur\n"
               "        laquelle les équipes produit livrent. J'aime les déploiements sans\n"
               "        surprise et les astreintes que personne ne redoute."),
        "es": ("Ingeniero de plataforma sénior",
               "Ingeniero de plataforma con nueve años construyendo la infraestructura\n"
               "        sobre la que despliegan los equipos de producto. Me gustan los\n"
               "        despliegues aburridos y las guardias que nadie teme."),
        "pt": ("Engenheiro de plataforma sênior",
               "Engenheiro de plataforma há nove anos, construindo a infraestrutura em\n"
               "        que os times de produto fazem deploy. Gosto de deploys sem surpresa\n"
               "        e de plantões que ninguém teme."),
    }
    bases = {"en": base}
    for code, (headline, summary) in TR.items():
        made_tr = studio.add_language(base, code)
        tp = ws / made_tr["path"]
        text = tp.read_text(encoding="utf-8")
        text = text.replace("Senior Platform Engineer\n  location", headline + "\n  location")
        text = text.replace(SUMMARY, summary)
        tp.write_text(text, encoding="utf-8")
        studio.mark_translation_current(tp)
        bases[code] = tp
    frp = bases["fr"]
    studio.apply_patches(base, [{"path": ["cv", "headline"],
                                 "value": "Senior Platform Engineer · Kubernetes, Go"}])

    # The logos, copied in the way save_logo would store them.
    logos = {}
    ldir = studio.logo_dir()
    ldir.mkdir(parents=True, exist_ok=True)
    for company, _, _ in COMPANIES:
        src = studio.STATIC_DIR / "sample-logos" / f"{studio._logo_stem(company)}.svg"
        if src.is_file():
            shutil.copyfile(src, ldir / src.name)
            logos[company] = src.name

    jobs = studio.jobstore
    made = []
    for i in range(max(1, min(int(applications), 2000))):
        company, city, lang = R.choice(COMPANIES)
        title = R.choice(TITLES)
        source = R.choice(SOURCES)
        start = now - dt.timedelta(days=R.randint(1, 150), hours=R.randint(0, 10))
        hist = [{"status": "pending", "at": start}]
        if R.random() < .9:
            sent = start + dt.timedelta(days=R.randint(0, 3), hours=R.randint(1, 8))
            if sent < now:
                hist.append({"status": "applied", "at": sent})
                age = (now - sent).days
                if R.random() < INTERVIEW_ODDS[source] and age > 5:
                    iv = sent + dt.timedelta(days=R.randint(3, 16))
                    if iv < now:
                        hist.append({"status": "interviewing", "at": iv})
                        r = R.random()
                        if (now - iv).days > 10:
                            if r < .3:
                                hist.append({"status": "offer", "at": iv + dt.timedelta(days=R.randint(7, 20))})
                            elif r < .7:
                                hist.append({"status": "rejected_interviewing",
                                             "at": iv + dt.timedelta(days=R.randint(5, 15))})
                            elif r < .8:
                                hist.append({"status": "ghosted_interviewing", "at": iv + dt.timedelta(days=21)})
                        if hist[-1]["status"] == "offer" and R.random() < .6:
                            hist.append({"status": R.choice(["accepted", "refused", "refused"]),
                                         "at": hist[-1]["at"] + dt.timedelta(days=4)})
                elif age > 6:
                    r = R.random()
                    if r < .45:
                        hist.append({"status": "rejected", "at": sent + dt.timedelta(days=R.randint(2, 20))})
                    elif r < .8 and age > 21:
                        hist.append({"status": "ghosted", "at": sent + dt.timedelta(days=21)})
        hist = [h for h in hist if h["at"] <= now]
        status = hist[-1]["status"]
        # Most French companies post in French; the Spanish and Brazilian
        # ones here always post in their language.
        plang = lang if lang in ("es", "pt") or (lang == "fr" and R.random() < .6) else "en"
        kind = plang if plang in POSTINGS else ("sre" if "Reliability" in title else "platform")
        data = {
            "company": company, "title": title, "source": source, "status": "pending",
            "location": city, "score": R.randint(2, 5), "language": plang,
            "salary_currency": "GBP" if city == "London" else "EUR",
            "url": "https://example.com/jobs/" + company.lower().replace(" ", "-"),
            "logo": logos.get(company),
        }
        if R.random() < .5:
            data["description"] = POSTINGS[kind]
        if R.random() < .3:
            data["notes"] = R.choice(NOTES)
        if status == "applied" and R.random() < .6:
            # Some follow-ups already due, some coming.
            data["followup_date"] = (now + dt.timedelta(days=R.randint(-6, 8))).date().isoformat()
        if status == "interviewing" and R.random() < .7:
            data["interview_at"] = _iso((now + dt.timedelta(days=R.randint(1, 9))).replace(
                hour=R.choice([10, 11, 14, 16]), minute=0, second=0))
            # The time as the invitation gave it, in the employer's zone.
            data["interview_tz"] = ZONES.get(city)
        if status in ("offer", "accepted", "refused"):
            data["salary_offered"] = R.choice([68000, 72000, 78000, 85000])
        j = jobs.add_job(ws, data)
        con = sqlite3.connect(jobs.db_path(ws))
        con.execute("UPDATE jobs SET status=?, status_history=?, created_at=?, updated_at=? WHERE id=?",
                    (status, json.dumps([{"status": h["status"], "at": _iso(h["at"])} for h in hist]),
                     _iso(start), _iso(hist[-1]["at"]), j["id"]))
        con.commit()
        con.close()
        made.append({**j, "status": status, "lang": plang})

    # Two interviews abroad, so the time zones show: London is an hour behind
    # Paris, São Paulo four or five.
    for company, title, city, days, hour, source in (
            ("Monzo", "Site Reliability Engineer", "London", 2, 10, "Recruiter"),
            ("Nubank", "Senior Platform Engineer", "São Paulo", 5, 14, "LinkedIn")):
        sent = now - dt.timedelta(days=18)
        iv = now - dt.timedelta(days=6)
        j = jobs.add_job(ws, {
            "company": company, "title": title, "location": city, "source": source,
            "status": "pending", "score": 4, "language": "pt" if city == "São Paulo" else "en",
            "description": POSTINGS["pt" if city == "São Paulo" else "sre"],
            "interview_at": _iso((now + dt.timedelta(days=days)).replace(hour=hour, minute=0, second=0)),
            "interview_tz": ZONES[city], "logo": logos.get(company),
            "notes": "Second round: system design, 60 minutes, video call."})
        hist = [{"status": "pending", "at": _iso(sent - dt.timedelta(days=1))},
                {"status": "applied", "at": _iso(sent)}, {"status": "interviewing", "at": _iso(iv)}]
        con = sqlite3.connect(jobs.db_path(ws))
        con.execute("UPDATE jobs SET status='interviewing', status_history=?, created_at=?, updated_at=? "
                    "WHERE id=?", (json.dumps(hist), hist[0]["at"], hist[-1]["at"], j["id"]))
        con.commit()
        con.close()
        made.append({**j, "status": "interviewing", "lang": j["language"]})

    # Tailored CVs for the applications furthest along, and letters for some.
    order = {"offer": 0, "accepted": 1, "interviewing": 2, "refused": 3, "applied": 4}
    live = sorted((m for m in made if m["status"] in order), key=lambda m: order[m["status"]])
    seen, tailored = set(), []
    # One per language first, so every flag has a CV behind it.
    firsts = [next((m for m in live if m["lang"] == c), None) for c in ("en", "fr", "es", "pt")]
    live = [m for m in firsts if m] + [m for m in live if m not in firsts]
    for m in live:
        if m["company"] in seen or len(tailored) >= 5:
            continue
        seen.add(m["company"])
        slug = m["company"].lower().replace(" ", "-")
        src = bases.get(m["lang"], base)
        suffix = "" if m["lang"] == "en" or src is base else "." + m["lang"]
        dest = ws / "profile" / f"cv-{slug}{suffix}.yaml"
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        studio.note_lineage(dest, studio.rel(src))
        if src is base:
            studio.apply_patches(dest, [{"path": ["cv", "headline"],
                                         "value": f"Platform Engineer for {m['company']}"}])
        jobs.update_job(ws, m["id"], {"cv_path": studio.rel(dest)})
        tailored.append(m)

    BODIES = {
        "fr": ("Madame, Monsieur,\n\nVotre annonce pour le poste de {title} m'a tout de suite parlé : "
               "depuis trois ans, je fais exactement ce travail chez Northwind Logistics, où j'ai migré "
               "140 services vers Kubernetes sans interruption de service.\n\nCe qui m'attire chez vous, "
               "c'est **l'ampleur du chantier** et l'attention portée à la qualité des astreintes.\n\n"
               "Je serais ravi d'en parler lors d'un entretien.\n\nJe vous prie d'agréer, Madame, "
               "Monsieur, mes salutations distinguées."),
        "es": ("Estimado equipo:\n\nMe dirijo a ustedes para optar al puesto de {title} en {company}. "
               "Durante los últimos tres años he hecho este trabajo en Northwind Logistics, donde migré "
               "140 servicios a Kubernetes sin interrupciones.\n\nMe atrae **la escala de su plataforma** "
               "y cómo cuidan las guardias.\n\nQuedo a su disposición para una entrevista.\n\n"
               "Atentamente,"),
        "pt": ("Prezada equipe,\n\nGostaria de me candidatar à vaga de {title} na {company}. Nos últimos "
               "três anos fiz exatamente esse trabalho na Northwind Logistics, onde migrei 140 serviços "
               "para Kubernetes sem indisponibilidade.\n\nO que me atrai é **a escala da plataforma** e "
               "o cuidado com os plantões.\n\nFico à disposição para uma conversa.\n\nAtenciosamente,"),
        "en": ("Dear Hiring Team,\n\nI am applying for the {title} role at {company}. For the last three "
               "years I have done this job at Northwind Logistics, where I moved 140 services to "
               "Kubernetes with no downtime and cut deploy time from three hours to eleven minutes.\n\n"
               "What draws me to your team is **the scale of the platform** and the way you write about "
               "on-call: as something to design, not endure.\n\n"
               "- I have run the kind of migration your posting describes\n"
               "- I have built the self-service tooling around it\n\n"
               "I would be glad to talk about it.\n\nKind regards,"),
    }
    letters_made = 0
    for m in tailored[:4]:
        r = studio.new_letter(m["id"])
        p = ws / r["path"]
        meta, _ = studio.letters.parse(p.read_text(encoding="utf-8"))
        body = BODIES.get(meta.get("language") or "en", BODIES["en"])
        p.write_text(studio.letters.dump(meta, body.format(title=m["title"], company=m["company"])),
                     encoding="utf-8")
        letters_made += 1

    return {"applications": len(made), "tailored": len(tailored), "letters": letters_made,
            "translations": len(TR)}
