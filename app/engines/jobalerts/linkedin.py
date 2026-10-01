"""Reading a LinkedIn job alert email.

LinkedIn sends alerts as HTML with the jobs laid out as repeated blocks. There
is no API and no schema; the shape is whatever marketing last shipped, and it
changes without notice.

So this parser is built to degrade rather than guess. Anything it cannot read
is kept and reported as unparsed, never dropped and never filled in with a
plausible-looking default. A job with no stated arrangement is UNKNOWN, which
is a real answer everywhere else in this feature.

It anchors on the job **links**, not on the flattened text. The job id lives in
the href, which flattening discards, so anchoring on text loses the only
reliable boundary between one job and the next -- an earlier version did that
and ran the email's preamble into the first job's title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape

from app.models.jobfeed import WorkplaceType
from app.services.jobfeed import classify_workplace

#: A job link together with its anchor text, which is the title LinkedIn shows.
_JOB_ANCHOR = re.compile(
    r"""<a\b[^>]*href=["']?https?://[\w.]*linkedin\.com/(?:comm/)?jobs/view/"""
    r"""(?P<id>\d+)[^>]*>(?P<title>.*?)</a>""",
    re.I | re.S,
)

#: Either form of job link, for the plain-text fallback and for recognition.
_JOB_URL_ANY = re.compile(
    r"""https?://[\w.]*linkedin\.com/(?:comm/)?jobs/view/(\d+)""",
    re.I,
)

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile("[ " + chr(9) + chr(0x00A0) + "]+")
_BLANK_LINES = re.compile(r"\n{3,}")

#: "Doha, Qatar (Remote)" -- LinkedIn brackets the arrangement after the
#: location when it knows it.
_BRACKETED = re.compile(r"\(([^)]{3,20})\)\s*$")

#: Separators LinkedIn puts around fields. Built from code points because an
#: en dash and a hyphen are indistinguishable in a source file.
_TRIM = " |-" + chr(9) + chr(0x00B7) + chr(0x2013) + chr(0x2014)

#: Lines that are furniture rather than job detail.
_NOISE = (
    "view job",
    "see all jobs",
    "see all",
    "unsubscribe",
    "actively recruiting",
    "easy apply",
    "be an early applicant",
    "your job alert",
    "new jobs match",
    "linkedin corporation",
    "this email was intended",
    "applicants",
    "promoted",
)


@dataclass(slots=True)
class ParsedJob:
    title: str
    company_name: str | None = None
    location: str | None = None
    workplace_type: WorkplaceType = WorkplaceType.UNKNOWN
    url: str | None = None
    external_id: str | None = None


@dataclass(slots=True)
class ParsedAlert:
    """What one alert email contained."""

    jobs: list[ParsedJob] = field(default_factory=list)
    #: Blocks that looked like a job but could not be read. Kept so a parser
    #: improvement can be run over history, and so nothing is silently lost.
    unparsed: list[str] = field(default_factory=list)
    is_linkedin: bool = False


def html_to_text(html: str) -> str:
    """Flatten HTML to text, keeping the line structure the layout implies."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|tr|td|h\d|li)>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = unescape(text)
    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def looks_like_linkedin(sender: str | None, subject: str | None, body: str) -> bool:
    """Is this actually a LinkedIn job alert?

    Checked before parsing so a newsletter forwarded to the same address is
    reported as unrecognised rather than mangled into fabricated jobs.
    """
    haystack = " ".join(filter(None, [sender or "", subject or "", body[:2000]])).lower()
    if "linkedin" not in haystack:
        return False
    return bool(_JOB_URL_ANY.search(body))


def _clean(value: str) -> str:
    return _WHITESPACE.sub(" ", unescape(value)).strip(_TRIM)


def _split_location(raw: str) -> tuple[str, WorkplaceType]:
    """Turn "Doha, Qatar (Remote)" into its two separate facts."""
    match = _BRACKETED.search(raw)
    if not match:
        return raw.strip(), WorkplaceType.UNKNOWN

    workplace = classify_workplace(match.group(1).strip())
    if workplace is WorkplaceType.UNKNOWN:
        # Brackets holding something else. Keep them: they are part of the
        # location, not an arrangement we failed to read.
        return raw.strip(), WorkplaceType.UNKNOWN
    return raw[: match.start()].strip().rstrip(",").strip(), workplace


def _anchors(html: str) -> list[tuple[str, str, int]]:
    """Each job link as (id, title, offset just past the anchor)."""
    return [
        (match.group("id"), _clean(_TAG.sub(" ", match.group("title"))), match.end())
        for match in _JOB_ANCHOR.finditer(html)
    ]


def _anchors_from_text(text: str) -> list[tuple[str, str, int]]:
    """Plain-text fallback.

    A text part carries no anchor, so the title is taken from the line above,
    which is where LinkedIn puts it.
    """
    found: list[tuple[str, str, int]] = []
    for match in _JOB_URL_ANY.finditer(text):
        preceding = text[: match.start()].rstrip().split("\n")
        found.append((match.group(1), _clean(preceding[-1]) if preceding else "", match.end()))
    return found


def _job_from(block: str, *, title: str, job_id: str) -> ParsedJob | None:
    """Company and location from the text that follows a job link.

    Furniture is dropped first; what remains is company, then location.
    """
    if not title or len(title) > 240:
        return None

    lines = [
        _clean(line)
        for line in block.split("\n")
        if _clean(line)
        and not any(word in line.lower() for word in _NOISE)
        and not line.strip().lower().startswith("http")
    ]

    company = lines[0] if lines else None
    location_raw = lines[1] if len(lines) > 1 else None

    workplace = WorkplaceType.UNKNOWN
    location = None
    if location_raw:
        location, workplace = _split_location(location_raw)

    if workplace is WorkplaceType.UNKNOWN:
        # Sometimes the arrangement is on its own line rather than bracketed.
        workplace = classify_workplace(" ".join(lines[:3]))

    return ParsedJob(
        title=title,
        company_name=company or None,
        location=location or None,
        workplace_type=workplace,
        url=f"https://www.linkedin.com/jobs/view/{job_id}",
        external_id=job_id,
    )


def parse_alert(
    *,
    body_html: str | None = None,
    body_text: str | None = None,
    subject: str | None = None,
    sender: str | None = None,
) -> ParsedAlert:
    """Pull the jobs out of one alert email."""
    raw = body_html or body_text or ""

    alert = ParsedAlert(is_linkedin=looks_like_linkedin(sender, subject, raw))
    if not alert.is_linkedin:
        return alert

    anchors = _anchors(raw) if body_html else _anchors_from_text(raw)

    seen: set[str] = set()
    for index, (job_id, title, after) in enumerate(anchors):
        if job_id in seen:
            continue
        seen.add(job_id)

        # Everything between this job's link and the next one belongs to it.
        end = anchors[index + 1][2] if index + 1 < len(anchors) else len(raw)
        block = html_to_text(raw[after:end]) if body_html else raw[after:end]

        job = _job_from(block, title=title, job_id=job_id)
        if job is None:
            alert.unparsed.append(block[:500])
        else:
            alert.jobs.append(job)

    return alert
