"""Per-user Web defaults, independent of immutable Agent and Run model snapshots."""

from dataclasses import asdict

import sqlalchemy as sa

from agentforge_storage.conversations import scoped
from agentforge_storage.identity import insert
from agentforge_storage.schema import rpc_model_preferences


class ModelPreferences:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings

    def default(self, scope, *, connection=None):
        if connection is None:
            with self.store.engine.connect() as conn:
                return self.default(scope, connection=conn)
        self.store.authorization.require(connection, scope)
        row = (
            connection.execute(
                sa.select(rpc_model_preferences).where(scoped(rpc_model_preferences, scope))
            )
            .mappings()
            .first()
        )
        if row and row["route_fingerprint"] == self.settings.snapshot().route_fingerprint:
            if row["model_id"] in self.settings.model_ids:
                return self.settings.select(self.settings.provider, row["model_id"])
        return self.settings

    def set_default(self, scope, provider, model):
        selected = self.settings.select(provider, model)
        with self.store.engine.begin() as conn:
            self.store.authorization.require(conn, scope, write=True)
            values = {
                "model_id": selected.model,
                "route_fingerprint": selected.snapshot().route_fingerprint,
            }
            conn.execute(
                insert(conn, rpc_model_preferences)
                .values(**asdict(scope), **values)
                .on_conflict_do_update(
                    index_elements=["tenant_id", "space_id", "user_id"], set_=values
                )
            )
        return {"ok": True}
