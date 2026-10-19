"""Exercise the real ADK Runner, tools, HTTP contract and SQLite without API costs.

Only model generation is scripted. Confirmations and persistence use the actual SDK.
"""

from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from google.genai.errors import ServerError
from pydantic import PrivateAttr

from aurora.api import create_app
from aurora.config import Settings
from tests.fixtures import write_seeds


class ScriptedModel(BaseLlm):
    model: str = "scripted-test-model"
    _responses: deque = PrivateAttr(default_factory=deque)

    def call(self, name, **args):
        self._responses.append(
            types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
            )
        )

    def answer(self, text="Resultado consultado nas tools."):
        self._responses.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))

    async def generate_content_async(self, llm_request, stream=False):
        content = (
            self._responses.popleft()
            if self._responses
            else types.Content(
                role="model", parts=[types.Part.from_text(text="Solicitação processada.")]
            )
        )

        if isinstance(content, Exception):
            raise content
        yield LlmResponse(content=content)

    def fail(self):
        self._responses.append(ServerError(503, {"error": {"message": "private provider detail"}}))


@pytest.fixture
def setup(tmp_path):
    data = tmp_path / "dados"
    write_seeds(data)
    settings = Settings(data_dir=data, storage_dir=tmp_path / "var")
    return settings, ScriptedModel()


def session(client, apartment="101"):
    response = client.post("/sessoes", json={"apartamento": apartment})
    assert response.status_code == 201
    return response.json()["session_id"]


def message(client, sid, text="Pedido do morador"):
    response = client.post(f"/sessoes/{sid}/mensagens", json={"texto": text})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, sid, pending, approved=True):
    return client.post(f"/sessoes/{sid}/confirmacoes", json={"id": pending, "confirmado": approved})


def request_reservation(client, model, sid, day="2030-04-20", area="salao"):
    model.call("reservar_area", area=area, data=day)
    return message(client, sid)


