import asyncio
import os
from pathlib import Path
from typing import cast

import pytest

from jdagent.adapters.fake import FakeApproval
from jdagent.composition import RuntimeOptions, build_runtime, load_deepseek_api_key
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths, workspace_identity
from jdagent.domain.errors import StopReason
from jdagent.domain.tools import ApprovalDecision
from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.eval_answer import median, score_citations
from jdagent.knowledge.eval_gold import GOLD_ROOT, SKIP_QUERIES, SOURCE_KB, load_jsonl
from jdagent.knowledge.index import FileKnowledgeIndex
from jdagent.knowledge.ingestion import KnowledgeIngestion
from jdagent.knowledge.store import ContentAddressedStore
from jdagent.knowledge.types import EmbeddingProfile

pytestmark = pytest.mark.integration

_RUNS = 3


def _live_ready() -> tuple[str, str, str]:
    if os.environ.get("JDAGENT_RUN_DEEPSEEK_INTEGRATION") != "1":
        pytest.skip("Set JDAGENT_RUN_DEEPSEEK_INTEGRATION=1 for live answer/citation eval")
    api_key = load_deepseek_api_key(os.environ.get("DEEPSEEK_API_KEY"))
    if not api_key:
        pytest.skip("A DeepSeek API key is required for live answer/citation eval")
    return (
        api_key,
        os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    )


def test_answer_and_citation_medians_meet_gates(tmp_path: Path) -> None:
    api_key, base_url, model = _live_ready()

    async def scenario() -> None:
        config_root = tmp_path / "config"
        data_root = tmp_path / "data"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        paths = DataPaths.for_workspace(
            workspace,
            config_root=config_root,
            data_root=data_root,
        )
        catalog = KnowledgeCatalog(paths.knowledge_catalog, paths.knowledge_backups)
        catalog.open()
        connection = catalog.register_connection(name="local")
        kbs: dict[str, str] = {}
        index = FileKnowledgeIndex(paths.knowledge_directory / "index.sqlite")
        ingestion = KnowledgeIngestion(
            catalog,
            ContentAddressedStore(paths.knowledge_objects),
            index,
        )
        for name in ("hr", "finance", "mixed"):
            kb = catalog.create_knowledge_base(
                connection_id=connection.connection_id,
                name=name,
                embedding_profile=EmbeddingProfile(dimension=32),
            )
            kbs[name] = kb.knowledge_base_id
        source_keys: dict[str, str] = {}
        for path in sorted((GOLD_ROOT / "sources").iterdir()):
            ingested = await ingestion.add_file(kbs[SOURCE_KB[path.stem]], path)
            source_keys[ingested.source_version_id] = path.stem
        identity = workspace_identity(workspace)
        queries = load_jsonl(GOLD_ROOT / "queries.jsonl")
        annotations = {
            str(row["query_id"]): row for row in load_jsonl(GOLD_ROOT / "annotations.jsonl")
        }
        configuration = resolve_configuration(
            workspace,
            CliOverrides(
                provider="deepseek",
                model=model,
                base_url=base_url,
                data_dir=paths.sessions_directory,
            ),
            data_paths=paths,
            environment={"DEEPSEEK_API_KEY": api_key},
        )
        precision_medians: list[float] = []
        completeness_medians: list[float] = []
        unsupported_medians: list[float] = []
        scored_queries: list[str] = []
        try:
            for query in queries:
                query_id = str(query["query_id"])
                if query_id in SKIP_QUERIES:
                    continue
                for binding in catalog.list_bindings(identity):
                    catalog.unbind(binding.binding_id, confirmed=True)
                for name in [str(item) for item in cast(list[object], query["knowledge_bases"])]:
                    catalog.bind(
                        workspace_identity=identity,
                        connection_id=connection.connection_id,
                        knowledge_base_id=kbs[name],
                    )
                runs_precision: list[float] = []
                runs_completeness: list[float] = []
                runs_unsupported: list[float] = []
                for _run in range(_RUNS):
                    composition = build_runtime(
                        configuration,
                        FakeApproval(ApprovalDecision.REJECT),
                        runtime_options=RuntimeOptions(
                            temperature=0.0,
                            include_tools=False,
                            allow_local_index=True,
                            provider_options={"deepseek": {"thinking": {"type": "disabled"}}},
                        ),
                    )
                    try:
                        turn = await composition.coordinator.send(str(query["text"]))
                    finally:
                        await composition.aclose()
                    failed = turn.result.stop_reason is not StopReason.COMPLETED
                    metrics = score_citations(
                        annotations[query_id],
                        tuple(item.locator for item in turn.result.citations),
                        protocol_failed=failed,
                        cited_source_keys=tuple(
                            source_keys.get(item.source_version_id, "")
                            for item in turn.result.citations
                        ),
                    )
                    runs_precision.append(metrics.precision)
                    runs_completeness.append(metrics.completeness)
                    runs_unsupported.append(metrics.unsupported_rate)
                precision_medians.append(median(tuple(runs_precision)))
                completeness_medians.append(median(tuple(runs_completeness)))
                unsupported_medians.append(median(tuple(runs_unsupported)))
                scored_queries.append(query_id)
        finally:
            index.close()
            catalog.close()
        report = list(
            zip(
                scored_queries,
                precision_medians,
                completeness_medians,
                unsupported_medians,
                strict=True,
            )
        )
        assert sum(precision_medians) / len(precision_medians) >= 0.90, report
        assert sum(completeness_medians) / len(completeness_medians) >= 0.85, report
        assert sum(unsupported_medians) / len(unsupported_medians) <= 0.05, report

    asyncio.run(scenario())
