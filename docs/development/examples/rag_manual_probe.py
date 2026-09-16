"""Inspect local RAG evidence; optionally run a real-model answer for manual review.

This diagnostic uses FileKnowledgeIndex and HashEmbedding. It is not the default
CLI retrieval configuration and does not validate Milvus or semantic embeddings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from jdagent.adapters.fake import FakeApproval
from jdagent.application.headless import run_headless
from jdagent.composition import RuntimeOptions, build_runtime
from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths, workspace_identity
from jdagent.domain.tools import ApprovalDecision
from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.index import FileKnowledgeIndex
from jdagent.knowledge.preparation import CatalogBaseResolver, TurnKnowledgePreparation


async def inspect(args: argparse.Namespace) -> None:
    paths = DataPaths.for_workspace(args.workspace)
    index_path = paths.knowledge_directory / "index.sqlite"
    if not paths.knowledge_catalog.is_file() or not index_path.is_file():
        raise ValueError("Import a local source in this isolated data root first")
    with KnowledgeCatalog(paths.knowledge_catalog, paths.knowledge_backups) as catalog:
        bindings = catalog.list_bindings(workspace_identity(paths.workspace))
        for binding in bindings:
            connection = catalog.get_connection(binding.connection_id)
            profile = catalog.get_knowledge_base(binding.knowledge_base_id).embedding_profile
            if connection.endpoint or profile.base_url or profile.model:
                raise ValueError("This probe supports only local HashEmbedding test bases")
        if not args.answer:
            frozen = CatalogBaseResolver(catalog).resolve(bindings)
            if args.mode:
                frozen = tuple(
                    (
                        replace(
                            base, retrieval_profile=replace(base.retrieval_profile, mode=args.mode)
                        )
                        if base is not None
                        else None,
                        failure,
                    )
                    for base, failure in frozen
                )
            source_names = {
                source.source_id: source.name
                for binding in bindings
                for source in catalog.list_sources(binding.knowledge_base_id)
            }
            index = FileKnowledgeIndex(index_path)
            try:
                prepared = await TurnKnowledgePreparation(
                    indexes={binding.knowledge_base_id: index for binding in bindings}
                ).prepare(
                    turn_id=f"manual-{uuid4().hex}",
                    query_text=args.query,
                    bindings=bindings,
                    frozen=frozen,
                    source_names=source_names,
                )
            finally:
                index.close()
            print(json.dumps(asdict(prepared), ensure_ascii=False, indent=2))
            return

    configuration = resolve_configuration(
        paths.workspace,
        CliOverrides(provider="deepseek", data_dir=paths.sessions_directory),
        data_paths=paths,
    )
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
        result = await run_headless(
            composition.coordinator,
            args.query,
            provider=configuration.provider,
            model=configuration.model,
            session_id=args.session_id,
        )
        payload = result.json_data()
        payload["diagnostic_ptk"] = asdict(result.knowledge)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        await composition.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("query")
    parser.add_argument("--mode", choices=("bm25", "dense", "hybrid_rrf"))
    parser.add_argument("--answer", action="store_true", help="Call DeepSeek and save a session")
    parser.add_argument("--session-id")
    args = parser.parse_args()
    if args.answer and args.mode:
        parser.error("--mode is an inspection-only override; answers use Catalog profiles")
    if args.session_id and not args.answer:
        parser.error("--session-id requires --answer")
    asyncio.run(inspect(args))


if __name__ == "__main__":
    main()
