"""Operator-provisioned email/password login for the unmodified Rakazo client."""

import hashlib
import hmac
import secrets
from dataclasses import asdict
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.identity import AuthenticationFailed
from agentforge_core.models import RunConflict, Scope
from sqlalchemy.exc import IntegrityError

from agentforge_storage.browser_sessions import BrowserSessions, digest
from agentforge_storage.identity import insert, validate_label
from agentforge_storage.schema import (
    browser_sessions,
    identity_memberships,
    identity_spaces,
    identity_tenants,
    identity_tokens,
    identity_users,
    web_credentials,
)


def normalized_email(value):
    if not isinstance(value, str):
        raise ValueError("Invalid email")
    value = value.strip().casefold()
    if (
        len(value) > 254
        or value.count("@") != 1
        or not all(value.split("@"))
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ValueError("Invalid email")
    return value


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 1 <= len(password.encode()) <= 1024:
        raise ValueError("Invalid password length")
    salt = salt or secrets.token_bytes(16)
    value = hashlib.scrypt(
        password.encode(), salt=salt, n=131072, r=8, p=1, maxmem=256 * 1024 * 1024, dklen=32
    )
    return "scrypt$131072$8$1$" + salt.hex() + "$" + value.hex()


def matches(password, encoded):
    try:
        prefix, n, r, p, salt, _ = encoded.split("$")
        if (prefix, n, r, p) != ("scrypt", "131072", "8", "1"):
            return False
        return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)), encoded)
    except (ValueError, TypeError):
        return False


class InvalidCurrentPassword(Exception):
    pass


