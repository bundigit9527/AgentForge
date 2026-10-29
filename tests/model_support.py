import json

from agentforge_runtime.config import ModelSettings


def model_settings(**changes):
    return ModelSettings(
        **{
            "provider": "chat_completions",
            "base_url": "https://gateway.example/v1/",
            "model": "test-model",
            "api_key": "test-only-model-credential",
            **changes,
        }
    )


def tool_call(call_id="call-1", name="example_report", arguments=None):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"text": "资料摘要"}) if arguments is None else arguments,
        },
    }


def completion(content="最终回答", calls=None, *, usage=True, finish_reason=None):
    body = {
        "choices": [
            {
                "message": {"role": "assistant", "content": content, "tool_calls": calls},
                "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
            }
        ],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return body
