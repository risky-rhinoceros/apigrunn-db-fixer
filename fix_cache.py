#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Convert an apigrunn cache from schema version 2 to version 3, in place.

apigrunn v0.2.0 stopped keeping the crawled HTML and started storing parsed
talks instead. It refuses to open an older file, raising ``CacheVersionError``
(``APIGRUNN-E300``), because from v3 onwards there is nothing left to migrate
*from*.

v2 is the exception: that file still holds the full HTML of every track page,
which is everything a parser needs. So this conversion is a local re-parse — no
network, no re-crawl — and it keeps the HTTP validators, so the next refresh is
still conditional.

Nothing needs installing first. The talk-card parser and the version 3 schema
are reproduced here from the standard library alone, so this runs on any Python
3.11; ``tests/test_parser.py`` holds the parser to apigrunn's own output, card
for card, against a page captured from the site.

Run it with no arguments to fix the default cache::

    python3 fix_cache.py
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sqlite3
import sys
import traceback
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

SOURCE_VERSION = 2
TARGET_VERSION = 3

EXIT_OK = 0
EXIT_ERROR = 1
# 2 is argparse's own exit code for a usage error.
EXIT_UNSUPPORTED = 3
EXIT_UNPARSEABLE = 4

#: SQLite writes these next to a database in WAL mode.
_SIDECARS = ("-wal", "-shm")


class RecoverableError(Exception):
    def __init__(self, message):
        super().__init__(message)


class V2Page(NamedTuple):
    """One row of a version 2 ``pages`` table."""

    track: str
    url: str
    html: str
    etag: str | None
    last_modified: str | None
    fetched_at: str


# ------------------------------------------------------------- cache location


