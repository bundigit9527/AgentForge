"""Frozen file discovery and lease-bound immutable revisions, without filesystem sharing."""

import base64
from uuid import UUID

import sqlalchemy as sa
from agentforge_core.attachments import MAX_FILE
from agentforge_core.models import RunNotFound, Scope
from agentforge_core.python_execution import FileReference, InputFile

from agentforge_storage.artifacts import ArtifactRepository
from agentforge_storage.authorization import owned
from agentforge_storage.schema import artifact_revisions, chat_artifacts, messages, runs


def freeze_files(store, conn, scope, thread, explicit, config):
    files = ArtifactRepository(store)
    table = files.query(scope, thread["agent_id"])
    visible = sa.select(messages.c.run_id).where(messages.c.run_id == runs.c.run_id).exists()
    recent = (
        conn.execute(
            sa.select(table)
            .join(runs, table.c.run_id == runs.c.run_id)
            .where(runs.c.thread_id == thread["thread_id"], runs.c.status == "succeeded", visible)
            .order_by(table.c.created_at.desc(), table.c.version.desc(), table.c.artifact_id.desc())
            .limit(64)
        )
        .mappings()
        .all()
    )
    # Previously uploaded inputs have no run_id; retain only those used in successful turns.
    prior_inputs = (
        conn.execute(
            sa.select(runs.c.input_files)
            .where(
                owned(runs, scope),
                runs.c.thread_id == thread["thread_id"],
                runs.c.status == "succeeded",
                visible,
            )
            .order_by(runs.c.created_at.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    ids = list(
        dict.fromkeys(
            [
                *explicit,
                *[r["artifact_id"] for r in recent],
                *[b["artifactId"] for blocks in prior_inputs for b in blocks or []],
            ]
        )
    )
    frozen, seen, used = [], set(), 0
    budget = min(6000, config.model.context_max_chars // 8)
    if config.model.context_max_bytes is not None:
        budget = min(budget, config.model.context_max_bytes // 8)
    for ref in ids:
        row = files.row(conn, scope, ref, thread["agent_id"])
        if row["family_id"] in seen:
            continue
        seen.add(row["family_id"])
        name = row["name"]
        if len(name) > 128 or name.startswith("_agentforge_"):
            name = "input-" + row["artifact_id"] + "." + name.rsplit(".", 1)[-1][:12]
        item = FileReference(
            artifact_id=ref,
            name=name,
            size=row["size"],
            mime_type=row["mime_type"],
            version=row["version"],
        )
        size = len(item.model_dump_json().encode())
        if len(frozen) == 12 or used + size > budget:
            break
        frozen.append(item)
        used += size
    return tuple(frozen)


def allowed_file(store, conn, claim, ref):
    config = claim.run.execution_config
    if not config or not config.python_image or not config.python_continuity:
        raise RunNotFound()
    if not conn.execute(sa.select(runs.c.run_id).where(store._owned(claim, store.clock()))).first():
        raise RunNotFound()
    row = ArtifactRepository(store).row(
        conn, claim.run.scope, ref, claim.run.agent_id, content=True
    )
    if (
        ref not in {f.artifact_id for f in config.python_files}
        and row["run_id"] != claim.run.run_id
    ):
        raise RunNotFound()
    return row


def load_inputs(store, claim, inputs):
    result, total = [], 0
    with store.engine.connect() as conn:
        for item in inputs:
            row = allowed_file(store, conn, claim, item.artifact_id)
            total += row["size"]
            if total > MAX_FILE:
                raise ValueError("Combined inputs exceed 10 MiB")
            result.append(dict(name=item.name, data=base64.b64encode(row["data"]).decode()))
    return result


def prepare_revisions(conn, run, files, arguments):
    config = run["execution_config"]
    versioned = [f for f in files if f.artifact_id or f.parent_id]
    if not versioned:
        return {}
    if not config.get("python_continuity") or any(not f.artifact_id for f in files):
        raise ValueError("Versioned publication requires continuity")
    inputs = {
        i.name: i.artifact_id
        for i in [InputFile.model_validate(i) for i in arguments.get("inputs", [])]
    }
    allowed = {f["artifact_id"] for f in config.get("python_files", [])}
    scope = Scope(run["tenant_id"], run["space_id"], run["user_id"])
    parents = {}
    for file in versioned:
        UUID(file.artifact_id)
        if file.parent_id != inputs.get(file.name):
            raise ValueError("Revision parent must be the exact input alias")
        if not file.parent_id:
            continue
        parent = (
            conn.execute(
                sa.select(chat_artifacts).where(
                    chat_artifacts.c.artifact_id == file.parent_id,
                    owned(chat_artifacts, scope),
                    chat_artifacts.c.agent_id == run["agent_id"],
                )
            )
            .mappings()
            .first()
        )
        if parent is None:
            # Legacy CSV inputs are readable; they start a new family on publication.
            from agentforge_storage.schema import artifacts

            legacy = conn.execute(
                sa.select(artifacts.c.artifact_id)
                .join(runs)
                .where(
                    artifacts.c.artifact_id == file.parent_id,
                    owned(runs, scope),
                    runs.c.agent_id == run["agent_id"],
                    artifacts.c.artifact_id.in_(allowed),
                )
            ).first()
            if legacy is None:
                raise RunNotFound()
            continue
        if file.parent_id not in allowed and parent["run_id"] != run["run_id"]:
            raise RunNotFound()
        family = (
            conn.execute(
                sa.select(artifact_revisions.c.family_id).where(
                    artifact_revisions.c.artifact_id == file.parent_id
                )
            ).scalar_one_or_none()
            or file.parent_id
        )
        parents[file.parent_id] = family
    # Ordered family locks serialize concurrent revisions without overwriting either base.
    for family in sorted(set(parents.values())):
        conn.execute(
            sa.select(chat_artifacts.c.artifact_id)
            .where(chat_artifacts.c.artifact_id == family, owned(chat_artifacts, scope))
            .with_for_update()
        ).one()
        if not conn.execute(
            sa.select(artifact_revisions.c.artifact_id).where(
                artifact_revisions.c.artifact_id == family
            )
        ).first():
            conn.execute(
                artifact_revisions.insert().values(artifact_id=family, family_id=family, version=1)
            )
    return parents


def save_revision(conn, artifact_id, parent_id, family):
    version = 1
    if family:
        version = (
            conn.execute(
                sa.select(sa.func.max(artifact_revisions.c.version)).where(
                    artifact_revisions.c.family_id == family
                )
            ).scalar_one()
            + 1
        )
    conn.execute(
        artifact_revisions.insert().values(
            artifact_id=artifact_id,
            family_id=family or artifact_id,
            parent_id=parent_id if family else None,
            version=version,
        )
    )
