"""Contract: an official Stove post becomes sections a search can return on their own, with the
period each section gives. Markup follows the real posts (2026-11-20)."""

from datetime import UTC, date, datetime
from itertools import pairwise

import pytest

from czn_data.posts import MAX_CHARS, split_post

POSTED = datetime(2026, 11, 11, tzinfo=UTC)


def sections(html: str) -> list[tuple]:
    return [(s.heading, s.text, s.starts_on, s.ends_on) for s in split_post(html, POSTED)]


def test_update_notice_markers_become_nested_headings_and_tables_become_rows():
    html = (
        "<p>Hello, Protos.</p>"
        "<p>■ Galactic Disaster Season 5 Chapter 2 Begins</p><p>New story.</p>"
        "<p>◆ Supply Store</p>"
        "<table><tr><td>Item</td><td>Limit</td></tr><tr><td>Aether</td><td>5</td></tr></table>"
    )

    assert [(h, t) for h, t, _, _ in sections(html)] == [
        (None, "Hello, Protos."),
        ("■ Galactic Disaster Season 5 Chapter 2 Begins", "New story."),
        (
            "■ Galactic Disaster Season 5 Chapter 2 Begins > ◆ Supply Store",
            "Item | Limit\nAether | 5",
        ),
    ]


def test_table_cells_written_as_paragraphs_stay_on_their_row():
    html = (
        "<p>■ Changes</p><table>"
        "<tr><td><p>Option</p></td><td><p>Before</p></td><td><p>After</p></td></tr>"
        "<tr><td><p>Gangrene 2</p></td><td><p>Lose 2%</p><p>per turn</p></td>"
        "<td><p>Lose 4%</p></td></tr></table>"
    )

    assert [t for _, t, _, _ in sections(html)] == [
        "Option | Before | After\nGangrene 2 | Lose 2% per turn | Lose 4%"
    ]


def test_struck_out_end_date_is_dropped_and_missing_year_is_the_posts():
    html = (
        "<p>◆ Event Period</p><p>- November 11th (Wed) – <s>November 29th (Fri)</s>"
        " December 2nd (Wed) 14:59 (UTC)</p>"
    )

    ((_, text, starts_on, ends_on),) = sections(html)

    assert "29th" not in text
    assert (starts_on, ends_on) == (date(2026, 11, 11), date(2026, 12, 2))


def test_numbered_events_are_sections_with_their_schedules():
    html = (
        "<p>① Event Name: Nightmare Dream Park</p><p>📆 Schedule:</p>"
        "<p>October 21, 2026 (Wed) after the maintenance – December 23, 2026 (Wed) before the"
        " maintenance</p>"
        "<p>② Event Name: Daily Login</p><p>Schedule: 11/03/2026 18:00 (UTC) – 11/24/2026 18:00"
        " (UTC)</p>"
    )

    assert [(h, s, e) for h, _, s, e in sections(html)] == [
        ("① Event Name: Nightmare Dream Park", date(2026, 10, 21), date(2026, 12, 23)),
        ("② Event Name: Daily Login", date(2026, 11, 3), date(2026, 11, 24)),
    ]


@pytest.mark.parametrize(
    ("line", "period"),
    [
        ("Schedule: 2026/06/09 18:00 – 2026/06/23 18:00 (UTC)", ((6, 9), (6, 23))),
        (
            (
                "Period: After maintenance on February 4, 2026 – until before the start of"
                " maintenance on April 8, 2026"
            ),
            ((2, 4), (4, 8)),
        ),
        (
            "Period: 11/11/2026 after the maintenance – before the 12/23/2026 maintenance",
            ((11, 11), (12, 23)),
        ),
        ("Duration: 21 October 2026 ~ 4 November 2026", ((10, 21), (11, 4))),
    ],
)
def test_period_forms_seen_in_posts(line, period):
    ((_, _, starts_on, ends_on),) = sections(f"<p>◆ Supply Store</p><p>{line}</p>")
    assert (starts_on, ends_on) == tuple(date(2026, m, d) for m, d in period)


def test_dated_heading_needs_no_period_word():
    html = "<p>◆ Rossweisse: 10/21/2026 after the maintenance – 12/02/2026</p><p>Clear it.</p>"
    ((_, _, starts_on, ends_on),) = sections(html)
    assert (starts_on, ends_on) == (date(2026, 10, 21), date(2026, 12, 2))


def test_sections_take_the_period_their_event_gives():
    html = (
        "<p>■ Event - Nightmare Dream Park</p><p>◆ Event Period</p>"
        "<p>11/11/2026 – 12/02/2026</p><p>◆ Rewards</p><p>Aether x5</p>"
        "<p>■ Bug Fixes</p><p>Fixed a typo.</p>"
    )
    assert [(h, s, e) for h, _, s, e in sections(html)] == [
        ("■ Event - Nightmare Dream Park", date(2026, 11, 11), date(2026, 12, 2)),
        (
            "■ Event - Nightmare Dream Park > ◆ Event Period",
            date(2026, 11, 11),
            date(2026, 12, 2),
        ),
        ("■ Event - Nightmare Dream Park > ◆ Rewards", date(2026, 11, 11), date(2026, 12, 2)),
        ("■ Bug Fixes", None, None),
    ]


def test_sections_under_differently_dated_subsections_stay_undated():
    html = (
        "<p>■ Supply Store</p><p>◆ 1st Period: 10/21/2026 – 12/23/2026</p><p>a</p>"
        "<p>◆ 2nd Period: 11/11/2026 – 12/23/2026</p><p>b</p><p>◆ Notes</p><p>c</p>"
    )
    assert sections(html)[-1][2:] == (None, None)


def test_period_across_new_year_ends_in_the_next_year():
    html = "<p>◆ Period: December 21st – January 4th</p>"
    ((_, _, starts_on, ends_on),) = sections(html)
    assert (starts_on, ends_on) == (date(2026, 12, 21), date(2027, 1, 4))


def test_text_without_a_parsable_period_keeps_no_dates():
    html = "<p>◆ Period</p><p>Until further notice</p><p>Open Schedule: 10/21/2026</p>"
    ((_, text, starts_on, ends_on),) = sections(html)
    assert (starts_on, ends_on) == (None, None)
    assert text == "Until further notice\nOpen Schedule: 10/21/2026"


def test_long_text_without_markers_becomes_overlapping_chunks_of_whole_lines():
    lines = [f"Line {n} " + "x" * 90 for n in range(60)]
    html = "".join(f"<p>{line}</p>" for line in lines)

    chunks = [text for _, text, _, _ in sections(html)]

    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_CHARS for chunk in chunks)
    for before, after in pairwise(chunks):
        assert after.split("\n")[0] in before.split("\n")  # overlap
    assert {line for chunk in chunks for line in chunk.split("\n")} == set(lines)


def test_marker_on_its_own_line_joins_its_heading_and_images_are_dropped():
    html = '<p>◆</p><p>Rewards</p><p><img src="calendar.png"/></p><p>Aether x5</p>'
    assert sections(html) == [("◆ Rewards", "Aether x5", None, None)]


def test_heading_without_text_is_kept_only_when_it_names_something():
    html = "<p>◆ Period: 11/11/2026 – 12/02/2026</p><p>◆ </p>"
    assert [h for h, *_ in sections(html)] == ["◆ Period: 11/11/2026 – 12/02/2026"]
