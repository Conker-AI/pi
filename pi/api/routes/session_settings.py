from fastapi import APIRouter, Depends

from pi.conversation import settings
from pi.conversation.chat_replies import ChatSettingsDocument
from pi.core.models import described


def router(store_provider, authorize):
    result = APIRouter(prefix="/sessions", dependencies=[Depends(authorize)])
    reply = described(ChatSettingsDocument)

    @result.get("/{identity}/settings", responses=reply)
    def load(identity: str):
        return settings.load(store_provider(), identity)

    @result.post("/{identity}/settings", responses=reply)
    def save(identity: str, body: settings.Update):
        return settings.save(store_provider(), identity, body)

    @result.get("/{identity}/settings/turns/{turn_id}")
    def execution(identity: str, turn_id: str):
        return settings.execution(store_provider(), identity, turn_id=turn_id)

    return result
