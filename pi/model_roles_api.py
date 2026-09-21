from fastapi import APIRouter, Depends, HTTPException

from . import model_roles


def router(store, authorize):
    routes = APIRouter(prefix="/models/configuration", dependencies=[Depends(authorize)])

    @routes.get("")
    def get():
        return model_roles.load(store())

    @routes.post("")
    def save(body: model_roles.Update):
        try:
            return model_roles.save(store(), body)
        except ValueError as exc:
            raise HTTPException(409, "Model settings changed; reload before saving.") from exc

    return routes
