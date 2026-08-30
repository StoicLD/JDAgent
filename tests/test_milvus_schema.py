from jdagent.knowledge.milvus import MIXED_ZH_EN_V1


def test_mixed_zh_en_v1_uses_jieba_analyzer() -> None:
    assert MIXED_ZH_EN_V1["tokenizer"] == "jieba"
    assert MIXED_ZH_EN_V1["filter"] == ["lowercase"]
