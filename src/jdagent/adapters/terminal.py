"""使用 Prompt Toolkit 读取终端输入、展示事件和询问工具授权。"""

import asyncio
import json
from asyncio import CancelledError
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.output import Output
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from jdagent.application.approval import ApprovalChoice, ApprovalScope
from jdagent.application.interactive import (
    CommandError,
    ExitAction,
    InteractionState,
    TerminalStatus,
    UiEvent,
    UiEventKind,
    UserAction,
    parse_user_action,
)
from jdagent.domain.events import (
    PermissionRequestedPayload,
    RuntimeEvent,
    RuntimeEventType,
    ToolCallRequestedPayload,
    ToolExecutionCompletedPayload,
)
from jdagent.domain.model import ModelEvent, TextDelta
from jdagent.domain.tools import ApprovalDecision, ApprovalRequest

_COMMANDS = (
    "/help",
    "/status",
    "/new",
    "/sessions",
    "/resume",
    "/rename",
    "/permissions",
    "/knowledge",
    "/logs",
    "/compact",
    "/context-window",
    "/mcp status",
    "/mcp list",
    "/mcp refresh",
    "/mcp tasks",
    "/mcp task",
    "/mcp prompt",
    "/cancel",
    "/trace",
    "/exit",
)


def format_approval_request(request: ApprovalRequest) -> str:
    """完整展示待审批的实际参数，并转义控制字符，避免参数内容改变终端显示。"""

    parameters = json.dumps(
        request.display or request.arguments, ensure_ascii=False, sort_keys=True
    )
    return (
        f"Approval required: tool={request.tool_name} target={request.target or 'n/a'} "
        f"parameters={parameters} risk={request.risk.value} call={request.call_id}"
    )


def _key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def newline(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        event.current_buffer.insert_text("\n")

    return bindings


class PromptToolkitTerminal:
    """通过 Prompt Toolkit 读取用户输入，将按键和命令转换为应用层操作。"""

    def __init__(
        self,
        *,
        history_file: Path,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        self._state = InteractionState.STARTING
        self._closed = False
        self._actions: asyncio.Queue[UserAction | CommandError] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._line: asyncio.Future[str] | None = None
        self._line_prompt = "> "
        self._draft = ""
        self._status = TerminalStatus(
            InteractionState.STARTING,
            "new",
            "unknown",
            "ask",
        )
        self._session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_file)),
            completer=WordCompleter(_COMMANDS, sentence=True),
            complete_while_typing=True,
            key_bindings=_key_bindings(),
            input=input,
            output=output,
        )

    async def next_action(self, state: InteractionState) -> UserAction:
        self._state = state
        self._ensure_reader()
        action = await self._actions.get()
        if isinstance(action, CommandError):
            raise action
        return action

    def _ensure_reader(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_input())

    async def _ask_line(self, prompt: str, *, multiline: bool = False) -> str:
        del multiline
        if self._line is not None:
            raise RuntimeError("Only one approval prompt may be active")
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        buffer = self._session.default_buffer
        if buffer.text:
            self._draft = buffer.text
            buffer.text = ""
        self._line = future
        self._line_prompt = prompt
        self._ensure_reader()
        self._session.app.invalidate()
        try:
            return await future
        finally:
            self._line = None
            self._line_prompt = "> "
            self._session.app.invalidate()

    async def _read_input(self) -> None:
        """协调输入读取与终端控制事件，将本次输入交还给交互应用。"""
        while not self._closed:
            try:
                raw = await self._session.prompt_async(
                    lambda: self._line_prompt,
                    multiline=True,
                    default=self._draft if self._line is None else "",
                    bottom_toolbar=lambda: (
                        f" JDAgent | {self._status.session} | {self._status.model} | "
                        f"write={self._status.write_permission} | {self._status.state.value} "
                        f"{self._status.activity}"
                    ),
                )
            except EOFError:
                if self._line is not None and not self._line.done():
                    self._line.set_exception(EOFError())
                await self._actions.put(ExitAction())
                return
            except KeyboardInterrupt:
                if self._line is not None and not self._line.done():
                    self._line.set_exception(CancelledError())
                await self._actions.put(ExitAction(cancelled=True))
                await asyncio.sleep(0)
                continue
            if not raw.strip().startswith("/") and self._line is not None:
                if not self._line.done():
                    self._line.set_result(raw)
                await asyncio.sleep(0)
                continue
            try:
                action = parse_user_action(raw)
            except CommandError as error:
                if str(error) == "Input must not be empty":
                    continue
                await self._actions.put(error)
            else:
                if not raw.strip().startswith("/"):
                    self._draft = raw if self._state is not InteractionState.IDLE else ""
                await self._actions.put(action)
            await asyncio.sleep(0)

    async def update_status(self, status: TerminalStatus) -> None:
        self._status = status
        self._state = status.state
        self._session.app.invalidate()

    async def request(self, request: ApprovalRequest) -> ApprovalChoice:
        """展示待授权的操作，读取用户选定的授权范围并返回相应选项。"""
        try:
            answer = await self._ask_line(
                f"{format_approval_request(request)}\n"
                + (
                    "Allow [o]nce or session [c]ommand prefix? [N] "
                    if "program" in request.display
                    else "Allow [o]nce, session [f]ile, session [d]irectory? [N] "
                ),
                multiline=False,
            )
        except KeyboardInterrupt as error:
            raise CancelledError from error
        except EOFError:
            return ApprovalChoice(ApprovalDecision.REJECT)
        normalized = answer.strip().lower()
        if normalized in {"o", "once", "y", "yes"}:
            return ApprovalChoice(ApprovalDecision.APPROVE, ApprovalScope.ONCE)
        if normalized == "c" and "program" in request.display:
            args = request.display.get("args", [])
            if not isinstance(args, list) or not args:
                return ApprovalChoice(ApprovalDecision.REJECT)
            try:
                raw_count = await self._ask_line(
                    f"Fixed leading argument count (1..{len(args)}): ", multiline=False
                )
                count = int(raw_count)
                if not 1 <= count <= len(args):
                    return ApprovalChoice(ApprovalDecision.REJECT)
                confirm = await self._ask_line(
                    "Allow this fixed prefix with ANY variable trailing arguments/options: "
                    + json.dumps(args[:count], ensure_ascii=False)
                    + "? [y/N] ",
                    multiline=False,
                )
                if confirm.strip().lower() == "y":
                    return ApprovalChoice(
                        ApprovalDecision.APPROVE, ApprovalScope.SESSION_COMMAND, count
                    )
            except (ValueError, EOFError, KeyboardInterrupt):
                pass
            return ApprovalChoice(ApprovalDecision.REJECT)
        if normalized in {"f", "file"}:
            return ApprovalChoice(ApprovalDecision.APPROVE, ApprovalScope.SESSION_FILE)
        if normalized in {"d", "directory", "dir"}:
            return ApprovalChoice(ApprovalDecision.APPROVE, ApprovalScope.SESSION_DIRECTORY)
        return ApprovalChoice(ApprovalDecision.REJECT)

    async def close(self) -> None:
        self._closed = True
        if self._line is not None:
            self._line.cancel()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)


