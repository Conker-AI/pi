from fastapi import APIRouter, Depends
from . import session_settings as settings


def router(store_provider, authorize):
    result = APIRouter(prefix="/sessions", dependencies=[Depends(authorize)])

    @result.get("/{identity}/settings")
    def load(identity: str):
        return settings.load(store_provider(), identity)

    @result.post("/{identity}/settings")
    def save(identity: str, body: settings.Update):
        return settings.save(store_provider(), identity, body)

    return result
