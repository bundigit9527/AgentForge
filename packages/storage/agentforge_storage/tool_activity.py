"""Native transcript blocks from actual tool journal rows, without arguments or results."""

import sqlalchemy as sa

from agentforge_storage.schema import tool_calls


def activity_blocks(connection, run_id):
    # Keep large and potentially sensitive argument/result columns out of this projection.
    calls = connection.execute(
        sa.select(
            tool_calls.c.name,
            tool_calls.c.version,
            tool_calls.c.status,
            tool_calls.c.error_code,
            tool_calls.c.started_at,
            tool_calls.c.finished_at,
        )
        .where(tool_calls.c.run_id == run_id)
        .order_by(tool_calls.c.sequence)
    ).mappings()
    blocks = []
    for call in calls:
        ref = f"{call['name']}@{call['version']}"
        if call["status"] == "succeeded":
            duration = max(
                0, round(((call["finished_at"] or call["started_at"]) - call["started_at"]) * 1000)
            )
            if not blocks or blocks[-1]["kind"] != "steps":
                blocks.append({"kind": "steps", "steps": [], "durationMs": 0})
            steps = blocks[-1]["steps"]
            if steps and steps[-1]["label"] == ref:
                steps[-1]["count"] += 1
            else:
                steps.append({"label": ref, "count": 1})
            blocks[-1]["durationMs"] += duration
        else:
            # The original steps renderer always shows success ticks. Failed/unknown
            # operations therefore use native meta blocks, never a green success step.
            if call["status"] == "running":
                text = f"工具执行中：{ref}"
            elif call["status"] == "failed":
                text = f"工具执行失败：{ref}（{call['error_code']}）"
            else:
                text = f"工具执行已中断，结果未确认：{ref}（{call['error_code']}）"
            blocks.append({"kind": "meta", "text": text})
    return blocks
