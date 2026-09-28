"""CV Studio's own themes, and ATS-safe icons for every theme.

RenderCV builds a page from one Typst template per part (preamble, header,
section title, each kind of entry) and lets a theme override any of them.
A theme of its own normally has to sit in a folder next to the CV; these are
registered with RenderCV instead, the way its built-in themes are, so a CV
names `theme: studio` and nothing is copied into the workspace.

Each theme is RenderCV's classic theme with its own defaults, so every
Design setting keeps working on it, plus the templates in its folder here.

The preamble here is used by every theme, the built-in ones included. It
draws the contact icons as SVG pictures rather than RenderCV's icon font:
a font icon is a character, and an ATS reads it as one ("\\uf0e0 jo@x.com"),
while a picture is not text at all.
"""
from __future__ import annotations

import copy
import functools
from pathlib import Path
from typing import Literal

HERE = Path(__file__).resolve().parent

# What each theme changes from classic. Kept as data so the Design panel
# can show these as the theme's own defaults rather than as your changes.
DEFAULTS: dict[str, dict] = {
    # After Awesome-CV (posquit0): the first name light and the last bold,
    # section titles whose first letters take the accent before a grey rule,
    # the place in the accent and the dates in grey italics on the right.
    "vivid": {
        "page": {"top_margin": "1.1cm", "bottom_margin": "1.3cm", "left_margin": "1.4cm",
                 "right_margin": "1.4cm", "show_top_note": False, "show_footer": False},
        "colors": {"name": "rgb(51, 51, 51)", "headline": "rgb(220, 53, 34)",
                   "connections": "rgb(51, 51, 51)", "section_titles": "rgb(220, 53, 34)",
                   "links": "rgb(51, 51, 51)", "body": "rgb(65, 65, 65)"},
        "typography": {"font_family": {"body": "Source Sans 3", "name": "Roboto",
                                       "headline": "Source Sans 3", "connections": "Roboto",
                                       "section_titles": "Source Sans 3"},
                       "font_size": {"body": "9.5pt", "name": "32pt", "headline": "8.5pt",
                                     "connections": "7.8pt", "section_titles": "1.65em"},
                       "small_caps": {"headline": True},
                       "alignment": "justified", "line_spacing": "0.55em"},
        "header": {"alignment": "center", "space_below_name": "0.35cm",
                   "space_below_headline": "0.3cm", "space_below_connections": "0.45cm",
                   "connections": {"show_icons": True, "display_urls_instead_of_usernames": False,
                                   "phone_number_format": "international", "separator": "|",
                                   "space_between_connections": "0.35cm"}},
        "section_titles": {"type": "without_line", "space_above": "0.45cm", "space_below": "0.2cm"},
        "sections": {"show_time_spans_in": [], "space_between_regular_entries": "0.9em"},
        "entries": {"date_and_location_width": "4.2cm", "degree_width": "0cm",
                    "highlights": {"space_left": "0.1cm", "space_between_items": "0.1em"}},
        "templates": {
            "single_date": "MONTH_ABBREVIATION. YEAR",
            "experience_entry": {"main_column": "#cvx-org[ COMPANY ]\n#cvx-role[ POSITION ]\nSUMMARY\nHIGHLIGHTS",
                                 "date_and_location_column": "#cvx-place[ LOCATION ]\n#cvx-when[ DATE ]"},
            "education_entry": {"main_column": "#cvx-org[ INSTITUTION ]\n#cvx-role[ DEGREE_WITH_AREA ]\nSUMMARY\nHIGHLIGHTS",
                                "degree_column": "",
                                "date_and_location_column": "#cvx-place[ LOCATION ]\n#cvx-when[ DATE ]"},
            "normal_entry": {"main_column": "#cvx-org[ NAME ]\nSUMMARY\nHIGHLIGHTS",
                             "date_and_location_column": "#cvx-place[ LOCATION ]\n#cvx-when[ DATE ]"},
            "one_line_entry": {"main_column": "#cvx-label[ LABEL ]DETAILS"},
        },
    },
    # After Google Docs' Swiss: a Raleway name, section titles in orange,
    # "Company \u2014 Role" with the dates and place in small capitals beneath:
    # one column all the way down, dates included.
    "swiss": {
        "page": {"top_margin": "1.5cm", "bottom_margin": "1.4cm", "left_margin": "1.9cm",
                 "right_margin": "1.9cm", "show_top_note": False, "show_footer": False},
        "colors": {"name": "rgb(0, 0, 0)", "headline": "rgb(255, 94, 14)",
                   "connections": "rgb(102, 102, 102)", "section_titles": "rgb(255, 94, 14)",
                   "links": "rgb(102, 102, 102)", "body": "rgb(51, 51, 51)"},
        "typography": {"font_family": {"body": "Lato", "name": "Raleway", "headline": "Raleway",
                                       "connections": "Lato", "section_titles": "Raleway"},
                       "font_size": {"body": "9.8pt", "name": "30pt", "headline": "12pt",
                                     "connections": "9pt", "section_titles": "1.05em"},
                       "alignment": "left", "line_spacing": "0.6em"},
        "header": {"alignment": "left", "space_below_name": "0.3cm",
                   "space_below_headline": "0.25cm", "space_below_connections": "0.55cm",
                   "connections": {"show_icons": False, "display_urls_instead_of_usernames": True,
                                   "phone_number_format": "international", "separator": "\u00b7",
                                   "space_between_connections": "0.25cm"}},
        "section_titles": {"type": "without_line", "space_above": "0.6cm", "space_below": "0.2cm"},
        "sections": {"show_time_spans_in": [], "space_between_regular_entries": "1em"},
        "entries": {"date_and_location_width": "0cm", "side_space": "0cm", "space_between_columns": "0cm",
                    "degree_width": "0cm",
                    "highlights": {"space_left": "0.1cm", "space_between_items": "0.15em"}},
        "templates": {
            "single_date": "MONTH_NAME YEAR",
            "experience_entry": {"main_column": "**COMPANY** \u2014 #cvx-it[ POSITION ]\n#cvx-when[ DATE ] #cvx-where[ LOCATION ]\nSUMMARY\nHIGHLIGHTS",
                                 "date_and_location_column": ""},
            "education_entry": {"main_column": "**INSTITUTION** \u2014 #cvx-it[ DEGREE_WITH_AREA ]\n#cvx-when[ DATE ] #cvx-where[ LOCATION ]\nSUMMARY\nHIGHLIGHTS",
                                "degree_column": "", "date_and_location_column": ""},
            "normal_entry": {"main_column": "**NAME**\n#cvx-when[ DATE ] #cvx-where[ LOCATION ]\nSUMMARY\nHIGHLIGHTS",
                             "date_and_location_column": ""},
            "publication_entry": {"main_column": "**TITLE**\n#cvx-when[ DATE ]\nSUMMARY\nAUTHORS\nURL (JOURNAL)",
                                  "date_and_location_column": ""},
        },
    },
    # After Enhancv's single column: a bold name, the role in the accent,
    # section titles in capitals over a heavy rule, each job's title then
    # its company in the accent, then the dates and place with small icons.
    "crisp": {
        "page": {"top_margin": "1.3cm", "bottom_margin": "1.3cm", "left_margin": "1.5cm",
                 "right_margin": "1.5cm", "show_top_note": False, "show_footer": False},
        "colors": {"name": "rgb(17, 17, 17)", "headline": "rgb(0, 116, 217)",
                   "connections": "rgb(55, 55, 55)", "section_titles": "rgb(28, 28, 28)",
                   "links": "rgb(55, 55, 55)", "body": "rgb(34, 34, 34)"},
        "typography": {"font_family": {"body": "Lato", "name": "Poppins", "headline": "Poppins",
                                       "connections": "Lato", "section_titles": "Poppins"},
                       "font_size": {"body": "9.6pt", "name": "27pt", "headline": "12pt",
                                     "connections": "9pt", "section_titles": "1.12em"},
                       "alignment": "left", "line_spacing": "0.55em"},
        "header": {"alignment": "left", "space_below_name": "0.25cm",
                   "space_below_headline": "0.3cm", "space_below_connections": "0.4cm",
                   "connections": {"show_icons": True, "display_urls_instead_of_usernames": True,
                                   "phone_number_format": "international",
                                   "space_between_connections": "0.45cm"}},
        "section_titles": {"type": "without_line", "space_above": "0.5cm", "space_below": "0.25cm"},
        "sections": {"show_time_spans_in": [], "space_between_regular_entries": "0.95em"},
        "entries": {"date_and_location_width": "0cm", "side_space": "0cm", "space_between_columns": "0cm",
                    "degree_width": "0cm",
                    "highlights": {"space_left": "0.1cm", "space_between_items": "0.12em"}},
        "templates": {
            "experience_entry": {"main_column": "#cvx-role[ POSITION ]\n#cvx-org[ COMPANY ]\n#cvx-when[ DATE ] #cvx-where[ LOCATION ]\nSUMMARY\nHIGHLIGHTS",
                                 "date_and_location_column": ""},
            "education_entry": {"main_column": "#cvx-role[ DEGREE_WITH_AREA ]\n#cvx-org[ INSTITUTION ]\n#cvx-when[ DATE ] #cvx-where[ LOCATION ]\nSUMMARY\nHIGHLIGHTS",
                                "degree_column": "", "date_and_location_column": ""},
            "normal_entry": {"main_column": "#cvx-role[ NAME ]\n#cvx-when[ DATE ] #cvx-where[ LOCATION ]\nSUMMARY\nHIGHLIGHTS",
                             "date_and_location_column": ""},
            "publication_entry": {"main_column": "#cvx-role[ TITLE ]\n#cvx-when[ DATE ]\nSUMMARY\nAUTHORS\nURL (JOURNAL)",
                                  "date_and_location_column": ""},
        },
    },
    # After Enhancv's Double Column: Crisp's type, with the summary, skills,
    # education and the other short sections in a column on the right.
    "duo": {"_like": "crisp",
            "page": {"top_margin": "1.3cm", "bottom_margin": "1.3cm", "left_margin": "1.4cm",
                     "right_margin": "1.4cm", "show_top_note": False, "show_footer": False}},
    "aurora": {
        "page": {"top_margin": "1.3cm", "bottom_margin": "1.3cm", "left_margin": "1.6cm",
                 "right_margin": "1.6cm", "show_top_note": False, "show_footer": False},
        "colors": {"name": "rgb(255, 255, 255)", "headline": "rgb(255, 255, 255)",
                   "connections": "rgb(255, 255, 255)", "section_titles": "rgb(79, 70, 229)",
                   "links": "rgb(79, 70, 229)", "body": "rgb(30, 30, 40)"},
        "typography": {"font_family": {"body": "Open Sans", "name": "Poppins",
                                       "headline": "Open Sans", "connections": "Open Sans",
                                       "section_titles": "Poppins"},
                       "font_size": {"body": "9.5pt", "name": "30pt", "headline": "11.5pt",
                                     "section_titles": "1.02em"},
                       "alignment": "left", "line_spacing": "0.55em"},
        "header": {"alignment": "left",
                   "connections": {"show_icons": False, "display_urls_instead_of_usernames": True,
                                   "phone_number_format": "international"}},
        "sections": {"show_time_spans_in": []},
        "section_titles": {"type": "without_line", "space_above": "0.5cm", "space_below": "0.25cm"},
    },
    "editorial": {
        "page": {"top_margin": "1.5cm", "bottom_margin": "1.4cm", "left_margin": "2.1cm",
                 "right_margin": "1.7cm", "show_top_note": False, "show_footer": False},
        "colors": {"name": "rgb(24, 24, 27)", "headline": "rgb(122, 31, 43)",
                   "connections": "rgb(70, 70, 70)", "section_titles": "rgb(122, 31, 43)",
                   "links": "rgb(122, 31, 43)", "body": "rgb(30, 30, 30)"},
        "typography": {"font_family": {"body": "XCharter", "name": "EB Garamond",
                                       "headline": "EB Garamond", "connections": "XCharter",
                                       "section_titles": "EB Garamond"},
                       "font_size": {"body": "9.8pt", "name": "36pt", "headline": "13pt",
                                     "section_titles": "1.25em"},
                       "small_caps": {"section_titles": True},
                       "bold": {"name": False, "section_titles": False},
                       "alignment": "left", "line_spacing": "0.55em"},
        "header": {"alignment": "left",
                   "connections": {"show_icons": False, "display_urls_instead_of_usernames": True,
                                   "phone_number_format": "international"}},
        "sections": {"show_time_spans_in": []},
        "section_titles": {"type": "without_line", "space_above": "0.5cm", "space_below": "0.2cm"},
    },
    "timeline": {
        "page": {"top_margin": "1.5cm", "bottom_margin": "1.4cm", "left_margin": "1.7cm",
                 "right_margin": "1.7cm", "show_top_note": False, "show_footer": False},
        "colors": {"name": "rgb(17, 24, 39)", "headline": "rgb(13, 148, 136)",
                   "connections": "rgb(75, 85, 99)", "section_titles": "rgb(13, 148, 136)",
                   "links": "rgb(13, 148, 136)", "body": "rgb(31, 41, 55)"},
        "typography": {"font_family": {"body": "Lato", "name": "Raleway", "headline": "Lato",
                                       "connections": "Lato", "section_titles": "Raleway"},
                       "font_size": {"body": "9.6pt", "name": "30pt", "headline": "12pt",
                                     "section_titles": "1.1em"},
                       "alignment": "left", "line_spacing": "0.55em"},
        "header": {"alignment": "left",
                   "connections": {"show_icons": False, "display_urls_instead_of_usernames": True,
                                   "phone_number_format": "international"}},
        "sections": {"show_time_spans_in": []},
        "section_titles": {"type": "without_line", "space_above": "0.5cm", "space_below": "0.25cm"},
    },
    "studio": {
        "page": {"top_margin": "1.3cm", "bottom_margin": "1.4cm", "left_margin": "1.6cm",
                 "right_margin": "1.6cm", "show_top_note": False, "show_footer": False},
        "colors": {"name": "rgb(255, 255, 255)", "headline": "rgb(255, 255, 255)",
                   "connections": "rgb(255, 255, 255)", "section_titles": "rgb(31, 78, 121)",
                   "links": "rgb(31, 78, 121)", "body": "rgb(33, 33, 33)"},
        "typography": {"font_family": {"body": "Source Sans 3", "name": "Poppins",
                                       "headline": "Source Sans 3", "connections": "Source Sans 3",
                                       "section_titles": "Poppins"},
                       "font_size": {"name": "26pt", "section_titles": "1.15em"},
                       "alignment": "left", "line_spacing": "0.55em"},
        "header": {"alignment": "left",
                   "connections": {"show_icons": False, "display_urls_instead_of_usernames": True,
                                   "phone_number_format": "international"}},
        "sections": {"show_time_spans_in": []},
        "section_titles": {"type": "without_line"},
    },
    "ledger": {
        "page": {"top_margin": "1.5cm", "bottom_margin": "1.5cm", "left_margin": "1.5cm",
                 "right_margin": "1.5cm", "show_top_note": False, "show_footer": False},
        "colors": {"name": "rgb(20, 20, 20)", "headline": "rgb(176, 74, 44)",
                   "connections": "rgb(70, 70, 70)", "section_titles": "rgb(176, 74, 44)",
                   "links": "rgb(176, 74, 44)", "body": "rgb(30, 30, 30)"},
        "typography": {"font_family": {"body": "Open Sauce Sans", "name": "Open Sauce Sans",
                                       "headline": "Open Sauce Sans", "connections": "Open Sauce Sans",
                                       "section_titles": "Open Sauce Sans"},
                       "font_size": {"body": "9.5pt", "name": "28pt", "section_titles": "0.9em"},
                       "alignment": "left", "line_spacing": "0.55em"},
        "section_titles": {"type": "moderncv", "line_thickness": "0pt", "space_above": "0.75cm",
                           "space_below": "0.1cm"},
        "entries": {"date_and_location_width": "3.9cm", "space_between_columns": "0.45cm"},
        "header": {"alignment": "left",
                   "connections": {"show_icons": False, "display_urls_instead_of_usernames": True,
                                   "phone_number_format": "international"}},
        "sections": {"show_time_spans_in": []},
    },
    "sidebar": {
        "page": {"top_margin": "1.4cm", "bottom_margin": "1.4cm", "left_margin": "1.5cm",
                 "right_margin": "1.1cm", "show_top_note": False, "show_footer": False},
        "colors": {"name": "rgb(25, 42, 61)", "headline": "rgb(31, 111, 107)",
                   "connections": "rgb(60, 60, 60)", "section_titles": "rgb(31, 111, 107)",
                   "links": "rgb(31, 111, 107)", "body": "rgb(35, 35, 35)"},
        "entries": {"date_and_location_width": "3.4cm"},
        "section_titles": {"type": "without_line"},
        "typography": {"font_family": {"body": "Lato", "name": "Raleway", "headline": "Lato",
                                       "connections": "Lato", "section_titles": "Raleway"},
                       "font_size": {"body": "9.5pt", "name": "25pt", "section_titles": "1.1em"},
                       "alignment": "left", "line_spacing": "0.55em"},
        "header": {"alignment": "left",
                   "connections": {"show_icons": False, "display_urls_instead_of_usernames": True,
                                   "phone_number_format": "international"}},
        "sections": {"show_time_spans_in": []},
    },
}
for _name, _d in list(DEFAULTS.items()):
    if "_like" in _d:
        _base = copy.deepcopy(DEFAULTS[_d.pop("_like")])
        _base.update(_d)
        DEFAULTS[_name] = _base

