"""Account-owned credentials; choices remain private to the user in each space."""

from contextlib import contextmanager
from dataclasses import asdict

import sqlalchemy as sa
from agentforge_core.model_management import ModelManagementError
from agentforge_core.models import RunConflict

from agentforge_storage.conversations import scoped
from agentforge_storage.identity import insert
from agentforge_storage.schema import (
    identity_users,
    model_backups,
    model_connections,
    model_selections,
)


class ModelConnections:
    def __init__(self, store):
        self.store = store

    def owner(self, scope):
        return sa.and_(
            model_connections.c.tenant_id == scope.tenant_id,
            model_connections.c.user_id == scope.user_id,
        )

    @contextmanager
    def connection(self, scope, existing=None, *, write=False):
        if existing is None:
            with self.store.engine.begin() as conn:
                with self.connection(scope, conn, write=write) as checked:
                    yield checked
        else:
            self.store.authorization.require(existing, scope, write=write)
            if write:
                if self.store.authorization.mode != "database":
                    raise ModelManagementError("Model management requires database identity", 403)
                existing.execute(
                    identity_users.update()
                    .where(
                        identity_users.c.tenant_id == scope.tenant_id,
                        identity_users.c.user_id == scope.user_id,
                    )
                    .values(active=identity_users.c.active)
                )
            yield existing

    def rows(self, scope, conn=None, *, include_inactive=False):
        with self.connection(scope, conn) as c:
            query = sa.select(model_connections).where(self.owner(scope))
            if not include_inactive:
                query = query.where(model_connections.c.active.is_(True))
            return [dict(row) for row in c.execute(query).mappings()]

    def get(self, scope, provider, conn=None, *, include_inactive=False):
        return next(
            (
                r
                for r in self.rows(scope, conn, include_inactive=include_inactive)
                if r["provider"] == provider
            ),
            None,
        )

    def save(self, scope, row, expected_revision):
        with self.connection(scope, write=True) as conn:
            current = self.get(scope, row["provider"], conn, include_inactive=True)
            if (current["revision"] if current else None) != expected_revision:
                raise RunConflict("Model connection changed; reload and retry")
            if current:
                conn.execute(
                    model_connections.update()
                    .where(
                        self.owner(scope),
                        model_connections.c.connection_id == current["connection_id"],
                    )
                    .values(**row)
                )
            else:
                conn.execute(model_connections.insert().values(**row))
            self.select(scope, row["provider"], row["configuration"]["modelId"], conn)

    def choice(self, scope, conn=None):
        with self.connection(scope, conn) as c:
            return (
                c.execute(sa.select(model_selections).where(scoped(model_selections, scope)))
                .mappings()
                .first()
            )

    def select(self, scope, provider, model, conn=None):
        with self.connection(scope, conn, write=True) as c:
            values = dict(provider=provider, model_id=model)
            c.execute(
                insert(c, model_selections)
                .values(**asdict(scope), **values)
                .on_conflict_do_update(
                    index_elements=["tenant_id", "space_id", "user_id"], set_=values
                )
            )

    def backups(self, scope, conn=None):
        with self.connection(scope, conn) as c:
            return (
                c.execute(
                    sa.select(model_backups.c.choices).where(scoped(model_backups, scope))
                ).scalar_one_or_none()
                or []
            )

    def set_backups(self, scope, choices, conn=None):
        with self.connection(scope, conn, write=True) as c:
            c.execute(
                insert(c, model_backups)
                .values(**asdict(scope), choices=choices)
                .on_conflict_do_update(
                    index_elements=["tenant_id", "space_id", "user_id"], set_={"choices": choices}
                )
            )

    def disconnect(self, scope, provider):
        with self.connection(scope, write=True) as conn:
            conn.execute(
                model_connections.update()
                .where(self.owner(scope), model_connections.c.provider == provider)
                .values(active=False, encrypted_key=b"", revision=model_connections.c.revision + 1)
            )
            conn.execute(
                model_selections.delete().where(
                    model_selections.c.tenant_id == scope.tenant_id,
                    model_selections.c.user_id == scope.user_id,
                    model_selections.c.provider == provider,
                )
            )
            for row in conn.execute(
                sa.select(model_backups).where(
                    model_backups.c.tenant_id == scope.tenant_id,
                    model_backups.c.user_id == scope.user_id,
                )
            ).mappings():
                conn.execute(
                    model_backups.update()
                    .where(
                        model_backups.c.tenant_id == scope.tenant_id,
                        model_backups.c.space_id == row["space_id"],
                        model_backups.c.user_id == scope.user_id,
                    )
                    .values(choices=[c for c in row["choices"] if c["provider"] != provider])
                )
        return {"ok": True}
