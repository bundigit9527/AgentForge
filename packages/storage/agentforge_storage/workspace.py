"""Verify a live tool and its approved external dispatch before touching a desktop."""

import sqlalchemy as sa
from agentforge_core.effects import EffectRequest
from agentforge_core.workspace import WORKSPACE_WRITES

from agentforge_storage.schema import external_effects, tool_calls


def dispatched_tool(store, claim, tool, arguments):
    config = claim.run.execution_config
    ref = next((r for r in config.tool_references if r.split("@")[0] == tool), None)
    if ref is None:
        return False
    with store.engine.connect() as conn:
        calls = (
            conn.execute(
                sa.select(tool_calls.c.arguments).where(
                    tool_calls.c.run_id == claim.run.run_id,
                    tool_calls.c.attempt_id == claim.attempt_id,
                    tool_calls.c.name == tool,
                    tool_calls.c.version == int(ref.split("@")[1]),
                    tool_calls.c.status == "running",
                )
            )
            .scalars()
            .all()
        )
        if arguments not in calls:
            return False
        if ref not in WORKSPACE_WRITES:
            return True
        digest = EffectRequest(
            tool_reference=ref, target="registered-tool", arguments=arguments
        ).digest()
        return (
            conn.execute(
                sa.select(external_effects.c.effect_id).where(
                    external_effects.c.run_id == claim.run.run_id,
                    external_effects.c.dispatch_attempt_id == claim.attempt_id,
                    external_effects.c.request_hash == digest,
                    external_effects.c.status == "dispatched",
                )
            ).first()
            is not None
        )
