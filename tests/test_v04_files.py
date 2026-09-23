import asyncio
import os
from pathlib import Path

import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.domain.tools import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    ToolCall,
    ToolExecutionContext,
)
from jdagent.tools.files import create_file_tools
from jdagent.tools.permissions import DefaultPermissionPolicy
from jdagent.tools.runtime import ToolRegistry, ToolRuntime


def test_subdirectory_scan_inherits_ancestor_ignore_rules(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    (tmp_path / "src" / "hidden.secret").write_text("needle", encoding="utf-8")
    (tmp_path / "src" / "visible.py").write_text("needle", encoding="utf-8")

    async def scenario() -> None:
        runtime = ToolRuntime(
            ToolRegistry(create_file_tools(tmp_path)),
            DefaultPermissionPolicy(),
            FakeApproval(ApprovalDecision.APPROVE),
        )
        for name, key in (("find_files", "paths"), ("search_text", "matches")):
            args = (
                {"directory": "src"}
                if name == "find_files"
                else {"directory": "src", "query": "needle"}
            )
            from jdagent.domain.json import normalize_json, require_object

            result = await runtime.execute(
                ToolCall(name, name, require_object(normalize_json(args), "args")),
                ToolExecutionContext("s", "t", tmp_path),
            )
            assert result.outcome is not None and result.error_code is None
            assert "hidden.secret" not in str(result.outcome.data[key])
            assert "visible.py" in str(result.outcome.data[key])

    asyncio.run(scenario())


def test_edit_uses_full_revision_and_preserves_unmodified_bytes(tmp_path: Path) -> None:
    original = b"\xef\xbb\xbfprice = 3\r\nresult = price - 2\r\n# untouched\n"
    target = tmp_path / "prices.py"
    target.write_bytes(original)

    async def scenario() -> None:
        runtime = ToolRuntime(
            ToolRegistry(create_file_tools(tmp_path)),
            DefaultPermissionPolicy(),
            FakeApproval(ApprovalDecision.APPROVE),
        )
        context = ToolExecutionContext("s", "t", tmp_path)
        read = await runtime.execute(ToolCall("r", "read_file", {"path": "prices.py"}), context)
        assert read.outcome is not None
        revision = read.outcome.data["revision"]
        edited = await runtime.execute(
            ToolCall(
                "e",
                "edit_file",
                {
                    "path": "prices.py",
                    "revision": revision,
                    "edits": [
                        {
                            "old_text": "price = 3\nresult = price - 2",
                            "new_text": "price = 4\nresult = price + 2",
                        }
                    ],
                },
            ),
            context,
        )
        assert edited.error_code is None
        assert (
            target.read_bytes() == b"\xef\xbb\xbfprice = 4\r\nresult = price + 2\r\n# untouched\n"
        )
        stale = await runtime.execute(
            ToolCall(
                "e2",
                "edit_file",
                {
                    "path": "prices.py",
                    "revision": revision,
                    "edits": [{"old_text": "price = 4", "new_text": "price = 9"}],
                },
            ),
            context,
        )
        assert stale.error_code is not None and stale.error_code.value == "conflict"
        assert b"price = 4" in target.read_bytes()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "content,edits",
    [
        (b"repeat repeat", [{"old_text": "repeat", "new_text": "x"}]),
        (b"abcdef", [{"old_text": "abc", "new_text": "x"}, {"old_text": "bc", "new_text": "y"}]),
        (
            b"abcdef",
            [{"old_text": "abc", "new_text": "x"}, {"old_text": "absent", "new_text": "y"}],
        ),
        (b"a\r\nb\nc", [{"old_text": "a\nb\nc", "new_text": "x"}]),
    ],
)
def test_failed_edit_batch_never_changes_bytes(
    tmp_path: Path, content: bytes, edits: list[dict[str, str]]
) -> None:
    from jdagent.domain.json import normalize_json, require_object

    target = tmp_path / "sample.txt"
    target.write_bytes(content)

    async def scenario() -> None:
        runtime = ToolRuntime(
            ToolRegistry(create_file_tools(tmp_path)),
            DefaultPermissionPolicy(),
            FakeApproval(ApprovalDecision.APPROVE),
        )
        context = ToolExecutionContext("s", "t", tmp_path)
        read = await runtime.execute(ToolCall("read", "read_file", {"path": "sample.txt"}), context)
        assert read.outcome is not None
        result = await runtime.execute(
            ToolCall(
                "edit",
                "edit_file",
                require_object(
                    normalize_json(
                        {
                            "path": "sample.txt",
                            "revision": read.outcome.data["revision"],
                            "edits": edits,
                        }
                    ),
                    "args",
                ),
            ),
            context,
        )
        assert result.error_code is not None
        assert target.read_bytes() == content
        assert not result.stop_turn

    asyncio.run(scenario())


