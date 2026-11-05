"""Native artifact views over small, immutable, owner-scoped CSV bytes."""

import hashlib
import json
from base64 import b64decode, b64encode
from dataclasses import asdict
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.attachments import MAX_FILE
from agentforge_core.models import RunConflict, RunNotFound, Scope

from agentforge_storage.authorization import owned
from agentforge_storage.conversations import agent_row
from agentforge_storage.identity import insert
from agentforge_storage.schema import artifacts, chat_artifacts, runs
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
        old = (
            sa.select(
                artifacts.c.artifact_id,
                artifacts.c.run_id,
                artifacts.c.created_at,
                runs.c.agent_id,
                sa.func.length(artifacts.c.data).label("size"),
                sa.literal(NAME).label("name"),
                sa.literal(MIME).label("mime_type"),
                sa.literal("数值统计报告").label("description"),
            )
            .select_from(artifacts.join(runs))
            .where(owned(runs, scope), self.store.authorization.member_condition(scope))
        )
        files = sa.select(
            *(
                chat_artifacts.c[key]
                for key in (
                    "artifact_id",
                    "run_id",
                    "created_at",
                    "agent_id",
                    "size",
                    "name",
                    "mime_type",
                    "description",
                )
            )
        ).where(owned(chat_artifacts, scope), self.store.authorization.member_condition(scope))
        if content:
            old, files = old.add_columns(artifacts.c.data), files.add_columns(chat_artifacts.c.data)
        if bot_id:
            old, files = (
                old.where(runs.c.agent_id == bot_id),
                files.where(chat_artifacts.c.agent_id == bot_id),
            )
        return old.union_all(files).subquery()

    def row(self, conn, scope, artifact_id, bot_id, *, content=False):
        rows = self.query(scope, bot_id, content=content)
        row = (
            conn.execute(sa.select(rows).where(rows.c.artifact_id == artifact_id))
            .mappings()
            .first()
        )
        if row is None:
            raise RunNotFound()
        return row

    def create(self, scope, body):
        if len(body.contentBase64) % 4:
            raise ValueError("Invalid base64 padding")
        data = b64decode(body.contentBase64, validate=True)
        if not 0 < len(data) <= MAX_FILE:
            raise ValueError("Invalid attachment size")
        bot = str(body.botId)
        with self.store.engine.begin() as conn:
            self.store.authorization.require(conn, scope, write=True)
            agent_row(conn, scope, bot, authorization=self.store.authorization)
            key = (
                "u:" + hashlib.sha256(body.clientNonce.encode()).hexdigest()
                if body.clientNonce
                else None
            )
            row, _ = store_file(
                conn,
                scope,
                bot,
                None,
                body.name,
                body.mimeType,
                body.description,
                data,
                key,
                self.store.clock(),
            )
            return self.view(row)

    @staticmethod
    def view(row):
        return {
            "id": row["artifact_id"],
            "botId": row["agent_id"],
            "groupId": None,
            "runId": row["run_id"],
            "name": row["name"],
            "description": row["description"],
            "mimeType": row["mime_type"],
            "size": row["size"],
            "version": 1,
            "createdAt": iso(row["created_at"]),
        }

    def get(self, scope, artifact_id, *, bot_id=None, content=True):
        with self.store.engine.connect() as conn:
            self.store.authorization.require(conn, scope)
            row = self.row(conn, scope, artifact_id, bot_id, content=content)
            view = self.view(row)
            if content:
                view["contentBase64"] = b64encode(row["data"]).decode("ascii")
            return view

    def listing(self, scope, bot_id=None, *, limit=None, cursor=None):
        rows_table = self.query(scope, bot_id)
        q = sa.select(rows_table)
        with self.store.engine.connect() as conn:
            self.store.authorization.require(conn, scope)
            if cursor:
                marker = (
                    conn.execute(q.where(rows_table.c.artifact_id == cursor)).mappings().first()
                )
                if marker is None:
                    raise RunNotFound()
                q = q.where(
                    sa.or_(
                        rows_table.c.created_at < marker["created_at"],
                        sa.and_(
                            rows_table.c.created_at == marker["created_at"],
                            rows_table.c.artifact_id < cursor,
                        ),
                    )
                )
            q = q.order_by(rows_table.c.created_at.desc(), rows_table.c.artifact_id.desc())
            rows = conn.execute(q.limit(limit + 1) if limit else q).mappings().all()
        if limit is None:
            return [self.view(row) for row in rows]
        return {
            "items": [{**self.view(row), "versionCount": 1} for row in rows[:limit]],
            "nextCursor": rows[limit - 1]["artifact_id"] if len(rows) > limit else None,
        }


def file_block(row):
    mime = row["mime_type"]
    block = dict(
        kind="image" if mime.startswith("image/") else "file",
        artifactId=row["artifact_id"],
        mimeType=mime,
        name=row["name"],
    )
    if block["kind"] == "file":
        block["size"] = row["size"]
    return block


def store_file(conn, scope, bot, run_id, name, mime, description, data, nonce, now):
    digest = hashlib.sha256(data).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps([name, mime, description, digest], ensure_ascii=False).encode()
    ).hexdigest()
    row = (
        conn.execute(
            insert(conn, chat_artifacts)
            .values(
                artifact_id=str(uuid4()),
                **asdict(scope),
                agent_id=bot,
                run_id=run_id,
                nonce=nonce,
                fingerprint=fingerprint,
                name=name,
                mime_type=mime,
                description=description,
                size=len(data),
                data=data,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "space_id", "user_id", "nonce"])
            .returning(
                chat_artifacts.c.artifact_id,
                chat_artifacts.c.agent_id,
                chat_artifacts.c.run_id,
                chat_artifacts.c.name,
                chat_artifacts.c.mime_type,
                chat_artifacts.c.description,
                chat_artifacts.c.size,
                chat_artifacts.c.created_at,
            )
        )
        .mappings()
        .first()
    )
    if row is not None:
        return row, True
    row = (
        conn.execute(
            sa.select(chat_artifacts).where(
                owned(chat_artifacts, scope),
                chat_artifacts.c.nonce == nonce,
            )
        )
        .mappings()
        .one()
    )
    if row["agent_id"] != bot:
        raise RunNotFound()
    if fingerprint != row["fingerprint"]:
        raise RunConflict("Attachment nonce reused with different content")
    return row, False


def save_attachment_result(conn, run, args):
    data = args["text"].encode()
    key = (
        "r:"
        + hashlib.sha256(json.dumps([run["run_id"], args], sort_keys=True).encode()).hexdigest()
    )
    scope = Scope(run["tenant_id"], run["space_id"], run["user_id"])
    row, new = store_file(
        conn,
        scope,
        run["agent_id"],
        run["run_id"],
        args["name"],
        args["mime_type"],
        None,
        data,
        key,
        run["updated_at"],
    )
    if new:
        append(
            conn,
            run,
            "thread.message.created",
            dict(
                messageId=row["artifact_id"],
                botId=run["agent_id"],
                role="bot",
                blocks=[file_block(row)],
                createdAt=iso(run["updated_at"]),
            ),
        )
