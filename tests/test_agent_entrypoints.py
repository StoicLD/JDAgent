import re
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_INDEX_PATH = _REPOSITORY_ROOT / "docs" / "context" / "index.md"
_ENTRYPOINTS = (
    _REPOSITORY_ROOT / "AGENTS.md",
    _REPOSITORY_ROOT / "CLAUDE.md",
)
_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_FORBIDDEN_STARTUP_REFERENCES = (
    "JDAgentAgentContext",
    "bootstrap-guide",
    "../codex/",
    "../cursor/",
    "../claude/",
)


def _violation(path: Path, rule: str, remediation: str) -> str:
    relative = path.relative_to(_REPOSITORY_ROOT).as_posix()
    return (
        f"违规对象：{relative}\n"
        f"违反规则：{rule}\n"
        f"修复动作：{remediation}\n"
        "权威说明：docs/development/agent-collaboration.md"
    )


def _markdown_links(path: Path) -> tuple[str, ...]:
    return tuple(_LINK_PATTERN.findall(path.read_text(encoding="utf-8")))


def test_product_entrypoints_are_thin_direct_maps() -> None:
    for path in _ENTRYPOINTS:
        text = path.read_text(encoding="utf-8")
        links = _markdown_links(path)
        assert len(text.splitlines()) <= 100, _violation(
            path,
            "产品原生入口不得超过 100 行",
            "把详细事实迁入 docs/，入口只保留链接和最小失败行为",
        )
        assert "docs/context/index.md" in links, _violation(
            path,
            "每个产品原生入口必须直接链接共同文档索引",
            "增加指向 docs/context/index.md 的真实 Markdown 相对链接",
        )
        for marker in _FORBIDDEN_STARTUP_REFERENCES:
            assert marker not in text, _violation(
                path,
                "产品启动链不得引用旧 Context 或 Agent 私有入口",
                f"移除启动引用 {marker}，将项目事实写入 docs/ 的唯一权威位置",
            )


def test_product_entrypoint_links_stay_inside_repository() -> None:
    root = _REPOSITORY_ROOT.resolve()
    for path in _ENTRYPOINTS:
        for link in _markdown_links(path):
            target_text = link.split("#", maxsplit=1)[0]
            target = (path.parent / target_text).resolve()
            assert target.is_relative_to(root), _violation(
                path,
                "产品原生入口链接不得逃逸产品 Git 仓库",
                f"把 {link} 替换为仓库内权威文档的相对链接",
            )
            assert target.is_file(), _violation(
                path,
                "产品原生入口链接必须指向真实文件",
                f"修复或移除失效链接 {link}",
            )


def test_common_index_reaches_required_structure_facts() -> None:
    assert len(_INDEX_PATH.read_text(encoding="utf-8").splitlines()) <= 200, _violation(
        _INDEX_PATH,
        "共同文档索引不得超过 200 行",
        "增加子索引或压缩重复说明",
    )
    expected = {
        "current.md",
        "../development/agent-collaboration.md",
        "../decisions/ADR-0005-agent-context-free-startup.md",
    }
    links = set(_markdown_links(_INDEX_PATH))
    missing = expected - links
    assert not missing, _violation(
        _INDEX_PATH,
        "共同索引必须到达当前状态、协作规则和结构决策",
        f"补充缺失链接：{', '.join(sorted(missing))}",
    )
