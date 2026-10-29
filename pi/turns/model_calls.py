"""How the loop calls a model: the tools it may offer, stop control, and one provider call."""

from __future__ import annotations

from pi.actions import tools as tool_protocol
from pi.actions.toolgate import ToolGateUnavailable
from pi.conversation import live_stream
from pi.models import roles as model_roles
from pi.models.openrouter import ModelUnusable
from pi.models.providers import Message, ProviderUnavailable
from pi.models.routing import Reason, Route, Tier, TurnContext
from pi.turns import control as turn_control
from pi.turns import steering as turn_steering

RETRY = (
    "Your last reply was not used: {reason} What was wrong: {mistake} "
    "Reply again with exactly one of these: only the JSON "
    'tool request and nothing else, as valid JSON in the form {{"tool": "<exact listed tool ID>", '
    '"args": {{...}}}}, or only your answer to the owner with no JSON in it.'
)


def identity_scopes(tool_ids):
    """An agent's toolkit as ToolGate scopes: tools by id, published workflows by their root.
    Conker's own tools live in Pi and need none."""
    scopes = []
    for tool_id in tool_ids:
        if tool_id.startswith("conker."):
            continue
        if tool_id.startswith("workflow:"):
            scopes.append("automation:" + tool_id.split(":")[1])
        else:
            scopes.append(tool_id)
    return scopes


