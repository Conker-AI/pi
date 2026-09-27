"""Owner call control and bounded transient speech transport; no device capture."""

import base64
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response

from . import calls


def router(store, loop, authorize, speech=None):
    def no_cache(response: Response):
        response.headers["Cache-Control"] = "no-store"

    routes = APIRouter(prefix="/calls", dependencies=[Depends(authorize), Depends(no_cache)])
    CallPath = Annotated[str, Path(pattern=r"^call_[a-f0-9]{32}$")]
    ConversationPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,200}$")]

    def exact(request):
        if request.url.query:
            raise HTTPException(422, "Browser call routes do not accept query parameters.")

    def execute(fn, *args, **kwargs):
        try:
            return fn(store(), *args, **kwargs)
        except calls.CallError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except ValueError as exc:
            raise HTTPException(422, "Invalid call request.") from exc

    def response(result):
        audio = result.get("audio")
        if audio is not None or result.get("transcription") is not None:
            from .providers import ProviderUnavailable

            try:
                calls.guard(
                    store(),
                    {
                        "callExecution": {
                            "id": result["call"]["id"],
                            "generation": result["audioGeneration"],
                        }
                    },
                )
            except ProviderUnavailable:
                result["audio"] = None
                result["transcription"] = None
                return result
        if audio is not None:
            result["audio"] = {
                "base64": base64.b64encode(audio["audio"]).decode("ascii"),
                "mime": audio["mime"],
                "durationSeconds": audio.get("duration_seconds"),
                "retention": "transient-response-only",
            }
        return result

    @routes.post("")
    def start(body: calls.Start):
        return execute(calls.start, body)

    @routes.get("")
    def listing(conversation_id: str = Query(min_length=1, max_length=200)):
        return execute(calls.listing, conversation_id)

    @routes.get("/capabilities")
    def capabilities():
        adapter = speech() if speech else None
        return {
            "typedTurns": True,
            "language": "en",
            "speech": adapter.capabilities()
            if adapter
            else {"stt": "unconfigured", "tts": "unconfigured"},
            "camera": "unavailable",
            "perception": "unavailable",
            "rawMediaRetention": "none",
            "devices": "client-owned; not captured by Pi",
        }

    @routes.get("/browser/capabilities", response_model=calls.BrowserCallAvailability)
    def browser_capabilities(request: Request):
        exact(request)
        return calls.browser_availability(speech() if speech else None)

    @routes.get("/browser/active/{conversation_id}", response_model=calls.BrowserCall)
    def browser_active(conversation_id: ConversationPath, request: Request):
        exact(request)
        return calls.browser_view(execute(calls.active, conversation_id))

    @routes.post("/browser", response_model=calls.BrowserCall)
    def browser_start(body: calls.Start, request: Request):
        exact(request)
        return calls.browser_view(execute(calls.start, body))

    @routes.get("/browser/{identity}", response_model=calls.BrowserCall)
    def browser_get(identity: CallPath, request: Request):
        exact(request)
        return calls.browser_view(execute(calls.get, identity))

    @routes.post("/browser/{identity}/update", response_model=calls.BrowserCall)
    def browser_update(identity: CallPath, body: calls.Update, request: Request):
        exact(request)
        return calls.browser_view(execute(calls.update, identity, body))

    @routes.post("/browser/{identity}/interrupt", response_model=calls.BrowserCall)
    def browser_interrupt(identity: CallPath, body: calls.Revision, request: Request):
        exact(request)
        return calls.browser_view(execute(calls.interrupt, identity, body))

    @routes.post("/browser/{identity}/end", response_model=calls.BrowserCall)
    def browser_end(identity: CallPath, body: calls.Revision, request: Request):
        exact(request)
        return calls.browser_view(execute(calls.interrupt, identity, body, ended=True))

    @routes.post("/browser/{identity}/turns", response_model=calls.BrowserCallTurn)
    def browser_typed(identity: CallPath, body: calls.Send, request: Request):
        exact(request)
        return calls.browser_turn(execute(calls.run, loop(), identity, body, speech=None))

    @routes.post("/browser/{identity}/audio", response_model=calls.BrowserAudioTurn)
    async def browser_audio(
        identity: CallPath,
        request: Request,
        request_id: str = Query(pattern=r"^[A-Za-z0-9_-]{16,128}$"),
    ):
        if set(request.query_params) != {"request_id"} or len(request.query_params.getlist("request_id")) != 1:
            raise HTTPException(422, "Audio turns require exactly one request_id parameter.")
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media not in {"audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave"}:
            raise HTTPException(415, "Browser audio turns require PCM WAV audio.")
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > 10 * 1024 * 1024 or int(declared) < 0:
                    raise HTTPException(413, "Audio exceeds 10 MiB.")
            except ValueError as exc:
                raise HTTPException(400, "Invalid content length.") from exc
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 10 * 1024 * 1024:
                raise HTTPException(413, "Audio exceeds 10 MiB.")
            data.extend(chunk)
        from starlette.concurrency import run_in_threadpool

        result = await run_in_threadpool(
            lambda: response(
                execute(
                    calls.run,
                    loop(),
                    identity,
                    calls.Send(request_id=request_id),
                    speech=speech() if speech else None,
                    audio=bytes(data),
                    mime=media,
                )
            )
        )
        return calls.browser_audio_turn(result)

    @routes.get("/{identity}")
    def get(identity: str):
        return execute(calls.get, identity)

    @routes.post("/{identity}/update")
    def update(identity: str, body: calls.Update):
        return execute(calls.update, identity, body)

    @routes.post("/{identity}/interrupt")
    def interrupt(identity: str, body: calls.Revision):
        return execute(calls.interrupt, identity, body)

    @routes.post("/{identity}/end")
    def end(identity: str, body: calls.Revision):
        return execute(calls.interrupt, identity, body, ended=True)

    @routes.post("/{identity}/turns")
    def typed(identity: str, body: calls.Send):
        return response(
            execute(calls.run, loop(), identity, body, speech=speech() if speech else None)
        )

    @routes.post("/{identity}/audio")
    async def audio(
        identity: str, request: Request, request_id: str = Query(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    ):
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > 10 * 1024 * 1024 or int(declared) < 0:
                    raise HTTPException(413, "Audio exceeds 10 MiB.")
            except ValueError as exc:
                raise HTTPException(400, "Invalid content length.") from exc
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 10 * 1024 * 1024:
                raise HTTPException(413, "Audio exceeds 10 MiB.")
            data.extend(chunk)
        # Blocking providers run off the ASGI event loop, so pause/end can still arrive.
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            lambda: response(
                execute(
                    calls.run,
                    loop(),
                    identity,
                    calls.Send(request_id=request_id),
                    speech=speech() if speech else None,
                    audio=bytes(data),
                    mime=request.headers.get("content-type", "audio/wav"),
                )
            )
        )

    return routes
