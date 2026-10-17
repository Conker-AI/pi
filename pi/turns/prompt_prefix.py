"""The system prefix of every turn: base prompt, agent, character, project, session, skills, tools."""

from pi.actions import tools as tool_protocol
from pi.agents import character_context, skills
from pi.agents import profiles as agents
from pi.content import attachments
from pi.context import project_context
from pi.models.providers import Message


def system_messages(store, system_prompt, execution, policy, tools) -> list[Message]:
    """Build the system prefix messages for a turn."""
    messages: list[Message] = []
    if system_prompt:
        messages.append(Message("system", system_prompt))
    configuration = execution.get("configuration")
    if configuration is not None:
        messages.append(Message("system", configuration["instructions"]))
    messages.extend(character_context.messages(execution))
    project = execution.get("project")
    if project and project["instructions"].strip():
        messages.append(Message("system", project["instructions"]))
    if policy and policy["sessionInstructions"].strip():
        messages.append(Message("system", policy["sessionInstructions"]))
    messages.extend(skills.messages(execution))

    # Local imports to avoid import cycles
    try:
        messages.extend(project_context.messages(store, execution))
    except (agents.AgentError, attachments.AttachmentError) as exc:
        from pi.context import controls as context_controls

        raise context_controls.ContextError(
            exc.detail["code"], exc.detail["message"], exc.status
        ) from exc

    # Tool definitions are stable across ordinary turns. Keep them ahead of
    # changing recall so providers can reuse the static prompt prefix.
    if tools:
        messages.append(Message("system", tool_protocol.describe(tools)))

    return messages
