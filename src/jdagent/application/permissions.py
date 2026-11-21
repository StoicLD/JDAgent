"""查询和撤销已保存的工具授权规则。"""

from jdagent.domain.events import PermissionRuleRevokedPayload, RuntimeEventType
from jdagent.eventing import EventJournal
from jdagent.ports import SessionPort
from jdagent.tools.permissions import active_session_rules


async def revoke_session_rule(
    session: SessionPort,
    session_id: str,
    rule_id: str,
) -> None:
    """仅在指定会话中仍有对应的有效许可时，追加撤销事件。"""

    journal = await EventJournal.open(session, session_id, require_existing=True)
    active = {rule.rule_id for rule in active_session_rules(journal.events)}
    if rule_id not in active:
        raise ValueError(f"Active Session permission rule not found: {rule_id}")
    await journal.record(
        None,
        RuntimeEventType.PERMISSION_RULE_REVOKED,
        PermissionRuleRevokedPayload(rule_id),
    )