def default_db_path() -> Path:
    """Where apigrunn keeps its cache, honouring ``$APIGRUNN_CACHE`` and XDG.

    Reproduced from ``apigrunn.cache``. The two have to agree: a cache found
    anywhere else is not the one that is failing to open.
    """
    override = os.environ.get("APIGRUNN_CACHE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "apigrunn" / "apigrunn.db"


# --------------------------------------------------------------------- output


def _out(message: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(message)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


# ------------------------------------------------------------------ inspection


def _stored_version(conn: sqlite3.Connection) -> int | None:
    """The version recorded in the file, or ``None`` if it does not say."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if exists is None:
        return None
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return row[0] if row else None


def _has_html_column(conn: sqlite3.Connection) -> bool:
    """True when ``pages`` still carries the crawled HTML we parse from."""
    return any(row[1] == "html" for row in conn.execute("PRAGMA table_info(pages)"))


def _read_v2_pages(conn: sqlite3.Connection) -> list[V2Page]:
    rows = conn.execute(
        "SELECT track, url, html, etag, last_modified, fetched_at FROM pages ORDER BY track"
    ).fetchall()
    return [V2Page(*row) for row in rows]


# -------------------------------------------------------------------- parsing

#: A lane's year comes from a class on its badge ("y2025"), with the badge's
#: own text as the fallback.
_YEAR_CLASS = re.compile(r"\by(\d{4})\b")
_YEAR_TEXT = re.compile(r"(\d{4})")

#: Elements that cannot contain anything, so they never go on the tag stack.
_VOID_ELEMENTS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)

#: The ``data-*`` attribute on a card, and the element it falls back to.
_CARD_FIELDS = (
    ("title", "data-title", "talk-title"),
    ("speaker", "data-speaker", "talk-speaker"),
    ("description", "data-desc", "talk-desc"),
)


class ParseError(Exception):
    """A track page was stored, but its markup could not be understood."""


@dataclass(frozen=True, slots=True)
class ParsedCard:
    """One talk as it appears on one track page."""

    video_id: str
    title: str
    speaker: str
    description: str
    url: str
    year: int | None
    thumbnail_url: str | None
    track: str


def _clean(value: str) -> str:
    """Collapse whitespace, including the non-breaking spaces the site uses."""
    return " ".join(value.replace("\xa0", " ").split())


def _youtube_video_id(url: str) -> str | None:
    """Extract the video id from a YouTube watch or short-form URL."""
    if not url:
        return None
    parts = urlparse(url)
    host = parts.netloc.lower().removeprefix("www.")
    if host == "youtu.be":
        return parts.path.lstrip("/").split("/")[0] or None
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parts.path.startswith(("/embed/", "/v/", "/shorts/")):
            return parts.path.split("/")[2] or None
        values = parse_qs(parts.query).get("v")
        return values[0] if values else None
    return None


class _TrackPageParser(HTMLParser):
    """Collects the talk cards from a track page, one tag at a time.

    A streaming parser rather than a document tree, because the page is only
    ever asked three things: which year lane are we inside, which anchor is a
    talk card, and what does that card contain. Each is answerable from a stack
    of open tags. Every piece of state below records the depth it began at and
    is closed when the stack unwinds back past it — that is what stops a stray
    end tag from leaking one card's text into the next.
    """

    def __init__(self, track: str) -> None:
        super().__init__(convert_charrefs=True)
        self.track = track
        self.cards: list[ParsedCard] = []
        #: Every ``a.talk-card`` on the page, those outside a year group
        #: included: the count that tells a changed layout from an empty page.
        self.anchors = 0

        self._stack: list[str] = []
        self._group_depth: int | None = None
        self._year: int | None = None
        self._badge_depth: int | None = None
        self._badge_seen = False
        self._badge_text: list[str] = []
        self._card: dict[str, str | None] | None = None
        self._card_depth: int | None = None
        self._thumb_depth: int | None = None
        self._thumbnail: str | None = None
        self._have_thumbnail = False
        self._text_key: str | None = None
        self._text_depth: int | None = None
        self._text: list[str] = []
        self._texts: dict[str, str] = {}

    # ------------------------------------------------------------ the tags

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        depth = len(self._stack)
        if tag not in _VOID_ELEMENTS:
            self._stack.append(tag)

        if tag == "div" and "talks-year-group" in classes:
            self._group_depth, self._year, self._badge_seen = depth, None, False
        elif (
            self._group_depth is not None
            and not self._badge_seen
            and tag == "span"
            and "talks-year-badge" in classes
        ):
            self._open_badge(attributes, depth)
        elif tag == "a" and "talk-card" in classes:
            self.anchors += 1
            if self._group_depth is not None:
                self._open_card(attributes, depth)
        elif self._card is not None:
            self._open_card_part(tag, attributes, classes, depth)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # "<div/>" does not close anything in HTML, but the site's inline SVG
        # icons use the XML spelling, so open and close it in one go.
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_ELEMENTS:
            self._stack.pop()
            self._unwind_to(len(self._stack))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_ELEMENTS or tag not in self._stack:
            # An end tag for something never opened says nothing about where we
            # are. A browser drops it on the floor, and so do we.
            return
        while self._stack:
            popped = self._stack.pop()
            self._unwind_to(len(self._stack))
            if popped == tag:
                return

    def handle_data(self, data: str) -> None:
        if self._text_depth is not None:
            self._text.append(data)
        elif self._badge_depth is not None:
            self._badge_text.append(data)

    def close(self) -> None:
        """Finish the page, closing whatever its markup left open."""
        super().close()
        while self._stack:
            self._stack.pop()
            self._unwind_to(len(self._stack))

    def _unwind_to(self, depth: int) -> None:
        """Close everything that ended when the stack shrank to ``depth``."""
        if self._text_depth is not None and depth <= self._text_depth:
            self._texts[self._text_key] = _clean(" ".join(self._text))
            self._text_key = self._text_depth = None
        if self._thumb_depth is not None and depth <= self._thumb_depth:
            self._thumb_depth = None
        if self._badge_depth is not None and depth <= self._badge_depth:
            self._close_badge()
        if self._card_depth is not None and depth <= self._card_depth:
            self._close_card()
        if self._group_depth is not None and depth <= self._group_depth:
            self._group_depth = self._year = None

    # ---------------------------------------------------------- the pieces

    def _open_badge(self, attributes: dict[str, str | None], depth: int) -> None:
        self._badge_seen = True
        self._badge_depth, self._badge_text = depth, []
        for class_name in (attributes.get("class") or "").split():
            match = _YEAR_CLASS.fullmatch(class_name)
            if match:
                self._year = int(match.group(1))

    def _close_badge(self) -> None:
        if self._year is None:
            match = _YEAR_TEXT.search("".join(t.strip() for t in self._badge_text))
            self._year = int(match.group(1)) if match else None
        self._badge_depth = None

    def _open_card(self, attributes: dict[str, str | None], depth: int) -> None:
        self._card, self._card_depth = attributes, depth
        self._texts = {}
        self._thumbnail, self._have_thumbnail = None, False

    def _open_card_part(
        self,
        tag: str,
        attributes: dict[str, str | None],
        classes: set[str],
        depth: int,
    ) -> None:
        if tag == "div" and "talk-thumb" in classes:
            self._thumb_depth = depth
        elif (
            tag == "img" and self._thumb_depth is not None and not self._have_thumbnail
        ):
            # The first thumbnail wins, even if it turns out to have no source.
            self._have_thumbnail = True
            self._thumbnail = (attributes.get("src") or "").strip() or None
        elif tag == "div":
            for key, _, class_name in _CARD_FIELDS:
                if class_name in classes and key not in self._texts:
                    self._text_key, self._text_depth, self._text = key, depth, []
                    return

    def _close_card(self) -> None:
        attributes, self._card, self._card_depth = self._card, None, None
        url = (attributes.get("href") or "").strip()
        video_id = _youtube_video_id(url)
        fields = {
            key: self._card_field(attributes, attribute, key)
            for key, attribute, _ in _CARD_FIELDS
        }

        if not video_id or not fields["title"]:
            # No video or no title: whatever this is, it is not a talk.
            return

        self.cards.append(
            ParsedCard(
                video_id=video_id,
                url=url,
                year=self._year,
                thumbnail_url=self._thumbnail,
                track=self.track,
                **fields,
            )
        )

    def _card_field(
        self, attributes: dict[str, str | None], attribute: str, key: str
    ) -> str:
        """Read a ``data-*`` attribute, falling back to the rendered element.

        The fallback applies only when the attribute is *absent*. One that is
        present but empty is taken at face value: a card carrying
        ``data-speaker=""`` sits next to a ``<div class="talk-speaker">aiGrunn
        2024</div>`` label, which is a year, not a speaker.
        """
        if attribute in attributes:
            return _clean(attributes[attribute] or "")
        return self._texts.get(key, "")


def parse_track_page(html: str, track_slug: str) -> list[ParsedCard]:
    """Parse every talk card on a track page.

    A single unreadable card is skipped: a page that is mostly intact is worth
    more than a clean failure. :class:`ParseError` is raised only when the page
    plainly holds talk cards and none of them could be read, which is the
    signal that the site's markup moved out from under this parser.
    """
    parser = _TrackPageParser(track_slug)
    parser.feed(html)
    parser.close()

    cards: list[ParsedCard] = []
    seen: set[str] = set()
    for card in parser.cards:
        # The same talk can be listed twice on one page; the first wins.
        if card.video_id not in seen:
            seen.add(card.video_id)
            cards.append(card)

    if not cards and (parser.anchors or "talk-card" in html):
        raise ParseError(
            f"/{track_slug}: found {parser.anchors} talk cards but could not extract "
            "any; the aigrunn.org markup has probably changed"
        )
    return cards


def _parse_pages(
    pages: Sequence[V2Page], *, quiet: bool
) -> tuple[list[ParsedCard], list[V2Page]]:
    """Parse the stored HTML, dropping pages whose markup cannot be read.

    A page that fails is skipped whole — cards *and* validators — so that the
    next refresh refetches it instead of trusting an ``ETag`` for content the
    cache never managed to store.
    """
    cards: list[ParsedCard] = []
    kept: list[V2Page] = []

    for page in pages:
        try:
            parsed = parse_track_page(page.html, page.track)
        except ParseError as exc:
            _err(f"  {page.track:<12} skipped: {exc}")
            continue
        _out(f"  {page.track:<12} {len(parsed):>4} cards", quiet=quiet)
        cards.extend(parsed)
        kept.append(page)

    return cards, kept


# -------------------------------------------------------------------- writing


#: The version 3 schema, copied verbatim from ``apigrunn.cache``. apigrunn
#: reads this file without migrating it, so this is the one place where the
#: script has to agree with the library exactly.
_V3_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE pages (
    track         TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE talks (
    video_id      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    speaker       TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL,
    year          INTEGER,
    thumbnail_url TEXT
);

CREATE TABLE talk_tracks (
    video_id TEXT NOT NULL REFERENCES talks(video_id) ON DELETE CASCADE,
    track    TEXT NOT NULL,
    PRIMARY KEY (video_id, track)
);

CREATE INDEX idx_talks_year ON talks(year);
CREATE INDEX idx_talk_tracks_track ON talk_tracks(track);
"""


def _dedupe(cards: Sequence[ParsedCard]) -> list[ParsedCard]:
    """Collapse the same talk, seen on several track pages, into one row."""
    best: dict[str, ParsedCard] = {}
    for card in cards:
        current = best.get(card.video_id)
        # Prefer a card that knows the year; only the lane headers carry it.
        if current is None or (current.year is None and card.year is not None):
            best[card.video_id] = card
    return list(best.values())


def _build_v3(
    path: str | Path, cards: Sequence[ParsedCard], pages: Sequence[V2Page]
) -> int:
    """Write the parsed archive into a fresh version 3 cache at ``path``.

    Returns the number of distinct talks stored. The file is new, which makes
    this simpler than the library's own incremental write: nothing to update,
    nothing to prune, and each page's original ``fetched_at`` goes straight in.
    A cache that was copied forward has not been refetched and must not claim
    it has, or the next TTL refresh is suppressed.
    """
    talks = _dedupe(cards)

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_V3_SCHEMA)

        with conn:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (TARGET_VERSION,)
            )
            conn.executemany(
                "INSERT INTO talks (video_id, title, description, speaker, url, year, "
                "thumbnail_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        talk.video_id,
                        talk.title,
                        talk.description,
                        talk.speaker,
                        talk.url,
                        talk.year,
                        talk.thumbnail_url,
                    )
                    for talk in talks
                ],
            )
            # Every card, not every talk: a talk listed on three track pages
            # is one row in ``talks`` and three here.
            conn.executemany(
                "INSERT OR IGNORE INTO talk_tracks (video_id, track) VALUES (?, ?)",
                [(card.video_id, card.track) for card in cards],
            )
            conn.executemany(
                "INSERT INTO pages (track, url, etag, last_modified, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        page.track,
                        page.url,
                        page.etag,
                        page.last_modified,
                        page.fetched_at,
                    )
                    for page in pages
                ],
            )

        # Fold the write-ahead log back into the file: what happens to it next
        # is a rename, which would leave the log behind.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    return len(talks)


