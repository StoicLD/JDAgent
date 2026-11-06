"""Published SDK server, with no JDAgent imports or hand-written protocol responses."""

import asyncio
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import uvicorn
from mcp.server import MCPServer
from pydantic import Field
from starlette.applications import Starlette

server: MCPServer[None] = MCPServer("jdagent-interop-fixture", version="1", log_level="ERROR")


@server.tool(structured_output=False)
def echo(value: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Value"})]) -> str:
    """Echo a value after the SDK validates its routing header."""
    return "SDK tool: " + value


@server.resource("docs://manual", name="manual")
def manual() -> str:
    return "SDK manual"


@server.resource("docs://{page}", name="page")
def page(page: str) -> str:
    return "SDK resource: " + page


@server.prompt()
def explain(topic: str) -> str:
    """Explain the requested topic."""
    return "SDK prompt: " + topic


def main() -> None:
    mode = sys.argv[1]
    if mode == "stdio":
        server.run()
        return
    app = server.streamable_http_app(json_response=mode == "json", stateless_http=True)
    original_lifespan = app.router.lifespan_context
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])

        @asynccontextmanager
        async def ready(application: Starlette) -> AsyncGenerator[None]:
            async with original_lifespan(application):
                await asyncio.to_thread(
                    Path(sys.argv[2]).write_text, f"http://127.0.0.1:{port}/mcp", encoding="utf-8"
                )
                yield

        app.router.lifespan_context = ready
        uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on")).run(
            sockets=[listener]
        )


if __name__ == "__main__":
    main()
