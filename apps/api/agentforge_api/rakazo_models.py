"""Native Rakazo model catalog over an operator-managed gateway."""

from agentforge_storage.model_preferences import ModelPreferences
from agentforge_storage.rakazo import compatibility_id


class ModelRpcAdapter:
    def __init__(self, store, settings):
        self.settings = settings
        self.preferences = ModelPreferences(store, settings)

    def catalog(self):
        return [
            {
                "provider": self.settings.provider,
                "providerName": "AgentForge Gateway",
                "id": model,
                "label": model,
                "billing": "由服务端路由计费",
                "auth": "api-key",
                "authHint": "管理员在 Worker 配置密钥，页面不保存密钥",
                "reasoning": False,
                "thinkingLevels": [],
            }
            for model in self.settings.model_ids
        ]

    def credentials(self, scope):
        if not self.settings.model_ids:
            return []
        selected = self.preferences.default(scope)
        return [
            {
                "id": compatibility_id(
                    "managed-model", scope.tenant_id, scope.space_id, scope.user_id
                ),
                "provider": selected.provider,
                "label": "服务端托管路由（Worker 凭据）",
                # API has no personal/stored key and cannot verify the Worker's credential.
                "hasKey": False,
                "isDefault": True,
                "modelId": selected.model,
                "reasoning": False,
                "thinkingLevels": [],
                "supportsImages": False,
                "maxTokens": selected.max_completion_tokens,
            }
        ]
