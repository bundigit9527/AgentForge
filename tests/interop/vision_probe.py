"""Run with uv --env-file .env.vision; validates actual image input and tool calling."""

import base64
import json

from agentforge_runtime.chat_model import ChatCompletionsModel
from vision_support import challenge, configure_vision


def main():
    settings = configure_vision()
    model = ChatCompletionsModel(settings)
    try:
        result = model.complete(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Inspect this image. Which colored panel extends past the content "
                                "border and is clipped at an outer edge? Report its color and the "
                                "clipped edge using report_observation."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(challenge()).decode()
                            },
                        },
                    ],
                }
            ],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "report_observation",
                        "description": "Report only what is visible",
                        "parameters": {
                            "type": "object",
                            "properties": {"color": {"type": "string"}, "edge": {"type": "string"}},
                            "required": ["color", "edge"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": "report_observation"}},
        )
        calls = result.choices[0].message.tool_calls or []
        assert len(calls) == 1 and calls[0].function.name == "report_observation", (
            "Vision tool call missing"
        )
        observation = json.loads(calls[0].function.arguments)
        assert observation["color"].lower() == "red" and observation["edge"].lower() == "right", (
            "Visual challenge not correctly identified"
        )
        print(
            json.dumps(
                {
                    "passed": True,
                    "model": settings.model,
                    "image_input": True,
                    "function_calling": True,
                    "observation": observation,
                }
            )
        )
    finally:
        model.close()


if __name__ == "__main__":
    main()