def _drop_sidecars(path: Path) -> None:
    """Remove a database's ``-wal``/``-shm`` files.

    Both schema versions run in WAL mode. A v2 write-ahead log left beside the
    freshly swapped-in v3 file would be replayed into it on the next open,
    which is the one way this conversion could corrupt data.
    """
    for suffix in _SIDECARS:
        Path(f"{path}{suffix}").unlink(missing_ok=True)


# ------------------------------------------------------------- error reporting

#: Where a crash report is POSTed, if — and only if — the user opted in.
#: $APIGRUNN_FIXER_REPORT_URL overrides it, to collect reports elsewhere.
REPORT_URL = "https://aigrunn-error-collection.jaap-a3f.workers.dev"
REPORT_TIMEOUT = 5.0
#: Bumped whenever the payload changes shape, so a collector can tell an old
#: report from a new one. 3 added the environment snapshot and conversion
#: summaries alongside crash reports.
REPORT_SCHEMA = 3

TOOL_NAME = "apigrunn-db-fixer"
#: Keep in step with pyproject.toml's version.
TOOL_VERSION = "0.1.0"

_MAX_FRAMES = 20
_MAX_MESSAGE = 500
_MAX_CODE = 200

_SCRIPT_DIR = Path(__file__).resolve().parent


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class _RunContext:
    """What a crash report can say about the run that crashed.

    Filled in as :func:`main` progresses, so a report can say *where* the
    conversion died and how much of the cache it had seen by then. Only what
    :func:`_build_payload` reads is ever sent; ``db_path`` is kept here purely
    so it can be scrubbed back out of messages and tracebacks.
    """

    report: bool = field(
        default_factory=lambda: _env_flag("APIGRUNN_FIXER_ERROR_REPORTING")
    )
    show_report: bool = False
    quiet: bool = False
    stage: str = "startup"
    db_path: Path | None = None
    db_path_given: bool = False
    dry_run: bool = False
    no_backup: bool = False
    source_version: int | None = None
    pages: int | None = None
    cards: int | None = None

    def note_args(self, args: argparse.Namespace) -> None:
        # The flag can only turn reporting on; it never turns off an opt-in
        # that came from the environment.
        self.report = self.report or args.report_errors
        self.show_report = args.show_report
        self.quiet = args.quiet
        self.dry_run = args.dry_run
        self.no_backup = args.no_backup
        self.db_path_given = args.db_path is not None