# The sections a sidebar carries, by RenderCV's snake_case title. Anything
# else stays in the main column.
SIDEBAR_SECTIONS = ["skills", "languages", "certifications", "interests", "awards",
                    "technologies", "tools", "hobbies", "competences", "compétences",
                    "langues", "idiomas", "habilidades", "certificações", "certificaciones"]


# Duo's right-hand column holds more: the summary and education as well.
DUO_SECTIONS = ["summary", "profile", "about_me", "résumé", "profil", "resumen", "perfil", "resumo",
                "education", "formation", "éducation", "educación", "formação", "educação"] + SIDEBAR_SECTIONS
SIDE_THEMES = {"sidebar": SIDEBAR_SECTIONS, "duo": DUO_SECTIONS}


# Themes that render but are not offered in the picker until they are ready.
HIDDEN: set[str] = set()


def _lum(rgb) -> float:
    """WCAG relative luminance of an (r, g, b) in 0..255."""
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a, b) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _rgb(color) -> tuple:
    t = color.as_rgb_tuple() if hasattr(color, "as_rgb_tuple") else color
    return tuple(int(x) for x in t[:3])


def band_ink(color) -> str:
    """Text on a band of this colour: white where it reads, near-black else."""
    rgb = _rgb(color)
    return "rgb(255, 255, 255)" if _contrast(rgb, (255, 255, 255)) >= 3.2 else "rgb(24, 24, 27)"


