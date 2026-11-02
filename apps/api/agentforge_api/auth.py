import secrets

from agentforge_core.identity import AuthenticationFailed, AuthenticationUnavailable


class DevelopmentIdentityProvider:
    def __init__(self, settings):
        self.settings = settings

    def authenticate(self, token):
        configured = self.settings.dev_token
        if not configured:
            raise AuthenticationUnavailable()
        if token is None or not secrets.compare_digest(token.encode(), configured.encode()):
            raise AuthenticationFailed()
        return self.settings.dev_scope
