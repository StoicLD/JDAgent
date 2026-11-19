import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jdagent.data_paths import DataPaths


@pytest.fixture(autouse=True)
def isolated_user_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    ):
        monkeypatch.setenv(name, str(tmp_path))
    paths = DataPaths.for_workspace(tmp_path)
    assert paths.user_config_file.is_relative_to(tmp_path)
    assert paths.project_directory.is_relative_to(tmp_path)


def _console_script() -> Path:
    name = "jdagent.exe" if os.name == "nt" else "jdagent"
    return Path(sys.executable).parent / name


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def test_console_script_and_module_entrypoint_are_equivalent(tmp_path: Path) -> None:
    common = [
        "hello",
        "--provider",
        "fake",
        "--workspace",
        str(tmp_path),
        "--output",
        "json",
    ]
    console = _run(
        [str(_console_script()), *common, "--data-dir", str(tmp_path / "console")],
        cwd=tmp_path,
    )
    module = _run(
        [
            sys.executable,
            "-m",
            "jdagent",
            *common,
            "--data-dir",
            str(tmp_path / "module"),
        ],
        cwd=tmp_path,
    )

    console_data = json.loads(console.stdout)
    module_data = json.loads(module.stdout)
    for payload in (console_data, module_data):
        payload.pop("session_id")
        payload.pop("turn_id")
        assert payload["maintenance"]["status_at_output"] == "pending"
        payload["maintenance"].pop("job_id")

    assert console.returncode == module.returncode == 0
    assert console.stderr == module.stderr == ""
    assert console_data == module_data


def test_headless_text_subprocess_has_only_answer_on_stdout(tmp_path: Path) -> None:
    completed = _run(
        [
            str(_console_script()),
            "hello",
            "--provider",
            "fake",
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "sessions"),
            "--output",
            "text",
        ],
        cwd=tmp_path,
    )

    assert completed.returncode == 0
    assert completed.stdout == "Offline fake model response.\n"
    assert completed.stderr == ""
    assert "\x1b[" not in completed.stdout


@pytest.mark.parametrize("legacy_engine", ["native", "langgraph"])
def test_legacy_engine_reports_migration_once_without_polluting_json(
    tmp_path: Path, legacy_engine: str
) -> None:
    completed = _run(
        [
            sys.executable,
            "-m",
            "jdagent",
            "hello",
            "--provider",
            "fake",
            "--runtime-engine",
            legacy_engine,
            "--workspace",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "sessions"),
            "--output",
            "json",
        ],
        cwd=tmp_path,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["stop_reason"] == "completed"
    assert completed.stderr.count("--runtime-engine") == 1
    assert "LangGraph" in completed.stderr
    if legacy_engine == "native":
        assert "native" in completed.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--runtime-engine", "native", "knowledge", "connection", "list"],
        ["maintain", "--pending", "--runtime-engine", "langgraph", "--provider", "fake"],
    ],
)
def test_management_commands_accept_legacy_engine_without_starting_a_turn(
    tmp_path: Path, args: list[str]
) -> None:
    completed = _run([sys.executable, "-m", "jdagent", *args], cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr.count("--runtime-engine") == 1
    assert not list(tmp_path.rglob("*.jsonl"))


def test_user_engine_reload_emits_one_notice_and_bad_project_setting_stops_startup(
    tmp_path: Path,
) -> None:
    paths = DataPaths.for_workspace(tmp_path)
    paths.user_config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.user_config_file.write_text('runtime_engine = "native"\n', encoding="utf-8")
    command = [sys.executable, "-m", "jdagent", "hello", "--output", "json", "--provider", "fake"]
    first = _run(command, cwd=tmp_path)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["schema_version"] == 2
    assert first.stderr.count("--runtime-engine") == 1
    records = {p: p.read_bytes() for p in tmp_path.rglob("*.jsonl")}
    assert records
    paths.project_config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.project_config_file.write_text('runtime_engine = "langgraph"\n', encoding="utf-8")
    rejected = _run(command, cwd=tmp_path)
    assert rejected.returncode == 2
    assert not rejected.stdout and "runtime_engine" in rejected.stderr
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.jsonl")} == records
