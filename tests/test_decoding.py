from pathlib import Path

import pytest

from jdagent.knowledge.decoding import decode_source
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode

GOLD = Path(__file__).resolve().parents[1] / "testdata" / "v0.3-gold" / "encodings"


def test_explicit_utf8_bom_and_utf16_decode() -> None:
    utf8 = decode_source((GOLD / "utf8.txt").read_bytes())
    bom = decode_source((GOLD / "utf8-bom.txt").read_bytes())
    utf16 = decode_source((GOLD / "utf16-le.txt").read_bytes())
    assert utf8.text.startswith("年假15天")
    assert utf8.method == "strict-utf8"
    assert bom.method == "bom"
    assert "年假15天" in utf16.text
    assert (
        decode_source((GOLD / "gb18030.txt").read_bytes(), encoding="gb18030").method == "explicit"
    )


def test_replacement_and_corrupt_bytes_fail_closed() -> None:
    with pytest.raises(KnowledgeError) as replacement:
        decode_source((GOLD / "replacement.txt").read_bytes())
    assert replacement.value.code is KnowledgeErrorCode.ENCODING_FAILED
    with pytest.raises(KnowledgeError) as corrupt:
        decode_source((GOLD / "corrupt.bin").read_bytes())
    assert corrupt.value.code in {
        KnowledgeErrorCode.ENCODING_FAILED,
        KnowledgeErrorCode.ENCODING_AMBIGUOUS,
        KnowledgeErrorCode.DEPENDENCY_MISSING,
    }


def test_unknown_explicit_codec_fails() -> None:
    with pytest.raises(KnowledgeError) as error:
        decode_source(b"abc", encoding="not-a-codec")
    assert error.value.code is KnowledgeErrorCode.ENCODING_FAILED
