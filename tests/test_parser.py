"""Tests for the talk-card parser.

The script used to import ``parse_track_page`` from apigrunn, which pulled in
BeautifulSoup and with it the whole dependency tree. The parser here reads the
same markup out of ``html.parser`` instead, so the tests do two things: pin its
behaviour against a page captured from the real site, and — whenever apigrunn
is installed — hold it to the library's own output card for card. That second
test is what makes the reimplementation honest.
"""

from __future__ import annotations

import pytest

from conftest import FIXTURES, card_html, page_html

import fix_cache

FIELDS = ("video_id", "title", "speaker", "description", "url", "year", "thumbnail_url")


@pytest.fixture(scope="module")
def healthcare() -> str:
    """The archive of aigrunn.org/healthcare, as it was actually served."""
    return (FIXTURES / "healthcare.html").read_text()


def fields(card: fix_cache.ParsedCard) -> tuple[object, ...]:
    return tuple(getattr(card, name) for name in FIELDS)


# ------------------------------------------------------------- the real page


def test_reads_a_real_track_page(healthcare: str) -> None:
    cards = fix_cache.parse_track_page(healthcare, "healthcare")

    assert [(card.video_id, card.year) for card in cards] == [
        ("ujzx9UGWDcI", 2025),
        ("DaJj7jZh0Xw", 2025),
        ("lzIjf0la28k", 2024),
        ("xtJr7hS8N7w", 2024),
        ("32pPkQVqOfM", 2024),
        ("BWxTxL-m7qY", 2023),
    ]
    assert all(card.track == "healthcare" for card in cards)


def test_reads_every_field_of_a_real_card(healthcare: str) -> None:
    card = fix_cache.parse_track_page(healthcare, "healthcare")[-1]

    assert card.title == "Fighting cancer with AI"
    assert card.speaker == "Hylke Donker"
    assert card.description.startswith("AI is revolutionising how we do science.")
    assert card.url == "https://www.youtube.com/watch?v=BWxTxL-m7qY"
    assert card.year == 2023
    assert card.thumbnail_url == "https://img.youtube.com/vi/BWxTxL-m7qY/mqdefault.jpg"


def test_matches_apigrunn_on_a_real_page(healthcare: str) -> None:
    """The library is the specification; this parser has to agree with it."""
    apigrunn = pytest.importorskip("apigrunn", reason="the oracle for the markup")

    theirs = apigrunn.parse_track_page(healthcare, "healthcare")
    ours = fix_cache.parse_track_page(healthcare, "healthcare")

    assert [fields(card) for card in ours] == [fields(card) for card in theirs]


# ----------------------------------------------------------- what it extracts


def test_the_year_comes_from_the_badge_class() -> None:
    html = page_html(2025, card_html("aaa111", "A talk"))
    assert fix_cache.parse_track_page(html, "tech")[0].year == 2025


def test_the_year_falls_back_to_the_badge_text() -> None:
    html = (
        '<div class="talks-year-group"><span class="talks-year-badge">2021 talks</span>'
        + card_html("aaa111", "A talk")
        + "</div>"
    )
    assert fix_cache.parse_track_page(html, "tech")[0].year == 2021


def test_a_card_outside_a_year_group_has_no_year_and_is_not_kept() -> None:
    """Such a card is counted, though — which is what makes this a ParseError."""
    html = "<html><body>" + card_html("aaa111", "A talk") + "</body></html>"
    with pytest.raises(fix_cache.ParseError, match="found 1 talk cards"):
        fix_cache.parse_track_page(html, "tech")


def test_text_elements_stand_in_for_missing_attributes() -> None:
    html = page_html(
        2024,
        '<a href="https://youtu.be/bbb222" class="talk-card">'
        '<div class="talk-info"><div class="talk-title">Rendered title</div>'
        '<div class="talk-speaker">A Speaker</div></div></a>',
    )
    card = fix_cache.parse_track_page(html, "tech")[0]

    assert (card.video_id, card.title, card.speaker) == (
        "bbb222",
        "Rendered title",
        "A Speaker",
    )


def test_an_empty_attribute_is_taken_at_face_value() -> None:
    """``data-speaker=""`` beats a ``talk-speaker`` label that holds a year."""
    html = page_html(
        2024,
        '<a href="https://youtu.be/bbb222" class="talk-card" data-title="T" '
        'data-speaker=""><div class="talk-speaker">aiGrunn 2024</div></a>',
    )
    assert fix_cache.parse_track_page(html, "tech")[0].speaker == ""


def test_entities_and_non_breaking_spaces_are_resolved() -> None:
    html = page_html(
        2024,
        '<a href="https://youtu.be/bbb222" class="talk-card" '
        'data-title="Tools&nbsp;&amp;&nbsp;Agents"></a>',
    )
    assert fix_cache.parse_track_page(html, "tech")[0].title == "Tools & Agents"


def test_a_talk_listed_twice_on_a_page_is_kept_once() -> None:
    html = page_html(2025, card_html("aaa111", "First") + card_html("aaa111", "Second"))
    cards = fix_cache.parse_track_page(html, "tech")

    assert [card.title for card in cards] == ["First"]


def test_a_card_without_a_video_or_a_title_is_skipped() -> None:
    html = page_html(
        2025,
        card_html("aaa111", "A talk")
        + '<a href="https://example.com/not-youtube" class="talk-card" data-title="T"></a>'
        + '<a href="https://youtu.be/ccc333" class="talk-card"></a>',
    )
    assert [card.video_id for card in fix_cache.parse_track_page(html, "tech")] == [
        "aaa111"
    ]


# -------------------------------------------------------------- what it rejects


def test_a_page_with_no_archive_yields_nothing() -> None:
    assert fix_cache.parse_track_page("<html><body>Hi</body></html>", "tech") == []


def test_cards_that_cannot_be_read_are_a_parse_error() -> None:
    html = page_html(2025, '<a class="talk-card"></a>')
    with pytest.raises(fix_cache.ParseError, match="markup has probably changed"):
        fix_cache.parse_track_page(html, "tech")


# ------------------------------------------------------------- broken markup


def test_a_stray_end_tag_does_not_derail_the_page() -> None:
    html = page_html(2025, "</div></span>" + card_html("aaa111", "A talk"))
    assert len(fix_cache.parse_track_page(html, "tech")) == 1


def test_a_page_that_stops_mid_card_still_yields_it(healthcare: str) -> None:
    """A truncated page is closed at the end, the way a browser closes one."""
    cut = healthcare[: healthcare.find('data-title="Private LLMs')]
    cards = fix_cache.parse_track_page(cut, "healthcare")

    assert [card.video_id for card in cards] == ["ujzx9UGWDcI"]
