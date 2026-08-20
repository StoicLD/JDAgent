"""Composition host for interactive and headless CLI modes."""

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from jdagent.adapters.jsonl_recovery import JsonlRecoveryStore
from jdagent.adapters.legacy_sessions import import_legacy_sessions
from jdagent.adapters.terminal import (
    PromptToolkitTerminal,
    RichPresenter,
    RichRuntimeObserver,
)
from jdagent.application.approval import RejectingApprovalChoices, ScopedApproval
from jdagent.application.coordinator import CoordinatedTurn, TurnCoordinator
from jdagent.application.headless import run_headless
from jdagent.application.interactive import (
    InteractionRuntimeObserver,
    InteractiveApplication,
    InteractiveContext,
)
from jdagent.application.session_catalog import SessionCatalog
from jdagent.application.session_recovery import SessionRecovery, prepare_session_resume
from jdagent.composition import RuntimeOptions, build_runtime
from jdagent.configuration import CliOverrides, ConfigurationError, resolve_configuration
from jdagent.domain.json import JsonObject, JsonValue
from jdagent.observability import TraceProjection

OutputFunction = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class CliStartup:
    """Parsed startup values before layered configuration resolution."""

    prompt: str | None
    session_id: str | None
    provider: str | None
    model: str | None
    base_url: str | None
    model_timeout_seconds: float | None
    tool_timeout_seconds: float | None
    max_context_tokens: int | None
    fake_delay_seconds: float | None
    workspace: Path
    data_dir: Path | None
    show_trace: bool
    output: str | None


async def run_single_turn(
    coordinator: TurnCoordinator,
    user_text: str,
    *,
    session_id: str | None = None,
    output_function: OutputFunction = print,
    render_final_text: bool = True,
) -> CoordinatedTurn:
    """Compatibility seam for the v0.1 vertical-slice tests."""

    turn = await coordinator.send(user_text, session_id=session_id)
    if render_final_text and turn.result.assistant_text:
        output_function(turn.result.assistant_text)
    elif not render_final_text and turn.result.assistant_text:
        output_function("")
    output_function(f"session_id={turn.session_id}")
    output_function(f"stop_reason={turn.result.stop_reason.value}")
    return turn


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


def _print_trace(trace: TraceProjection, *, file: TextIO | None = None) -> None:
    print(
        f"trace={json.dumps(_trace_data(trace), ensure_ascii=False, sort_keys=True)}",
        file=file or sys.stderr,
    )


async def run_cli(startup: CliStartup) -> int:
    """Resolve, compose, and own one CLI process lifecycle."""

    if startup.prompt is None and startup.output is not None:
        raise ConfigurationError("--output requires a one-shot prompt")
    resolved = resolve_configuration(
        startup.workspace,
        CliOverrides(
            provider=startup.provider,
            model=startup.model,
            base_url=startup.base_url,
            model_timeout_seconds=startup.model_timeout_seconds,
            tool_timeout_seconds=startup.tool_timeout_seconds,
            max_context_tokens=startup.max_context_tokens,
            fake_delay_seconds=startup.fake_delay_seconds,
            data_dir=startup.data_dir,
        ),
    )
    if startup.data_dir is None or startup.prompt is None:
        resolved.data_paths.ensure_project_partition()
    if startup.data_dir is None:
        await import_legacy_sessions(
            resolved.workspace / ".jdagent" / "sessions",
            resolved.session_directory,
        )
    presenter = RichPresenter() if startup.prompt is None else None
    terminal = (
        PromptToolkitTerminal(history_file=resolved.data_paths.input_history)
        if startup.prompt is None
        else None
    )
    approval = ScopedApproval(terminal or RejectingApprovalChoices())
    interaction_observer = InteractionRuntimeObserver() if terminal is not None else None
    model_observers = (presenter,) if presenter is not None else ()
    event_observers = (
        (interaction_observer, RichRuntimeObserver(presenter))
        if presenter is not None and interaction_observer is not None
        else ()
    )
    provider_options: JsonObject = (
        {"deepseek": {"thinking": {"type": "disabled"}}} if resolved.provider == "deepseek" else {}
    )
    composition = build_runtime(
        resolved,
        approval,
        runtime_options=RuntimeOptions(provider_options=provider_options),
        model_event_observers=model_observers,
        event_observers=event_observers,
    )
    try:
        session_id = startup.session_id
        if startup.prompt is not None:
            if session_id is not None:
                recovery = SessionRecovery(JsonlRecoveryStore(resolved.session_directory))
                prepared = await prepare_session_resume(
                    session=composition.session,
                    recovery=recovery,
                    session_id=session_id,
                    workspace_identity=resolved.data_paths.identity,
                    allow_recovery_snapshot=False,
                )
                session_id = prepared.session_id
                for warning in prepared.warnings:
                    print(f"jdagent: warning: {warning}", file=sys.stderr)
            result = await run_headless(
                composition.coordinator,
                startup.prompt,
                provider=resolved.provider,
                model=resolved.model,
                session_id=session_id,
            )
            if startup.output == "json":
                payload = result.json_data()
                if startup.show_trace:
                    payload["trace"] = _trace_data(result.trace)
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                if result.answer:
                    print(result.answer)
                if startup.show_trace:
                    _print_trace(result.trace)
            return int(result.exit_status)

        assert presenter is not None
        assert terminal is not None
        assert interaction_observer is not None
        recovery = SessionRecovery(JsonlRecoveryStore(resolved.session_directory))
        application = InteractiveApplication(
            terminal=terminal,
            presenter=presenter,
            coordinator=composition.coordinator,
            catalog=SessionCatalog(
                session=composition.session,
                discovery=composition.session,
                workspace_identity=resolved.data_paths.identity,
                recovery=recovery,
            ),
            session=composition.session,
            context=InteractiveContext(
                provider=resolved.provider,
                model=resolved.model,
                workspace=resolved.workspace,
                write_permission=resolved.write_permission,
                model_timeout_seconds=resolved.model_timeout_seconds,
                tool_timeout_seconds=resolved.tool_timeout_seconds,
                max_context_tokens=resolved.max_context_tokens,
            ),
            initial_session_id=session_id,
            recovery=recovery,
            workspace_identity=resolved.data_paths.identity,
        )
        interaction_observer.bind(application)
        return await application.run()
    finally:
        if presenter is not None:
            await presenter.close()
        await composition.aclose()
