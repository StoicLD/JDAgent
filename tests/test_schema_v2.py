import json
from pathlib import Path

import pytest

from jdagent.adapters.jsonl_session import event_from_data
from jdagent.cli import main
from jdagent.domain.errors import SessionError, SessionErrorCode
from jdagent.domain.json import JsonObject


def test_schema_v1_and_unknown_are_unsupported_not_corrupt() -> None:
    v1: JsonObject = {
        "schema_version": 1,
        "event_id": "e1",
        "session_id": "s1",
        "turn_id": None,
        "sequence": 1,
        "event_type": "session_started",
        "timestamp": "2026-08-30T12:00:00+00:00",
        "payload": {},
    }
    with pytest.raises(SessionError) as error:
        event_from_data(v1)
    assert error.value.code is SessionErrorCode.UNSUPPORTED_SCHEMA

    unknown = dict(v1)
    unknown["schema_version"] = 99
    with pytest.raises(SessionError) as error:
        event_from_data(unknown)
    assert error.value.code is SessionErrorCode.UNSUPPORTED_SCHEMA


def test_resume_of_v1_session_is_unsupported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    event = {
        "schema_version": 1,
        "event_id": "e1",
        "session_id": "legacy",
        "turn_id": None,
        "sequence": 1,
        "event_type": "session_started",
        "timestamp": "2026-08-30T12:00:00+00:00",
        "payload": {"name": "old", "workspace_identity": None},
    }
    (session_dir / "legacy.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    code = main(
        [
            "hello",
            "--provider",
            "fake",
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(session_dir),
            "--session-id",
            "legacy",
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "Unsupported schema" in captured.err
