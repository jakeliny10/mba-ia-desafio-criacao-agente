import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from aurora.config import APP_NAME, USER_ID, Settings
from aurora.runtime import Runtime
from aurora.storage import Conflict


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SessionInput(Input):
    apartamento: str = Field(min_length=1, max_length=30)


class MessageInput(Input):
    texto: str = Field(min_length=1, max_length=20000)


class ConfirmationInput(Input):
    id: str = Field(min_length=1, max_length=200)
    confirmado: StrictBool


def create_app(settings=None, model=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = Runtime(settings or Settings.from_env(), model)
        try:
            yield
        finally:
            await app.state.runtime.close()

    app = FastAPI(title="Residencial Aurora", lifespan=lifespan)

    @app.exception_handler(APIError)
    async def model_unavailable(request, exc):
        # Provider errors may include request metadata; do not expose them to clients.
        return JSONResponse(
            status_code=503,
            content={"detail": "Gemini indisponível. Tente novamente em alguns instantes."},
        )

    async def require_session(session_id):
        runtime = app.state.runtime
        session = await runtime.session(session_id)
        if session is None:
            raise HTTPException(404, "Sessão não encontrada.")
        return session

    @app.post("/sessoes", status_code=201)
    async def create_session(body: SessionInput):
        runtime = app.state.runtime
        if not runtime.store.apartment_exists(body.apartamento):
            raise HTTPException(404, "Apartamento não encontrado.")
        session_id = str(uuid.uuid4())
        await runtime.sessions.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
            state={"apartamento": body.apartamento, "session_id": session_id},
        )
        runtime.store.register_session(session_id, body.apartamento)
        return {"session_id": session_id}

    @app.post("/sessoes/{session_id}/mensagens")
    async def message(session_id: str, body: MessageInput):
        runtime = app.state.runtime
        await require_session(session_id)
        async with runtime.locks[session_id]:
            if runtime.store.pending(session_id):
                return runtime.response(session_id, "Responda às confirmações pendentes pela API.")
            return await runtime.run(
                session_id,
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=body.texto)],
                ),
            )

    @app.post("/sessoes/{session_id}/confirmacoes")
    async def confirm(session_id: str, body: ConfirmationInput):
        runtime = app.state.runtime
        await require_session(session_id)
        async with runtime.locks[session_id]:
            try:
                return await runtime.confirm(session_id, body.id, body.confirmado)
            except Conflict as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.get("/sessoes/{session_id}/eventos")
    async def events(session_id: str):
        session = await require_session(session_id)
        return [event.model_dump(mode="json") for event in session.events]

    @app.get("/apartamentos/{apartamento}/reservas")
    async def reservations(apartamento: str):
        return app.state.runtime.store.reservations(apartamento)

    @app.get("/apartamentos/{apartamento}/visitantes")
    async def visitors(apartamento: str):
        return app.state.runtime.store.visitors(apartamento)

    return app


app = create_app()
