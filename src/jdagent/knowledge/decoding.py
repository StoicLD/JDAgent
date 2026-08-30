"""Strict decoding for raw source artifacts."""

from __future__ import annotations

import codecs
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import cast

from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode


@dataclass(frozen=True, slots=True)
class DecodedText:
    text: str
    encoding: str
    method: str


def decode_source(data: bytes, *, encoding: str | None = None) -> DecodedText:
    """Decode bytes with explicit codec, BOM, UTF-8, then charset-normalizer."""

    if encoding is not None:
        return _decode_explicit(data, encoding)
    bom = _bom_encoding(data)
    if bom is not None:
        return _decode_named(data, bom, method="bom")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _decode_detected(data)
    _reject_replacement(text, "utf-8")
    return DecodedText(text, "utf-8", "strict-utf8")


def _decode_explicit(data: bytes, encoding: str) -> DecodedText:
    try:
        codecs.lookup(encoding)
    except LookupError as error:
        raise KnowledgeError(
            KnowledgeErrorCode.ENCODING_FAILED,
            f"Unknown encoding: {encoding}",
        ) from error
    return _decode_named(data, encoding, method="explicit")


def _bom_encoding(data: bytes) -> str | None:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32"
    if data.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32"
    if data.startswith(b"\xff\xfe"):
        return "utf-16"
    if data.startswith(b"\xfe\xff"):
        return "utf-16"
    return None


def _decode_named(data: bytes, encoding: str, *, method: str) -> DecodedText:
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as error:
        raise KnowledgeError(
            KnowledgeErrorCode.ENCODING_FAILED,
            f"Decoding failed with {encoding}",
        ) from error
    _reject_replacement(text, encoding)
    return DecodedText(text, encoding, method)


def _decode_detected(data: bytes) -> DecodedText:
    detector = _charset_detector()
    viable: list[tuple[str, float, str]] = []
    for item in detector(data):
        encoding = str(getattr(item, "encoding", "") or "")
        if not encoding:
            continue
        coherence = float(getattr(item, "coherence", 0.0))
        viable.append((encoding, coherence, str(item)))
    if not viable:
        raise KnowledgeError(
            KnowledgeErrorCode.ENCODING_FAILED,
            "No encoding candidate",
        )
    best_encoding, best_coherence, text = viable[0]
    threshold = best_coherence - 0.05
    close = [encoding for encoding, coherence, _text in viable if coherence >= threshold]
    if len(set(close)) > 1:
        names = ",".join(sorted(set(close)))
        raise KnowledgeError(
            KnowledgeErrorCode.ENCODING_AMBIGUOUS,
            f"Ambiguous encoding candidates: {names}",
        )
    _reject_replacement(text, best_encoding)
    return DecodedText(text, best_encoding, "charset-normalizer")


def _charset_detector() -> Callable[[bytes], Iterable[object]]:
    try:
        module = __import__("charset_normalizer")
    except ImportError as error:
        raise KnowledgeError(
            KnowledgeErrorCode.DEPENDENCY_MISSING,
            "charset-normalizer is required for automatic encoding detection",
        ) from error
    return cast(Callable[[bytes], Iterable[object]], module.from_bytes)


def _reject_replacement(text: str, encoding: str) -> None:
    if "\ufffd" in text:
        raise KnowledgeError(
            KnowledgeErrorCode.ENCODING_FAILED,
            f"Replacement character present after decoding as {encoding}",
        )
