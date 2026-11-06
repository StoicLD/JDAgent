"""Session-scoped MCP discovery, kept independent of knowledge ingestion and RAG."""

import asyncio
import hmac
import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import uuid4

from jsonschema import Draft202012Validator, SchemaError

from jdagent.domain.json import JsonObject, JsonValue, require_array, require_object, require_string
from jdagent.domain.mcp import (
    MCPActivityStatus,
    MCPCapability,
    MCPCapabilityKind,
    MCPCatalogView,
    MCPDirectoryLimits,
    MCPDirectoryState,
    MCPFailure,
    MCPMethod,
    MCPProgressCallback,
    MCPRejectedCapability,
    MCPSearchHit,
    MCPSearchResult,
    MCPServerStatus,
    MCPTask,
    MCPTaskSnapshot,
    MCPTaskStatus,
)
from jdagent.mcp_configuration import MCPServerConfiguration
from jdagent.mcp_definitions import (
    MCPDefinitionCache,
    MCPDefinitionLimits,
    MCPSelection,
    definition_digest,
)
from jdagent.ports import MCPConnectionPort


@dataclass(slots=True)
class _Activity:
    name: str
    operation: str
    started: float = field(default_factory=time.monotonic)
    progress: float | None = None
    total: float | None = None
    task_status: MCPTaskStatus | None = None

    def snapshot(self) -> MCPActivityStatus:
        return MCPActivityStatus(
            self.name,
            self.operation,
            max(0, time.monotonic() - self.started),
            self.progress,
            self.total,
            self.task_status,
        )


@dataclass(slots=True)
class _Server:
    configuration: MCPServerConfiguration
    generation: str = field(default_factory=lambda: uuid4().hex)
    peer: MCPConnectionPort | None = None
    entries: tuple[MCPCapability, ...] = ()
    directory: MCPDirectoryState = "not_loaded"
    reason: str | None = None
    terms: tuple[Counter[str], ...] = ()
    metadata_bytes: int = 0
    index_bytes: int = 0
    touched: int = 0
    rejected: tuple[MCPRejectedCapability, ...] = ()
    quarantined: bool = False
    activity: _Activity | None = None

    def progress_callback(self) -> MCPProgressCallback | None:
        activity = self.activity
        if activity is None:
            return None
        activity.progress, activity.total = None, None

        async def report(progress: float, total: float | None) -> None:
            if self.activity is activity:
                activity.progress, activity.total = progress, total

        return report

    def status(self) -> MCPServerStatus:
        information = None if self.peer is None else self.peer.information
        return MCPServerStatus(
            self.configuration.server_id,
            self.generation,
            "not_connected" if self.peer is None else self.peer.state,
            self.directory if self.configuration.enabled else "disabled",
            None if information is None else information.protocol_version,
            self.reason,
            len(self.entries) + len(self.rejected),
            self.metadata_bytes,
            self.index_bytes,
            self.rejected,
            None if self.activity is None else self.activity.snapshot(),
        )


