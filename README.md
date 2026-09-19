# apigrunn-db-fixer

Fixes this:

```
error: [APIGRUNN-E300] ~/.cache/apigrunn/apigrunn.db was written by apigrunn
schema version 2, but this version requires 3. The cache cannot be migrated.
```

One command, no re-crawl:

```bash
uv run fix_cache.py
```

## Why this works

[apigrunn](https://github.com/evoso-software/apigrunn) v0.2.0 changed what the
cache stores:

| | v2 (`v0.1.0`) | v3 (`v0.2.0`) |
| --- | --- | --- |
| `pages` | track, url, **html**, etag, last_modified, fetched_at | track, url, etag, last_modified, fetched_at |
| talks | parsed out of the stored HTML per query | `talks` + `talk_tracks` rows, written at refresh |
| on mismatch | tables dropped, cache rebuilt | `CacheVersionError` — `APIGRUNN-E300` |

apigrunn says the cache "cannot be migrated", and from v3 onwards that is true:
v3 no longer keeps the pages, so there is nothing to re-derive from.

v2 is the exception. That file still holds the complete HTML of every track
page, which is precisely what apigrunn's own parser consumes. So this script
re-parses what is already on disk and writes the v3 tables — offline, from your
own cache, with no request to aigrunn.org.

It imports `parse_track_page()` and `Cache` from apigrunn v0.2.0 rather than
reimplementing them, so the schema and the scraping stay owned by the library.

## Usage

```
uv run fix_cache.py [DB_PATH] [--dry-run] [--no-backup] [-q]
                    [--report-errors] [--show-report]
```

`DB_PATH` defaults to apigrunn's own cache location, honouring `$APIGRUNN_CACHE`
and `$XDG_CACHE_HOME`.

| Option | Effect |
| --- | --- |
| `--dry-run` | report what would happen, write nothing |
| `--no-backup` | skip the copy of the old file |
| `-q`, `--quiet` | only report problems |
| `--report-errors` | on an unexpected error, send a crash report (off by default) |
| `--show-report` | print such a report instead of sending it |

```console
$ uv run fix_cache.py --dry-run
converting /home/you/.cache/apigrunn/apigrunn.db  v2 -> v3
  tech           66 cards
  business       19 cards
  ...
dry run: would write 66 talks from 8 pages; /home/you/.cache/apigrunn/apigrunn.db left untouched
```

Nothing needs to be installed first: the script carries a
[PEP 723](https://peps.python.org/pep-0723/) header, so `uv run` resolves
apigrunn v0.2.0 into a throwaway environment of its own. Running it twice is
safe — a cache that is already v3 is left alone.

### What it preserves

- every talk, with its description, speaker, year and thumbnail
- track tags, including talks that appear on several tracks
- the `ETag` and `Last-Modified` of each page, so the next refresh is still conditional
- each page's original `fetched_at`, so your TTL keeps telling the truth

### What it cannot do

- **convert anything but v2.** Any other version is refused untouched; delete
  the file and refresh to rebuild it from aigrunn.org.
- **recover a page whose markup it cannot read.** Such a page is skipped whole
  — talks *and* validators — so the next refresh refetches it. If no page at
  all can be parsed, the script aborts and leaves your cache as it was.

### Safety

The original is opened read-only. The new database is built beside it under a
temporary name and only then swapped in with an atomic `os.replace()`, so an
interrupted run leaves the v2 file intact. Unless you pass `--no-backup`, a
consistent copy is kept at `<cache>.v2.bak`; the script refuses to overwrite one
that already exists.

## Error reporting

Off unless you ask for it. If the script crashes — an unexpected error, not a
cache it refuses or a page it cannot parse — and only then, it can POST a
description of the crash to the maintainers:

```bash
uv run fix_cache.py --report-errors
# or, for a scripted run:
APIGRUNN_FIXER_ERROR_REPORTING=1 uv run fix_cache.py
```

`--show-report` prints the report to stderr and sends nothing, so you can see
exactly what would leave the machine before you turn this on. It looks like
this:

```json
{
  "schema": 1,
  "tool": "apigrunn-db-fixer",
  "tool_version": "0.1.0",
  "reported_at": "2026-09-18T11:02:44Z",
  "error": {
    "type": "sqlite3.OperationalError",
    "message": "attempt to write a readonly database: <db>.v3-4131.tmp",
    "frames": [{"file": "fix_cache.py", "line": 517, "func": "main", "code": "talks = _build_v3(temp, cards, kept)"}]
  },
  "environment": {
    "python": "3.13.15",
    "implementation": "cpython",
    "platform": "Linux-6.18.45-x86_64-with-glibc2.40",
    "apigrunn": "0.2.0",
    "apigrunn_schema_version": 3
  },
  "run": {
    "stage": "build_v3",
    "dry_run": false,
    "no_backup": false,
    "db_path_given": false,
    "source_version": 2,
    "pages": 8,
    "cards": 66
  }
}
```

That is the whole payload. It carries no argv, no cache path, no page HTML and
no talk titles; paths in the error message and the traceback are rewritten to
`<db>`, `<cache-dir>` and `~` first, and the tests assert that your home
directory cannot appear in a report.

Reporting is best-effort and never changes the outcome: an endpoint that is
down or slow costs one line on stderr and a five-second timeout, and the
original traceback and exit code reach you unchanged either way.

The endpoint in `fix_cache.py` is still a placeholder — `.invalid` is reserved
and cannot resolve, so nothing goes anywhere until `REPORT_URL` points at a
real collector. `$APIGRUNN_FIXER_REPORT_URL` overrides it if you want to run
one of your own.

## Development

```bash
nix develop -c uv run pytest -q
```

The tests build their own v2 databases from synthetic track-page markup, so
they need neither the network nor a captured copy of aigrunn.org.

`pyproject.toml` exists only for the test environment. The apigrunn pin that
matters is the one in `fix_cache.py`'s PEP 723 header — that is what users run.
Bump both together.
