"""One durable reservation policy shared by foreground and maintenance requests."""

import asyncio
from dataclasses import dataclass

from jdagent.memory_store import MemoryStore


@dataclass(slots=True)
class RequestAccounting:
    store: MemoryStore
    workspace: str
    total_input: int = 30_000_000
    total_output: int = 3_000_000
    maintenance_input: int = 10_000_000
    maintenance_output: int = 1_000_000

    async def reserve(
        self, request: str, kind: str, input_tokens: int, output_tokens: int, manifest: str
    ) -> None:
        await asyncio.to_thread(
            self.store.reserve,
            request,
            self.workspace,
            kind,
            input_tokens,
            output_tokens,
            input_limit=self.total_input,
            output_limit=self.total_output,
            maintenance_input_limit=self.maintenance_input,
            maintenance_output_limit=self.maintenance_output,
            manifest=manifest,
        )

    async def settle(self, request: str, input_tokens: int, output_tokens: int) -> None:
        await asyncio.to_thread(self.store.settle, request, input_tokens, output_tokens)
