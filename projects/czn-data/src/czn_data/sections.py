"""Split a guide page (Game8, Prydwen) into searchable sections at its headings.

The page text is cut at headings; each section's heading names its parents ("A > B > C"). A
section longer than ``posts.MAX_CHARS`` becomes overlapping pieces. The page's ``body`` is
rebuilt from the kept parts (title, summary, then each section under its heading path), so
``offset`` and ``length`` locate a section in it and get_page can read around it.
"""

import re
from dataclasses import dataclass

from bs4 import Tag

from .posts import chunk_lines
from .text import text_of

# Sections that only link to other pages.
NAVIGATION = re.compile(r"Related (Guides|Pages)", re.IGNORECASE)
# Marks heading lines through text_of; a private-use character, since it treats control
# characters as spaces.
_MARK = "\ue000"


@dataclass
class GuideSection:
    """One section of a page, or one piece of a long one: ``heading`` names its parents
    ("A > B > C"); ``offset`` and ``length`` locate ``text`` in the page's ``body``."""

    position: int
    heading: str
    text: str
    offset: int
    length: int


@dataclass
class GuidePage:
    """A page as search serves it: ``summary`` is its text before the first heading, ``body``
    the whole text (title, summary, then each section under its heading path)."""

    title: str
    summary: str
    body: str
    sections: list[GuideSection]


def split_at_headings(
    root: Tag, title: str, levels: dict[str, int], separate_spans: bool = False
) -> GuidePage:
    """Split root's text at the headings named in levels (tag: depth, 0 outermost). Text
    before the first heading is the summary; empty sections, sections Game8 has not filled
    ("TBD"), and link lists are dropped. root is changed."""
    for heading in root.find_all(list(levels)):
        heading.string = f"{_MARK}{levels[heading.name]}{_MARK}{text_of(heading)}"

    summary: list[str] = []
    raw: list[tuple[str, list[str]]] = []
    path: list[str] = []
    for line in text_of(root, separate_spans).split("\n"):
        if not line.startswith(_MARK):
            (raw[-1][1] if raw else summary).append(line)
            continue
        _, level, name = line.split(_MARK, 2)
        path = [*path[: int(level)], name.strip()]
        raw.append((" > ".join(path), []))

    body = "\n".join(filter(None, [title, *summary]))
    sections: list[GuideSection] = []
    for heading, lines in raw:
        if NAVIGATION.search(heading.split(" > ")[0]) or lines in ([], ["TBD"]):
            continue
        text = "\n".join(lines)
        body += f"\n\n{heading}\n"
        start = len(body)
        body += text
        found = 0
        for chunk in chunk_lines(lines):
            found = text.find(chunk, found)
            sections.append(GuideSection(len(sections), heading, chunk, start + found, len(chunk)))
            found += 1
    return GuidePage(title, "\n".join(summary), body, sections)
