"""The shapes CV Studio's tools return, as output schemas a client can read.

Without them every structured result was published as "an object, any keys",
which tells a model nothing until it has called the tool and read the answer.
With them the schema says what comes back and what each field means, and the
structured content is checked against it on the way out.

Each shape allows keys beyond the ones listed: a tool adds notes as it learns
something (posting_note, logo_note, next), and a field the job store gains
later must not be stripped from an answer for want of a line here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict
from typing_extensions import NotRequired, TypedDict

_OPEN = ConfigDict(extra="allow")

Status = Literal["pending", "applied", "interviewing", "offer", "accepted", "refused",
                 "rejected", "ghosted", "rejected_interviewing", "ghosted_interviewing"]


class Document(TypedDict):
    __pydantic_config__ = _OPEN
    path: str
    label: NotRequired[str]
    group: NotRequired[str]
    lang: NotRequired[str | None]
    base: NotRequired[str | None]        # the CV this one was copied from
    translation_of: NotRequired[str | None]


class Brief(TypedDict):
    """An application, trimmed to what identifies it."""
    __pydantic_config__ = _OPEN
    id: str
    title: str
    company: str
    status: Status
    location: NotRequired[str]
    url: NotRequired[str]
    source: NotRequired[str]
    followup_date: NotRequired[str]
    interview_at: NotRequired[str]
    interview_tz: NotRequired[str]
    contact_email: NotRequired[str]
    last_contact_at: NotRequired[str]
    cv_path: NotRequired[str]
    letter_path: NotRequired[str]
    updated_at: NotRequired[str]


class Person(TypedDict):
    __pydantic_config__ = _OPEN
    name: NotRequired[str]
    email: NotRequired[str]
    role: NotRequired[str]
    link: NotRequired[str]


class StatusChange(TypedDict):
    __pydantic_config__ = _OPEN
    status: str
    at: str


class Application(TypedDict):
    """An application in full: the posting, its history, the people."""
    __pydantic_config__ = _OPEN
    id: str
    title: str
    company: str
    status: Status
    description: NotRequired[str | None]
    notes: NotRequired[str | None]
    url: NotRequired[str | None]
    location: NotRequired[str | None]
    source: NotRequired[str | None]
    language: NotRequired[str | None]
    followup_date: NotRequired[str | None]
    interview_at: NotRequired[str | None]
    interview_tz: NotRequired[str | None]
    contact_email: NotRequired[str | None]
    last_contact_at: NotRequired[str | None]
    cv_path: NotRequired[str | None]
    letter_path: NotRequired[str | None]
    status_history: NotRequired[list[StatusChange]]
    people: NotRequired[list[Person] | None]
    created_at: NotRequired[str]
    updated_at: NotRequired[str]


class Match(TypedDict):
    candidates: list[Brief]
    confident: bool
    note: str


class AlertCounts(TypedDict):
    __pydantic_config__ = _OPEN
    interview_soon: int
    followup_due: int
    interview_passed: int
    silent: int


class Alerts(TypedDict):
    __pydantic_config__ = _OPEN
    counts: AlertCounts
    summary: str
    interview_soon: list[dict[str, Any]]
    followup_due: list[dict[str, Any]]
    interview_passed: list[dict[str, Any]]
    silent: list[dict[str, Any]]


class Interview(TypedDict):
    __pydantic_config__ = _OPEN
    job_id: str
    company: str
    title: str
    status: str
    interview_at: str
    interview_tz: str | None
    local: str
    utc: str


class Followup(TypedDict):
    __pydantic_config__ = _OPEN
    job_id: str
    company: str
    title: str
    status: str
    date: str
    overdue: bool


class Calendar(TypedDict):
    days_ahead: int
    interviews: list[Interview]
    followups: list[Followup]
    summary: str
    ics: NotRequired[str]


class People(TypedDict):
    id: str
    people: list[Person] | None


class PrepQuestion(TypedDict):
    __pydantic_config__ = _OPEN
    q: str
    src: str
    why: NotRequired[str]
    cv: NotRequired[str]
    note: NotRequired[str]
    state: NotRequired[str]


class Prep(TypedDict):
    __pydantic_config__ = _OPEN
    questions: list[PrepQuestion]
    stories: NotRequired[list[dict[str, Any]]]
    asks: NotRequired[list[dict[str, Any]]]
    local: NotRequired[bool]


class Posting(TypedDict):
    __pydantic_config__ = _OPEN
    title: str
    company: str
    location: NotRequired[str | None]
    description: str
    via: str
    url: str


class Problem(TypedDict):
    title: str
    detail: str


class Keywords(TypedDict):
    used: str
    found: list[str]
    missing: list[str]


class AtsReport(TypedDict):
    pages: int
    words: int
    problems: list[Problem]
    against: str | dict[str, Any] | None
    keywords: Keywords | None


class Letter(TypedDict):
    __pydantic_config__ = _OPEN
    path: str
    header: dict[str, Any]
    body: str
    next: str


class Theme(TypedDict):
    name: str
    label: str
    based_on: str
    error: str | None


class DesignOptions(TypedDict):
    themes: list[str]
    your_themes: list[Theme]
    fonts: list[str]
    page_sizes: list[str]
    note: str


class SavedTheme(TypedDict):
    theme: str
    label: str
    based_on: str
    file: str
    next: str


class Workspace(TypedDict):
    workspace: str
    cv_count: int
    job_count: int
    base_cv: str | None
    base_cv_note: str
    photo: str
    storage: str


class Translation(TypedDict):
    __pydantic_config__ = _OPEN
    changes: NotRequired[list[dict[str, Any]]]


class ThemeTile(TypedDict):
    theme: str
    label: str
    pages: int | None
    ok: bool


class ThemeGallery(TypedDict):
    path: str
    current: str
    themes: list[ThemeTile]
    note: str


class DocumentReview(TypedDict):
    path: str
    by: str | None
    units: list[str]


class ApplicationChange(TypedDict):
    __pydantic_config__ = _OPEN
    id: str
    company: str | None
    title: str | None
    kind: str
    tool: str | None
    changes: list[dict[str, Any]]


class Review(TypedDict):
    documents: list[DocumentReview]
    applications: list[ApplicationChange]
    summary: str


class Picture(TypedDict):
    png: str
    page: int
    pages: int


class Resolved(TypedDict):
    ok: bool
    kind: str
    id: str
    action: str
    deleted: bool
