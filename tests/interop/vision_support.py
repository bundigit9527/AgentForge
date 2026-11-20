"""Explicit opt-in Qwen settings and a synthetic visual challenge, no user data."""

import os
import struct
import zlib

from agentforge_runtime.config import ModelSettings


def configure_vision():
    base = os.environ.get("AGENTFORGE_VISION_BASE_URL", "")
    model = os.environ.get("AGENTFORGE_VISION_MODEL", "")
    key = os.environ.get("AGENTFORGE_VISION_API_KEY", "")
    if not base or "YOUR_WORKSPACE_ID" in base or not model or not key:
        raise ValueError("Complete .env.vision base URL, model and API key first")
    settings = ModelSettings(
        provider="chat_completions",
        base_url=base,
        api_key=key,
        model=model,
        supports_images=True,
        image_models=(model,),
        api_style="compatible",
        context_max_chars=240000,
        compact_tool_results=True,
        timeout_seconds=120,
        max_rounds=24,
        max_completion_tokens=4096,
        max_tool_repairs=2,
    )
    # Only this disposable runner's process changes; the working .env is untouched.
    for k in list(os.environ):
        if k.startswith("AGENTFORGE_MODEL_"):
            del os.environ[k]
    os.environ.update(
        AGENTFORGE_MODEL_PROVIDER="chat_completions",
        AGENTFORGE_MODEL_BASE_URL=base,
        AGENTFORGE_MODEL_NAME=model,
        AGENTFORGE_MODEL_API_KEY=key,
        AGENTFORGE_MODEL_IMAGE_MODELS=model,
        AGENTFORGE_MODEL_API_STYLE="compatible",
        AGENTFORGE_MODEL_CONTEXT_MAX_CHARS="240000",
        AGENTFORGE_MODEL_MAX_ROUNDS="24",
        AGENTFORGE_MODEL_MAX_COMPLETION_TOKENS="4096",
        AGENTFORGE_MODEL_TIMEOUT_SECONDS="120",
    )
    return settings


def challenge():
    # Red panel crosses the right content border; green panel appears below the fold.
    width, height = 1000, 1200
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            color = (250, 250, 250)
            if (
                x in range(39, 43)
                or x in range(957, 961)
                or y in range(39, 43)
                or y in range(1157, 1161)
            ):
                color = (30, 30, 30)
            if 650 <= x and 150 <= y < 400:
                color = (230, 20, 20)
            if 150 <= x < 700 and 900 <= y < 1100:
                color = (20, 170, 40)
            row.extend(color)
        rows.append(b"\0" + row)

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )
