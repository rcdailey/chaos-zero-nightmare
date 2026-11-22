"""Visible text of scraped HTML, shared by every source's parser."""

import copy
import re

from bs4 import Comment, Tag


def text_of(el: Tag | None, separate_spans: bool = False) -> str:
    """Visible text with line breaks kept at block boundaries and spacing normalized.

    separate_spans puts a space after every span. Prydwen lays out table-like rows as adjacent
    spans ("Scarlet", "E4") that otherwise fuse ("ScarletE4"), which full-text search cannot
    find; field values keep spans joined, since their inline markup can split one word."""
    if el is None:
        return ""
    el = copy.copy(el)
    # Source line breaks are layout-insignificant, as in a browser; breaks come from markup.
    for string in el.find_all(string=True):
        if isinstance(string, Comment):
            string.extract()
        else:
            string.replace_with(re.sub(r"\s+", " ", string))
    # A table row is one line, so blocks inside a cell must not break it.
    for cell in el.find_all(["td", "th"]):
        flat = text_of(cell, separate_spans).replace("\n", " ")
        cell.clear()
        cell.append(flat)
    for br in el.find_all("br"):
        br.replace_with("\n")
    for block in el.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"]):
        block.insert_before("\n")
        block.insert_after("\n")
    for cell in el.find_all(["td", "th"]):
        cell.insert_after(" | ")
    if separate_spans:
        for span in el.find_all("span"):
            span.insert_after(" ")
    text = el.get_text().replace("\u200b", "")
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip(" |") for line in text.split("\n"))
    return "\n".join(line for line in lines if line)
