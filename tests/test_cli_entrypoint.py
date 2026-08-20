import json
from pathlib import Path
from typing import cast

import pytest

import jdagent.cli as cli_module
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
            "--output",
            "json",
        ]
    )
    first_payload = json.loads(capsys.readouterr().out)
    session_id = first_payload["session_id"]

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
            "--output",
            "json",
        ]
    )
    second_payload = json.loads(capsys.readouterr().out)

    assert first_code == 0
    assert second_code == 0
    assert second_payload["session_id"] == session_id
    assert second_payload["stop_reason"] == "completed"


def test_headless_json_stdout_is_machine_parseable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
            "--output",
            "json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert captured.err == ""
    assert payload == {
        "schema_version": 1,
        "status": "success",
        "session_id": payload["session_id"],
        "turn_id": payload["turn_id"],
        "stop_reason": "completed",
        "answer": "Offline fake model response.",
        "provider": "fake",
        "model": "deepseek-v4-flash",
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "error": None,
    }


def test_missing_deepseek_key_reports_action_and_exits_2_without_fake_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    monkeypatch.setenv("APPDATA", str(config_root))
    monkeypatch.setenv("LOCALAPPDATA", str(data_root))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    user_config = config_root / "JDAgent" / "config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text('api_key_file = "missing-key.txt"\n', encoding="utf-8")

    code = main(["hello", "--workspace", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "DEEPSEEK_API_KEY" in captured.err
    assert "provider=deepseek" in captured.err
    assert not any(data_root.rglob("*.jsonl"))


def test_explicit_fake_does_not_read_deepseek_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    monkeypatch.setenv("APPDATA", str(config_root))
    monkeypatch.setenv("LOCALAPPDATA", str(data_root))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    user_config = config_root / "JDAgent" / "config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text('api_key_file = "missing-key.txt"\n', encoding="utf-8")

    code = main(["hello", "--provider", "fake", "--workspace", str(tmp_path)])

    assert code == 0
    assert capsys.readouterr().out == "Offline fake model response.\n"


def test_output_without_prompt_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--output", "json"])

    assert code == 2
    assert "--output requires a one-shot prompt" in capsys.readouterr().err


def test_version_comes_from_installed_package_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == "jdagent 0.2.0\n"


def test_unclassified_internal_error_is_safely_mapped_to_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(arguments: object) -> int:
        del arguments
        raise RuntimeError("private internal detail")

    monkeypatch.setattr(cli_module, "run_cli", cast(object, fail))

    code = main(["hello", "--provider", "fake"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "RuntimeError" in captured.err
    assert "private internal detail" not in captured.err


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

    assert code == 3
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

    captured = capsys.readouterr()
    trace_line = next(line for line in captured.err.splitlines() if line.startswith("trace="))
    trace = json.loads(trace_line.removeprefix("trace="))

    assert code == 0
    assert trace["summary"]["provider"] == "fake"
    assert trace["summary"]["stop_reason"] == "completed"
    assert trace["entries"][-1]["event_type"] == "turn_completed"
    assert "private prompt text" not in trace_line
    assert captured.out == "Offline fake model response.\n"


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

    captured = capsys.readouterr()
    trace_line = next(line for line in captured.err.splitlines() if line.startswith("trace="))
    trace = json.loads(trace_line.removeprefix("trace="))

    assert code == 4
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

    captured = capsys.readouterr()
    trace_line = next(line for line in captured.err.splitlines() if line.startswith("trace="))
    trace = json.loads(trace_line.removeprefix("trace="))

    assert code == 4
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