def test_change_during_approval_is_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"before")

    class ConcurrentApproval:
        async def request(self, request: ApprovalRequest) -> ApprovalOutcome:
            assert request.display["revision"]
            target.write_bytes(b"external change")
            return ApprovalOutcome(ApprovalDecision.APPROVE)

    async def scenario() -> None:
        runtime = ToolRuntime(
            ToolRegistry(create_file_tools(tmp_path)),
            DefaultPermissionPolicy(),
            ConcurrentApproval(),
        )
        context = ToolExecutionContext("s", "t", tmp_path)
        read = await runtime.execute(ToolCall("r", "read_file", {"path": "sample.txt"}), context)
        assert read.outcome is not None
        result = await runtime.execute(
            ToolCall(
                "e",
                "edit_file",
                {
                    "path": "sample.txt",
                    "revision": read.outcome.data["revision"],
                    "edits": [{"old_text": "before", "new_text": "after"}],
                },
            ),
            context,
        )
        assert result.error_code is not None and result.error_code.value == "conflict"
        assert target.read_bytes() == b"external change"

    asyncio.run(scenario())


def test_create_refuses_existing_and_search_obeys_ignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\n*.cache\n!keep.cache\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "hidden.txt").write_text("needle")
    (tmp_path / "drop.cache").write_text("needle")
    (tmp_path / "keep.cache").write_text("needle")
    (tmp_path / "visible.txt").write_bytes("第一行\r\nneedle [literal]\r\n".encode())

    async def scenario() -> None:
        runtime = ToolRuntime(
            ToolRegistry(create_file_tools(tmp_path)),
            DefaultPermissionPolicy(),
            FakeApproval(ApprovalDecision.APPROVE),
        )
        context = ToolExecutionContext("s", "t", tmp_path)
        found = await runtime.execute(ToolCall("f", "find_files", {}), context)
        assert found.outcome is not None
        paths = found.outcome.data["paths"]
        assert isinstance(paths, list)
        assert set(str(path) for path in paths) == {".gitignore", "keep.cache", "visible.txt"}
        searched = await runtime.execute(
            ToolCall("s", "search_text", {"query": "[literal]"}), context
        )
        assert searched.outcome is not None
        assert searched.outcome.data["matches"] == [
            {"path": "visible.txt", "line": 2, "content": "needle [literal]"}
        ]
        created = await runtime.execute(
            ToolCall("c", "create_file", {"path": "keep.cache", "content": "replace"}), context
        )
        assert created.error_code is not None
        assert (tmp_path / "keep.cache").read_text() == "needle"

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["../escape", "NUL", "sample.txt:stream", "name."])
def test_unsupported_paths_are_rejected_before_creation(tmp_path: Path, path: str) -> None:
    runtime = ToolRuntime(
        ToolRegistry(create_file_tools(tmp_path)),
        DefaultPermissionPolicy(),
        FakeApproval(ApprovalDecision.APPROVE),
    )
    result = asyncio.run(
        runtime.execute(
            ToolCall("c", "create_file", {"path": path, "content": "bad"}),
            ToolExecutionContext("s", "t", tmp_path),
        )
    )
    assert result.error_code is not None
    assert not list(tmp_path.iterdir())


def test_hardlink_read_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "a"
    target.write_text("secret")
    os.link(target, tmp_path / "b")
    runtime = ToolRuntime(
        ToolRegistry(create_file_tools(tmp_path)),
        DefaultPermissionPolicy(),
        FakeApproval(ApprovalDecision.APPROVE),
    )
    result = asyncio.run(
        runtime.execute(
            ToolCall("r", "read_file", {"path": "b"}), ToolExecutionContext("s", "t", tmp_path)
        )
    )
    assert result.error_code is not None


def test_exclusive_file_handle_blocks_external_write_and_rename(tmp_path: Path) -> None:
    from jdagent.adapters.windows_files import locked_ancestors, locked_file

    target = tmp_path / "sample.txt"
    target.write_bytes(b"original")
    with locked_ancestors(tmp_path, target), locked_file(target, write=True):
        with pytest.raises(OSError):
            target.write_bytes(b"external")
        with pytest.raises(OSError):
            target.rename(tmp_path / "renamed.txt")
    assert target.read_bytes() == b"original"
