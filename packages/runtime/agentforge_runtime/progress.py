"""Coalesce provider tokens into bounded durable UI updates."""

import time

from agentforge_core.models import RunErrorCode

from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure


class TextProgress:
    def __init__(self, publish, *, clock=time.monotonic):
        self.publish = publish
        self.clock = clock
        self.pending = ""
        self.step = 0
        self.characters = 0
        self.count = 0
        self.last_flush = 0.0

    def push(self, step, text):
        if not text or self.publish is None:
            return
        if self.step and self.step != step:
            text = "\n\n" + text
        self.step = step
        self.characters += len(text)
        if self.characters > 80000:
            raise RuntimeFailure(RunErrorCode.MODEL_LIMIT_EXCEEDED)
        self.pending += text
        if self.count == 0 or len(self.pending) >= 256 or self.clock() - self.last_flush >= 0.25:
            self.flush()

    def flush(self):
        while self.pending:
            if self.count >= 4096:
                raise RuntimeFailure(RunErrorCode.MODEL_LIMIT_EXCEEDED)
            text, self.pending = self.pending[:2048], self.pending[2048:]
            if not self.publish(step=self.step, text=text, reset=self.count == 0):
                raise ExecutionStopped()
            self.count += 1
            self.last_flush = self.clock()
