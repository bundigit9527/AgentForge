from dataclasses import asdict
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.models import RunConflict, RunNotFound
from agentforge_core.skills import SkillManifest, SkillSnapshot
from sqlalchemy.exc import IntegrityError

from agentforge_storage.conversations import scoped
from agentforge_storage.schema import skill_installations


def snapshot(row):
    return SkillSnapshot(
        reference=row["installation_id"],
        source="hub",
        source_id=row["source_id"],
        manifest=SkillManifest.model_validate(row["manifest"]),
        sha256=row["sha256"],
    )


class SkillRepository:
    def __init__(self, engine, clock):
        self.engine = engine
        self.clock = clock

    def install(self, scope, source_id, manifest, digest):
        identity = sa.and_(
            scoped(skill_installations, scope),
            skill_installations.c.source_id == source_id,
            skill_installations.c.skill_id == manifest.skill_id,
            skill_installations.c.version == manifest.version,
        )
        for attempt in range(2):
            try:
                with self.engine.begin() as connection:
                    existing = (
                        connection.execute(sa.select(skill_installations).where(identity))
                        .mappings()
                        .first()
                    )
                    if existing:
                        if existing["sha256"] != digest:
                            raise RunConflict("Published skill version has changed")
                        connection.execute(
                            skill_installations.update().where(identity).values(enabled=True)
                        )
                        return snapshot(existing)
                    row = dict(
                        installation_id=str(uuid4()),
                        **asdict(scope),
                        source_id=source_id,
                        skill_id=manifest.skill_id,
                        version=manifest.version,
                        sha256=digest,
                        manifest=manifest.model_dump(mode="json"),
                        enabled=True,
                        installed_at=self.clock(),
                    )
                    connection.execute(skill_installations.insert().values(**row))
                    return snapshot(row)
            except IntegrityError:
                if attempt:
                    raise

    def get(self, scope, installation_id):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(skill_installations).where(
                        scoped(skill_installations, scope),
                        skill_installations.c.installation_id == installation_id,
                        skill_installations.c.enabled.is_(True),
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise RunNotFound()
        return snapshot(row)

    def list(self, scope, offset, limit):
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(skill_installations)
                    .where(
                        scoped(skill_installations, scope), skill_installations.c.enabled.is_(True)
                    )
                    .order_by(
                        skill_installations.c.installed_at, skill_installations.c.installation_id
                    )
                    .offset(offset)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [snapshot(row) for row in rows]

    def uninstall(self, scope, installation_id):
        with self.engine.begin() as connection:
            changed = connection.execute(
                skill_installations.update()
                .where(
                    scoped(skill_installations, scope),
                    skill_installations.c.installation_id == installation_id,
                )
                .values(enabled=False)
            )
            if changed.rowcount != 1:
                raise RunNotFound()
