"""Verify a live tool and its approved external dispatch before touching a desktop."""

import sqlalchemy as sa
from agentforge_core.effects import EffectRequest

from agentforge_storage.schema import external_effects, tool_calls


def dispatched_tool(store, claim, tool, arguments):
    with store.engine.connect() as conn:
        calls = (
            conn.execute(
                sa.select(tool_calls.c.arguments).where(
                    tool_calls.c.run_id == claim.run.run_id,
                    tool_calls.c.attempt_id == claim.attempt_id,
                    tool_calls.c.name == tool,
                    tool_calls.c.version == 1,
                    tool_calls.c.status == "running",
                )
            )
            .scalars()
            .all()
        )
        if arguments not in calls:
            return False
        if tool != "workspace_save":
            return True
        digest = EffectRequest(
            tool_reference="workspace_save@1", target="registered-tool", arguments=arguments
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
