import sqlalchemy as sa
from agentforge_core.identity import PermissionDenied
from agentforge_core.models import RunNotFound

from agentforge_storage.schema import (
    agents,
    identity_memberships,
    identity_spaces,
    identity_tenants,
    identity_users,
    resource_shares,
    runs,
    skill_installations,
    threads,
    user_skill_versions,
    user_skills,
)

RESOURCES = {
    "agent": (agents, "agent_id"),
    "skill": (skill_installations, "installation_id"),
    "thread": (threads, "thread_id"),
    "run": (runs, "run_id"),
}


def workspace(table, scope):
    return sa.and_(table.c.tenant_id == scope.tenant_id, table.c.space_id == scope.space_id)


def owned(table, scope):
    return sa.and_(workspace(table, scope), table.c.user_id == scope.user_id)


def membership_query(scope):
    return (
        sa.select(identity_memberships.c.role)
        .join(
            identity_tenants,
            identity_tenants.c.tenant_id == identity_memberships.c.tenant_id,
        )
        .join(
            identity_spaces,
            sa.and_(
                identity_spaces.c.tenant_id == identity_memberships.c.tenant_id,
                identity_spaces.c.space_id == identity_memberships.c.space_id,
            ),
        )
        .join(
            identity_users,
            sa.and_(
                identity_users.c.tenant_id == identity_memberships.c.tenant_id,
                identity_users.c.user_id == identity_memberships.c.user_id,
            ),
        )
        .where(
            owned(identity_memberships, scope),
            identity_memberships.c.active.is_(True),
            identity_tenants.c.active.is_(True),
            identity_spaces.c.active.is_(True),
            identity_users.c.active.is_(True),
        )
    )


class Authorization:
    def __init__(self, mode="development"):
        if mode not in {"development", "database"}:
            raise ValueError("Unknown authorization mode")
        self.mode = mode

    def member_condition(self, scope, *, write=False):
        if self.mode == "development":
            return sa.true()
        query = membership_query(scope)
        if write:
            query = query.where(identity_memberships.c.role.in_(["admin", "member"]))
        return query.exists()

    def require(self, connection, scope, *, write=False, admin=False):
        role = (
            "member"
            if self.mode == "development"
            else connection.execute(membership_query(scope)).scalar_one_or_none()
        )
        if role is None or (write and role == "viewer") or (admin and role != "admin"):
            raise PermissionDenied()
        return role

    def shared(self, scope, kind, resource_id):
        return (
            sa.select(resource_shares.c.resource_id)
            .where(
                workspace(resource_shares, scope),
                resource_shares.c.resource_kind == kind,
                resource_shares.c.resource_id == resource_id,
            )
            .exists()
        )

    def visible(self, table, scope, kind, key):
        access = sa.or_(table.c.user_id == scope.user_id, self.shared(scope, kind, table.c[key]))
        if kind == "run":
            access = sa.or_(access, self.shared(scope, "thread", table.c.thread_id))
        return sa.and_(workspace(table, scope), self.member_condition(scope), access)

    def check_row(self, connection, scope, kind, row, *, owner_only=False):
        self.require(connection, scope)
        if row is None or row["tenant_id"] != scope.tenant_id or row["space_id"] != scope.space_id:
            raise RunNotFound()
        if row["user_id"] == scope.user_id:
            return
        if owner_only:
            raise RunNotFound()
        _, key = RESOURCES[kind]
        visible = self.shared(scope, kind, row[key])
        if kind == "run" and row["thread_id"]:
            visible = sa.or_(visible, self.shared(scope, "thread", row["thread_id"]))
        if not connection.execute(sa.select(visible)).scalar_one():
            raise RunNotFound()

    def dependencies(self, scope, config, agent_id=None):
        checks = []
        if agent_id:
            checks.append(
                sa.select(agents.c.agent_id)
                .where(
                    agents.c.agent_id == agent_id, self.visible(agents, scope, "agent", "agent_id")
                )
                .exists()
            )
        for skill in config.available_skills:
            if skill.source == "hub":
                # Uninstall hides future selection; existing snapshots keep their owner access.
                checks.append(
                    sa.select(skill_installations.c.installation_id)
                    .where(
                        skill_installations.c.installation_id == skill.reference,
                        self.visible(skill_installations, scope, "skill", "installation_id"),
                    )
                    .exists()
                )
            elif skill.source == "user":
                # Deletion hides future selection, while owner snapshots remain executable.
                # User skill versions never acquire sharing through the Hub asset table.
                checks.append(
                    sa.select(user_skill_versions.c.version_id)
                    .join(user_skills)
                    .where(
                        owned(user_skills, scope),
                        user_skills.c.skill_id == skill.source_id[5:],
                        user_skill_versions.c.version_id == skill.reference,
                    )
                    .exists()
                )
        return sa.and_(sa.true(), *checks)

    def require_dependencies(self, connection, scope, config, agent_id=None):
        if not connection.execute(
            sa.select(self.dependencies(scope, config, agent_id))
        ).scalar_one():
            raise RunNotFound()

    def execution_condition(self, run):
        mode = (
            "database"
            if (
                self.mode == "database"
                or (run.execution_config and run.execution_config.authorization_mode == "database")
            )
            else "development"
        )
        policy = Authorization(mode)
        dependencies = (
            policy.dependencies(run.scope, run.execution_config, run.agent_id)
            if run.execution_config
            else sa.true()
        )
        return sa.and_(policy.member_condition(run.scope, write=True), dependencies)

    def visibility(self, connection, scope, kind, resource_id):
        table, key = RESOURCES[kind]
        row = (
            connection.execute(sa.select(table).where(table.c[key] == resource_id))
            .mappings()
            .first()
        )
        self.check_row(connection, scope, kind, row, owner_only=True)
        shared = connection.execute(sa.select(self.shared(scope, kind, resource_id))).scalar_one()
        return "space" if shared else "private"

    def set_visibility(self, connection, scope, kind, resource_id, visibility):
        self.require(connection, scope, write=True)
        self.visibility(connection, scope, kind, resource_id)
        identity = sa.and_(
            workspace(resource_shares, scope),
            resource_shares.c.resource_kind == kind,
            resource_shares.c.resource_id == resource_id,
        )
        if visibility == "private":
            connection.execute(resource_shares.delete().where(identity))
        else:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            insert = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
            connection.execute(
                insert(resource_shares)
                .values(
                    tenant_id=scope.tenant_id,
                    space_id=scope.space_id,
                    resource_kind=kind,
                    resource_id=resource_id,
                )
                .on_conflict_do_nothing()
            )
        return visibility
