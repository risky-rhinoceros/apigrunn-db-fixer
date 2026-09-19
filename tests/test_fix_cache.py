"""Tests for the version 2 -> version 3 conversion.

Every test builds its own version 2 database with plain SQLite and markup
shaped like a track page, so the suite needs neither the network nor a captured
copy of aigrunn.org.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from apigrunn import Cache

import fix_cache

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


@pytest.fixture
def v2_db(tmp_path: Path) -> Path:
    """A two-page cache: one talk on both tracks, one only on ``tech``."""
    shared = card_html("aaa111", "Shipping agents in production")
    path = tmp_path / "apigrunn.db"
    write_v2(
        path,
        {
            "tech": page_html(2025, shared + card_html("bbb222", "Sensors on the edge")),
            "business": page_html(2025, shared),
        },
    )
    return path


def stored_version(path: Path) -> int | None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return fix_cache._stored_version(conn)
    finally:
        conn.close()


def test_converts_a_v2_cache(v2_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert fix_cache.main([str(v2_db)]) == fix_cache.EXIT_OK

    cache = Cache(v2_db)  # raises CacheVersionError if the file is still v2
    try:
        talks = cache.talks()
        assert {talk.video_id for talk in talks} == {"aaa111", "bbb222"}
        assert cache.track_counts() == {"tech": 2, "business": 1}
        assert cache.years() == [2025]

        shared = cache.talk("aaa111")
        assert shared is not None
        assert shared.tracks == ["business", "tech"]
        assert shared.title == "Shipping agents in production"
        assert shared.description == "About Shipping agents in production."
        assert shared.thumbnail_url == "https://img.youtube.com/vi/aaa111/hq.jpg"
    finally:
        cache.close()

    assert "2 talks from 2 pages" in capsys.readouterr().out


def test_keeps_validators_and_fetch_times(v2_db: Path) -> None:
    """A converted cache must still look as old as it is, and refresh conditionally."""
    fix_cache.main([str(v2_db)])

    conn = sqlite3.connect(f"file:{v2_db}?mode=ro", uri=True)
    try:
        rows = dict(
            (row[0], row[1:])
            for row in conn.execute("SELECT track, etag, last_modified, fetched_at FROM pages")
        )
    finally:
        conn.close()

    assert rows["tech"] == ('W/"tech-etag"', "Mon, 14 Sep 2026 08:01:00 GMT", FETCHED_AT)
    assert rows["business"][2] == FETCHED_AT


def test_backs_the_old_cache_up(v2_db: Path) -> None:
    fix_cache.main([str(v2_db)])

    backup = v2_db.parent / "apigrunn.db.v2.bak"
    assert backup.exists()
    assert stored_version(backup) == 2

    conn = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM pages WHERE html != ''").fetchone()[0] == 2
    finally:
        conn.close()


def test_refuses_to_overwrite_an_existing_backup(v2_db: Path) -> None:
    backup = v2_db.parent / "apigrunn.db.v2.bak"
    backup.write_bytes(b"precious")

    assert fix_cache.main([str(v2_db)]) == fix_cache.EXIT_UNSUPPORTED
    assert backup.read_bytes() == b"precious"
    assert stored_version(v2_db) == 2


def test_no_backup_flag(v2_db: Path) -> None:
    assert fix_cache.main([str(v2_db), "--no-backup"]) == fix_cache.EXIT_OK
    assert not (v2_db.parent / "apigrunn.db.v2.bak").exists()
    assert stored_version(v2_db) == 3


def test_leaves_no_temporary_files(v2_db: Path) -> None:
    fix_cache.main([str(v2_db)])
    names = {path.name for path in v2_db.parent.iterdir()}
    assert names == {"apigrunn.db", "apigrunn.db.v2.bak"}


def test_already_v3_is_a_no_op(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "apigrunn.db"
    Cache(path).close()
    before = path.read_bytes()

    assert fix_cache.main([str(path)]) == fix_cache.EXIT_OK
    assert path.read_bytes() == before
    assert "already schema version 3" in capsys.readouterr().out
    assert not (tmp_path / "apigrunn.db.v2.bak").exists()


def test_running_twice_is_safe(v2_db: Path) -> None:
    assert fix_cache.main([str(v2_db)]) == fix_cache.EXIT_OK
    converted = v2_db.read_bytes()
    assert fix_cache.main([str(v2_db)]) == fix_cache.EXIT_OK
    assert v2_db.read_bytes() == converted


def test_unknown_version_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "apigrunn.db"
    write_v2(path, {"tech": page_html(2025, card_html("aaa111", "A talk"))}, version=1)
    before = path.read_bytes()

    assert fix_cache.main([str(path)]) == fix_cache.EXIT_UNSUPPORTED
    assert path.read_bytes() == before
    assert "only version 2 can be converted" in capsys.readouterr().err


def test_missing_file_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert fix_cache.main([str(tmp_path / "nope.db")]) == fix_cache.EXIT_UNSUPPORTED
    assert "no such file" in capsys.readouterr().err


def test_not_a_database_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "apigrunn.db"
    path.write_text("this is not sqlite")

    assert fix_cache.main([str(path)]) == fix_cache.EXIT_UNSUPPORTED
    assert "not a readable SQLite database" in capsys.readouterr().err


def test_unreadable_page_is_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """One page with changed markup must not cost us the other seven."""
    path = tmp_path / "apigrunn.db"
    write_v2(
        path,
        {
            "tech": page_html(2025, card_html("aaa111", "A talk")),
            # Cards the parser can see but cannot read: no href, no title.
            "business": page_html(2025, '<a class="talk-card"></a>'),
        },
    )

    assert fix_cache.main([str(path)]) == fix_cache.EXIT_OK

    cache = Cache(path)
    try:
        assert cache.track_counts() == {"tech": 1}
        # No validators for the skipped page, so the next refresh refetches it.
        assert set(cache.page_meta()) == {"tech"}
    finally:
        cache.close()

    assert "business     skipped" in capsys.readouterr().err


def test_every_page_unreadable_aborts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "apigrunn.db"
    write_v2(path, {"tech": page_html(2025, '<a class="talk-card"></a>')})
    before = path.read_bytes()

    assert fix_cache.main([str(path)]) == fix_cache.EXIT_UNPARSEABLE
    assert path.read_bytes() == before
    assert "no page could be parsed" in capsys.readouterr().err
    assert not (tmp_path / "apigrunn.db.v2.bak").exists()


def test_dry_run_writes_nothing(v2_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = v2_db.read_bytes()

    assert fix_cache.main([str(v2_db), "--dry-run"]) == fix_cache.EXIT_OK
    assert v2_db.read_bytes() == before
    assert {path.name for path in v2_db.parent.iterdir()} == {"apigrunn.db"}
    assert "would write 2 talks from 2 pages" in capsys.readouterr().out


def test_quiet_reports_only_problems(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "apigrunn.db"
    write_v2(
        path,
        {
            "tech": page_html(2025, card_html("aaa111", "A talk")),
            "business": page_html(2025, '<a class="talk-card"></a>'),
        },
    )

    assert fix_cache.main([str(path), "-q"]) == fix_cache.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "business     skipped" in captured.err


def test_empty_cache_converts_to_an_empty_v3(tmp_path: Path) -> None:
    path = tmp_path / "apigrunn.db"
    write_v2(path, {})

    assert fix_cache.main([str(path)]) == fix_cache.EXIT_OK

    cache = Cache(path)
    try:
        assert cache.is_empty()
    finally:
        cache.close()


def test_default_path_comes_from_apigrunn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "cache.db"
    write_v2(path, {"tech": page_html(2025, card_html("aaa111", "A talk"))})
    monkeypatch.setenv("APIGRUNN_CACHE", str(path))

    assert fix_cache.main([]) == fix_cache.EXIT_OK
    assert stored_version(path) == 3