class PasswordLogin:
    def __init__(self, identities):
        self.identities = identities
        self.engine = identities.engine
        self.clock = identities.clock
        self.browser = BrowserSessions(identities)
        # This dummy is never an account credential; only its KDF cost is used.
        self._dummy = "scrypt$131072$8$1$" + "00" * 16 + "$" + "00" * 32

    def set_password(self, scope, email, password):
        """Local operator only. No unauthenticated account creation or implicit membership."""
        email = normalized_email(email)
        if len(password) < 12:
            raise ValueError("Password must contain at least 12 characters")
        encoded = password_hash(password)
        now = self.clock()
        try:
            with self.engine.begin() as conn:
                self.identities.authorization.require(conn, scope)
                # This upsert also locks the credential against a concurrent sign-in/reset.
                credential = conn.execute(
                    insert(conn, web_credentials)
                    .values(
                        credential_id=str(uuid4()),
                        **asdict(scope),
                        email=email,
                        password_hash=encoded,
                        revision=1,
                        failed_attempts=0,
                        locked_until=0,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["tenant_id", "user_id"],
                        set_={
                            "space_id": scope.space_id,
                            "email": email,
                            "password_hash": encoded,
                            "revision": web_credentials.c.revision + 1,
                            "failed_attempts": 0,
                            "locked_until": 0,
                            "updated_at": now,
                        },
                    )
                    .returning(web_credentials.c.credential_id)
                ).scalar_one()
                # Password changes revoke only password-backed sessions, not operator API tokens.
                conn.execute(
                    identity_tokens.update()
                    .where(
                        identity_tokens.c.token_id.in_(
                            sa.select(browser_sessions.c.token_id).where(
                                browser_sessions.c.credential_id == credential
                            )
                        )
                    )
                    .values(revoked_at=now)
                )
        except IntegrityError:
            raise RunConflict("Email is already assigned") from None

    def register(self, email, password, name):
        """Create an isolated personal workspace and session in one transaction.

        The HTTP boundary must explicitly enable registration. No caller-supplied
        scope or membership is accepted here, and existing accounts are never updated.
        """
        email = normalized_email(email)
        name = name.strip()
        validate_label(name)
        if not 8 <= len(password.encode("utf-16-le")) // 2 <= 128:
            raise ValueError("Password must contain 8–128 UTF-16 code units")
        encoded = password_hash(password)
        scope = Scope(str(uuid4()), str(uuid4()), str(uuid4()))
        credential = str(uuid4())
        now = self.clock()
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    identity_tenants.insert().values(
                        tenant_id=scope.tenant_id,
                        name="Personal",
                        active=True,
                    )
                )
                conn.execute(
                    identity_spaces.insert().values(
                        tenant_id=scope.tenant_id,
                        space_id=scope.space_id,
                        name="Personal",
                        active=True,
                        revision=0,
                    )
                )
                conn.execute(
                    identity_users.insert().values(
                        tenant_id=scope.tenant_id,
                        user_id=scope.user_id,
                        display_name=name,
                        active=True,
                    )
                )
                conn.execute(
                    identity_memberships.insert().values(
                        **asdict(scope),
                        role="admin",
                        active=True,
                    )
                )
                conn.execute(
                    web_credentials.insert().values(
                        credential_id=credential,
                        **asdict(scope),
                        email=email,
                        password_hash=encoded,
                        revision=1,
                        failed_attempts=0,
                        locked_until=0,
                        updated_at=now,
                    )
                )
                raw = self._issue_session(conn, scope, credential, now, now + 28800)
        except IntegrityError:
            raise RunConflict("Email is already registered") from None
        return raw, 28800

    def sign_in(self, email, password):
        try:
            email = normalized_email(email)
        except ValueError:
            raise AuthenticationFailed() from None
        with self.engine.connect() as conn:
            candidate = (
                conn.execute(sa.select(web_credentials).where(web_credentials.c.email == email))
                .mappings()
                .first()
            )
        # Unknown users do the same expensive check; responses never reveal account existence.
        if candidate is None:
            matches(password, self._dummy)
            raise AuthenticationFailed()
        valid = matches(password, candidate["password_hash"])
        now = self.clock()
        result = None
        with self.engine.begin() as conn:
            conn.execute(
                web_credentials.update()
                .where(web_credentials.c.credential_id == candidate["credential_id"])
                .values(revision=web_credentials.c.revision)
            )
            current = (
                conn.execute(
                    sa.select(web_credentials).where(
                        web_credentials.c.credential_id == candidate["credential_id"]
                    )
                )
                .mappings()
                .one()
            )
            if current["revision"] != candidate["revision"]:
                raise AuthenticationFailed()
            if current["locked_until"] > now:
                raise AuthenticationFailed()
            if not valid:
                failures = current["failed_attempts"] + 1
                conn.execute(
                    web_credentials.update()
                    .where(web_credentials.c.credential_id == current["credential_id"])
                    .values(
                        failed_attempts=failures if failures < 5 else 0,
                        locked_until=now + 900 if failures >= 5 else 0,
                    )
                )
            else:
                scope = Scope(current["tenant_id"], current["space_id"], current["user_id"])
                self.identities.authorization.require(conn, scope)
                conn.execute(
                    web_credentials.update()
                    .where(web_credentials.c.credential_id == current["credential_id"])
                    .values(failed_attempts=0, locked_until=0)
                )
                raw = self._issue_session(conn, scope, current["credential_id"], now, now + 28800)
                result = raw, 28800
        if result is None:
            raise AuthenticationFailed()
        return result

    def _issue_session(self, conn, scope, credential_id, now, expiry):
        token_id = str(uuid4())
        conn.execute(
            identity_tokens.insert().values(
                token_id=token_id,
                **asdict(scope),
                label="browser password session",
                token_hash=digest(secrets.token_urlsafe(48)),
                created_at=now,
                expires_at=expiry,
            )
        )
        raw = "afs_" + secrets.token_urlsafe(32)
        conn.execute(
            browser_sessions.insert().values(
                session_id=str(uuid4()),
                session_hash=digest(raw),
                token_id=token_id,
                credential_id=credential_id,
                created_at=now,
                expires_at=expiry,
            )
        )
        return raw

    def change_password(self, raw, current_password, new_password, revoke_others):
        scope, _ = self.browser.resolve(raw)
        credential = sa.select(web_credentials).where(
            web_credentials.c.tenant_id == scope.tenant_id,
            web_credentials.c.user_id == scope.user_id,
        )
        with self.engine.connect() as conn:
            row = conn.execute(credential).mappings().first()
        if not row or not matches(current_password, row["password_hash"]):
            raise InvalidCurrentPassword()
        encoded = password_hash(new_password)
        with self.engine.begin() as conn:
            now = self.clock()
            changed = conn.execute(
                web_credentials.update()
                .where(
                    web_credentials.c.credential_id == row["credential_id"],
                    web_credentials.c.revision == row["revision"],
                )
                .values(
                    password_hash=encoded,
                    revision=row["revision"] + 1,
                    updated_at=now,
                    failed_attempts=0,
                    locked_until=0,
                )
            )
            if not changed.rowcount:
                raise InvalidCurrentPassword()
            # Recheck the calling session after the slow hash and under the credential lock.
            scope, session = self.browser.resolve(raw, connection=conn)
            if not revoke_others:
                return raw
            account_tokens = sa.select(identity_tokens.c.token_id).where(
                identity_tokens.c.tenant_id == scope.tenant_id,
                identity_tokens.c.user_id == scope.user_id,
            )
            conn.execute(
                browser_sessions.update()
                .where(
                    browser_sessions.c.token_id.in_(account_tokens),
                    browser_sessions.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            password_tokens = sa.select(browser_sessions.c.token_id).where(
                browser_sessions.c.credential_id == row["credential_id"]
            )
            conn.execute(
                identity_tokens.update()
                .where(
                    identity_tokens.c.token_id.in_(password_tokens),
                    identity_tokens.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            return self._issue_session(
                conn, scope, row["credential_id"], now, session["expires_at"]
            )
