"""Owner-private editable skill heads with immutable versions for Agent/Run snapshots."""

from dataclasses import asdict
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.models import RunConflict, RunNotFound
from agentforge_core.skills import SkillDocument, SkillManifest, SkillSnapshot
from sqlalchemy.exc import IntegrityError

from agentforge_storage.authorization import Authorization
from agentforge_storage.conversations import scoped
from agentforge_storage.schema import user_skill_versions, user_skills


def snapshot(row):
    return SkillSnapshot(
        reference=row["version_id"],
        source="user",
        source_id="user:" + row["skill_id"],
        manifest=SkillManifest.model_validate(row["manifest"]),
        sha256=row["sha256"],
    )


class UserSkillRepository:
    def __init__(self, engine, clock, *, auth_mode="development"):
        self.engine, self.clock = engine, clock
        self.authorization = Authorization(auth_mode)

    def _current(self, conn, skill_id, version):
        return (
            conn.execute(
                sa.select(user_skill_versions).where(
                    user_skill_versions.c.skill_id == skill_id,
                    user_skill_versions.c.version == version,
                )
            )
            .mappings()
            .one()
        )

    def _version(self, conn, skill_id, version, document):
        manifest = SkillManifest(
            skill_id="user_" + skill_id.replace("-", ""),
            version=version,
            name=document.name,
            description=document.description,
            content=document.content,
            required_tools=document.required_tools,
        )
        row = dict(
            version_id=str(uuid4()),
            skill_id=skill_id,
            version=version,
            manifest=manifest.model_dump(mode="json"),
            sha256=manifest.digest(),
        )
        conn.execute(user_skill_versions.insert().values(**row))
        return snapshot(row)

    @staticmethod
    def _name(document, reserved_names):
        key = document.name.casefold()
        if key in reserved_names:
            raise RunConflict("Skill name is reserved")
        return key

    def create(self, scope, document: SkillDocument, reserved_names):
        try:
            with self.engine.begin() as conn:
                self.authorization.require(conn, scope, write=True)
                now, skill_id = self.clock(), str(uuid4())
                conn.execute(
                    user_skills.insert().values(
                        skill_id=skill_id,
                        **asdict(scope),
                        name_key=self._name(document, reserved_names),
                        version=1,
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
                skill = self._version(conn, skill_id, 1, document)
                return skill, now, now
        except IntegrityError:
            raise RunConflict("Skill name already exists") from None

    def update(self, scope, skill_id, prepare, reserved_names, expected_version=None):
        try:
            with self.engine.begin() as conn:
                self.authorization.require(conn, scope, write=True)
                # Serialize edits/deletion, including partial updates from the native form.
                row = (
                    conn.execute(
                        user_skills.update()
                        .where(
                            scoped(user_skills, scope),
                            user_skills.c.skill_id == skill_id,
                            user_skills.c.enabled.is_(True),
                        )
                        .values(version=user_skills.c.version)
                        .returning(user_skills)
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise RunNotFound()
                if expected_version is not None and expected_version != row["version"]:
                    raise RunConflict("Skill version changed")
                prior = snapshot(self._current(conn, skill_id, row["version"]))
                document = prepare(prior.manifest.content)
                key = self._name(document, reserved_names)
                if document.content == prior.manifest.content:
                    return prior, row["created_at"], row["updated_at"]
                version, now = row["version"] + 1, self.clock()
                skill = self._version(conn, skill_id, version, document)
                conn.execute(
                    user_skills.update()
                    .where(user_skills.c.skill_id == skill_id)
                    .values(
                        name_key=key,
                        version=version,
                        updated_at=now,
                    )
                )
                return skill, row["created_at"], now
        except IntegrityError:
            raise RunConflict("Skill name already exists") from None

    def remove(self, scope, skill_id):
        with self.engine.begin() as conn:
            self.authorization.require(conn, scope, write=True)
            changed = conn.execute(
                user_skills.update()
                .where(
                    scoped(user_skills, scope),
                    user_skills.c.skill_id == skill_id,
                    user_skills.c.enabled.is_(True),
                )
                .values(enabled=False, name_key=None, updated_at=self.clock())
            )
            if changed.rowcount != 1:
                raise RunNotFound()

    def catalog(self, scope, *, connection=None):
        if connection is None:
            with self.engine.connect() as conn:
                return self.catalog(scope, connection=conn)
        self.authorization.require(connection, scope)
        rows = (
            connection.execute(
                sa.select(
                    user_skill_versions,
                    user_skills.c.created_at,
                    user_skills.c.updated_at,
                )
                .join(
                    user_skills,
                    sa.and_(
                        user_skills.c.skill_id == user_skill_versions.c.skill_id,
                        user_skills.c.version == user_skill_versions.c.version,
                    ),
                )
                .where(scoped(user_skills, scope), user_skills.c.enabled.is_(True))
                .order_by(
                    user_skills.c.name_key,
                    user_skills.c.skill_id,
                )
            )
            .mappings()
            .all()
        )
        return [(snapshot(row), row["created_at"], row["updated_at"]) for row in rows]

    def resolve(self, scope, reference):
        with self.engine.connect() as conn:
            self.authorization.require(conn, scope)
            row = (
                conn.execute(
                    sa.select(user_skill_versions)
                    .join(user_skills)
                    .where(
                        scoped(user_skills, scope),
                        user_skills.c.enabled.is_(True),
                        user_skill_versions.c.version_id == reference,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise RunNotFound()
        return snapshot(row)