class RichPresenter:
    """使用 Rich 展示界面事件和模型的流式文本。"""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._streamed_text = ""
        self._state_text = InteractionState.STARTING.value
        self._live: Live | None = None

    def _render_live(self) -> Group:
        body = Markdown(self._streamed_text) if self._streamed_text else Text("")
        return Group(body, Text(f"JDAgent | {self._state_text}", style="dim"))

    def _ensure_live(self) -> None:
        if self._live is None:
            self._live = Live(
                self._render_live(),
                console=self._console,
                refresh_per_second=12,
                transient=False,
            )
            self._live.start(refresh=True)
        else:
            self._live.update(self._render_live(), refresh=True)

    def _stop_live(self, *, reset_text: bool) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        if reset_text:
            self._streamed_text = ""

    async def observe(self, event: ModelEvent) -> None:
        if not isinstance(event, TextDelta):
            return
        self._streamed_text += event.text
        self._ensure_live()

    async def publish(self, event: UiEvent) -> None:
        if event.kind is UiEventKind.STATE:
            self._state_text = event.message
            if event.state is InteractionState.WAITING_APPROVAL:
                # 读取授权选择期间，底部状态栏由 Prompt Toolkit 绘制。
                self._stop_live(reset_text=False)
            elif event.state in {
                InteractionState.RUNNING_TOOL,
                InteractionState.CANCELLING,
            }:
                self._ensure_live()
            elif self._live is not None:
                self._live.update(self._render_live(), refresh=True)
            return
        if event.kind is UiEventKind.ASSISTANT:
            if self._live is not None:
                self._streamed_text = event.message
                self._live.update(self._render_live(), refresh=True)
                self._stop_live(reset_text=True)
            else:
                self._console.print(Markdown(event.message))
            return
        if event.kind is UiEventKind.ERROR:
            self._stop_live(reset_text=True)
            self._console.print(f"Error: {event.message}", style="bold red", markup=False)
        elif event.kind is UiEventKind.WARNING:
            self._stop_live(reset_text=True)
            self._console.print(f"Warning: {event.message}", style="yellow", markup=False)
        else:
            self._console.print(event.message, markup=False)

    async def close(self) -> None:
        self._stop_live(reset_text=True)


class RichRuntimeObserver:
    """将已持久化的工具生命周期事件转换为界面可以展示的状态变化。"""

    def __init__(self, presenter: RichPresenter) -> None:
        self._presenter = presenter

    async def observe(self, event: RuntimeEvent) -> None:
        payload = event.payload
        if event.event_type is RuntimeEventType.TOOL_CALL_REQUESTED and isinstance(
            payload, ToolCallRequestedPayload
        ):
            await self._presenter.publish(
                UiEvent(UiEventKind.INFO, f"tool requested: {payload.call.name}")
            )
        elif event.event_type is RuntimeEventType.PERMISSION_REQUESTED and isinstance(
            payload, PermissionRequestedPayload
        ):
            target = payload.request.target or "n/a"
            await self._presenter.publish(
                UiEvent(
                    UiEventKind.INFO,
                    f"approval requested: {payload.request.tool_name} target={target}",
                )
            )
        elif event.event_type is RuntimeEventType.TOOL_EXECUTION_COMPLETED and isinstance(
            payload, ToolExecutionCompletedPayload
        ):
            await self._presenter.publish(
                UiEvent(
                    UiEventKind.INFO,
                    f"tool {payload.result.status.value}: {payload.result.tool_name}",
                )
            )
