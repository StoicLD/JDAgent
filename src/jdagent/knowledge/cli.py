"""CLI adapter for knowledge catalog use cases."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from jdagent.configuration import CliOverrides, resolve_configuration
from jdagent.data_paths import DataPaths, workspace_identity
from jdagent.knowledge.catalog import KnowledgeCatalog
from jdagent.knowledge.errors import KnowledgeError, KnowledgeErrorCode
from jdagent.knowledge.types import ConnectionRecord, KnowledgeBaseRecord


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jdagent knowledge")
    sub = parser.add_subparsers(dest="group", required=True)

    connection = sub.add_parser("connection")
    connection_sub = connection.add_subparsers(dest="action", required=True)
    add = connection_sub.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--endpoint")
    add.add_argument("--credential-ref")
    connection_sub.add_parser("list")
    test = connection_sub.add_parser("test")
    test.add_argument("--id", dest="connection_id", required=True)
    remove = connection_sub.add_parser("remove")
    remove.add_argument("--id", dest="connection_id", required=True)
    remove.add_argument("--yes", action="store_true")

    kb = sub.add_parser("kb")
    kb_sub = kb.add_subparsers(dest="action", required=True)
    create = kb_sub.add_parser("create")
    create.add_argument("--connection", required=True)
    create.add_argument("--name", required=True)
    kb_list = kb_sub.add_parser("list")
    kb_list.add_argument("--connection")
    status = kb_sub.add_parser("status")
    status.add_argument("--id", dest="knowledge_base_id", required=True)
    delete = kb_sub.add_parser("delete")
    delete.add_argument("--id", dest="knowledge_base_id", required=True)
    delete.add_argument("--yes", action="store_true")

    binding = sub.add_parser("binding")
    binding_sub = binding.add_subparsers(dest="action", required=True)
    bind = binding_sub.add_parser("add")
    bind.add_argument("--connection", required=True)
    bind.add_argument("--kb", required=True)
    binding_sub.add_parser("list")
    unbind = binding_sub.add_parser("remove")
    unbind.add_argument("--id", dest="binding_id", required=True)
    unbind.add_argument("--yes", action="store_true")
    return parser


def _open_catalog(paths: DataPaths) -> KnowledgeCatalog:
    catalog = KnowledgeCatalog(paths.knowledge_catalog, paths.knowledge_backups)
    catalog.open()
    return catalog


def _connection_json(record: ConnectionRecord) -> dict[str, str | None]:
    return {
        "connection_id": record.connection_id,
        "name": record.name,
        "provider_kind": record.provider_kind.value,
        "endpoint": record.endpoint,
        "credential_ref": None
        if record.credential_ref is None
        else record.credential_ref.redacted(),
    }


def _kb_json(record: KnowledgeBaseRecord) -> dict[str, object]:
    return {
        "knowledge_base_id": record.knowledge_base_id,
        "connection_id": record.connection_id,
        "name": record.name,
        "status": record.status.value,
        "language_profile": record.language_profile,
        "current_revision": record.current_revision,
    }


def run_knowledge_cli(
    argv: Sequence[str],
    *,
    workspace: Path,
    data_dir: Path | None = None,
) -> int:
    """Execute one knowledge management command and print JSON."""

    namespace = _parser().parse_args(list(argv))
    resolved = resolve_configuration(
        workspace,
        CliOverrides(data_dir=data_dir),
    )
    paths = resolved.data_paths
    catalog = _open_catalog(paths)
    try:
        payload = _dispatch(catalog, namespace, paths)
    finally:
        catalog.close()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _dispatch(catalog: KnowledgeCatalog, namespace: argparse.Namespace, paths: DataPaths) -> object:
    group = namespace.group
    action = namespace.action
    if group == "connection" and action == "add":
        record = catalog.register_connection(
            name=namespace.name,
            endpoint=namespace.endpoint,
            credential_ref=namespace.credential_ref,
        )
        return _connection_json(record)
    if group == "connection" and action == "list":
        return [_connection_json(item) for item in catalog.list_connections()]
    if group == "connection" and action == "test":
        return catalog.test_connection(namespace.connection_id)
    if group == "connection" and action == "remove":
        catalog.remove_connection(namespace.connection_id, confirmed=namespace.yes)
        return {"removed": namespace.connection_id}
    if group == "kb" and action == "create":
        record = catalog.create_knowledge_base(
            connection_id=namespace.connection,
            name=namespace.name,
        )
        return _kb_json(record)
    if group == "kb" and action == "list":
        return [_kb_json(item) for item in catalog.list_knowledge_bases(namespace.connection)]
    if group == "kb" and action == "status":
        return catalog.knowledge_base_status(namespace.knowledge_base_id)
    if group == "kb" and action == "delete":
        catalog.delete_knowledge_base(namespace.knowledge_base_id, confirmed=namespace.yes)
        return {"deleted": namespace.knowledge_base_id}
    if group == "binding" and action == "add":
        identity = workspace_identity(paths.workspace)
        record = catalog.bind(
            workspace_identity=identity,
            connection_id=namespace.connection,
            knowledge_base_id=namespace.kb,
        )
        return {
            "binding_id": record.binding_id,
            "workspace_identity": record.workspace_identity,
            "connection_id": record.connection_id,
            "knowledge_base_id": record.knowledge_base_id,
        }
    if group == "binding" and action == "list":
        identity = workspace_identity(paths.workspace)
        return [
            {
                "binding_id": item.binding_id,
                "workspace_identity": item.workspace_identity,
                "connection_id": item.connection_id,
                "knowledge_base_id": item.knowledge_base_id,
            }
            for item in catalog.list_bindings(identity)
        ]
    if group == "binding" and action == "remove":
        catalog.unbind(namespace.binding_id, confirmed=namespace.yes)
        return {"removed": namespace.binding_id}
    raise KnowledgeError(KnowledgeErrorCode.INVALID_ARGUMENT, "Unsupported knowledge command")