def readable(color, on=(255, 255, 255), target: float = 3.2) -> str:
    """The colour itself if text in it reads on white, else darkened until it
    does: a yellow accent still gives yellow bands, but brown-gold titles."""
    r, g, b = _rgb(color)
    for _ in range(40):
        if _contrast((r, g, b), on) >= target:
            break
        r, g, b = int(r * 0.9), int(g * 0.9), int(b * 0.9)
    return f"rgb({r}, {g}, {b})"


def swap(text: str, key: str, value: str) -> str:
    """Set one of the parameters RenderCV's preamble passes to its template,
    or fail loudly: a silent miss would render with the wrong colour or
    margin and nobody would know why."""
    import re
    pat = re.compile(rf"^(\s*{re.escape(key)}:\s*).+?,\s*$", re.M)
    if not pat.search(text):
        raise ValueError(f"CV Studio themes: RenderCV's preamble has no '{key}' to set")
    return pat.sub(lambda m: f"{m.group(1)}{value},", text, count=1)


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


# --- Icons -------------------------------------------------------------------
# 24x24 paths, filled. The brand marks are the public Simple Icons shapes
# (CC0); the rest are drawn here.
ICON_PATHS = {
    "envelope": "M3 5h18a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zm.8 2.1V17h16.4V7.1L12 12.6zM5.3 7 12 11.3 18.7 7z",
    "phone": "M6.6 2.5 9.4 5.3c.5.5.6 1.3.2 1.9L8.3 9.1a12.4 12.4 0 0 0 6.6 6.6l1.9-1.3c.6-.4 1.4-.3 1.9.2l2.8 2.8c.6.6.6 1.5 0 2.1l-1.5 1.5c-1 1-2.6 1.3-3.9.7A21 21 0 0 1 2.3 7.9c-.6-1.3-.3-2.9.7-3.9l1.5-1.5c.6-.6 1.5-.6 2.1 0z",
    "location-dot": "M12 1.5a7.5 7.5 0 0 1 7.5 7.5c0 5.2-6.3 12.3-6.9 13a.8.8 0 0 1-1.2 0C10.8 21.3 4.5 14.2 4.5 9A7.5 7.5 0 0 1 12 1.5zm0 4.5a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
    "link": "M12 1.5a10.5 10.5 0 1 1 0 21 10.5 10.5 0 0 1 0-21zm-2.1 2.4a8.6 8.6 0 0 0-6.1 7.2h3.9c.1-2.6.9-5.1 2.2-7.2zm4.2 0c1.3 2.1 2.1 4.6 2.2 7.2h3.9a8.6 8.6 0 0 0-6.1-7.2zM12 4.2c-1.4 1.9-2.2 4.3-2.4 6.9h4.8c-.2-2.6-1-5-2.4-6.9zM3.8 12.9a8.6 8.6 0 0 0 6.1 7.2 14.8 14.8 0 0 1-2.2-7.2zm5.8 0c.2 2.6 1 5 2.4 6.9 1.4-1.9 2.2-4.3 2.4-6.9zm6.7 0c-.1 2.6-.9 5.1-2.2 7.2a8.6 8.6 0 0 0 6.1-7.2z",
    "linkedin": "M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z",
    "github": "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12",
    "calendar": "M7 2h2v2h6V2h2v2h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2zM5 10v10h14V10zm0-2h14V6H5z",
    "x-twitter": "M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z",
}
ICON_FALLBACK = "link"