def _report_url() -> str:
    return os.environ.get("APIGRUNN_FIXER_REPORT_URL") or REPORT_URL


def _scrub(text: str, context: _RunContext) -> str:
    """Strip the local filesystem out of a string bound for the endpoint.

    The failures worth hearing about — a sqlite error, a refused
    ``os.replace()`` — name paths in their message, and those paths carry the
    user's login name and the shape of their home directory. Neither says
    anything about the bug.
    """
    if context.db_path is not None:
        # Longest first: the temporary and backup files are the db path plus a
        # suffix, so replacing that prefix covers them too.
        text = text.replace(str(context.db_path), "<db>")
        text = text.replace(str(context.db_path.parent), "<cache-dir>")
    home = str(Path.home())
    if home not in ("", "/"):  # a root home would swallow every path there is
        text = text.replace(home, "~")
    return text


def _frame_file(filename: str) -> str:
    """Name a traceback frame's file without shipping the install prefix."""
    path = Path(filename)
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - a frame with no real file
        return path.name
    if resolved.parent == _SCRIPT_DIR:
        return resolved.name
    # Library and stdlib code: two components is enough to find the file.
    return "/".join(resolved.parts[-2:])


def _frames(exc: BaseException, context: _RunContext) -> list[dict[str, object]]:
    summaries = traceback.extract_tb(exc.__traceback__)[-_MAX_FRAMES:]
    return [
        {
            "file": _frame_file(frame.filename),
            "line": frame.lineno,
            "func": frame.name,
            "code": _scrub(frame.line, context)[:_MAX_CODE] if frame.line else None,
        }
        for frame in summaries
    ]


