# Assistente do Residencial Aurora

API em Python com Google ADK para reservar áreas comuns, cancelar reservas próprias,
autorizar visitantes e consultar o regulamento. A conversa usa Gemini; a autorização
e a integridade dos dados são verificadas em código e no SQLite.

## Arquitetura

O FastAPI recebe as requisições e executa o `Runner` com um `App` do ADK. A composição
está em [aurora/agents.py](aurora/agents.py), e o ciclo de execução em
[aurora/runtime.py](aurora/runtime.py).

| Agente | Responsabilidade | Acionamento |
| --- | --- | --- |
| `assistente_aurora` | Conversa com o morador, delega consultas e solicita criação de reservas e autorizações de visitantes. | Agente principal do `App`, executado pelo `Runner`. |
| `consultas_condominio` | Consulta reservas e visitantes do apartamento autenticado, áreas e disponibilidade; cancela reservas próprias. | `AgentTool`, escolhido pelo principal conforme o pedido. |
| `especialista_regulamento` | Consulta a seção pertinente do regulamento e responde dúvidas sobre regras e horários. | `AgentTool`, escolhido pelo principal para perguntas sobre o regulamento. |

Os dois especialistas recebem tarefas específicas. Essa divisão mantém a consulta
documental separada das operações do condomínio. Os agentes não recebem arquivos
JSON ou o regulamento inteiro nas instruções: os dados chegam por tools.

As tools que exigem confirmação pertencem ao principal. Essa escolha é deliberada:
`AgentTool` retorna ao chamador, sem transferir a sessão principal para o especialista.
Assim, o `FunctionResponse` de confirmação volta ao mesmo agente que solicitou a
ação. O `App` não ativa `ResumabilityConfig`; a retomada usa o mecanismo nativo de
confirmação de tools e os eventos persistidos, sem sobrescrever métodos internos do
`Runner`.

Há dois arquivos SQLite em `var/`: `condominio.sqlite3` guarda reservas, visitantes,
vínculos de sessão e confirmações; `sessoes.sqlite3` é administrado pelo
`DatabaseSessionService` e guarda estado e eventos completos do ADK. Os bancos são
locais e dispensam containers. `dados/` é somente a fonte inicial e não recebe escritas.

## Garantias

| Garantia | Arquivo e trecho | Proteção independente do modelo |
| --- | --- | --- |
| **1. Cobrança e acesso exigem confirmação** | [tools.py](aurora/tools.py): `execute_or_request`; [runtime.py](aurora/runtime.py): `run` e `confirm`; [storage.py](aurora/storage.py): `decide` e `execute`. | A tool consulta a taxa armazenada e chama `request_confirmation` antes de gravar. A API expõe o ID de `adk_request_confirmation` e seus detalhes. Somente a rota de confirmação registra uma decisão e envia o `FunctionResponse` ao Runner. A transação exige decisão correspondente à sessão, chamada, ação e detalhes, além da confirmação nativa do ADK. A mesma transação grava o resultado para impedir efeito duplicado. |
| **2. Sessão pertence a um apartamento** | [api.py](aurora/api.py): `create_session`; [tools.py](aurora/tools.py): `identity`; [storage.py](aurora/storage.py): `apartamento_imutavel`, `reservations`, `visitors` e `cancel`. | A identidade é criada no servidor e lida do estado da sessão, inclusive nas chamadas aos especialistas. Nenhuma tool recebe apartamento como argumento. O vínculo é conferido com o banco, e um trigger impede sua alteração. Leituras e cancelamentos filtram a unidade na consulta SQL. Disponibilidade usa `SELECT 1`, sem retornar códigos, nomes ou apartamento de terceiros. |
| **3. Persistência após reinício** | [runtime.py](aurora/runtime.py): `Runtime.__init__`; [storage.py](aurora/storage.py): `initialize`, `execute` e `restore`; [api.py](aurora/api.py): `events`. | Eventos e estado usam `DatabaseSessionService` em arquivo; dados e confirmações usam transações SQLite. Subir a API novamente não reinicializa os registros. Cancelamentos preservam os códigos, e um contador persistente gera novos códigos. Mesmo a restauração conserva o registro de códigos já emitidos. |
| **4. Regulamento consultado por assunto** | [regulation.py](aurora/regulation.py): `Regulation.consult`; [agents.py](aurora/agents.py): `especialista_regulamento`. | O arquivo é indexado localmente por títulos. A tool retorna uma única seção correspondente ao assunto; consultas sem correspondência ou ambíguas não retornam o documento. O principal não possui o texto nas instruções. A execução isolada do especialista devolve a resposta pertinente por `AgentTool`. |
| **5. Uma reserva por área e data** | [storage.py](aurora/storage.py): índice `reserva_ativa_por_data` e método `execute`. | O índice `UNIQUE(area, data) WHERE ativa=1` é aplicado no próprio `INSERT`. A confirmação não segura a data. Na disputa, uma transação grava e a outra recebe `indisponivel`, sem dados da reserva vencedora e sem erro de servidor. `BEGIN IMMEDIATE` e o timeout do SQLite organizam os escritores concorrentes. |

