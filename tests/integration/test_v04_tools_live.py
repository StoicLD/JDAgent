import asyncio
import json
import os
import runpy
import shutil
import sys
from pathlib import Path

import pytest

from jdagent.composition import RuntimeOptions, build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.errors import StopReason
from jdagent.domain.events import ToolExecutionCompletedPayload
from jdagent.domain.tools import ApprovalDecision, ApprovalOutcome, ApprovalRequest
from tests.test_v04_e2e import FIXTURE

pytestmark = pytest.mark.integration
EXECUTABLE = str(Path(sys.executable).resolve())


class FixtureApproval:
    """Allow only the fixture source edit and fixed verification command."""

    async def request(self, request: ApprovalRequest) -> ApprovalOutcome:
        allowed = (request.tool_name == "edit_file" and request.target == "prices.py") or (
            request.tool_name == "run_command"
            and request.display.get("program") == EXECUTABLE
            and request.display.get("args") == ["verify.py"]
        )
        return ApprovalOutcome(ApprovalDecision.APPROVE if allowed else ApprovalDecision.REJECT)


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_deepseek_completes_real_tool_chain(tmp_path: Path, attempt: int) -> None:
    if os.environ.get("JDAGENT_RUN_DEEPSEEK_INTEGRATION") != "1":
        pytest.skip("Set JDAGENT_RUN_DEEPSEEK_INTEGRATION=1 for live tools eval")
    workspace = tmp_path / "project"
    shutil.copytree(FIXTURE, workspace)
    protected = {name: (workspace / name).read_bytes() for name in ("verify.py", "notes.txt")}

    async def scenario() -> None:
        paths = DataPaths.for_workspace(
            workspace, config_root=tmp_path / "config", data_root=tmp_path / "data"
        )
        configuration = resolve_configuration(
            workspace,
            CliOverrides(
                provider="deepseek", model_timeout_seconds=120, data_dir=tmp_path / "sessions"
            ),
            data_paths=paths,
        )
        runtime = build_runtime(
            configuration,
            FixtureApproval(),
            runtime_options=RuntimeOptions(
                provider_options={"deepseek": {"thinking": {"type": "disabled"}}}
            ),
        )
        try:
            turn = await runtime.coordinator.send(
                "Find and fix the sum_prices calculation bug in this small project. "
                "Use find_files, search_text and read_file to inspect it, then edit_file with the "
                "read revision. Only change prices.py. Run verification with run_command using "
                f'program={json.dumps(EXECUTABLE)}, args=["verify.py"]. '
                "Report the actual verification result honestly."
            )
            events = tuple([event async for event in runtime.session.read(turn.session_id)])
            results = [
                event.payload.result
                for event in events
                if isinstance(event.payload, ToolExecutionCompletedPayload)
            ]
            report = {
                "attempt": attempt,
                "session_id": turn.session_id,
                "model": configuration.model,
                "stop_reason": turn.result.stop_reason.value,
                "model_calls": turn.result.model_calls,
                "tool_calls": turn.result.tool_calls,
                "tools": [result.tool_name for result in results],
                "results": [
                    result.outcome.to_data() if result.outcome else {"output": result.output}
                    for result in results
                ],
                "final_report": turn.result.assistant_text,
                "source_after": (workspace / "prices.py").read_text(encoding="utf-8"),
            }
            report_path = tmp_path / f"live-attempt-{attempt}.json"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"Live evidence: {report_path}")
            assert turn.result.stop_reason is StopReason.COMPLETED, turn.result
            names = {result.tool_name for result in results}
            assert {"find_files", "search_text", "read_file", "edit_file", "run_command"} <= names
            assert any(
                result.tool_name == "run_command"
                and result.outcome is not None
                and result.outcome.data.get("exit_code") == 0
                and not result.stop_turn
                for result in results
            )
            for name, original in protected.items():
                assert (workspace / name).read_bytes() == original
            namespace = runpy.run_path(str(workspace / "prices.py"))
            assert namespace["sum_prices"]([2, 3, 7]) == 12
            assert namespace["sum_prices"]([]) == 0
            assert namespace["sum_prices"]([-1, 4]) == 3
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
