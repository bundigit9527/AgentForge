"""Native artifact views over small, immutable, owner-scoped CSV bytes."""

from base64 import b64encode

import sqlalchemy as sa
from agentforge_core.models import RunNotFound

from agentforge_storage.authorization import owned
from agentforge_storage.schema import artifacts, runs
from agentforge_storage.thread_feed import append, iso

NAME = "number-summary.csv"
MIME = "text/csv"


def save_csv(conn, run, effect_id, data):
    conn.execute(
        artifacts.insert().values(
            artifact_id=effect_id, run_id=run["run_id"], data=data, created_at=run["updated_at"]
        )
    )
    if run["thread_id"]:
        block = {
            "kind": "file",
            "artifactId": effect_id,
            "mimeType": MIME,
            "name": NAME,
            "size": len(data),
        }
        append(
            conn,
            run,
            "thread.message.created",
            {
                "messageId": effect_id,
                "botId": run["agent_id"],
                "role": "bot",
                "blocks": [block],
                "createdAt": iso(run["updated_at"]),
            },
        )


class ArtifactRepository:
    def __init__(self, store):
        self.store = store

    def query(self, scope, bot_id=None, *, content=False):
        columns = [
            artifacts.c.artifact_id,
            artifacts.c.run_id,
            artifacts.c.created_at,
            runs.c.agent_id,
            sa.func.length(artifacts.c.data).label("size"),
        ]
        if content:
            columns.append(artifacts.c.data)
        q = (
            sa.select(*columns)
            .select_from(artifacts.join(runs))
            .where(owned(runs, scope), self.store.authorization.member_condition(scope))
        )
        return q.where(runs.c.agent_id == bot_id) if bot_id else q

    @staticmethod
    def view(row):
        return {
            "id": row["artifact_id"],
            "botId": row["agent_id"],
            "groupId": None,
            "runId": row["run_id"],
            "name": NAME,
            "description": "数值统计报告",
            "mimeType": MIME,
            "size": row["size"],
            "version": 1,
            "createdAt": iso(row["created_at"]),
        }

    def get(self, scope, artifact_id, *, bot_id=None, content=True):
        with self.store.engine.connect() as conn:
            self.store.authorization.require(conn, scope)
            row = (
                conn.execute(
                    self.query(scope, bot_id, content=content).where(
                        artifacts.c.artifact_id == artifact_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RunNotFound()
            view = self.view(row)
            if content:
                view["contentBase64"] = b64encode(row["data"]).decode("ascii")
            return view

    def listing(self, scope, bot_id=None, *, limit=None, cursor=None):
        q = self.query(scope, bot_id)
        with self.store.engine.connect() as conn:
            self.store.authorization.require(conn, scope)
            if cursor:
                marker = conn.execute(q.where(artifacts.c.artifact_id == cursor)).mappings().first()
                if marker is None:
                    raise RunNotFound()
                q = q.where(
                    sa.or_(
                        artifacts.c.created_at < marker["created_at"],
                        sa.and_(
                            artifacts.c.created_at == marker["created_at"],
                            artifacts.c.artifact_id < cursor,
                        ),
                    )
                )
            q = q.order_by(artifacts.c.created_at.desc(), artifacts.c.artifact_id.desc())
            rows = conn.execute(q.limit(limit + 1) if limit else q).mappings().all()
        if limit is None:
            return [self.view(row) for row in rows]
        return {
            "items": [{**self.view(row), "versionCount": 1} for row in rows[:limit]],
            "nextCursor": rows[limit - 1]["artifact_id"] if len(rows) > limit else None,
        }
