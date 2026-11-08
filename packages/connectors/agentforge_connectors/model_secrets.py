"""Authenticated encryption; the deployment key never lives in the database."""

import base64
import json
import os
import secrets

from agentforge_core.model_management import ModelManagementError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def cipher():
    try:
        encoded = os.environ["AGENTFORGE_MODEL_ENCRYPTION_KEY"]
        # secrets.token_urlsafe(32) omits the trailing '='. Accept both this
        # documented format and padded base64 without altering the key bytes.
        if len(encoded) not in {43, 44}:
            raise ValueError()
        key = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if len(key) != 32:
            raise ValueError()
        return AESGCM(key)
    except (KeyError, ValueError):
        raise ModelManagementError(
            "Configure the shared model encryption key on API and Worker", 503
        ) from None


def aad(row):
    return json.dumps(
        [
            row[k]
            for k in (
                "tenant_id",
                "user_id",
                "connection_id",
                "revision",
                "provider",
                "configuration",
            )
        ],
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def seal(row, key):
    nonce = secrets.token_bytes(12)
    return nonce + cipher().encrypt(nonce, key.encode(), aad(row))


def unseal(row):
    try:
        value = row["encrypted_key"]
        return cipher().decrypt(value[:12], value[12:], aad(row)).decode()
    except ModelManagementError:
        raise
    except Exception:
        raise ModelManagementError(
            "Stored model credentials cannot be opened; reconnect this provider", 503
        ) from None
