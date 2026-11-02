import hashlib
import secrets
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.identity import AuthenticationFailed
from agentforge_core.models import Scope

from agentforge_storage.identity import IdentityRepository
from agentforge_storage.schema import (
    browser_sessions,
    identity_spaces,
    identity_tokens,
    identity_users,
)
from agentforge_storage.thread_feed import iso


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class BrowserSessions:
    def __init__(self, identities: IdentityRepository):
        self.identities = identities
        self.engine = identities.engine
        self.clock = identities.clock

    def create(self, token):
        scope = self.identities.authenticate(token)
        raw = "afs_" + secrets.token_urlsafe(32)
        now = self.clock()
        with self.engine.begin() as conn:
            credential = (
                conn.execute(
                    sa.select(identity_tokens).where(
                        identity_tokens.c.token_hash == digest(token),
                        identity_tokens.c.revoked_at.is_(None),
                        identity_tokens.c.expires_at > now,
                    )
                )
                .mappings()
                .first()
            )
            if credential is None:
                raise AuthenticationFailed()
            self.identities.authorization.require(conn, scope)
            expiry = min(credential["expires_at"], now + 28800)
            conn.execute(
                browser_sessions.insert().values(
                    session_id=str(uuid4()),
                    session_hash=digest(raw),
                    token_id=credential["token_id"],
                    created_at=now,
                    expires_at=expiry,
                )
            )
        return raw, int(expiry - now)

    def resolve(self, raw):
        if not raw or not raw.startswith("afs_") or len(raw) > 128:
            raise AuthenticationFailed()
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(
                        browser_sessions,
                        identity_tokens.c.tenant_id,
                        identity_tokens.c.space_id,
                        identity_tokens.c.user_id,
                    )
                    .join(identity_tokens)
                    .where(
                        browser_sessions.c.session_hash == digest(raw),
                        browser_sessions.c.revoked_at.is_(None),
                        browser_sessions.c.expires_at > self.clock(),
                        identity_tokens.c.revoked_at.is_(None),
                        identity_tokens.c.expires_at > self.clock(),
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AuthenticationFailed()
            scope = Scope(row["tenant_id"], row["space_id"], row["user_id"])
            self.identities.authorization.require(conn, scope)
            return scope, dict(row)

    def revoke(self, raw):
        if raw:
            with self.engine.begin() as conn:
                conn.execute(
                    browser_sessions.update()
                    .where(browser_sessions.c.session_hash == digest(raw))
                    .values(revoked_at=self.clock())
                )

    def profile(self, scope):
        with self.engine.connect() as conn:
            user = (
                conn.execute(
                    sa.select(identity_users).where(
                        identity_users.c.tenant_id == scope.tenant_id,
                        identity_users.c.user_id == scope.user_id,
                    )
                )
                .mappings()
                .first()
            )
            space = conn.execute(
                sa.select(identity_spaces.c.name).where(
                    identity_spaces.c.tenant_id == scope.tenant_id,
                    identity_spaces.c.space_id == scope.space_id,
                )
            ).scalar_one_or_none()
        # Display-only address for Better Auth UI; never an identity lookup or verified email.
        address = digest(scope.tenant_id + "\0" + scope.user_id)[:24] + "@agentforge.invalid"
        return {
            "id": scope.user_id,
            "name": user["display_name"] if user else scope.user_id,
            "email": address,
            "emailVerified": False,
            "image": None,
        }, space or scope.space_id

    def session_view(self, raw):
        scope, row = self.resolve(raw)
        user, _ = self.profile(scope)
        now = iso(row["created_at"])
        return {
            "user": {**user, "createdAt": now, "updatedAt": now},
            "session": {
                "id": row["session_id"],
                "userId": scope.user_id,
                "expiresAt": iso(row["expires_at"]),
                "createdAt": now,
                "updatedAt": now,
            },
        }
