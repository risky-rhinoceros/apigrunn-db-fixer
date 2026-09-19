"""Tests for the opt-in crash reporting.

Nothing here touches the network: ``fix_cache._send_report`` is replaced with a
recorder, and the tests that care about a failing endpoint make it raise. The
crash itself is manufactured by making ``_build_v3`` blow up part-way through a
real conversion, so the reported stage and counts are the real ones.
"""

from __future__ import annotations

import json
import platform
import sqlite3
import urllib.error
from pathlib import Path

import pytest

import fix_cache
from conftest import card_html, page_html, write_v2


@pytest.fixture(autouse=True)
def _no_ambient_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit an opt-in, or an endpoint, from the developer's shell."""
    monkeypatch.delenv("APIGRUNN_FIXER_ERROR_REPORTING", raising=False)
    monkeypatch.delenv("APIGRUNN_FIXER_REPORT_URL", raising=False)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Collects the payloads that would have been POSTed."""
    payloads: list[dict] = []
    monkeypatch.setattr(fix_cache, "_send_report", payloads.append)
    return payloads


@pytest.fixture
def crashing_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A convertible cache whose conversion dies while writing the v3 file.

    The failure quotes the database's path, the way a real sqlite error would,
    which is what makes the scrubbing test meaningful.
    """
    path = tmp_path / "apigrunn.db"
    write_v2(
        path,
        {
            "tech": page_html(2026, card_html("aaaaaaaaaaa", "A Talk")),
            "business": page_html(2026, card_html("bbbbbbbbbbb", "Another Talk")),
        },
    )

    def boom(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError(f"disk I/O error writing {path}")

    monkeypatch.setattr(fix_cache, "_build_v3", boom)
    return path


@pytest.fixture
def v2_db_like(tmp_path: Path) -> Path:
    """A convertible cache that converts cleanly."""
    path = tmp_path / "apigrunn.db"
    write_v2(path, {"tech": page_html(2026, card_html("ccccccccccc", "A Talk"))})
    return path


def crash(db: Path, *args: str) -> None:
    with pytest.raises(sqlite3.OperationalError):
        fix_cache.run([str(db), *args])


def test_nothing_is_sent_without_an_opt_in(crashing_db: Path, sent: list[dict]) -> None:
    crash(crashing_db)
    assert sent == []


def test_the_flag_opts_in(crashing_db: Path, sent: list[dict]) -> None:
    crash(crashing_db, "--report-errors")
    assert len(sent) == 1


def test_the_environment_opts_in(
    crashing_db: Path, sent: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APIGRUNN_FIXER_ERROR_REPORTING", "1")
    crash(crashing_db)
    assert len(sent) == 1


def test_a_successful_run_sends_a_summary(v2_db_like: Path, sent: list[dict]) -> None:
    assert fix_cache.run([str(v2_db_like)]) == fix_cache.EXIT_OK
    assert len(sent) == 1
    assert sent[0]["result"] == "ok"
    assert sent[0]["run"]["cards"] == 1


def test_the_payload_describes_the_error(crashing_db: Path, sent: list[dict]) -> None:
    crash(crashing_db, "--report-errors")
    error = sent[0]["error"]

    assert error["type"] == "sqlite3.OperationalError"
    assert "disk I/O error" in error["message"]
    # The traceback runs from this script's entry point to the raising frame.
    assert error["frames"][0]["file"] == "fix_cache.py"
    assert error["frames"][-1]["func"] == "boom"


def test_the_payload_describes_the_run_and_the_environment(
    crashing_db: Path, sent: list[dict]
) -> None:
    payload = sent
    crash(crashing_db, "--report-errors", "--no-backup")
    report = payload[0]

    assert report["schema"] == fix_cache.REPORT_SCHEMA
    assert report["tool"] == fix_cache.TOOL_NAME
    assert report["reported_at"].endswith("Z")
    assert report["environment"]["python"] == platform.python_version()
    assert report["environment"]["target_schema"] == fix_cache.TARGET_VERSION
    assert report["run"] == {
        "stage": "build_v3",
        "dry_run": False,
        "no_backup": True,
        "db_path_given": True,
        "source_version": 2,
        "pages": 2,
        "cards": 2,
    }


def test_local_paths_are_scrubbed(
    crashing_db: Path, tmp_path: Path, sent: list[dict]
) -> None:
    crash(crashing_db, "--report-errors")
    serialised = json.dumps(sent[0])

    assert "<db>" in sent[0]["error"]["message"]
    assert str(crashing_db) not in serialised
    assert str(tmp_path) not in serialised
    assert str(Path.home()) not in serialised


def test_apigrunn_environment_variables_are_scrubbed(
    crashing_db: Path, tmp_path: Path, sent: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APIGRUNN_CACHE", str(tmp_path / "apigrunn.db"))
    crash(crashing_db, "--report-errors")

    variables = sent[0]["environment"]["variables"]
    assert "APIGRUNN_CACHE" in variables
    assert str(tmp_path) not in json.dumps(variables)
    assert str(Path.home()) not in json.dumps(variables)


def test_redaction_blanks_secret_words() -> None:
    context = fix_cache._RunContext()
    assert fix_cache._redact("PASSWORD", "hunter2", context) == "<redacted>"
    assert fix_cache._redact("token", "s3cret", context) == "<redacted>"


def test_a_failing_endpoint_does_not_mask_the_crash(
    crashing_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(payload: dict) -> None:
        raise urllib.error.URLError("nope")

    monkeypatch.setattr(fix_cache, "_send_report", refuse)

    crash(crashing_db, "--report-errors")

    assert "could not send the error report" in capsys.readouterr().err


def test_an_interrupted_run_is_not_reported(
    crashing_db: Path, monkeypatch: pytest.MonkeyPatch, sent: list[dict]
) -> None:
    def interrupt(*args: object, **kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(fix_cache, "_build_v3", interrupt)

    with pytest.raises(KeyboardInterrupt):
        fix_cache.run([str(crashing_db), "--report-errors"])
    assert sent == []


def test_a_usage_error_is_not_reported(sent: list[dict]) -> None:
    with pytest.raises(SystemExit):
        fix_cache.run(["--no-such-option"])
    assert sent == []


def test_show_report_prints_instead_of_sending(
    crashing_db: Path, sent: list[dict], capsys: pytest.CaptureFixture[str]
) -> None:
    crash(crashing_db, "--show-report")

    printed = json.loads(capsys.readouterr().err)
    assert printed["error"]["type"] == "sqlite3.OperationalError"
    assert sent == []

