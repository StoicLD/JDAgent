"""Bounded byte transports beneath the official MCP protocol implementation."""

import zlib
from collections.abc import AsyncIterator

import httpx2


class ResponseLimitExceeded(httpx2.ReadError):
    """A response exceeded the configured receive budget before full parsing."""


class ResponseEncodingError(httpx2.ReadError):
    """The transport could not safely decode a complete response."""


class _LimitedStream(httpx2.AsyncByteStream):
    def __init__(self, source: httpx2.AsyncByteStream, limit_bytes: int, encoding: str) -> None:
        self._source = source
        self._limit = limit_bytes
        self._encoding = encoding

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        decoder = (
            zlib.decompressobj(31 if self._encoding == "gzip" else 15)
            if self._encoding in {"gzip", "deflate"}
            else None
        )
        try:
            async for chunk in self._source:
                pending = chunk
                while pending:
                    if decoder is not None:
                        if decoder.eof:
                            if self._encoding != "gzip":
                                raise ResponseEncodingError("Trailing compressed response data")
                            decoder = zlib.decompressobj(31)
                        # Bound allocation inside decompression, not just the returned chunk.
                        decoded = decoder.decompress(pending, self._limit - received + 1)
                        pending = decoder.unused_data or decoder.unconsumed_tail
                    else:
                        decoded, pending = pending, b""
                    received += len(decoded)
                    if received > self._limit:
                        raise ResponseLimitExceeded("MCP response exceeds the receive limit")
                    if decoded:
                        yield decoded
            if decoder is not None and not decoder.eof:
                raise ResponseEncodingError("Incomplete compressed response")
        except zlib.error:
            raise ResponseEncodingError("Invalid compressed response") from None
        finally:
            await self._source.aclose()

    async def aclose(self) -> None:
        await self._source.aclose()


class BoundedHttpTransport(httpx2.AsyncBaseTransport):
    """Own an HTTP transport and bound each response before protocol parsing."""

    def __init__(self, transport: httpx2.AsyncBaseTransport, *, limit_bytes: int = 1 << 20) -> None:
        if limit_bytes <= 0:
            raise ValueError("Receive limit must be positive")
        self._transport = transport
        self._limit = limit_bytes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        request.headers["accept-encoding"] = "gzip, deflate"
        response = await self._transport.handle_async_request(request)
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"identity", "gzip", "deflate"}:
            await response.aclose()
            raise ResponseEncodingError("Unsupported response content encoding")
        headers = httpx2.Headers(response.headers)
        # Our stream decodes once with an allocation budget; HTTPX must not decode again.
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        return httpx2.Response(
            response.status_code,
            headers=headers,
            stream=_LimitedStream(response.stream, self._limit, encoding),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()
