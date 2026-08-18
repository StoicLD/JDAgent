"""Text CLI adapter: parse input, render events, and call the application seam."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from jdagent.application.coordinator import CoordinatedTurn, TurnCoordinator
from jdagent.composition import RuntimeConfiguration, build_runtime
from jdagent.domain.errors import SessionError
from jdagent.domain.events import (
    PermissionRequestedPayload,
    RuntimeEvent,
    RuntimeEventType,
    ToolCallRequestedPayload,
    ToolExecutionCompletedPayload,
)
from jdagent.domain.json import JsonObject, JsonValue
from jdagent.domain.model import ModelEvent, TextDelta
from jdagent.domain.tools import ApprovalDecision, ApprovalRequest
from jdagent.observability import TraceProjection

InputFunction = Callable[[str], str]
OutputFunction = Callable[[str], None]
StreamOutputFunction = Callable[[str], None]


class ConsoleApproval:
    """Collect an approval decision without printing tool arguments or file content."""

    def __init__(
        self,
        *,
        input_function: InputFunction = input,
        output_function: OutputFunction = print,
    ) -> None:
        self._input = input_function
        self._output = output_function

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        self._output(f"Approval required for tool: {request.tool_name}")
        answer = await asyncio.to_thread(self._input, "Approve? [y/N] ")
        return (
            ApprovalDecision.APPROVE
            if answer.strip().lower() in {"y", "yes"}
            else ApprovalDecision.REJECT
        )


class ConsoleModelObserver:
    """Render model text deltas as they arrive."""

    def __init__(self, output_function: StreamOutputFunction) -> None:
        self._output = output_function

    async def observe(self, event: ModelEvent) -> None:
        if isinstance(event, TextDelta):
            self._output(event.text)


class ConsoleEventObserver:
    """Render non-sensitive tool lifecycle status after event persistence."""

    def __init__(self, output_function: OutputFunction = print) -> None:
        self._output = output_function

    async def observe(self, event: RuntimeEvent) -> None:
        payload = event.payload
        if event.event_type is RuntimeEventType.TOOL_CALL_REQUESTED and isinstance(
            payload, ToolCallRequestedPayload
        ):
            self._output(f"[tool requested] {payload.call.name}")
        elif event.event_type is RuntimeEventType.PERMISSION_REQUESTED and isinstance(
            payload, PermissionRequestedPayload
        ):
            self._output(f"[approval requested] {payload.request.tool_name}")
        elif event.event_type is RuntimeEventType.TOOL_EXECUTION_COMPLETED and isinstance(
            payload, ToolExecutionCompletedPayload
        ):
            self._output(f"[tool {payload.result.status.value}] {payload.result.tool_name}")


async def run_single_turn(
    coordinator: TurnCoordinator,
    user_text: str,
    *,
    session_id: str | None = None,
    output_function: OutputFunction = print,
    render_final_text: bool = True,
) -> CoordinatedTurn:
    """Run one CLI turn and render its caller-visible result and stable IDs."""

    turn = await coordinator.send(user_text, session_id=session_id)
    if render_final_text and turn.result.assistant_text:
        output_function(turn.result.assistant_text)
    elif not render_final_text and turn.result.assistant_text:
        output_function("")
    output_function(f"session_id={turn.session_id}")
    output_function(f"stop_reason={turn.result.stop_reason.value}")
    return turn


@dataclass(frozen=True, slots=True)
class CliArguments:
    prompt: str | None
    session_id: str | None
    provider: str
    model: str
    base_url: str
    model_timeout_seconds: float
    tool_timeout_seconds: float
    max_context_tokens: int | None
    fake_delay_seconds: float
    workspace: Path
    data_dir: Path
    show_trace: bool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jdagent", description="JDAgent v0.1 runtime")
    parser.add_argument("prompt", nargs="?", help="Run one prompt and exit")
    parser.add_argument("--session-id", help="Resume an existing session")
    parser.add_argument("--provider", choices=("fake", "deepseek"), default="fake")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    )
    parser.add_argument("--model-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--tool-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-context-tokens", type=int)
    parser.add_argument(
        "--fake-delay-seconds",
        type=float,
        default=0.0,
        help="Inject Fake Model latency for offline failure exercises",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--show-trace",
        action="store_true",
        help="Print a safe structured trace after each turn",
    )
    return parser


def _parse_arguments(argv: Sequence[str] | None) -> CliArguments:
    namespace = _parser().parse_args(argv)
    workspace = cast(Path, namespace.workspace)
    supplied_data_dir = cast(Path | None, namespace.data_dir)
    return CliArguments(
        prompt=cast(str | None, namespace.prompt),
        session_id=cast(str | None, namespace.session_id),
        provider=cast(str, namespace.provider),
        model=cast(str, namespace.model),
        base_url=cast(str, namespace.base_url),
        model_timeout_seconds=cast(float, namespace.model_timeout_seconds),
        tool_timeout_seconds=cast(float, namespace.tool_timeout_seconds),
        max_context_tokens=cast(int | None, namespace.max_context_tokens),
        fake_delay_seconds=cast(float, namespace.fake_delay_seconds),
        workspace=workspace,
        data_dir=supplied_data_dir or workspace / ".jdagent" / "sessions",
        show_trace=cast(bool, namespace.show_trace),
    )


def _stream_to_console(fragment: str) -> None:
    print(fragment, end="", flush=True)


def _trace_data(trace: TraceProjection) -> JsonObject:
    summary = trace.summary
    entries: list[JsonValue] = []
    for entry in trace.entries:
        entries.append(
            {
                "event_id": entry.event_id,
                "session_id": entry.session_id,
                "turn_id": entry.turn_id,
                "sequence": entry.sequence,
                "event_type": entry.event_type.value,
                "timestamp": entry.timestamp.isoformat(),
                "tool_status": entry.tool_status,
                "permission_policy": entry.permission_policy,
                "approval_decision": entry.approval_decision,
                "stop_reason": entry.stop_reason.value if entry.stop_reason else None,
                "error_category": entry.error_category,
            }
        )
    return {
        "summary": {
            "event_count": summary.event_count,
            "provider": summary.provider,
            "model": summary.model,
            "model_calls": summary.model_calls,
            "tool_calls": summary.tool_calls,
            "input_tokens": summary.input_tokens,
            "output_tokens": summary.output_tokens,
            "stop_reason": summary.stop_reason.value if summary.stop_reason else None,
            "error_category": summary.error_category,
            "duration_ms": summary.duration_ms,
        },
        "entries": entries,
    }


def _print_trace(trace: TraceProjection) -> None:
    print(f"trace={json.dumps(_trace_data(trace), ensure_ascii=False, sort_keys=True)}")


async def _run(arguments: CliArguments) -> int:
    approval = ConsoleApproval()
    model_observer = ConsoleModelObserver(_stream_to_console)
    event_observer = ConsoleEventObserver()
    provider_options: JsonObject = (
        {"deepseek": {"thinking": {"type": "disabled"}}} if arguments.provider == "deepseek" else {}
    )
    composition = build_runtime(
        RuntimeConfiguration(
            provider=arguments.provider,
            model=arguments.model,
            workspace=arguments.workspace,
            session_directory=arguments.data_dir,
            api_key=os.environ.get("DEEPSEEK_API_KEY"),
            base_url=arguments.base_url,
            model_timeout_seconds=arguments.model_timeout_seconds,
            tool_timeout_seconds=arguments.tool_timeout_seconds,
            max_context_tokens=arguments.max_context_tokens,
            fake_delay_seconds=arguments.fake_delay_seconds,
            provider_options=provider_options,
        ),
        approval,
        model_event_observers=(model_observer,),
        event_observers=(event_observer,),
    )
    try:
        session_id = arguments.session_id
        if arguments.prompt is not None:
            turn = await run_single_turn(
                composition.coordinator,
                arguments.prompt,
                session_id=session_id,
                render_final_text=False,
            )
            if arguments.show_trace:
                _print_trace(turn.trace)
            return 0

        print("JDAgent interactive mode. Type 'exit' or 'quit' to stop.")
        while True:
            try:
                user_text = await asyncio.to_thread(input, "> ")
            except EOFError:
                return 0
            if user_text.strip().lower() in {"exit", "quit"}:
                return 0
            if not user_text.strip():
                continue
            turn = await run_single_turn(
                composition.coordinator,
                user_text,
                session_id=session_id,
                render_final_text=False,
            )
            if arguments.show_trace:
                _print_trace(turn.trace)
            session_id = turn.session_id
    finally:
        await composition.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI with a composition root selected from explicit arguments."""

    try:
        return asyncio.run(_run(_parse_arguments(argv)))
    except (OSError, SessionError, ValueError) as error:
        print(f"jdagent: {error}", file=sys.stderr)
        return 2
