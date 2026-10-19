from google.adk.tools import ToolContext

from aurora.storage import valid_date


def build_tools(store, regulation):
    def identity(context):
        # AgentTool copies this server-created state to its isolated execution.
        session_id = context.state["session_id"]
        apartment = context.state["apartamento"]
        if store.apartment_for_session(session_id) != apartment:
            raise ValueError("Identidade da sessão inválida.")
        return session_id, apartment

    def listar_areas() -> dict:
        """Consulta os IDs, nomes e taxas das áreas disponíveis no condomínio."""
        return {"areas": store.areas()}

    def minhas_reservas(tool_context: ToolContext) -> dict:
        """Lista exclusivamente as reservas ativas do apartamento autenticado."""
        _, apartment = identity(tool_context)
        return {"reservas": store.reservations(apartment)}

    def meus_visitantes(tool_context: ToolContext) -> dict:
        """Lista exclusivamente os visitantes do apartamento autenticado."""
        _, apartment = identity(tool_context)
        return {"visitantes": store.visitors(apartment)}

    def consultar_disponibilidade(area: str, data: str) -> dict:
        """Informa apenas se uma área está livre na data AAAA-MM-DD, sem dados de moradores."""
        try:
            day = valid_date(data)
        except ValueError:
            return {"status": "data_invalida"}
        if not store.area(area):
            return {"status": "area_invalida", "areas": store.areas()}
        return {"area": area, "data": day, "livre": store.available(area, day)}

    def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
        """Cancela a própria reserva da área/data, sem confirmação. Nunca opera outra unidade."""
        _, apartment = identity(tool_context)
        try:
            day = valid_date(data)
        except ValueError:
            return {"status": "data_invalida"}
        return store.cancel(apartment, area, day)

    def execute_or_request(context, action, details, needs_confirmation):
        session_id, apartment = identity(context)
        confirmation = context.tool_confirmation
        if needs_confirmation and confirmation is None:
            context.request_confirmation(
                hint="Aprove ou negue pela rota de confirmações da API.",
                payload={"acao": action, "detalhes": details},
            )
            return {"status": "aguardando_confirmacao", **details}
        return store.execute(
            session_id=session_id,
            apartment=apartment,
            call_id=context.function_call_id,
            action=action,
            details=details,
            confirmed=bool(confirmation and confirmation.confirmed),
        )

    def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
        """Reserva uma área na data AAAA-MM-DD. Taxa positiva exige confirmação externa à conversa."""
        try:
            day = valid_date(data)
        except ValueError:
            return {"status": "data_invalida"}
        selected = store.area(area)
        if not selected:
            return {"status": "area_invalida", "areas": store.areas()}
        details = {"area": area, "data": day, "taxa": selected["taxa"]}
        # A pending request does not hold the date. The unique index settles the race.
        return execute_or_request(tool_context, "reservar_area", details, selected["taxa"] > 0)

    def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
        """Solicita autorização de visitante. Somente a rota de confirmações pode liberar a entrada."""
        name = nome.strip()
        if not name or len(name) > 200:
            return {"status": "nome_invalido"}
        try:
            day = valid_date(data)
        except ValueError:
            return {"status": "data_invalida"}
        return execute_or_request(
            tool_context, "autorizar_visitante", {"nome": name, "data": day}, True
        )

    def consultar_regulamento(assunto: str) -> dict:
        """Consulta uma única seção por assunto específico, por exemplo 'piscina'."""
        return regulation.consult(assunto)

    return {
        function.__name__: function
        for function in (
            listar_areas,
            minhas_reservas,
            meus_visitantes,
            consultar_disponibilidade,
            cancelar_reserva,
            reservar_area,
            autorizar_visitante,
            consultar_regulamento,
        )
    }
