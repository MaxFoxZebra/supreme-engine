"""What CV Studio knows about languages, without a model.

A CV's language is RenderCV's `locale.language`, which already puts dates,
month names and "present" into 22 languages. Everything here is the part of
translating a CV that has one right answer and so should never wait on an AI
client: which languages exist, what the common section titles are called in
the languages people most often apply in, and what language a job posting is
written in. The prose itself is the user's, or their AI client's, to translate.
"""

from __future__ import annotations

import re
import unicodedata

# code -> (RenderCV's name, the language in itself, in English). The codes are
# what files and applications store; RenderCV's names are what the YAML needs.
LANGS: dict[str, tuple[str, str, str]] = {
    "en": ("english", "English", "English"),
    "fr": ("french", "Français", "French"),
    "de": ("german", "Deutsch", "German"),
    "es": ("spanish", "Español", "Spanish"),
    "it": ("italian", "Italiano", "Italian"),
    "pt": ("portuguese", "Português", "Portuguese"),
    "nl": ("dutch", "Nederlands", "Dutch"),
    "da": ("danish", "Dansk", "Danish"),
    "nb": ("norwegian_bokmål", "Norsk bokmål", "Norwegian (Bokmål)"),
    "nn": ("norwegian_nynorsk", "Norsk nynorsk", "Norwegian (Nynorsk)"),
    "hu": ("hungarian", "Magyar", "Hungarian"),
    "tr": ("turkish", "Türkçe", "Turkish"),
    "ru": ("russian", "Русский", "Russian"),
    "id": ("indonesian", "Bahasa Indonesia", "Indonesian"),
    "vi": ("vietnamese", "Tiếng Việt", "Vietnamese"),
    "hi": ("hindi", "हिन्दी", "Hindi"),
    "ar": ("arabic", "العربية", "Arabic"),
    "he": ("hebrew", "עברית", "Hebrew"),
    "fa": ("persian", "فارسی", "Persian"),
    "ja": ("japanese", "日本語", "Japanese"),
    "ko": ("korean", "한국어", "Korean"),
    "zh": ("mandarin_chinese", "中文", "Chinese"),
}
BY_NAME = {v[0]: k for k, v in LANGS.items()}

# Where RenderCV's word for an ongoing date is not the one CVs in that language
# use. French CVs say "aujourd'hui" or "à ce jour"; "présent" reads as a
# translation. Written into the locale of a new copy, where it stays editable.
PRESENT = {"fr": "aujourd'hui"}


def code_of(language: str | None) -> str:
    """A RenderCV language name, or a code, as a code. Unknown means English,
    which is what RenderCV itself does with a file that says nothing."""
    if not language:
        return "en"
    s = str(language).strip().lower()
    return s if s in LANGS else BY_NAME.get(s, "en")


def catalogue() -> list[dict]:
    return [{"code": k, "name": v[0], "native": v[1], "english": v[2], "present": PRESENT.get(k)}
            for k, v in LANGS.items()]