def icon_svg(name: str, color: str) -> str:
    path = ICON_PATHS.get(name) or ICON_PATHS[ICON_FALLBACK]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            f'<path fill="{color}" d="{path}"/></svg>')


# --- Registration with RenderCV ----------------------------------------------
@functools.cache
def theme_classes() -> dict:
    """A pydantic model per theme: classic's options, its own name."""
    import pydantic
    from rendercv.schema.models.design.classic_theme import ClassicTheme

    out = {}
    for name in DEFAULTS:
        fields = {"theme": (Literal[name], name)}  # type: ignore[valid-type]
        if name in SIDE_THEMES:
            fields["sidebar_sections"] = (list[str], pydantic.Field(
                default_factory=lambda d=SIDE_THEMES[name]: list(d),
                description="Sections shown in the sidebar, by their title in snake_case."))
        out[name] = pydantic.create_model(f"{name.capitalize()}Theme", __base__=ClassicTheme, **fields)
    return out


def design_for(design: dict):
    """A validated design for one of our themes, its defaults under yours."""
    name = design.get("theme")
    return theme_classes()[name](**merge(DEFAULTS[name], design))


@functools.cache
def _builtin() -> frozenset:
    try:
        from rendercv.schema.models.design.built_in_design import available_themes
        return frozenset(available_themes)
    except Exception:
        return frozenset()