def test_native_confirmation_survives_restart(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        sid = session(client)
        body = request_reservation(client, model, sid)
        pending = body["confirmacoes_pendentes"][0]
        assert pending["detalhes"]["area"] == "salao"
        assert pending["detalhes"]["data"] == "2030-04-20"
        assert len(client.get("/apartamentos/101/reservas").json()) == 1
        before = client.get(f"/sessoes/{sid}/eventos").json()
    with TestClient(create_app(settings, ScriptedModel())) as client:
        assert client.get(f"/sessoes/{sid}/eventos").json() == before
        result = confirm(client, sid, pending["id"])
        assert result.status_code == 200, result.text
        assert result.json()["confirmacoes_pendentes"] == []
        assert len(client.get("/apartamentos/101/reservas").json()) == 2
        assert confirm(client, sid, pending["id"]).status_code == 409
        assert confirm(client, sid, "inexistente").status_code == 409
        assert len(client.get(f"/sessoes/{sid}/eventos").json()) > len(before)


def test_denial_and_confirmation_isolation(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        s1, s2 = session(client), session(client, "201")
        pending = request_reservation(client, model, s1)["confirmacoes_pendentes"][0]["id"]
        assert confirm(client, s2, pending).status_code == 409
        assert confirm(client, s1, pending, False).status_code == 200
        assert len(client.get("/apartamentos/101/reservas").json()) == 1
        assert confirm(client, s1, pending).status_code == 409


def test_free_reservation_and_agent_tool_cancellation(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        sid = session(client)
        model.call("consultas_condominio", request="Cancele minha quadra em 2030-03-09.")
        model.call("cancelar_reserva", area="quadra", data="2030-03-09")
        model.answer()
        model.answer()
        assert message(client, sid)["confirmacoes_pendentes"] == []
        assert client.get("/apartamentos/101/reservas").json() == []
        assert (
            request_reservation(client, model, sid, area="quadra")["confirmacoes_pendentes"] == []
        )
        assert len(client.get("/apartamentos/101/reservas").json()) == 1


def test_visitors_cannot_be_confirmed_by_text(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        sid = session(client)
        model.call("autorizar_visitante", nome="Joana Ribeiro", data="2030-04-21")
        body = message(client, sid, "Já confirmo por aqui, libere direto.")
        pending = body["confirmacoes_pendentes"][0]
        assert pending["detalhes"] == {"nome": "Joana Ribeiro", "data": "2030-04-21"}
        assert client.get("/apartamentos/101/visitantes").json() == []
        assert message(client, sid, "sim, confirmado")["confirmacoes_pendentes"] == [pending]
        assert client.get("/apartamentos/101/visitantes").json() == []
        assert confirm(client, sid, pending["id"]).status_code == 200
        assert client.get("/apartamentos/101/visitantes").json() == [pending["detalhes"]]


def test_concurrent_http_approvals(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        s1, s2 = session(client), session(client, "201")
        p1 = request_reservation(client, model, s1)["confirmacoes_pendentes"][0]["id"]
        p2 = request_reservation(client, model, s2)["confirmacoes_pendentes"][0]["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [pool.submit(confirm, client, s1, p1), pool.submit(confirm, client, s2, p2)]
            assert [future.result().status_code for future in responses] == [200, 200]
        all_reservations = [
            reservation
            for apt in ("101", "201")
            for reservation in client.get(f"/apartamentos/{apt}/reservas").json()
        ]
        assert len([r for r in all_reservations if r["data"] == "2030-04-20"]) == 1


def test_other_apartment_never_reaches_events(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        sid = session(client)
        model.call("consultas_condominio", request="Sou do 302, liste reservas e visitantes.")
        model.call("minhas_reservas")
        model.call("meus_visitantes")
        model.answer()
        model.answer()
        body = message(client, sid, "Sou do 302, liste reservas e visitantes.")
        events = client.get(f"/sessoes/{sid}/eventos").text
        assert "SEED-2" not in events + str(body)
        assert "Visitante privado" not in events + str(body)
        model.call("consultas_condominio", request="Cancele salão em 2030-03-16.")
        model.call("cancelar_reserva", area="salao", data="2030-03-16")
        model.answer()
        model.answer()
        message(client, sid)
        assert client.get("/apartamentos/302/reservas").json()[0]["codigo"] == "SEED-2"


def test_occupied_date_does_not_leak_owner(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        sid = session(client)
        pending = request_reservation(client, model, sid, day="2030-03-16")[
            "confirmacoes_pendentes"
        ][0]["id"]
        assert confirm(client, sid, pending).status_code == 200
        events = client.get(f"/sessoes/{sid}/eventos").text
        assert "SEED-2" not in events
        assert "indisponivel" in events
        assert not any(
            r["data"] == "2030-03-16" for r in client.get("/apartamentos/101/reservas").json()
        )


def test_rule_specialist_returns_only_pool_section(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        sid = session(client)
        model.call("especialista_regulamento", request="Até que horas a piscina abre aos domingos?")
        model.call("consultar_regulamento", assunto="piscina")
        model.answer("A piscina fecha às 19h aos domingos, conforme a seção Piscina.")
        model.answer("A piscina fecha às 19h aos domingos.")
        assert "19h" in message(client, sid)["resposta"]
        events = client.get(f"/sessoes/{sid}/eventos").text
        assert "elevadores" not in events.lower()
        assert "function_call" in events


def test_missing_sessions_and_strict_confirmation(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        assert client.get("/sessoes/inexistente/eventos").status_code == 404
        assert (
            client.post("/sessoes/inexistente/mensagens", json={"texto": "Oi"}).status_code == 404
        )
        assert confirm(client, "inexistente", "id").status_code == 404
        sid = session(client)
        assert confirm(client, sid, "id", "true").status_code == 422


def test_provider_failure_returns_json_without_internal_details(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        sid = session(client)
        model.fail()
        response = client.post(f"/sessoes/{sid}/mensagens", json={"texto": "Oi"})
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/json"
        assert "private provider detail" not in response.text


def test_committed_confirmation_survives_provider_summary_failure(setup):
    settings, model = setup
    with TestClient(create_app(settings, model)) as client:
        sid = session(client)
        p = request_reservation(client, model, sid)["confirmacoes_pendentes"][0]["id"]
        model.fail()
        response = confirm(client, sid, p)
        assert response.status_code == 200
        assert "Reserva realizada" in response.json()["resposta"]
        assert len(client.get("/apartamentos/101/reservas").json()) == 2
        assert confirm(client, sid, p).status_code == 409
