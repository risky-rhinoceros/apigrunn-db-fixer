"""Shared helpers: version 2 databases, and the markup they hold.

Every test builds its own version 2 cache with plain SQLite, so the suite needs
neither the network nor apigrunn to make one. ``tests/fixtures/`` holds the one
page captured from the real site; it belongs to the parser tests alone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import fix_cache

FIXTURES = Path(__file__).parent / "fixtures"

V2_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE pages (
    track         TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    html          TEXT NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);
"""

FETCHED_AT = "2026-09-17T08:40:31+00:00"


def card_html(video_id: str, title: str, speaker: str = "A Speaker") -> str:
    return (
        f'<a href="https://www.youtube.com/watch?v={video_id}" class="talk-card" '
        f'data-title="{title}" data-speaker="{speaker}" data-desc="About {title}.">'
        f'<div class="talk-thumb"><img src="https://img.youtube.com/vi/{video_id}/hq.jpg"/></div>'
        f"</a>"
    )


def page_html(year: int, cards: str) -> str:
    return (
        '<html><body><div class="talks-year-group">'
        f'<span class="talks-year-badge y{year}">{year}</span>'
        f'<div class="talks-lane">{cards}</div>'
        "</div></body></html>"
    )


def write_v2(path: Path, pages: dict[str, str], *, version: int = 2) -> None:
    """Create a version 2 cache holding ``{track: html}``."""
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(V2_SCHEMA)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.executemany(
            "INSERT INTO pages (track, url, html, etag, last_modified, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    track,
                    f"https://www.aigrunn.org/{track}",
                    html,
                    f'W/"{track}-etag"',
                    "Mon, 14 Sep 2026 08:01:00 GMT",
                    FETCHED_AT,
                )
                for track, html in pages.items()
            ],
        )
    conn.close()


def stored_version(path: Path) -> int | None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return fix_cache._stored_version(conn)
    finally:
        conn.close()
