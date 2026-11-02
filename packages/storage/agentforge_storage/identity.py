import hashlib
import math
import secrets
import time
from dataclasses import asdict
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.identity import AuthenticationFailed, PermissionDenied
from agentforge_core.models import RunConflict, RunNotFound, Scope
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from agentforge_storage.authorization import Authorization, owned, workspace
from agentforge_storage.schema import (
    identity_memberships as memberships,
)
from agentforge_storage.schema import (
    identity_spaces as spaces,
)
from agentforge_storage.schema import (
    identity_tenants as tenants,
)
from agentforge_storage.schema import (
    identity_tokens as tokens,
)
from agentforge_storage.schema import (
    identity_users as users,
)


def insert(connection, table):
    return (pg_insert if connection.dialect.name == "postgresql" else sqlite_insert)(table)


def validate_label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("Names must contain 1–128 characters")


class IdentityRepository:
    def __init__(self, engine, clock=time.time):
        self.engine = engine
        self.clock = clock
        self.authorization = Authorization("database")

    def bootstrap(self, scope, *, tenant_name, space_name, display_name):
        """Operator-only provisioning. Never invoked by API startup or an unauthenticated route."""
        for value in (tenant_name, space_name, display_name):
            validate_label(value)
        try:
            with self.engine.begin() as connection:
                if connection.execute(
                    sa.select(spaces.c.space_id).where(workspace(spaces, scope))
                ).first():
                    raise RunConflict("Space already exists")
                connection.execute(
                    insert(connection, tenants)
                    .values(
                        tenant_id=scope.tenant_id,
                        name=tenant_name,
                        active=True,
                    )
                    .on_conflict_do_nothing()
                )
                connection.execute(
                    spaces.insert().values(
                        tenant_id=scope.tenant_id,
                        space_id=scope.space_id,
                        name=space_name,
                        active=True,
                        revision=0,
                    )
                )
                connection.execute(
                    insert(connection, users)
                    .values(
                        tenant_id=scope.tenant_id,
                        user_id=scope.user_id,
                        display_name=display_name,
                        active=True,
                    )
                    .on_conflict_do_nothing()
                )
                connection.execute(
                    memberships.insert().values(**asdict(scope), role="admin", active=True)
                )
        except IntegrityError:
            raise RunConflict("Identity already exists") from None

    def authenticate(self, token):
        if not isinstance(token, str) or not token.startswith("af_") or not 40 <= len(token) <= 256:
            raise AuthenticationFailed()
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(tokens).where(
                        tokens.c.token_hash == digest,
                        tokens.c.revoked_at.is_(None),
                        tokens.c.expires_at > self.clock(),
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AuthenticationFailed()
            scope = Scope(row["tenant_id"], row["space_id"], row["user_id"])
            try:
                self.authorization.require(connection, scope)
            except PermissionDenied:
                raise AuthenticationFailed() from None
            return scope

    def issue_token(self, scope, *, label="local access", lifetime_seconds=86400):
        """Operator-only API: return plaintext once, persist only a SHA-256 digest."""
        validate_label(label)
        if not math.isfinite(lifetime_seconds) or not 60 <= lifetime_seconds <= 86400 * 365:
            raise ValueError("Token lifetime must be between one minute and one year")
        token = "af_" + secrets.token_urlsafe(32)
        now = self.clock()
        identifier = str(uuid4())
        with self.engine.begin() as connection:
            self.authorization.require(connection, scope)
            connection.execute(
                tokens.insert().values(
                    token_id=identifier,
                    **asdict(scope),
                    label=label,
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    created_at=now,
                    expires_at=now + lifetime_seconds,
                )
            )
        return {"token_id": identifier, "token": token, "expires_at": now + lifetime_seconds}

    def list_tokens(self, scope):
        with self.engine.connect() as connection:
            self.authorization.require(connection, scope)
            return (
                connection.execute(
                    sa.select(
                        tokens.c.token_id,
                        tokens.c.label,
                        tokens.c.created_at,
                        tokens.c.expires_at,
                        tokens.c.revoked_at,
                    )
                    .where(owned(tokens, scope))
                    .order_by(tokens.c.created_at, tokens.c.token_id)
                )
                .mappings()
                .all()
            )

    def revoke_token(self, scope, token_id):
        with self.engine.begin() as connection:
            role = self.authorization.require(connection, scope)
            rule = workspace(tokens, scope) if role == "admin" else owned(tokens, scope)
            changed = connection.execute(
                tokens.update()
                .where(
                    rule,
                    tokens.c.token_id == token_id,
                )
                .values(revoked_at=self.clock())
            )
            if changed.rowcount != 1:
                raise RunNotFound()

    def list_members(self, scope, offset=0, limit=100):
        with self.engine.connect() as connection:
            self.authorization.require(connection, scope, admin=True)
            return (
                connection.execute(
                    sa.select(
                        memberships.c.user_id,
                        memberships.c.role,
                        memberships.c.active,
                        users.c.display_name,
                    )
                    .join(
                        users,
                        sa.and_(
                            users.c.tenant_id == memberships.c.tenant_id,
                            users.c.user_id == memberships.c.user_id,
                        ),
                    )
                    .where(workspace(memberships, scope))
                    .order_by(memberships.c.user_id)
                    .offset(offset)
                    .limit(limit)
                )
                .mappings()
                .all()
            )

    def set_member(self, actor, user_id, *, display_name, role, active=True):
        target = Scope(actor.tenant_id, actor.space_id, user_id)
        validate_label(display_name)
        if role not in {"admin", "member", "viewer"} or type(active) is not bool:
            raise ValueError("Invalid membership")
        with self.engine.begin() as connection:
            self.authorization.require(connection, actor, admin=True)
            # Serialize membership changes before evaluating the last-admin invariant.
            connection.execute(
                spaces.update()
                .where(workspace(spaces, actor))
                .values(
                    revision=spaces.c.revision + 1,
                )
            )
            self.authorization.require(connection, actor, admin=True)
            current = (
                connection.execute(sa.select(memberships).where(owned(memberships, target)))
                .mappings()
                .first()
            )
            if (
                current
                and current["active"]
                and current["role"] == "admin"
                and (not active or role != "admin")
            ):
                count = connection.execute(
                    sa.select(sa.func.count())
                    .select_from(memberships)
                    .join(
                        users,
                        sa.and_(
                            users.c.tenant_id == memberships.c.tenant_id,
                            users.c.user_id == memberships.c.user_id,
                        ),
                    )
                    .where(
                        workspace(memberships, actor),
                        memberships.c.active.is_(True),
                        memberships.c.role == "admin",
                        users.c.active.is_(True),
                    )
                ).scalar_one()
                if count <= 1:
                    raise RunConflict("The last active administrator cannot be removed")
            # A space admin does not rename an existing tenant-wide user in another space.
            connection.execute(
                insert(connection, users)
                .values(
                    tenant_id=target.tenant_id,
                    user_id=target.user_id,
                    display_name=display_name,
                    active=True,
                )
                .on_conflict_do_nothing()
            )
            statement = insert(connection, memberships).values(
                **asdict(target), role=role, active=active
            )
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=["tenant_id", "space_id", "user_id"],
                    set_={"role": role, "active": active},
                )
            )
        return {"user_id": user_id, "role": role, "active": active}
