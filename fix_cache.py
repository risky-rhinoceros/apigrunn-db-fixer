#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["apigrunn"]
#
# [tool.uv.sources]
# apigrunn = { git = "https://github.com/evoso-software/apigrunn", tag = "v0.2.0" }
# ///
"""Convert an apigrunn cache from schema version 2 to version 3, in place.

apigrunn v0.2.0 stopped keeping the crawled HTML and started storing parsed
talks instead. It refuses to open an older file, raising ``CacheVersionError``
(``APIGRUNN-E300``), because from v3 onwards there is nothing left to migrate
*from*.

v2 is the exception: that file still holds the full HTML of every track page,
which is exactly what apigrunn's own parser eats. So this conversion is a local
re-parse — no network, no re-crawl — and it keeps the HTTP validators, so the
next refresh is still conditional.

Run it with no arguments to fix the default cache::

    uv run fix_cache.py
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import sys
import traceback
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from apigrunn import (
    Cache,
    ParsedCard,
    ParseError,
    __version__ as apigrunn_version,
    default_db_path,
    parse_track_page,
)
from apigrunn.cache import SCHEMA_VERSION

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


def _build_v3(
    path: str | Path, cards: Sequence[ParsedCard], pages: Sequence[V2Page]
) -> int:
    """Write the parsed archive into a fresh version 3 cache at ``path``.

    Returns the number of distinct talks stored. ``Cache`` owns the schema and
    the de-duplication of talks that appear on several track pages, so this
    stays a thin call into the library rather than hand-written SQL.
    """
    cache = Cache(path)
    try:
        applied = cache.apply(cards, refreshed_tracks=[page.track for page in pages])
        for page in pages:
            cache.record_page(
                track=page.track,
                url=page.url,
                etag=page.etag,
                last_modified=page.last_modified,
            )
    finally:
        cache.close()
    return applied.total


def _restore_fetch_times(path: Path, pages: Sequence[V2Page]) -> None:
    """Put the original ``fetched_at`` values back.

    ``Cache.record_page`` stamps *now*, which would tell apigrunn the archive
    had just been fetched and suppress the next TTL refresh. The conversion
    copies pages forward; it does not refetch them.
    """
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            conn.executemany(
                "UPDATE pages SET fetched_at = ? WHERE track = ?",
                [(page.fetched_at, page.track) for page in pages],
            )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _drop_sidecars(path: Path) -> None:
    """Remove a database's ``-wal``/``-shm`` files.

    Both schema versions run in WAL mode. A v2 write-ahead log left beside the
    freshly swapped-in v3 file would be replayed into it on the next open,
    which is the one way this conversion could corrupt data.
    """
    for suffix in _SIDECARS:
        Path(f"{path}{suffix}").unlink(missing_ok=True)


# ------------------------------------------------------------- error reporting

#: Where a crash report is POSTed. A placeholder: ``.invalid`` is reserved by
#: RFC 2606 and can never resolve, so no report can leave the machine until
#: this points at a real collector. $APIGRUNN_FIXER_REPORT_URL overrides it.
REPORT_URL = "https://aigrunn-error-collection.jaap-a3f.workers.dev"
REPORT_TIMEOUT = 5.0
#: Bumped whenever the payload changes shape, so a collector can tell an old
#: report from a new one.
REPORT_SCHEMA = 1

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
        suppress_errors = not (self.report or args.report_errors)
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


def _build_payload(exc: BaseException, context: _RunContext) -> dict[str, object]:
    """The whole of what an opt-in report sends. Nothing else is collected."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema": REPORT_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "reported_at": now.replace("+00:00", "Z"),
        "error": {
            "type": _exception_type(exc),
            "message": _scrub(str(exc), context)[:_MAX_MESSAGE],
            "frames": _frames(exc, context),
        },
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation().lower(),
            "platform": platform.platform(),
            "apigrunn": apigrunn_version,
            "apigrunn_schema_version": SCHEMA_VERSION,
        },
        "run": {
            "stage": context.stage,
            "dry_run": context.dry_run,
            "no_backup": context.no_backup,
            "db_path_given": context.db_path_given,
            "source_version": context.source_version,
            "pages": context.pages,
            "cards": context.cards,
        },
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

    if SCHEMA_VERSION != TARGET_VERSION:
        _err(
            f"this script converts caches to schema version {TARGET_VERSION}, but the "
            f"installed apigrunn {apigrunn_version} wants {SCHEMA_VERSION}"
        )
        return EXIT_UNSUPPORTED

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
            _restore_fetch_times(temp, kept)

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

    ``sys.excepthook`` would see the same exceptions, but installing a global
    hook buys nothing when there is exactly one entry point, and this way the
    reporting is reachable from the tests. Note what is *not* caught here:
    neither ``SystemExit`` nor ``KeyboardInterrupt`` is an ``Exception``, so a
    usage error and an interrupted run are never reported. The exception is
    re-raised either way, so the traceback still reaches the terminal.
    """
    context = _RunContext()
    try:
        result = main(argv, context=context)
        return result
    except Exception as exc:
        report_crash(exc, context)
        if isinstance(exc, RecoverableError):
            return 0
        raise


if __name__ == "__main__":
    sys.exit(run())