def _loader():
    import jinja2

    preamble = (HERE / "Preamble.j2.typ").read_text(encoding="utf-8")

    def load(name: str):
        theme, _, rest = name.partition("/")
        # RenderCV's own themes and ours get this preamble; a theme folder
        # of your own keeps its own.
        if rest == "Preamble.j2.typ" and (theme in DEFAULTS or theme in _builtin()):
            return preamble, None, lambda: True
        if theme in DEFAULTS:
            f = HERE / theme / rest
            if f.is_file():
                return f.read_text(encoding="utf-8"), str(f), lambda: True
        return None
    return jinja2.FunctionLoader(load)


_installed = False


def install() -> None:
    """Teach RenderCV our themes and icons. Safe to call more than once."""
    global _installed
    if _installed:
        return
    import jinja2
    from rendercv.renderer.templater import templater
    from rendercv.schema.models.design import design as design_mod

    original_validate = design_mod.validate_design

    def validate_design(design, info):
        if isinstance(design, dict) and design.get("theme") in DEFAULTS:
            return design_for(design)
        if hasattr(design, "theme") and getattr(design, "theme") in DEFAULTS:
            return design
        return original_validate(design, info)

    design_mod.validate_design = validate_design

    original_env = templater.get_jinja2_environment
    ours = _loader()

    def get_env(input_file_path=None):
        env = original_env(input_file_path)
        if not getattr(env, "_cvstudio", False):
            env.loader = jinja2.ChoiceLoader([ours, env.loader])
            env.globals["cvstudio_icon"] = icon_svg
            env.globals["cvstudio_band_ink"] = band_ink
            env.globals["cvstudio_readable"] = readable
            env.globals["cvstudio_swap"] = swap
            env.globals["cvstudio_ours"] = list(DEFAULTS)
            env.globals["cvstudio_sidebar_default"] = SIDEBAR_SECTIONS
            # SectionEnding is not told which section it ends; the sidebar's
            # SectionBeginning leaves a note here for it. Renders run one at
            # a time (cv_render's lock), so one list is enough.
            env.globals["cvstudio_sides"] = []
            env._cvstudio = True
        return env

    templater.get_jinja2_environment = get_env
    _installed = True
