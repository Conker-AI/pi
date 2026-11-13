from fastapi import APIRouter, Depends, HTTPException

from pi.models import roles as model_roles


def router(store, authorize):
    routes = APIRouter(prefix="/models/configuration", dependencies=[Depends(authorize)])
    # Described, not re-validated: the reply stays exactly what the handler returns.
    reply = {200: {"model": model_roles.ConfigurationDocument}}

    @routes.get("", responses=reply)
    def get():
        return model_roles.load(store())

    @routes.post("", responses=reply)
    def save(body: model_roles.Update):
        try:
            return model_roles.save(store(), body)
        except ValueError as exc:
            raise HTTPException(409, "Model settings changed; reload before saving.") from exc

    return routes
