"""Direct memory intent, versions and suppression through all four compositions."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.domain.errors import StopReason
from jdagent.domain.events import ToolExecutionCompletedPayload
from jdagent.domain.json import normalize_json, require_object
from jdagent.domain.tools import ApprovalDecision, ToolCall, ToolResultStatus
from tests.runtime_matrix import COMBINATIONS, RuntimeCase, model_response


@pytest.mark.parametrize("combination", COMBINATIONS, ids=lambda pair: "/".join(pair))
@pytest.mark.parametrize("forged", [False, True], ids=["bound_intent", "forged_statement"])
def test_memory_intent_versions_disable_and_expiry_survive_backend_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, combination: tuple[str, str], forged: bool
) -> None:
    case = RuntimeCase(tmp_path, monkeypatch, combination)
    approval = FakeApproval(ApprovalDecision.REJECT)
    requests = 0
    prefix = "Current direct user memory intent; use update_memory with exactly these arguments: "

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests % 2 == 0:
            return model_response("Finished the requested memory operation.")
        body = json.loads(request.content)
        instruction = next(
            message["content"]
            for message in body["messages"]
            if message["role"] == "system" and prefix in message["content"]
        )
        args = require_object(normalize_json(json.loads(instruction.split(prefix, 1)[1])), "intent")
        if forged:
            args["statement"] = "Forged unrelated statement."
        return model_response("", (ToolCall(f"memory-{requests}", "update_memory", args),))

    async def scenario() -> None:
        async with case.open(handler, approval=approval) as runtime:
            first = await runtime.coordinator.send("记住：项目使用 pytest")
            assert first.result.tool_calls == 1
            assert runtime.maintenance is not None
            memory = runtime.maintenance.memory
            store = memory.store
            events = [event async for event in runtime.session.read(first.session_id)]
            results = [
                e.payload.result
                for e in events
                if isinstance(e.payload, ToolExecutionCompletedPayload)
            ]
            assert len(results) == 1
            if forged:
                assert first.result.stop_reason is StopReason.LIMIT_REACHED
                assert first.result.error_category == "uncertain_side_effect"
                assert first.result.model_calls == 1
                assert results[0].status is ToolResultStatus.ERROR
                assert store.recall(case.paths.identity, include_inactive=True) == ()
                assert not approval.requests
                return
            assert first.result.stop_reason is StopReason.COMPLETED
            assert first.result.model_calls == 2
            assert results[0].status is ToolResultStatus.SUCCESS
            (item,) = store.recall(case.paths.identity)
            assert item.version == 1 and item.statement == "项目使用 pytest"
            identity = item.memory_id
            operations = [
                (f"把本项目记忆 {identity} 更正为：测试位于 tests 目录", 2, "active"),
                (f"忘记本项目记忆 {identity}", 3, "disabled"),
                (f"把本项目记忆 {identity} 更正为：测试使用 unittest", 4, "disabled"),
                (f"重新启用本项目记忆 {identity}", 5, "active"),
            ]
            for prompt, version, state in operations:
                turn = await runtime.coordinator.send(prompt, session_id=first.session_id)
                assert turn.result.stop_reason is StopReason.COMPLETED
                assert (turn.result.model_calls, turn.result.tool_calls) == (2, 1)
                latest = store.versions(identity)[-1]
                assert (latest.version, latest.state) == (version, state)
                if state == "disabled":
                    assert not store.recall(case.paths.identity)
                    assert (await memory.search_memory("测试"))["total"] == 0
            assert len(store.versions(identity)) == 5
            assert store.versions(identity)[0].statement == "项目使用 pytest"
            assert store.recall("another-workspace") == ()
            expired = await runtime.coordinator.send(
                "记住：临时规则 [有效至：2020-01-01T00:00:00+00:00]", session_id=first.session_id
            )
            assert expired.result.stop_reason is StopReason.COMPLETED
            all_items = store.recall(case.paths.identity, include_inactive=True)
            assert len(all_items) == 2 and {i.state for i in all_items} == {"active", "expired"}
            assert len(store.recall(case.paths.identity)) == 1
            assert not approval.requests

    asyncio.run(scenario())
    assert requests == (1 if forged else 12)
