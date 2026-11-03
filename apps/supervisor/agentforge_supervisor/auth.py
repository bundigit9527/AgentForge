import hmac
import os

from starlette.responses import Response


class RequireWorkerToken:
    def __init__(self, app, token: bytes):
        self.app = app
        self.expected = b"Bearer " + token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
            if len(values) != 1 or not hmac.compare_digest(values[0], self.expected):
                await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def service_token(variable):
    token = os.environ.get(variable, "")
    if not (
        32 <= len(token) <= 256 and token.isascii() and token.isprintable() and " " not in token
    ):
        raise ValueError(f"Configure a 32–256 character {variable}")
    return token
