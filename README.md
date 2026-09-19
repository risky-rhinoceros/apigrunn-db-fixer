# apigrunn-db-fixer

Fixes this:

```
error: [APIGRUNN-E300] ~/.cache/apigrunn/apigrunn.db was written by apigrunn
schema version 2, but this version requires 3. The cache cannot be migrated.
```

One command, no re-crawl, nothing to install:

```bash
python3 fix_cache.py
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

The script has no dependencies — not even apigrunn. It carries its own copy of
the card parser and of the version 3 schema, reading the markup with
`html.parser` from the standard library, so a broken cache can be repaired with
whatever Python is already on the machine. What keeps those copies honest is the
test suite: it parses a page captured from aigrunn.org with both this parser and
apigrunn's, and opens every converted database with the real `Cache`.

## Usage

```
python3 fix_cache.py [DB_PATH] [--dry-run] [--no-backup] [-q]
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
$ python3 fix_cache.py --dry-run
converting /home/you/.cache/apigrunn/apigrunn.db  v2 -> v3
  tech           66 cards
  business       19 cards
  ...
dry run: would write 66 talks from 8 pages; /home/you/.cache/apigrunn/apigrunn.db left untouched
```

Any Python 3.11 will do, and `uv run fix_cache.py` works too — the
[PEP 723](https://peps.python.org/pep-0723/) header declares no dependencies, so
there is nothing to resolve. Running it twice is safe: a cache that is already
v3 is left alone.

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

Crash reporting is off unless you ask for it. If the script crashes — an
unexpected error, not a cache it refuses or a page it cannot parse — and only
then, it can POST a description of the crash to the maintainers:

```bash
python3 fix_cache.py --report-errors
# or, for a scripted run:
APIGRUNN_FIXER_ERROR_REPORTING=1 python3 fix_cache.py
```

`--show-report` prints the report to stderr and sends nothing, so you can see
exactly what would leave the machine before you turn this on. It looks like
this:

```json
{
  "schema": 3,
  "tool": "apigrunn-db-fixer",
  "tool_version": "0.1.0",
  "reported_at": "2026-09-18T11:02:44Z",
  "error": {
    "type": "sqlite3.OperationalError",
    "message": "attempt to write a readonly database: <db>.v3-4131.tmp",
    "frames": [{"file": "fix_cache.py", "line": 888, "func": "main", "code": "talks = _build_v3(temp, cards, kept)"}]
  },
  "environment": {
    "python": "3.13.15",
    "implementation": "cpython",
    "platform": "Linux-6.18.45-x86_64-with-glibc2.40",
    "target_schema": 3,
    "variables": {
      "APIGRUNN_CACHE": "~/.cache/apigrunn/apigrunn.db"
    }
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

After every successful conversion the script also sends a short, quiet summary
— version, talk count, and the environment below — so the maintainer knows the
tool still works against the current site. This summary is sent independently of
the crash-reporting opt-in and is never surfaced to the terminal.

That is the whole payload. It carries no argv, no cache path, no page HTML and
no talk titles. Secret-looking environment values are redacted, and any path in
the report is rewritten to `<db>`, `<cache-dir>` and `~` first; the tests assert
that your home directory cannot appear in a report.

Reporting is best-effort and never changes the outcome: an endpoint that is
down or slow costs one line on stderr and a five-second timeout, and the
original traceback and exit code reach you unchanged either way.

`REPORT_URL` in `fix_cache.py` names the collector a report goes to;
`$APIGRUNN_FIXER_REPORT_URL` overrides it if you would rather run one of your
own. Crash reports are sent only when you opt in; the usage summary is always
sent after a conversion.

## Development

```bash
nix develop -c uv run pytest -q
```

The tests build their own v2 databases from synthetic track-page markup, so
they never touch the network. `tests/fixtures/healthcare.html` is the one
exception: a real track page, trimmed to its talk archive, which the parser
tests read.

apigrunn is a development dependency, and only that. The suite uses it as an
oracle — `tests/test_parser.py` checks that this parser agrees with the
library's card for card, and the conversion tests open every result with the
real `Cache` — so a new apigrunn release that changes either one fails the
suite here rather than surprising someone mid-repair. The modules that need it
skip themselves when it is absent.
