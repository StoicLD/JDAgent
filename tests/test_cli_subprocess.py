import json
import os
import subprocess
import sys
from pathlib import Path


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