def _exception_type(exc: BaseException) -> str:
    cls = type(exc)
    if cls.__module__ in ("builtins", "__main__"):
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


#: Words that mark an environment value as a secret. Redacted before a report
#: leaves the machine. Word-bounded so a value like ``TOKEN`` is caught without
#: clobbering every variable that merely contains the word.
_SECRET_WORDS = re.compile(r"\b(key|secret|password|token|credential|auth)\b", re.IGNORECASE)


def _redact(name: str, value: str, context: _RunContext) -> str:
    """Blank a secret-looking value, otherwise scrub local paths out of it."""
    if _SECRET_WORDS.search(name):
        return "<redacted>"
    return _scrub(value, context)


def _environment_variables(context: _RunContext) -> dict[str, str]:
    """The apigrunn-related variables that shaped this run, for reproduction.

    Everything the tool reads from the environment is prefixed ``APIGRUNN``;
    shipping that prefix lets a report be reproduced against the same
    configuration. Secrets are redacted and local paths scrubbed first.
    """
    return {
        name: _redact(name, value, context)
        for name, value in os.environ.items()
        if name.startswith("APIGRUNN")
    }


def _environment(context: _RunContext) -> dict[str, object]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation().lower(),
        "platform": platform.platform(),
        "target_schema": TARGET_VERSION,
        "variables": _environment_variables(context),
    }


