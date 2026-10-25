"""How the loop calls a model: the tools it may offer, stop control, and one provider call."""

from __future__ import annotations

from pi.actions.toolgate import ToolGateUnavailable
from pi.conversation import live_stream
from pi.models import roles as model_roles
from pi.models.openrouter import ModelUnusable
from pi.models.providers import Message, ProviderUnavailable
from pi.models.routing import Reason, Route, Tier, TurnContext
from pi.turns import control as turn_control
from pi.turns import steering as turn_steering


class ModelCalls:
    """Part of Loop: model calls. Uses the attributes Loop.__init__ sets."""

    def _offers_tools(self) -> bool:
        """ToolGate is set up and the owner did not leave tools out at setup."""
        if self.toolgate is None:
            return False
        from pi.setup import choices as setup_choices

        return setup_choices.current(self.store, "capabilities").choice != "skip"

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
            tools = self.toolgate.tools()
            if execution and execution["kind"] != "companion":
                selected = execution["configuration"]["toolIds"]
                tools = [tool for tool in tools if tool.id in selected]
            return tools
        except ToolGateUnavailable:
            # Tools unavailable is not the turn failing. Conversation still
            # works, and the model is simply offered nothing.
            return []

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
