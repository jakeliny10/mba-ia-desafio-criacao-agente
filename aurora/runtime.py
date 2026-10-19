import asyncio
import logging
from collections import defaultdict

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from google.genai.errors import APIError

from aurora.agents import build_app
from aurora.config import APP_NAME, USER_ID
from aurora.regulation import Regulation
from aurora.storage import Store

CONFIRMATION = "adk_request_confirmation"
logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings, model=None):
        self.store = Store(settings.database, settings.data_dir)
        self.store.initialize()
        self.sessions = DatabaseSessionService(db_url=settings.sessions_url)
        self.runner = Runner(
            app=build_app(
                settings, self.store, Regulation(settings.data_dir / "regulamento.md"), model
            ),
            session_service=self.sessions,
        )
        self.locks = defaultdict(asyncio.Lock)

    async def close(self):
        await self.runner.close()
        await self.sessions.close()

    async def session(self, session_id):
        return await self.sessions.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
        )

    async def run(self, session_id, content):
        texts = []
        async for event in self.runner.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=content,
        ):
            for call in event.get_function_calls():
                if call.name != CONFIRMATION:
                    continue
                args = call.args
                original = args["originalFunctionCall"]
                payload = args["toolConfirmation"]["payload"]
                self.store.add_pending(
                    confirmation_id=call.id,
                    session_id=session_id,
                    call_id=original["id"],
                    action=payload["acao"],
                    details=payload["detalhes"],
                    author=event.author,
                    invocation=event.invocation_id,
                )
            if event.content and event.content.parts:
                texts.extend(
                    part.text for part in event.content.parts if part.text and not part.thought
                )
        return self.response(session_id, "\n".join(texts))

    def response(self, session_id, text):
        return {"resposta": text, "confirmacoes_pendentes": self.store.pending(session_id)}

    async def confirm(self, session_id, confirmation_id, approved):
        record = self.store.decide(session_id, confirmation_id, approved)
        content = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=confirmation_id,
                        name=CONFIRMATION,
                        response={"confirmed": approved},
                    )
                )
            ],
        )
        try:
            result = await self.run(session_id, content)
        except APIError:
            # A failed natural-language summary must not hide an already committed
            # confirmation or turn the losing reservation into a server error.
            outcome = self.store.operation_result(session_id, record["chamada"])
            if outcome is None:
                raise
            logger.warning(
                "Gemini indisponível após confirmação; retornando o resultado persistido."
            )
            messages = {
                "reservada": f"Reserva realizada. Código: {outcome.get('codigo', '')}.",
                "autorizado": f"Visita de {outcome.get('nome', '')} autorizada para {outcome.get('data', '')}.",
                "negada": "Confirmação negada. Nenhuma alteração realizada.",
                "indisponivel": "A área está ocupada nesta data. Nenhuma reserva foi criada.",
            }
            result = self.response(session_id, messages[outcome["status"]])
        # Detect the silent failure described in the assignment instead of reporting success.
        if self.store.operation_result(session_id, record["chamada"]) is None:
            raise RuntimeError("O ADK não retomou a tool da confirmação.")
        return result
