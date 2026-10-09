import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from . import drafts


def router(store, authorize):
    async def bounded_body(request: Request):
        if request.method not in {"POST", "PUT"}:
            return
        raw = await request.body()
        if len(raw) > 65536:
            raise HTTPException(413, "Draft exceeds the request size limit.")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate field")
                result[key] = value
            return result

        def constant(_):
            raise ValueError("invalid number")

        try:
            json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(422, "Send unambiguous draft JSON.") from None

    routes = APIRouter(prefix="/drafts", dependencies=[Depends(authorize), Depends(bounded_body)])

    def call(function, *args):
        try:
            return function(store(), *args)
        except drafts.DraftError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc

    def scope(request, session_id=None, task_id=None, *, legacy=False):
        params = request.query_params
        if legacy and request.method in {"GET", "PUT"}:
            if set(params) - {"task_id"} or len(params.getlist("task_id")) > 1:
                raise HTTPException(422, "Invalid draft parameters.")
            task_id = params.get("task_id")
        elif params:
            raise HTTPException(422, "Draft operations accept no query parameters.")
        for identity in (session_id, task_id):
            if identity is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
                raise HTTPException(422, "Invalid draft identity.")
        return task_id

    @routes.get("/new")
    def load_new(request: Request):
        scope(request)
        return call(drafts.load_new)

    @routes.post("/new")
    def save_new(request: Request, body: drafts.NewSave):
        scope(request)
        return call(drafts.save_new, body)

    @routes.post("/new/start")
    def start(request: Request, body: drafts.Start):
        scope(request)
        return call(drafts.start, body)

    @routes.get("/new/start/{request_id}")
    def get_start(request: Request, request_id: str):
        scope(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id):
            raise HTTPException(422, "Invalid start identity.")
        return call(drafts.get_start, request_id)

    @routes.get("/{session_id}/tasks/{task_id}")
    def load_task(request: Request, session_id: str, task_id: str):
        scope(request, session_id, task_id)
        return call(drafts.load, session_id, task_id)

    @routes.post("/{session_id}/tasks/{task_id}")
    def save_task(request: Request, session_id: str, task_id: str, body: drafts.Save):
        scope(request, session_id, task_id)
        return call(drafts.save, session_id, body, task_id)

    @routes.get("/{session_id}")
    def load(request: Request, session_id: str):
        return call(drafts.load, session_id, scope(request, session_id, legacy=True))

    @routes.post("/{session_id}")
    @routes.put("/{session_id}")
    def save(
        request: Request,
        session_id: str,
        body: drafts.Save,
    ):
        return call(drafts.save, session_id, body, scope(request, session_id, legacy=True))

    return routes
