"""Only ChatHarness owns the durable step/call keys; model text cannot authorize tools."""

import json

from agentforge_core.effects import EffectRequest
from agentforge_core.models import RunConflict, RunErrorCode
from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure
from agentforge_storage.approvals import ApprovalRepository


class ApprovalExecution:
    def __init__(self, repository, claim):
        self.store = ApprovalRepository(repository)
        self.claim = claim
        self.required = set(claim.run.execution_config.approval_tools)

    def load(self):
        return self.store.load(self.claim)

    def save(self, state):
        self.store.save(self.claim, state)

    def execute(self, executor, invocation, key):
        ref = invocation.definition.reference
        if ref not in self.required:
            return executor.execute(invocation)
        effect = self.store.effects.request(
            self.claim,
            key,
            EffectRequest(
                tool_reference=ref,
                target="registered-tool",
                arguments=invocation.arguments.model_dump(),
            ),
        )
        eid = effect["effect_id"]
        if effect["status"] == "pending":
            if self.store.granted(self.claim.run.scope, ref):
                effect = self.store.effects.decide(
                    self.claim.run.scope,
                    self.claim.run.run_id,
                    eid,
                    decision="approve",
                    request_hash=effect["request_hash"],
                )
            elif self.store.pause(self.claim, effect):
                raise ExecutionStopped()
            else:
                raise ExecutionStopped()
        if effect["status"] == "rejected":
            return json.dumps({"error": "User denied this operation. Do not retry or rephrase it."})
        if effect["status"] == "succeeded":
            return effect["result"]["content"]
        if effect["status"] != "approved":
            raise RuntimeFailure(RunErrorCode.EFFECT_UNCERTAIN)
        try:
            self.store.effects.begin_dispatch(self.claim, eid)
        except RunConflict:
            raise ExecutionStopped() from None
        # Unknown/exceptional outcomes deliberately remain dispatched. Never automatically replay.
        result = executor.execute(invocation)
        self.store.effects.complete(self.claim, eid, succeeded=True, result={"content": result})
        return result