def _run(context: _RunContext) -> dict[str, object]:
    return {
        "stage": context.stage,
        "dry_run": context.dry_run,
        "no_backup": context.no_backup,
        "db_path_given": context.db_path_given,
        "source_version": context.source_version,
        "pages": context.pages,
        "cards": context.cards,
    }


def _report_header() -> dict[str, object]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema": REPORT_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "reported_at": now.replace("+00:00", "Z"),
    }


def _build_payload(exc: BaseException, context: _RunContext) -> dict[str, object]:
    """The whole of what an opt-in error report sends."""
    return {
        **_report_header(),
        "error": {
            "type": _exception_type(exc),
            "message": _scrub(str(exc), context)[:_MAX_MESSAGE],
            "frames": _frames(exc, context),
        },
        "environment": _environment(context),
        "run": _run(context),
    }


def _build_summary(context: _RunContext) -> dict[str, object]:
    """The whole of what a conversion summary sends."""
    return {
        **_report_header(),
        "result": "ok",
        "environment": _environment(context),
        "run": _run(context),
    }


def _send_report(payload: dict[str, object]) -> None:
    request = urllib.request.Request(
        _report_url(),
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"{TOOL_NAME}/{TOOL_VERSION}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REPORT_TIMEOUT):
        pass


def report_crash(exc: BaseException, context: _RunContext) -> None:
    """Report an unexpected error, if the user asked for that.

    Opt-in and best-effort: whatever happens in here, the original traceback
    still reaches the terminal and the exit code is the one the crash earned.
    Hence every step is guarded — a reporter that raised would be a second bug
    hiding the first.
    """
    if not (context.report or context.show_report):
        return

    try:
        payload = _build_payload(exc, context)
    except Exception as failure:  # pragma: no cover - defensive
        _err(f"could not build an error report: {failure}")
        return

    if context.show_report:
        _err(json.dumps(payload, indent=2))
        return

    try:
        _send_report(payload)
    except Exception as failure:
        _err(f"could not send the error report: {failure}")
    else:
        _err(f"error report sent to {_report_url()}")


def report_summary(context: _RunContext) -> None:
    """Send a summary of a successful conversion.

    Unlike crash reporting this is not opt-in: a one-line "this still works"
    ping tells the maintainer the tool is alive and costs the user nothing. It
    is deliberately quiet — a ping that cannot be delivered is dropped, and the
    outcome is never changed.
    """
    try:
        payload = _build_summary(context)
    except Exception:  # pragma: no cover - defensive
        return

    try:
        _send_report(payload)
    except Exception:
        pass


# ----------------------------------------------------------------------- main


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fix_cache.py",
        description=(
            "Convert an apigrunn cache from schema version 2 to version 3 in place, "
            "by re-parsing the HTML the old cache still holds. Fixes APIGRUNN-E300."
        ),
    )
    parser.add_argument(
        "db_path",
        nargs="?",
        help="cache to convert (default: apigrunn's own, honouring $APIGRUNN_CACHE)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen, write nothing",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="do not keep a copy of the version 2 file",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="only report problems"
    )
    parser.add_argument(
        "--report-errors",
        action="store_true",
        help=(
            "on an unexpected error, send an anonymised report of it and of this "
            "machine's Python to the maintainers (off by default)"
        ),
    )
    parser.add_argument(
        "--show-report",
        action="store_true",
        help="print such a report instead of sending it, to see what it contains",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, context: _RunContext | None = None) -> int:
    args = _parse_args(argv)
    quiet = args.quiet
    context = _RunContext() if context is None else context
    context.note_args(args)

    path = Path(args.db_path).expanduser() if args.db_path else default_db_path()
    context.db_path = path
    context.stage = "inspect"
    if not path.exists():
        _err(f"{path}: no such file — there is no cache to convert")
        return EXIT_UNSUPPORTED

    try:
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - unreadable file
        _err(f"{path}: cannot be opened ({exc})")
        return EXIT_UNSUPPORTED

    try:
        try:
            version = _stored_version(source)
            context.source_version = version
            convertible = version == SOURCE_VERSION and _has_html_column(source)
        except sqlite3.DatabaseError as exc:
            _err(f"{path}: not a readable SQLite database ({exc})")
            return EXIT_UNSUPPORTED

        if version == TARGET_VERSION:
            _out(
                f"{path} is already schema version {TARGET_VERSION} — nothing to do",
                quiet=quiet,
            )
            return EXIT_OK

        if not convertible:
            found = (
                f"schema version {version}"
                if version is not None
                else "no schema version"
            )
            _err(
                f"{path}: {found}; only version {SOURCE_VERSION} can be converted, because "
                f"only version {SOURCE_VERSION} still stores the pages to re-parse.\n"
                f"Delete the file and refresh to rebuild it from aigrunn.org."
            )
            return EXIT_UNSUPPORTED

        context.stage = "read_pages"
        pages = _read_v2_pages(source)
        context.pages = len(pages)

        context.stage = "parse"
        _out(f"converting {path}  v{SOURCE_VERSION} -> v{TARGET_VERSION}", quiet=quiet)
        cards, kept = _parse_pages(pages, quiet=quiet)
        context.cards = len(cards)

        if pages and not kept:
            _err("no page could be parsed; leaving the cache alone")
            return EXIT_UNPARSEABLE

        if args.dry_run:
            context.stage = "build_v3"
            talks = _build_v3(":memory:", cards, kept)
            _out(
                f"dry run: would write {talks} talks from {len(kept)} pages; "
                f"{path} left untouched",
                quiet=quiet,
            )
            return EXIT_OK

        backup = (
            None
            if args.no_backup
            else path.parent / f"{path.name}.v{SOURCE_VERSION}.bak"
        )
        if backup is not None and backup.exists():
            _err(
                f"{backup} already exists; move it aside or pass --no-backup so an "
                f"existing copy is not overwritten"
            )
            return EXIT_UNSUPPORTED

        temp = path.parent / f"{path.name}.v{TARGET_VERSION}-{os.getpid()}.tmp"
        _drop_sidecars(temp)
        temp.unlink(missing_ok=True)

        try:
            context.stage = "build_v3"
            talks = _build_v3(temp, cards, kept)

            if backup is not None:
                context.stage = "backup"
                # sqlite's own backup rather than a file copy: it is consistent
                # and folds in anything still sitting in the source's WAL.
                target = sqlite3.connect(str(backup))
                try:
                    source.backup(target)
                finally:
                    target.close()

            context.stage = "swap"
            source.close()
            os.replace(temp, path)
            _drop_sidecars(path)
        except BaseException:
            # Nothing has replaced the original yet, so clean up after
            # ourselves and leave the version 2 cache exactly as it was.
            _drop_sidecars(temp)
            temp.unlink(missing_ok=True)
            if backup is not None:
                backup.unlink(missing_ok=True)
            raise
    finally:
        source.close()

    _out(f"{talks} talks from {len(kept)} pages", quiet=quiet)
    if backup is not None:
        _out(f"version {SOURCE_VERSION} cache backed up to {backup}", quiet=quiet)

    return EXIT_OK


def run(argv: Sequence[str] | None = None) -> int:
    """Run :func:`main`, reporting an unexpected error before it propagates.

    A successful conversion is followed by a quiet usage ping — see
    :func:`report_summary`. ``sys.excepthook`` would see the same exceptions,
    but installing a global hook buys nothing when there is exactly one entry
    point, and this way the reporting is reachable from the tests. Note what is
    *not* caught here: neither ``SystemExit`` nor ``KeyboardInterrupt`` is an
    ``Exception``, so a usage error and an interrupted run are never reported.
    The exception is re-raised either way, so the traceback still reaches the
    terminal.
    """
    context = _RunContext()
    try:
        result = main(argv, context=context)
        if result == EXIT_OK:
            report_summary(context)
        return result
    except Exception as exc:
        report_crash(exc, context)
        if isinstance(exc, RecoverableError):
            return 0
        raise


if __name__ == "__main__":
    sys.exit(run())
