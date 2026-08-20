"""Text CLI adapter: parse input, render events, and call the application seam."""

import argparse
import asyncio
import importlib.metadata
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from jdagent.application.headless import ExitStatus
from jdagent.configuration import ConfigurationError
from jdagent.domain.errors import SessionError
from jdagent.host import CliStartup, run_cli
from jdagent.host import run_single_turn as run_single_turn


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jdagent", description="JDAgent interactive agent CLI")
    parser.add_argument("prompt", nargs="?", help="Run one prompt and exit")
    parser.add_argument("--session-id", help="Resume an existing session")
    parser.add_argument("--provider", choices=("fake", "deepseek"))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--model-timeout-seconds", type=float)
    parser.add_argument("--tool-timeout-seconds", type=float)
    parser.add_argument("--max-context-tokens", type=int)
    parser.add_argument(
        "--fake-delay-seconds",
        type=float,
        help="Inject Fake Model latency for offline failure exercises",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--show-trace",
        action="store_true",
        help="Print a safe structured trace after each turn",
    )
    parser.add_argument("--output", choices=("text", "json"))
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {importlib.metadata.version('jdagent')}",
    )
    return parser


def _parse_arguments(argv: Sequence[str] | None) -> CliStartup:
    namespace = _parser().parse_args(argv)
    workspace = cast(Path, namespace.workspace)
    supplied_data_dir = cast(Path | None, namespace.data_dir)
    return CliStartup(
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
        data_dir=supplied_data_dir,
        show_trace=cast(bool, namespace.show_trace),
        output=cast(str | None, namespace.output),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI with a composition root selected from explicit arguments."""

    try:
        return asyncio.run(run_cli(_parse_arguments(argv)))
    except SessionError as error:
        print(f"jdagent: {error}", file=sys.stderr)
        return int(ExitStatus.SESSION_ERROR)
    except (OSError, ConfigurationError, ValueError) as error:
        print(f"jdagent: {error}", file=sys.stderr)
        return int(ExitStatus.USAGE_OR_CONFIG_ERROR)
    except KeyboardInterrupt:
        return int(ExitStatus.CANCELLED)
    except Exception as error:
        print(f"jdagent: internal error ({type(error).__name__})", file=sys.stderr)
        return int(ExitStatus.INTERNAL_ERROR)
