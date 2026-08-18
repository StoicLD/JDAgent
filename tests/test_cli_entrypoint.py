import json
from pathlib import Path

import pytest

from jdagent.cli import main


def test_cli_exposes_model_and_tool_timeout_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    output = capsys.readouterr().out
    assert exit_info.value.code == 0
    assert "--model-timeout-seconds" in output
    assert "--tool-timeout-seconds" in output
    assert "--max-context-tokens" in output


def test_cli_fake_provider_creates_and_resumes_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "sessions"
    first_code = main(
        [
            "first question",
            "--provider",
            "fake",
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(data_dir),
        ]
    )
    first_output = capsys.readouterr().out
    session_line = next(
        line for line in first_output.splitlines() if line.startswith("session_id=")
    )
    session_id = session_line.split("=", 1)[1]

    second_code = main(
        [
            "second question",
            "--provider",
            "fake",
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(data_dir),
            "--session-id",
            session_id,
        ]
    )
    second_output = capsys.readouterr().out

    assert first_code == 0
    assert second_code == 0
    assert f"session_id={session_id}" in second_output
    assert "stop_reason=completed" in second_output


def test_cli_unknown_resume_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "hello",
            "--provider",
            "fake",
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "sessions"),
            "--session-id",
            "missing-session",
        ]
    )

    assert code == 2
    assert "Session not found" in capsys.readouterr().err


def test_cli_show_trace_outputs_safe_structured_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "private prompt text",
            "--provider",
            "fake",
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "sessions"),
            "--show-trace",
        ]
    )

    output = capsys.readouterr().out
    trace_line = next(line for line in output.splitlines() if line.startswith("trace="))
    trace = json.loads(trace_line.removeprefix("trace="))

    assert code == 0
    assert trace["summary"]["provider"] == "fake"
    assert trace["summary"]["stop_reason"] == "completed"
    assert trace["entries"][-1]["event_type"] == "turn_completed"
    assert "private prompt text" not in trace_line


def test_cli_trace_explains_injected_context_limit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "diagnose this context failure",
            "--provider",
            "fake",
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "sessions"),
            "--max-context-tokens",
            "1",
            "--show-trace",
        ]
    )

    output = capsys.readouterr().out
    trace_line = next(line for line in output.splitlines() if line.startswith("trace="))
    trace = json.loads(trace_line.removeprefix("trace="))

    assert code == 0
    assert trace["summary"]["stop_reason"] == "context_limit"
    assert trace["summary"]["error_category"] == "context_length"
    assert trace["summary"]["model_calls"] == 0


def test_cli_trace_explains_injected_fake_model_timeout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "diagnose this timeout",
            "--provider",
            "fake",
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "sessions"),
            "--fake-delay-seconds",
            "1",
            "--model-timeout-seconds",
            "0.01",
            "--show-trace",
        ]
    )

    output = capsys.readouterr().out
    trace_line = next(line for line in output.splitlines() if line.startswith("trace="))
    trace = json.loads(trace_line.removeprefix("trace="))

    assert code == 0
    assert trace["summary"]["stop_reason"] == "model_error"
    assert trace["summary"]["error_category"] == "timeout"
    assert trace["summary"]["model_calls"] == 1


@pytest.mark.parametrize(
    ("option", "value"),
    (
        ("--model-timeout-seconds", "0"),
        ("--tool-timeout-seconds", "-1"),
        ("--max-context-tokens", "0"),
        ("--fake-delay-seconds", "-1"),
    ),
)
def test_cli_rejects_invalid_runtime_configuration_before_session_side_effect(
    option: str,
    value: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "sessions"

    code = main(
        [
            "must not persist",
            "--provider",
            "fake",
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(data_dir),
            option,
            value,
        ]
    )

    assert code == 2
    assert "must be" in capsys.readouterr().err
    assert not data_dir.exists()
