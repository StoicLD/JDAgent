"""Exercise real CLI parsing and host wiring with four offline backend combinations."""

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

import jdagent.composition as composition_module
import jdagent.host as host_module
from jdagent.adapters.deepseek import DeepSeekModelPort
from jdagent.adapters.langchain_model import LangChainModelPort
from jdagent.adapters.scripted_terminal import CapturePresenter, ScriptedTerminal
from jdagent.application.interactive import (
    CommandAction,
    CommandName,
    ExitAction,
    InteractionState,
    PromptAction,
    UserAction,
)
from jdagent.cli import main
from jdagent.core.loop import AgentLoop
from jdagent.data_paths import DataPaths
from jdagent.domain.model import ModelEvent, Usage
from jdagent.domain.tools import ToolCall
from jdagent.memory_store import MemoryStore
from jdagent.runtime.langgraph_runner import LangGraphTurnRunner
from tests.runtime_matrix import COMBINATIONS, model_response


class IdleCommandsTerminal(ScriptedTerminal):
    async def next_action(self, state: InteractionState) -> UserAction:
        if state is not InteractionState.IDLE:
            await asyncio.Event().wait()
        return await super().next_action(state)


class Presenter(CapturePresenter):
    closed = False

    async def observe(self, event: ModelEvent) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class CliCase:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            monkeypatch.setenv(name, str(root / name))
        monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-key")
        self.paths = DataPaths.for_workspace(self.workspace)
        self.paths.project_config_file.parent.mkdir(parents=True)
        self.configure(maintenance=False)
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []
        self.adapters: list[str] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda _: model_response(
            "CLI answer", usage=Usage(12, 4)
        )

        def transport(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.handler(request)

        def factory(adapter_type: type[DeepSeekModelPort] | type[LangChainModelPort]) -> object:
            def make(api_key: str, *, base_url: str) -> DeepSeekModelPort | LangChainModelPort:
                client = httpx.AsyncClient(
                    base_url=base_url, transport=httpx.MockTransport(transport)
                )
                self.clients.append(client)
                self.adapters.append(adapter_type.__name__)
                return adapter_type(api_key, base_url=base_url, client=client)

            return make

        for adapter in (DeepSeekModelPort, LangChainModelPort):
            monkeypatch.setattr(composition_module, adapter.__name__, factory(adapter))

    def configure(self, *, maintenance: bool) -> None:
        self.paths.project_config_file.write_text(
            "model_context_window = 64000\nmax_context_tokens = 24000\nmax_output_tokens = 2048\n"
            f"[memory]\nauto_consolidation = {str(maintenance).lower()}\n"
            f"headless_drain_seconds = {10 if maintenance else 0}\n",
            encoding="utf-8",
        )

    def run(self, combination: tuple[str, str], *arguments: str) -> int:
        engine, adapter = combination

        def wrong_runner(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("CLI selected the wrong execution engine")

        self.monkeypatch.setattr(
            composition_module, "AgentLoop", AgentLoop if engine == "native" else wrong_runner
        )
        self.monkeypatch.setattr(
            composition_module,
            "LangGraphTurnRunner",
            LangGraphTurnRunner if engine == "langgraph" else wrong_runner,
        )
        before = len(self.adapters)
        result = main(
            [
                *arguments,
                "--workspace",
                str(self.workspace),
                "--provider",
                "deepseek",
                "--base-url",
                "https://test",
                "--runtime-engine",
                engine,
                "--model-adapter",
                adapter,
            ]
        )
        assert self.adapters[before:] == [
            "LangChainModelPort" if adapter == "langchain" else "DeepSeekModelPort"
        ]
        return result

    def close(self) -> None:
        async def close_clients() -> None:
            for client in self.clients:
                assert not client.is_closed  # Adapters borrow the injected HTTP boundary.
                await client.aclose()

        asyncio.run(close_clients())


@pytest.fixture
def cli_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CliCase]:
    case = CliCase(tmp_path, monkeypatch)
    try:
        yield case
    finally:
        case.close()


@pytest.mark.parametrize("combination", COMBINATIONS)
@pytest.mark.parametrize("failure", [False, True])
def test_cli_headless_json_exit_status_and_restart(
    cli_case: CliCase,
    capsys: pytest.CaptureFixture[str],
    combination: tuple[str, str],
    failure: bool,
) -> None:
    if failure:
        cli_case.handler = lambda _: httpx.Response(503, json={"error": {"message": "offline"}})
    assert cli_case.run(combination, "hello", "--output", "json") == (4 if failure else 0)
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 2
    assert payload["status"] == ("error" if failure else "success")
    assert payload["stop_reason"] == ("model_error" if failure else "completed")
    assert len(cli_case.requests) == 1  # A 503 must not activate SDK retries.
    if not failure:
        assert payload["usage"] == {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}
        assert payload["answer"] == "CLI answer"
        assert (
            cli_case.run(
                ("native", "native"),
                "continue",
                "--session-id",
                payload["session_id"],
                "--output",
                "json",
            )
            == 0
        )
        resumed = json.loads(capsys.readouterr().out)
        assert resumed["session_id"] == payload["session_id"]
        assert len(cli_case.requests) == 2
        assert "hello" in cli_case.requests[-1].content.decode()


@pytest.mark.parametrize("combination", COMBINATIONS)
def test_cli_interactive_context_and_compact_commands_use_selected_backends(
    cli_case: CliCase, monkeypatch: pytest.MonkeyPatch, combination: tuple[str, str]
) -> None:
    (cli_case.workspace / "report.txt").write_text("original report line\n" * 150, encoding="utf-8")
    terminal = IdleCommandsTerminal(
        (
            PromptAction("Read report.txt. Never modify protected.txt."),
            CommandAction(CommandName.CONTEXT_WINDOW),
            CommandAction(CommandName.COMPACT),
            CommandAction(CommandName.CONTEXT_WINDOW),
            PromptAction("Continue after the summary."),
            ExitAction(),
        )
    )
    presenter = Presenter()

    def terminal_factory(*, history_file: Path) -> IdleCommandsTerminal:
        del history_file
        return terminal

    monkeypatch.setattr(host_module, "PromptToolkitTerminal", terminal_factory)
    monkeypatch.setattr(host_module, "RichPresenter", lambda: presenter)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("response_format") == {"type": "json_object"}:
            assert "tools" not in body
            return model_response(
                json.dumps(
                    {
                        "goal": "CLI compacted goal",
                        "facts": [],
                        "pending": [],
                        "decisions": [],
                        "paths": [],
                    }
                )
            )
        if len(cli_case.requests) == 1:
            return model_response(calls=(ToolCall("read", "read_file", {"path": "report.txt"}),))
        return model_response("CLI answer")

    cli_case.handler = handler
    assert cli_case.run(combination) == 0
    assert terminal.closed and presenter.closed
    messages = [event.message for event in presenter.events]
    assert sum("input_budget=" in message for message in messages) == 2
    assert any("compaction=committed" in message for message in messages)
    assert len(cli_case.requests) == 4  # Read, answer, manual compaction, next turn.
    final = cli_case.requests[-1].content.decode()
    assert "CLI compacted goal" in final
    assert "Never modify protected.txt." in final
    assert not (cli_case.workspace / "protected.txt").exists()


@pytest.mark.parametrize("combination", COMBINATIONS)
def test_cli_maintain_resumes_jobs_without_creating_sessions_or_repeating_calls(
    cli_case: CliCase, capsys: pytest.CaptureFixture[str], combination: tuple[str, str]
) -> None:
    jobs: list[str] = []
    for prompt in ("first source", "second source"):
        assert cli_case.run(combination, prompt, "--output", "json") == 0
        jobs.append(json.loads(capsys.readouterr().out)["maintenance"]["job_id"])
    sessions = sorted(cli_case.paths.sessions_directory.glob("*.jsonl"))
    snapshots = {path: path.read_bytes() for path in sessions}
    assert len(sessions) == 2
    cli_case.configure(maintenance=True)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "tools" not in body
        assert body["response_format"] == {"type": "json_object"}
        return model_response('{"candidates":[]}', usage=Usage(12, 4))

    cli_case.handler = handler
    assert cli_case.run(combination, "maintain", "--pending") == 0
    assert capsys.readouterr().out == ""
    store = MemoryStore(cli_case.paths.project_directory.parent.parent / "memory-v5.sqlite")
    assert all(store.job_status(job) == "succeeded" for job in jobs)
    calls = len(cli_case.requests)
    assert calls > 2
    assert cli_case.run(combination, "maintain", "--pending") == 0
    assert capsys.readouterr().out == ""
    assert len(cli_case.requests) == calls
    assert {
        path: path.read_bytes() for path in cli_case.paths.sessions_directory.glob("*.jsonl")
    } == snapshots