# The sections people actually have, by what they are rather than by what a
# given file calls them, in the languages CV Studio can title them in. RenderCV
# prints a section's YAML key as its title, so a translated CV needs the key
# itself translated, written exactly as it should print.
TITLES: dict[str, dict[str, str]] = {
    "summary": {"en": "Summary", "fr": "Résumé", "de": "Profil",
                "es": "Resumen", "it": "Profilo", "pt": "Resumo", "nl": "Profiel"},
    "experience": {"en": "Experience", "fr": "Expérience professionnelle",
                   "de": "Berufserfahrung", "es": "Experiencia profesional",
                   "it": "Esperienza professionale", "pt": "Experiência profissional",
                   "nl": "Werkervaring"},
    "education": {"en": "Education", "fr": "Formation", "de": "Ausbildung",
                  "es": "Formación", "it": "Istruzione", "pt": "Formação",
                  "nl": "Opleiding"},
    "skills": {"en": "Skills", "fr": "Compétences", "de": "Kenntnisse",
               "es": "Habilidades", "it": "Competenze", "pt": "Competências",
               "nl": "Vaardigheden"},
    "projects": {"en": "Projects", "fr": "Projets", "de": "Projekte",
                 "es": "Proyectos", "it": "Progetti", "pt": "Projetos",
                 "nl": "Projecten"},
    "languages": {"en": "Languages", "fr": "Langues", "de": "Sprachen",
                  "es": "Idiomas", "it": "Lingue", "pt": "Idiomas", "nl": "Talen"},
    "certifications": {"en": "Certifications", "fr": "Certifications",
                       "de": "Zertifikate", "es": "Certificaciones",
                       "it": "Certificazioni", "pt": "Certificações",
                       "nl": "Certificeringen"},
    "publications": {"en": "Publications", "fr": "Publications",
                     "de": "Publikationen", "es": "Publicaciones",
                     "it": "Pubblicazioni", "pt": "Publicações", "nl": "Publicaties"},
    "awards": {"en": "Awards", "fr": "Distinctions", "de": "Auszeichnungen",
               "es": "Premios", "it": "Riconoscimenti", "pt": "Prémios",
               "nl": "Onderscheidingen"},
    "volunteering": {"en": "Volunteering", "fr": "Bénévolat",
                     "de": "Ehrenamt", "es": "Voluntariado", "it": "Volontariato",
                     "pt": "Voluntariado", "nl": "Vrijwilligerswerk"},
    "interests": {"en": "Interests", "fr": "Centres d'intérêt", "de": "Interessen",
                  "es": "Intereses", "it": "Interessi", "pt": "Interesses",
                  "nl": "Interesses"},
}
# Other names the same sections go by, so a file that says "Work experience"
# or "about" is still recognised.
ALIASES = {
    "summary": ["about", "about me", "profile", "professional summary", "objective",
                "profil professionnel", "à propos"],
    "experience": ["work experience", "professional experience", "employment",
                   "work history", "expérience", "expériences", "expériences professionnelles",
                   "experiencia", "esperienza", "erfahrung"],
    "skills": ["technical skills", "core skills", "compétences techniques",
               "fähigkeiten"],
    "certifications": ["certificates", "licenses & certifications"],
    "awards": ["honors", "honours", "honors & awards", "prix"],
    "volunteering": ["volunteer experience", "volunteer work"],
    "interests": ["hobbies", "loisirs"],
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", str(s)).replace("_", " ").strip().lower()
    return re.sub(r"\s+", " ", s)


_CONCEPT = {}
for concept, names in TITLES.items():
    _CONCEPT[_norm(concept)] = concept
    for title in names.values():
        _CONCEPT.setdefault(_norm(title), concept)
for concept, names in ALIASES.items():
    for n in names:
        _CONCEPT.setdefault(_norm(n), concept)


def section_title(key: str, target: str) -> str | None:
    """The title for this section in `target`, or None when it is not one CV
    Studio can title there: then the section keeps its name for the
    translator."""
    concept = _CONCEPT.get(_norm(key))
    return TITLES.get(concept, {}).get(target) if concept else None


# A posting's language, from the words every sentence in it is made of. Only
# the languages people commonly apply in: a posting is long enough that a few
# dozen function words each are decisive, and a wrong guess is one click to
# correct on the application.
STOPWORDS = {
    "en": "the and of to in for with you we our are is will on as be your this that an at or have",
    "fr": "le la les des et de du en pour un une vous nous notre est sont dans avec sur au aux votre ce qui",
    "de": "der die das und zu den mit für ist sie wir ein eine von auf im dem des nicht unser ihre als",
    "es": "el la los las de y en para con un una que por del es su nuestro somos se al como tu",
    "it": "il la le di e che per con un una del della sono nel si è al dei gli nostro come tua",
    "pt": "o a os as de e do da em para com um uma que por no na é seu nossa você como ao",
    "nl": "de het een en van in op voor met is zijn wij je jouw ons naar bij als dat die",
}
_SW = {k: set(v.split()) for k, v in STOPWORDS.items()}


def detect(text: str | None) -> str | None:
    """The language a posting is written in, or None when there is too little
    of it to say."""
    words = re.findall(r"[^\W\d_]+", (text or "").lower())
    if len(words) < 12:
        return None
    scores = {k: sum(w in sw for w in words) for k, sw in _SW.items()}
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    # A clear winner only: a bilingual posting or a list of technologies is
    # better left for the user to say.
    if ranked[0] < 4 or ranked[0] < ranked[1] * 1.4:
        return None
    return best


def file_language(text: str) -> str:
    """A document's language, read straight off its YAML text.

    The document list must not parse every file to say what language each is
    in, so this reads the one line it needs: `language:` under the top-level
    `locale:`.
    """
    m = re.search(r"(?m)^locale:[ \t]*\n((?:[ \t]+.*\n?|[ \t]*\n)*)", text)
    if not m:
        return "en"
    lm = re.search(r"(?m)^[ \t]+language:[ \t]*['\"]?([^'\"\n#]+?)['\"]?[ \t]*(?:#.*)?$",
                   m.group(1))
    return code_of(lm.group(1) if lm else None)