Uma confirmação negada registra o resultado da negativa, sem criar reserva ou visita.
IDs inexistentes, de outra sessão ou já respondidos recebem **409**. Reservas de taxa
zero e cancelamentos não solicitam confirmação. Mensagens enviadas enquanto houver
pendências devolvem as pendências existentes e orientam o uso da rota de confirmação.

As rotas de verificação leem diretamente o banco. Elas permitem acesso aos dados de
qualquer apartamento por exigência do contrato de avaliação; não são tools dos agentes.
Em um ambiente de produção, precisariam de acesso administrativo.

A documentação do ADK registra uma limitação de suporte à confirmação com
`DatabaseSessionService`. Por isso, a retomada em SQLite tem um teste específico que
fecha a aplicação entre o pedido e a aprovação; `Runtime.confirm` também verifica
se a tool realmente produziu um resultado, evitando aceitar silenciosamente uma
retomada sem efeito. Referências: [confirmações do ADK](https://adk.dev/tools-custom/confirmation/)
e [índices únicos parciais do SQLite](https://www.sqlite.org/partialindex.html).

## Como rodar

Pré-requisitos: Python **3.12 ou superior**, [uv](https://docs.astral.sh/uv/getting-started/installation/)
e uma chave do [Google AI Studio](https://aistudio.google.com/apikey).
O ADK está fixado em **2.2.0** no `pyproject.toml`.

Na raiz do repositório:

```bash
cp .env.example .env
uv sync
```

Preencha o `.env` local. Ele está ignorado pelo Git.

| Variável | Uso |
| --- | --- |
| `GOOGLE_API_KEY` | Obrigatória: chave do Google AI Studio. |
| `GEMINI_MODEL` | Opcional: modelo Gemini usado pelos três agentes. Se vazia, usa `gemini-3.5-flash-lite`. |
| `AURORA_STORAGE_DIR` | Opcional: diretório dos bancos. Se vazia, usa `var/` na raiz do projeto. |

O modelo padrão consta na [documentação oficial de modelos Gemini](https://ai.google.dev/gemini-api/docs/models).
Confira sua disponibilidade e os [limites ativos do projeto](https://aistudio.google.com/usage?tab=rate-limit)
no AI Studio. Os limites dependem da conta; o fluxo envolve dezenas de chamadas.
A chave do avaliador é configurada localmente, sem alterar o código.
O cliente realiza até quatro tentativas para falhas temporárias do provedor. Se elas
se esgotarem, a API retorna um erro JSON com status 503. Quando a confirmação já
foi executada e só a redação da resposta falhar, a API retorna o resultado gravado
com status 200, sem executar novamente a ação.

Para restaurar os dados iniciais, **com a API parada**:

```bash
uv run aurora restaurar
```

O comando restaura reservas e visitantes a partir de `dados/`, apaga sessões e
confirmações, e mantém apenas o registro interno necessário para não reutilizar códigos.
Não altera os arquivos fornecidos pelo desafio.

Para subir a API:

```bash
uv run aurora servir
```

A API responde em **http://localhost:8000**. A documentação interativa fica em
**http://localhost:8000/docs**. Para reiniciar preservando conversas e dados, use
`Ctrl+C` e execute novamente apenas `uv run aurora servir`.

| Método e rota | Corpo / retorno |
| --- | --- |
| `POST /sessoes` | `{"apartamento":"101"}` → **201**, `{"session_id":"..."}` |
| `POST /sessoes/{session_id}/mensagens` | `{"texto":"Reserve a quadra para 2030-04-06."}` → **200**, resposta e pendências |
| `POST /sessoes/{session_id}/confirmacoes` | `{"id":"...","confirmado":true}` → **200** ou **409** |
| `GET /sessoes/{session_id}/eventos` | **200**, lista ordenada com o conteúdo completo dos eventos persistidos |
| `GET /apartamentos/101/reservas` | **200**, lista de objetos com `codigo`, `area` e `data` |
| `GET /apartamentos/302/visitantes` | **200**, lista de objetos com `nome` e `data` |

As rotas que contêm `session_id` retornam **404** para uma sessão inexistente.
As respostas das rotas de mensagens e confirmações têm o formato:

```json
{
  "resposta": "",
  "confirmacoes_pendentes": [
    {
      "id": "ID retornado pelo ADK",
      "acao": "reservar_area",
      "detalhes": {"area": "salao-de-festas", "data": "2030-04-20", "taxa": 150}
    }
  ]
}
```

O ID, a área e a taxa acima são ilustrativos. Use os valores devolvidos pela API.
`confirmacoes_pendentes` sempre contém todas as pendências da sessão.

Para executar as verificações automatizadas:

```bash
uv run pytest -q
```

Os testes usam dados sintéticos em diretórios temporários. Os testes de API executam
o Runner, as tools e a persistência reais do ADK, substituindo somente as respostas
do modelo por chamadas controladas. Incluem aprovação após reinício, negativa,
reenvio, identidade, cancelamento, visitante e duas aprovações concorrentes por HTTP.
Essa suíte não substitui a validação da conversa com a chave e o modelo Gemini escolhidos.
