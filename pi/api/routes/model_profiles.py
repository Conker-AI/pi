"""Model profiles API route."""

from fastapi import APIRouter, Depends

from pi.models import profiles


def router(store, authorize):
    routes = APIRouter(dependencies=[Depends(authorize)])

    @routes.get("/models/profiles")
    def get():
        return profiles.profiles(store())

    return routes