class MCPService:
    """Own lazy peers and reconstructible directories for each active Session."""

    def __init__(
        self,
        connection_factory: Callable[[MCPServerConfiguration], MCPConnectionPort],
        *,
        limits: MCPDirectoryLimits | None = None,
        definition_limits: MCPDefinitionLimits | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._limits = limits if limits is not None else MCPDirectoryLimits()
        self._tick = 0
        self._sessions: dict[str, dict[str, _Server]] = {}
        self._retired: list[MCPConnectionPort] = []
        self._cleanup: set[asyncio.Task[bool]] = set()
        self._definition_limits = definition_limits or MCPDefinitionLimits()
        self._definitions: dict[str, MCPDefinitionCache] = {}

    async def configure(
        self, session_id: str, configurations: tuple[MCPServerConfiguration, ...]
    ) -> None:
        current = self._sessions.setdefault(session_id, {})
        definitions = self._definitions.setdefault(
            session_id, MCPDefinitionCache(self._definition_limits)
        )
        allowed = {item.server_id for item in configurations}
        for name in tuple(current):
            if name not in allowed:
                definitions.invalidate(name)
                await self._close_peer(current.pop(name))
        for configuration in configurations:
            old = current.get(configuration.server_id)
            if old is not None and old.configuration == configuration:
                continue
            if old is not None:
                definitions.invalidate(configuration.server_id)
                if not await self._close_peer(old):
                    _discard(old, "failed", "cleanup_pending")
                    continue
            current[configuration.server_id] = _Server(configuration)

    def execution_timeout(self, session_id: str, entry: MCPCapability) -> float:
        config = self._sessions[session_id][entry.server_id].configuration
        if entry.kind != "tool":
            return config.timeout_seconds
        return next(
            (item.timeout_seconds for item in config.tool_overrides if item.name == entry.name),
            config.timeout_seconds,
        )

    def safe_repeat(self, session_id: str, entry: MCPCapability) -> bool:
        config = self._sessions[session_id][entry.server_id].configuration
        return entry.kind == "tool" and any(
            item.name == entry.name and item.safe_repeat for item in config.tool_overrides
        )

    async def call(
        self, session_id: str, entry: MCPCapability, arguments: JsonObject
    ) -> JsonObject | MCPTask:
        """Use the previously exposed host route without reconnecting or replaying."""
        if not self.capability_allowed(session_id, entry):
            raise MCPFailure("configuration")
        server = self._sessions[session_id][entry.server_id]
        assert server.peer is not None
        report = server.progress_callback()
        if entry.kind == "resource":
            return await server.peer.request(
                "resources/read", {"uri": entry.name}, on_progress=report
            )
        if entry.kind == "prompt":
            return await server.peer.request(
                "prompts/get", {"name": entry.name, "arguments": arguments}, on_progress=report
            )
        raw = await server.peer.request(
            "tools/call", {"name": entry.name, "arguments": arguments}, on_progress=report
        )
        if raw.get("resultType") != "task":
            return raw
        task = MCPTask.from_data(raw)
        if server.activity is not None:
            server.activity.task_status = task.status
        return task

    def begin_activity(
        self,
        session_id: str,
        entry: MCPCapability,
        operation: str,
        *,
        task_status: MCPTaskStatus | None = None,
    ) -> None:
        """Start one logical wait, including retries and Task polling gaps."""
        self._sessions[session_id][entry.server_id].activity = _Activity(
            entry.name,
            operation,
            task_status=task_status,
        )

    def end_activity(self, session_id: str, entry: MCPCapability) -> None:
        """Discard the transient display when this serialized operation ends."""
        server = self._sessions.get(session_id, {}).get(entry.server_id)
        if server is not None:
            server.activity = None

    def task_identity(self, session_id: str, entry: MCPCapability) -> str:
        peer = self._task_peer(session_id, entry)
        assert peer.information is not None
        return peer.information.identity_digest

    async def bind_task(
        self, session_id: str, entry: MCPCapability, identity_digest: str
    ) -> MCPCapability:
        """Reconnect only for explicit control; verify identity before sending a Task ID."""
        server = self._sessions.get(session_id, {}).get(entry.server_id)
        if server is None or not server.configuration.enabled:
            raise MCPFailure("configuration")
        if server.peer is not None and (server.quarantined or server.peer.state != "ready"):
            if not await self._close_peer(server):
                raise MCPFailure("connection")
            self._definitions[session_id].invalidate(entry.server_id)
            server.peer = None
            server.generation = uuid4().hex
            server.quarantined = False
            _discard(server, "not_loaded", "task_reconnect")
        if server.peer is None:
            server.peer = self._connection_factory(server.configuration)
        information = await server.peer.connect()
        if not identity_digest or not hmac.compare_digest(
            information.identity_digest, identity_digest
        ):
            raise MCPFailure("identity_mismatch")
        if not information.tasks:
            raise MCPFailure("unsupported")
        return replace(entry, generation=server.generation)

    def supports_tasks(self, session_id: str, entry: MCPCapability) -> bool:
        peer = self._sessions[session_id][entry.server_id].peer
        return bool(
            entry.kind == "tool"
            and peer is not None
            and peer.information is not None
            and peer.information.tasks
        )

    async def get_task(
        self, session_id: str, entry: MCPCapability, task: MCPTask
    ) -> MCPTaskSnapshot:
        raw = await self._task_peer(session_id, entry).request(
            "tasks/get",
            {"taskId": task.task_id},
            on_progress=self._sessions[session_id][entry.server_id].progress_callback(),
        )
        observed = MCPTask.from_data(raw)
        if datetime.fromisoformat(observed.created_at) != datetime.fromisoformat(task.created_at):
            raise MCPFailure("protocol")
        if task.status in {"completed", "failed", "cancelled"} and observed.status != task.status:
            raise MCPFailure("protocol")
        result = raw.get("result") if observed.status == "completed" else None
        activity = self._sessions[session_id][entry.server_id].activity
        if activity is not None:
            activity.task_status = observed.status
        return MCPTaskSnapshot(
            observed,
            None if result is None else require_object(result, "Task result"),
        )

    async def cancel_task(self, session_id: str, entry: MCPCapability, task: MCPTask) -> None:
        await self._task_peer(session_id, entry).request(
            "tasks/cancel",
            {"taskId": task.task_id},
            on_progress=self._sessions[session_id][entry.server_id].progress_callback(),
        )

    def quarantine(self, session_id: str, entry: MCPCapability) -> None:
        """Forbid reuse immediately while retaining bounded resource-close ownership."""
        server = self._sessions[session_id][entry.server_id]
        if server.generation != entry.generation:
            return
        server.quarantined = True
        _discard(server, "failed", "cleanup_pending")
        self._definitions[session_id].invalidate(entry.server_id)
        if server.peer is not None:
            if server.peer not in self._retired:
                self._retired.append(server.peer)
            closing = asyncio.create_task(server.peer.aclose(), name="mcp-quarantined-close")
            self._cleanup.add(closing)
            closing.add_done_callback(self._cleanup_finished)

    def _cleanup_finished(self, task: asyncio.Task[bool]) -> None:
        self._cleanup.discard(task)
        if not task.cancelled():
            task.exception()

    def _task_peer(self, session_id: str, entry: MCPCapability) -> MCPConnectionPort:
        server = self._sessions.get(session_id, {}).get(entry.server_id)
        if (
            server is None
            or not server.configuration.enabled
            or server.quarantined
            or server.generation != entry.generation
            or server.peer is None
            or server.peer.state != "ready"
        ):
            raise MCPFailure("configuration")
        peer = server.peer
        if peer.information is None or not peer.information.tasks:
            raise MCPFailure("unsupported")
        return peer

    def material_enabled(self, session_id: str, kind: MCPCapabilityKind) -> bool:
        """Inspect only host configuration; exposing controls must not connect a peer."""
        return any(
            server.configuration.enabled
            and (
                server.configuration.prompts if kind == "prompt" else server.configuration.resources
            )
            for server in self._sessions.get(session_id, {}).values()
        )

    def capability_allowed(self, session_id: str, entry: MCPCapability) -> bool:
        if entry.kind == "tool":
            return self.tool_allowed(session_id, entry.server_id, entry.name, entry.generation)
        server = self._sessions.get(session_id, {}).get(entry.server_id)
        if (
            server is None
            or not server.configuration.enabled
            or server.generation != entry.generation
            or server.peer is None
            or server.quarantined
            or server.peer.state != "ready"
            or server.peer.information is None
        ):
            return False
        return (
            (server.configuration.prompts and server.peer.information.prompts)
            if entry.kind == "prompt"
            else (server.configuration.resources and server.peer.information.resources)
        )

    async def resolve_material(
        self, session_id: str, server_id: str, kind: MCPCapabilityKind, name: str
    ) -> MCPCapability:
        """Resolve Prompt metadata or a concrete remote URI, never a host filesystem path."""
        server = self._sessions.get(session_id, {}).get(server_id)
        if (
            server is None
            or not server.configuration.enabled
            or kind not in {"resource", "prompt"}
            or not (
                server.configuration.prompts if kind == "prompt" else server.configuration.resources
            )
        ):
            raise MCPFailure("configuration")
        directory = await self.capabilities(session_id, server_id=server_id)
        if kind == "resource":
            entry = MCPCapability(
                server_id,
                server.generation,
                "resource",
                name,
                "",
                json.dumps({"uri": name}, sort_keys=True),
            )
        else:
            found = next(
                (item for item in directory.entries if item.kind == "prompt" and item.name == name),
                None,
            )
            if found is None:
                raise MCPFailure("unsupported")
            entry = found
        if not self.capability_allowed(session_id, entry):
            raise MCPFailure("unsupported")
        return entry

    async def refresh(self, session_id: str, *, server_id: str | None = None) -> None:
        """Replace idle peers and invalidate their old configuration generation."""
        servers = self._sessions[session_id]
        if server_id is not None and server_id not in servers:
            raise ValueError("Unknown MCP Server")
        for name in tuple(servers) if server_id is None else (server_id,):
            server = servers[name]
            self._definitions[session_id].invalidate(name)
            if not await self._close_peer(server):
                _discard(server, "failed", "cleanup_pending")
            else:
                servers[name] = _Server(server.configuration)

    def status(
        self, session_id: str, configurations: tuple[MCPServerConfiguration, ...]
    ) -> tuple[MCPServerStatus, ...]:
        current = self._sessions.get(session_id, {})
        return tuple(
            current[item.server_id].status()
            if item.server_id in current
            else MCPServerStatus(
                item.server_id, None, "not_connected", "not_loaded" if item.enabled else "disabled"
            )
            for item in configurations
        )

    async def capabilities(
        self, session_id: str, *, server_id: str | None = None
    ) -> MCPCatalogView:
        servers = self._sessions[session_id]
        if server_id is not None and server_id not in servers:
            raise ValueError("Unknown MCP Server")
        names = tuple(servers) if server_id is None else (server_id,)
        for name in names:
            server = servers[name]
            if server.configuration.enabled and server.directory in {"not_loaded", "evicted"}:
                await self._load(server)
                self._admit(servers, server)
            self._touch(server)
        if server_id is not None and servers[server_id].directory == "evicted":
            await self._load(servers[server_id])
            self._admit(servers, servers[server_id])
            self._touch(servers[server_id])
        selected = [
            server for name, server in servers.items() if server_id is None or name == server_id
        ]
        return MCPCatalogView(
            tuple(entry for server in selected for entry in server.entries),
            tuple(server.status() for server in selected),
        )

    def _touch(self, server: _Server) -> None:
        self._tick += 1
        server.touched = self._tick

    def _admit(self, servers: dict[str, _Server], incoming: _Server) -> None:
        while (
            sum(len(item.entries) + len(item.rejected) for item in servers.values())
            > self._limits.entries
            or sum(item.metadata_bytes for item in servers.values()) > self._limits.metadata_bytes
            or sum(item.index_bytes for item in servers.values()) > self._limits.index_bytes
        ):
            evicted = min(
                (
                    item
                    for item in servers.values()
                    if item is not incoming and (item.entries or item.rejected)
                ),
                key=lambda item: item.touched,
            )
            evicted.entries, evicted.terms = (), ()
            evicted.rejected = ()
            evicted.metadata_bytes = evicted.index_bytes = 0
            evicted.directory, evicted.reason = "evicted", "capacity_eviction"

    def search(
        self,
        session_id: str,
        query: str,
        *,
        kind: MCPCapabilityKind = "tool",
        server_id: str | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> MCPSearchResult:
        """Rank cached names/descriptions with BM25; never call embeddings or RAG."""
        self.validate_search(query, offset=offset, limit=limit)
        servers = self._sessions[session_id]
        if server_id is not None and server_id not in servers:
            raise ValueError("Unknown MCP Server")
        selected = [
            server for name, server in servers.items() if server_id is None or name == server_id
        ]
        corpus = [
            (entry, terms)
            for server in selected
            for entry, terms in zip(server.entries, server.terms, strict=True)
            if entry.kind == kind
        ]
        query_terms = set(_terms(query))
        frequencies: Counter[str] = Counter()
        for _, terms in corpus:
            frequencies.update(query_terms.intersection(terms))
        average = sum(sum(terms.values()) for _, terms in corpus) / max(1, len(corpus))
        hits: list[MCPSearchHit] = []
        for entry, terms in corpus:
            length = sum(terms.values())
            score = 0.0
            for term in query_terms:
                count = terms[term]
                if not count:
                    continue
                inverse = math.log(
                    1 + (len(corpus) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
                )
                score += (
                    inverse * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / max(1, average)))
                )
            if score > 0:
                hits.append(MCPSearchHit(entry, score))
        hits.sort(key=lambda hit: (-hit.score, hit.capability.server_id, hit.capability.name))
        end = offset + limit
        return MCPSearchResult(
            tuple(hits[offset:end]),
            tuple(server.status() for server in selected),
            end if end < len(hits) else None,
            (
                "Try different keywords, English names, or a Server filter; "
                "check incomplete directories."
            )
            if not hits
            else None,
        )

    @staticmethod
    def validate_search(query: str, *, offset: int, limit: int) -> None:
        """Reject invalid retrieval input before any peer can be started."""
        if not query.strip() or len(query) > 512:
            raise ValueError("MCP query must contain 1-512 characters")
        if offset < 0 or not 1 <= limit <= 20:
            raise ValueError("MCP search requires a nonnegative offset and limit 1-20")

    async def release_session(self, session_id: str) -> None:
        self._definitions.pop(session_id, None)
        for server in self._sessions.pop(session_id, {}).values():
            await self._close_peer(server)

    def select_tool(self, session_id: str, entry: MCPCapability) -> str:
        return self._definitions[session_id].stage(entry)

    async def loaded_tools(self, session_id: str) -> tuple[MCPCapability, ...]:
        async def resolve(selection: MCPSelection) -> MCPCapability | None:
            server = self._sessions[session_id].get(selection.server_id)
            if (
                server is None
                or server.generation != selection.generation
                or not server.configuration.enabled
            ):
                return None
            directory = await self.capabilities(session_id, server_id=selection.server_id)
            return next(
                (
                    entry
                    for entry in directory.entries
                    if entry.kind == "tool"
                    and entry.name == selection.name
                    and definition_digest(entry) == selection.digest
                ),
                None,
            )

        cache = self._definitions.get(session_id)
        return () if cache is None else await cache.prepare(resolve)

    def tool_allowed(self, session_id: str, server_id: str, name: str, generation: str) -> bool:
        server = self._sessions.get(session_id, {}).get(server_id)
        return bool(
            server
            and server.configuration.enabled
            and not server.quarantined
            and server.generation == generation
            and name in server.configuration.tools
            and server.peer is not None
            and server.peer.state == "ready"
        )

    def tool_used(self, session_id: str, server_id: str, name: str) -> None:
        cache = self._definitions.get(session_id)
        if cache is not None:
            cache.used(server_id, name)

    def unload_tool(self, session_id: str, server_id: str, name: str) -> None:
        cache = self._definitions.get(session_id)
        if cache is not None:
            cache.unload(server_id, name)

    async def aclose(self) -> None:
        for session_id in tuple(self._sessions):
            await self.release_session(session_id)
        retained: list[MCPConnectionPort] = []
        for peer in self._retired:
            if not await peer.aclose():
                retained.append(peer)
        self._retired = retained

    async def _close_peer(self, server: _Server) -> bool:
        if server.peer is not None and not await server.peer.aclose():
            if server.peer not in self._retired:
                self._retired.append(server.peer)
            return False
        return True

    async def _load(self, server: _Server) -> None:
        config = server.configuration
        peer = server.peer or self._connection_factory(config)
        server.peer = peer
        try:
            async with asyncio.timeout(30):
                information = await peer.connect()
                operations: list[tuple[MCPCapabilityKind, MCPMethod, str]] = []
                if information.tools and config.tools:
                    operations.append(("tool", "tools/list", "tools"))
                if information.resources and config.resources:
                    operations.extend(
                        (
                            ("resource", "resources/list", "resources"),
                            ("resource_template", "resources/templates/list", "resourceTemplates"),
                        )
                    )
                if information.prompts and config.prompts:
                    operations.append(("prompt", "prompts/list", "prompts"))
                entries: list[MCPCapability] = []
                indexed: list[Counter[str]] = []
                rejected: list[MCPRejectedCapability] = []
                names: set[tuple[MCPCapabilityKind, str]] = set()
                metadata_bytes = index_bytes = 0
                partial = False
                for kind, method, key in operations:
                    cursor: str | None = None
                    seen: set[str] = set()
                    for _ in range(self._limits.pages_per_kind):
                        params: JsonObject = {} if cursor is None else {"cursor": cursor}
                        result = await peer.request(method, params)
                        for raw in require_array(result[key], key):
                            item = require_object(raw, key)
                            name = require_string(item, "name")
                            if kind == "tool" and name not in config.tools:
                                continue
                            description = item.get("description", "")
                            if not isinstance(description, str):
                                raise ValueError("Invalid MCP description")
                            definition = json.dumps(
                                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                            )
                            metadata_bytes += len(definition.encode("utf-8"))
                            if (
                                len(entries) + len(rejected) >= self._limits.entries
                                or metadata_bytes > self._limits.metadata_bytes
                            ):
                                raise MCPFailure("directory_capacity")
                            if kind in {"tool", "prompt"} and (kind, name) in names:
                                for index, entry in enumerate(entries):
                                    if (entry.kind, entry.name) == (kind, name):
                                        entries.pop(index)
                                        old_terms = indexed.pop(index)
                                        index_bytes -= 128 + sum(
                                            len(term.encode("utf-8")) + 72 for term in old_terms
                                        )
                                        break
                                duplicate = MCPRejectedCapability(name, "duplicate_name", kind)
                                if duplicate not in rejected:
                                    rejected.append(duplicate)
                                continue
                            names.add((kind, name))
                            if kind == "tool" and not _valid_input_schema(item.get("inputSchema")):
                                rejected.append(
                                    MCPRejectedCapability(name, "unsupported_input_schema")
                                )
                                continue
                            if (
                                kind == "tool"
                                and "outputSchema" in item
                                and not _valid_schema(item["outputSchema"])
                            ):
                                rejected.append(
                                    MCPRejectedCapability(name, "unsupported_output_schema")
                                )
                                continue
                            terms, cost = _index(
                                name + " " + description, self._limits.index_bytes - index_bytes
                            )
                            index_bytes += cost
                            indexed.append(terms)
                            entries.append(
                                MCPCapability(
                                    config.server_id,
                                    server.generation,
                                    kind,
                                    name,
                                    description,
                                    definition,
                                )
                            )
                        following = result.get("nextCursor")
                        if following is None:
                            break
                        if not isinstance(following, str) or not following or following in seen:
                            partial = True
                            break
                        cursor = following
                        seen.add(cursor)
                    else:
                        partial = True
                server.entries = tuple(entries)
                server.terms = tuple(indexed)
                server.rejected = tuple(rejected)
                server.metadata_bytes, server.index_bytes = metadata_bytes, index_bytes
                server.directory = "partial" if partial else "ready"
                server.reason = "page_limit_or_repeated_cursor" if partial else None
        except (MCPFailure, TimeoutError, ValueError, KeyError) as error:
            server.directory = "failed"
            server.reason = (
                error.kind
                if isinstance(error, MCPFailure)
                else (
                    "directory_timeout" if isinstance(error, TimeoutError) else "invalid_directory"
                )
            )
            await self._close_peer(server)
        except asyncio.CancelledError:
            server.directory, server.reason = "failed", "cancelled"
            await self._close_peer(server)
            raise


def _discard(server: _Server, state: MCPDirectoryState, reason: str) -> None:
    server.entries, server.terms, server.rejected = (), (), ()
    server.metadata_bytes = server.index_bytes = 0
    server.directory, server.reason = state, reason


def _valid_input_schema(schema: JsonValue) -> bool:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    return _valid_schema(schema)


def _valid_schema(schema: JsonValue) -> bool:
    if not isinstance(schema, dict):
        return isinstance(schema, bool)
    dialect = schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
    if dialect not in (
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    ):
        return False
    pending: list[JsonValue] = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in node:
                    reference = node[key]
                    if not isinstance(reference, str) or not reference.startswith("#"):
                        return False
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    try:
        Draft202012Validator.check_schema(schema)  # pyright: ignore[reportUnknownMemberType]
    except (SchemaError, RecursionError):
        return False
    return True


def _index(text: str, available: int) -> tuple[Counter[str], int]:
    terms: Counter[str] = Counter()
    cost = 128
    if cost > available:
        raise MCPFailure("directory_capacity")
    for term in _terms(text):
        if term not in terms:
            cost += len(term.encode("utf-8")) + 72
            if cost > available:
                raise MCPFailure("directory_capacity")
        terms[term] += 1
    return terms, cost


def _terms(text: str) -> Iterator[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).casefold()
    for match in re.finditer(r"[a-z0-9]+|[\u3400-\u9fff]+", separated):
        run = match.group()
        if "\u3400" <= run[0] <= "\u9fff":
            yield from run
            yield from (run[index : index + 2] for index in range(len(run) - 1))
        else:
            yield run
