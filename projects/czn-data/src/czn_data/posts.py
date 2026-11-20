"""Split official Stove posts into searchable sections, deterministically.

Stove posts are editor HTML written by the game's team, not structured data. Their writing
habits give the structure: "■" opens a section and "◆" a subsection (update notices), and
"①".."⑳" open one event each (event overviews). Each marker starts a section whose heading names
its parent ("■ New Chaos > ◆ Season Restrictions"). A section longer than ``MAX_CHARS``, or a
post with no markers, is cut into overlapping chunks of whole lines.

Cleanup: struck-out text is dropped (an extended event strikes its old end date), images are
lost (some calendars are images), and tables become one line per row with " | " between cells.

Periods: a date range in a section's heading, or the first one shortly after "Period",
"Schedule", or "Duration" in its heading or text, becomes ``starts_on`` and ``ends_on`` (dates
only; the posts give times as "after the maintenance" more often than as clock times). A section
without one takes its "■" section's period (see ``_group_period``). A date without a year takes
the post's year. Text that does not parse keeps no dates and stays searchable.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .parse import text_of

# Longest section kept whole; longer ones become chunks of about this size, each repeating the
# last OVERLAP_CHARS of lines of the one before, so a fact split across chunks is still found.
MAX_CHARS = 2000
OVERLAP_CHARS = 300

_LEVEL_ONE = "■"
_LEVEL_TWO = "◆" + "".join(chr(c) for c in range(ord("①"), ord("⑳") + 1))

# Finds where a date is written; dateutil reads it. Forms seen: 2026/06/09, 11/11/2026, 11/11,
# November 11th, 2026, Nov 11, 21 December 2026.
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATE = re.compile(
    r"\b(?:\d{4}[/.-]\d{1,2}[/.-]\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{4})?"
    rf"|{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?|\d{{1,2}}\s+{_MONTH}\s+\d{{4}})\b",
    re.IGNORECASE,
)
# Between the two dates of a range, after words like "after the maintenance".
_RANGE_SEPARATOR = re.compile(r"[–~—]|\s-\s|\bto\b|\buntil\b", re.IGNORECASE)
_RANGE_GAP_CHARS = 80
_PERIOD_WORD = re.compile(r"\b(?:Period|Schedule|Duration)\b", re.IGNORECASE)
# How far after "Period" its range may start ("Schedule:" sometimes has the dates a line later).
_PERIOD_REACH_CHARS = 200


@dataclass
class Section:
    position: int
    heading: str | None
    text: str
    starts_on: date | None = None
    ends_on: date | None = None


def split_post(html: str, posted: datetime) -> list[Section]:
    soup = BeautifulSoup(html, "lxml")
    for gone in soup(["s", "strike", "del", "img", "script", "style"]):
        gone.decompose()
    lines = []
    for line in text_of(soup.body or soup).split("\n"):
        # A marker styled apart from its heading lands on a line of its own.
        if lines and lines[-1] in _LEVEL_ONE + _LEVEL_TWO:
            line = f"{lines.pop()} {line}"
        lines.append(line)

    raw: list[tuple[str | None, list[str]]] = [(None, [])]
    parent = None
    for line in lines:
        if line[0] in _LEVEL_ONE:
            parent = line
            raw.append((line, []))
        elif line[0] in _LEVEL_TWO:
            raw.append((f"{parent} > {line}" if parent else line, []))
        else:
            raw[-1][1].append(line)

    periods = [(heading, _period(heading or "", "\n".join(body), posted)) for heading, body in raw]
    sections = []
    for (heading, body), (_, period) in zip(raw, periods, strict=True):
        if not body and (heading is None or not heading.strip(_LEVEL_ONE + _LEVEL_TWO + " >")):
            continue
        starts_on, ends_on = period or _group_period(heading, periods) or (None, None)
        for chunk in _chunks(body) or [""]:
            sections.append(Section(len(sections), heading, chunk, starts_on, ends_on))
    return sections


def _group_period(
    heading: str | None, periods: list[tuple[str | None, tuple[date, date] | None]]
) -> tuple[date, date] | None:
    """The period of the "■" section a section sits in, so an event's details are dated by its
    "◆ Period" sibling: the period of the "■" section itself, or of its "◆" subsections whose
    headings say "Period" or "Schedule", when they all give the same one."""
    if heading is None or not heading.startswith(_LEVEL_ONE):
        return None
    parent = heading.split(" > ")[0]
    found = {
        period
        for other, period in periods
        if period
        and other
        and (other == parent or (other.startswith(f"{parent} > ") and _PERIOD_WORD.search(other)))
    }
    return found.pop() if len(found) == 1 else None


def _chunks(lines: list[str]) -> list[str]:
    """The lines as one text, or as chunks of whole lines near ``MAX_CHARS`` when longer."""
    text = "\n".join(lines)
    if len(text) <= MAX_CHARS:
        return [text] if text else []
    chunks, current = [], []
    for line in lines:
        if current and len("\n".join([*current, line])) > MAX_CHARS:
            chunks.append("\n".join(current))
            overlap = []
            for previous in reversed(current):
                if len("\n".join([previous, *overlap])) > OVERLAP_CHARS:
                    break
                overlap.insert(0, previous)
            current = overlap
        current.append(line)
    chunks.append("\n".join(current))
    return chunks


def _period(heading: str, body: str, posted: datetime) -> tuple[date, date] | None:
    """A date range in the heading itself ("◆ Rossweisse: 10/21/2026 ... – 12/02/2026"), else
    the first range shortly after "Period", "Schedule", or "Duration" in heading or text."""
    if found := _range(heading, posted):
        return found
    text = f"{heading}\n{body}"
    for word in _PERIOD_WORD.finditer(text):
        window = text[word.end() : word.end() + _PERIOD_REACH_CHARS]
        if found := _range(window, posted):
            return found
    return None


def _range(text: str, posted: datetime) -> tuple[date, date] | None:
    """The first two dates in text with a range separator ("–", "~", "to") between them."""
    dates = list(_DATE.finditer(text))
    for first, second in pairwise(dates):
        gap = text[first.end() : second.start()]
        if len(gap) > _RANGE_GAP_CHARS or not _RANGE_SEPARATOR.search(gap):
            continue
        default = datetime(posted.year, 1, 1)
        try:
            start = dateparser.parse(first[0], default=default).date()
            end = dateparser.parse(second[0], default=default).date()
        except ValueError, OverflowError:
            continue
        if end < start:  # "December 21 - January 4" without years crosses into the next year
            end = end.replace(year=end.year + 1)
        return start, end
    return None
