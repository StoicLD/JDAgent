"""Four-backend runtime fixtures with real adapters and an offline HTTP boundary."""

import json
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest

import jdagent.composition as composition_module
from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.fake import FakeApproval
from jdagent.adapters.langchain_model import LangChainModelPort
from jdagent.composition import RuntimeComposition, RuntimeOptions, build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths
from jdagent.domain.json import JsonObject
from jdagent.domain.tools import ApprovalDecision, ToolCall
from jdagent.ports import EventObserver

COMBINATIONS = (
    ("native", "native"),
    ("native", "langchain"),
    ("langgraph", "native"),
    ("langgraph", "langchain"),
)


def model_response(text: str = "done", calls: tuple[ToolCall, ...] = ()) -> httpx.Response:
    delta: JsonObject = {"role": "assistant", "content": text}
    if calls:
        delta["tool_calls"] = [
            {
                "index": index,
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for index, call in enumerate(calls)
        ]
    chunk = {
        "id": "response",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-test",
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": "tool_calls" if calls else "stop"}
        ],
    }
    return httpx.Response(200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())


@dataclass
class RuntimeCase:
    root: Path
    monkeypatch: pytest.MonkeyPatch
    combination: tuple[str, str]

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def paths(self) -> DataPaths:
        return DataPaths.for_workspace(
            self.workspace, config_root=self.root / "config", data_root=self.root / "data"
        )

    @asynccontextmanager
    async def open(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        approval: FakeApproval | None = None,
        options: RuntimeOptions | None = None,
        combination: tuple[str, str] | None = None,
        observers: tuple[EventObserver, ...] = (),
    ) -> AsyncGenerator[RuntimeComposition]:
        self.workspace.mkdir(exist_ok=True)
        engine, adapter_name = combination or self.combination
        config = resolve_configuration(
            self.workspace,
            CliOverrides(
                provider="deepseek",
                base_url="https://test",
                runtime_engine=engine,
                model_adapter=adapter_name,
                model_context_window=120000,
            ),
            data_paths=self.paths,
            environment={"DEEPSEEK_API_KEY": "dummy-key"},
        )
        config = replace(config, memory_auto_consolidation=False)
        adapter_type = LangChainModelPort if adapter_name == "langchain" else DeepSeekModelPort
        async with httpx.AsyncClient(
            base_url="https://test", transport=httpx.MockTransport(handler)
        ) as client:

            def adapter(api_key: str, *, base_url: str) -> LangChainModelPort | DeepSeekModelPort:
                return adapter_type(api_key, base_url=base_url, client=client)

            self.monkeypatch.setattr(composition_module, adapter_type.__name__, adapter)
            runtime = build_runtime(
                config,
                approval or FakeApproval(ApprovalDecision.APPROVE),
                runtime_options=options,
                event_observers=observers,
            )
            assert isinstance(runtime.model, adapter_type)
            try:
                yield runtime
            finally:
                await runtime.aclose()
            assert not client.is_closed