class ModelCalls:
    """Part of Loop: model calls. Uses the attributes Loop.__init__ sets."""

    def _offers_tools(self) -> bool:
        """ToolGate is set up and the owner did not leave tools out at setup."""
        if self.toolgate is None:
            return False
        from pi.setup import choices as setup_choices

        return setup_choices.current(self.store, "capabilities").choice != "skip"

    def _gate(self, execution=None):
        """ToolGate as the running agent, so its toolkit is enforced there and not only by Pi."""
        execution = execution or {}
        agent = execution.get("agentId") or "companion"
        if execution.get("kind", "companion") == "companion":
            scopes = ["*"]
        else:
            scopes = identity_scopes(execution["configuration"]["toolIds"])
        acting = getattr(self.toolgate, "acting_as", None)
        return acting(agent, scopes) if acting else self.toolgate

    def _available_tools(self, execution=None):
        """What this key is scoped to right now, or nothing.

        Asked per turn rather than cached: the owner can widen or narrow scope
        at any moment, and a cached list would let Pi offer the model a tool it
        no longer has, then explain a refusal it should have predicted.
        """
        if self.toolgate is None:
            return []
        from pi.setup import choices as setup_choices

        if setup_choices.current(self.store, "capabilities").choice == "skip":
            return []
        try:
            tools = self._gate(execution).tools()
            if execution and execution["kind"] != "companion":
                selected = execution["configuration"]["toolIds"]
                tools = [tool for tool in tools if tool.id in selected]
            return tools
        except ToolGateUnavailable:
            # Tools unavailable is not the turn failing. Conversation still
            # works, and the model is simply offered nothing.
            return []

    def _requested(self, route, completion, history, ctx, execution, allowed, tools=()):
        """The model's tool request, if any. The invalid outputs small models make (a request
        wrapped in prose or narrated and quoted in a code block, request JSON that does not parse,
        a tool's bare arguments, an input name with something added) get one more try with the
        rule spelled out and the mistake named; nothing from that reply runs or is kept, and a
        second invalid reply fails the turn. Every other invalid output fails at once, as before."""
        try:
            return route, completion, self._request(completion.text, allowed, tools)
        except tool_protocol.InvalidToolOutput as exc:
            if exc.reason not in tool_protocol.RETRIED:
                raise
            note = RETRY.format(reason=exc.reason, mistake=exc.mistake)
            route, completion, _ = self._call([*history, Message("system", note)], ctx, execution)
            return route, completion, self._request(completion.text, allowed, tools)

    @staticmethod
    def _request(text, allowed, tools):
        call = tool_protocol.parse(text, allowed)
        if call is None and tool_protocol.bare_arguments(text, list(tools)):
            raise tool_protocol.InvalidToolOutput(tool_protocol.BARE)
        if call is not None:
            tool_protocol.misnamed_inputs(call, list(tools))
        return call

    def _call(self, messages: list[Message], ctx: TurnContext, execution=None, role="answer"):
        from pi.turns import foreground_inference

        with foreground_inference.scope(self.store, execution):
            return self._call_controlled(messages, ctx, execution, role)

    def _call_controlled(self, messages, ctx, execution, role):
        from pi.turns import inference_control

        inference_control.check()
        if role == "answer":
            turn_steering.guard(self.store, execution)
            with live_stream.answering():
                result = self._call_once(messages, ctx, execution, role)
        else:
            result = self._call_once(messages, ctx, execution, role)
        if role == "answer":
            try:
                turn_steering.guard(self.store, execution)
            except turn_steering.Pending:
                turn_steering.discarded(self.store, execution["turnExecutionId"], result[1])
                raise
        inference_control.check()
        return result

    def _call_once(self, messages: list[Message], ctx: TurnContext, execution=None, role="answer"):
        """Route, then call, falling through models that refuse to serve us.

        The loop never picks a model itself - it walks the order the router
        gave. Falling through is not a silent retry: what was skipped and why
        is returned and recorded on the turn.
        """
        from pi.turns import inference_control

        def guard():
            inference_control.check()
            turn_control.guard(self.store, execution)

        configuration = execution.get("modelConfiguration") if execution else None
        override = (
            (
                execution.get("answerModelId")
                or (execution.get("configuration") or {}).get("modelId")
            )
            if execution
            else None
        )
        if configuration is not None:
            adapters = self.router.adapters()
            from pi.agents import team_execution

            adapters = team_execution.metered_providers(self.store, execution, adapters)
            from pi.context import research_usage
            from pi.turns import routing_memo

            result = model_roles.dispatch(
                configuration,
                role,
                messages,
                adapters,
                harness_disabled=execution["privacy"]["harnessDisabled"],
                override=override if role == "answer" else None,
                guard=guard,
                observe_call=research_usage.observer(self.store, execution, role),
                routing_memo=(
                    routing_memo.current(self.store.path, execution.get("turnExecutionId"))
                    if role == "answer"
                    else None
                ),
                configuration_revision=execution["modelConfigurationRevision"],
                hints=ctx.owner_hints() if role == "answer" else None,
            )
            completion = result["completion"]
            evidence = {
                "role": role,
                "modelConfigurationRevision": execution["modelConfigurationRevision"],
                "modelId": result["modelId"],
                "attempts": result["attempts"],
            }
            return (
                Route(
                    Tier.CONFIGURED,
                    Reason.CONFIGURED,
                    result["providerId"],
                    completion.model,
                    selection=evidence,
                ),
                completion,
                [
                    f"{attempt['role']}: {attempt['modelId']} unavailable"
                    for attempt in result["attempts"]
                    if attempt["status"] == "unavailable"
                ],
            )
        if override:
            raise ProviderUnavailable(
                "Explicit agent model mapping is not configured; no fallback was attempted."
            )
        # The current router is deterministic policy, not a helper model call.
        # No-harness excludes auxiliary models, not hosted answer providers.
        routes = self.router.candidates(ctx)
        skipped: list[str] = []
        unavailable_providers = set()
        for route in routes:
            if route.provider in unavailable_providers:
                continue
            if route.unavailable_reason:
                skipped.append(route.unavailable_reason)
            provider = self.router.provider_for(route)
            from pi.context import research_usage

            provider = research_usage.wrap(self.store, execution, provider, role)
            turn_control.guard(self.store, execution)
            try:
                from pi.models.providers import require_images

                require_images(provider, messages)
                completion = provider.complete(messages, model=route.model)
            except ModelUnusable as exc:
                turn_control.guard(self.store, execution)
                skipped.append(exc.reason)
                continue
            except ProviderUnavailable as exc:
                turn_control.guard(self.store, execution)
                # A provider outage or bad key affects all its catalogue models.
                # Keep the local candidate without repeating the same hosted failure.
                skipped.append(exc.reason)
                unavailable_providers.add(route.provider)
                continue
            turn_control.guard(self.store, execution)
            return route, completion, skipped
        raise ProviderUnavailable(
            "; ".join(skipped) if skipped else "no candidate model could be reached"
        )
