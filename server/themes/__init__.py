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
        "colors": {"name": "rgb(20, 20, 20)", "headline": "rgb(90, 90, 90)",
                   "connections": "rgb(70, 70, 70)", "section_titles": "rgb(176, 74, 44)",
                   "links": "rgb(176, 74, 44)", "body": "rgb(30, 30, 30)"},
        "typography": {"font_family": {"body": "Open Sauce Sans", "name": "Open Sauce Sans",
                                       "headline": "Open Sauce Sans", "connections": "Open Sauce Sans",
                                       "section_titles": "Open Sauce Sans"},
                       "font_size": {"body": "9.5pt", "name": "28pt", "section_titles": "0.9em"},
                       "alignment": "left", "line_spacing": "0.55em"},
        "section_titles": {"type": "moderncv", "line_thickness": "0pt", "space_above": "0.45cm",
                           "space_below": "0.2cm"},
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
# The sections a sidebar carries, by RenderCV's snake_case title. Anything
# else stays in the main column.
SIDEBAR_SECTIONS = ["skills", "languages", "certifications", "interests", "awards",
                    "technologies", "tools", "hobbies", "competences", "compétences",
                    "langues", "idiomas", "habilidades", "certificações", "certificaciones"]


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
        if name == "sidebar":
            fields["sidebar_sections"] = (list[str], pydantic.Field(
                default_factory=lambda: list(SIDEBAR_SECTIONS),
                description="Sections shown in the sidebar, by their title in snake_case."))
        out[name] = pydantic.create_model(f"{name.capitalize()}Theme", __base__=ClassicTheme, **fields)
    return out


def design_for(design: dict):
    """A validated design for one of our themes, its defaults under yours."""
    name = design.get("theme")
    return theme_classes()[name](**merge(DEFAULTS[name], design))


def _loader():
    import jinja2

    preamble = (HERE / "Preamble.j2.typ").read_text(encoding="utf-8")

    def load(name: str):
        theme, _, rest = name.partition("/")
        if rest == "Preamble.j2.typ" and theme != "typst":
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
            env.globals["cvstudio_sidebar_default"] = SIDEBAR_SECTIONS
            # SectionEnding is not told which section it ends; the sidebar's
            # SectionBeginning leaves a note here for it. Renders run one at
            # a time (cv_render's lock), so one list is enough.
            env.globals["cvstudio_sides"] = []
            env._cvstudio = True
        return env

    templater.get_jinja2_environment = get_env
    _installed = True
