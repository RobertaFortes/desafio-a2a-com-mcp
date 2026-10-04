"""Agente Central de Salas: servidor A2A por fora, host MCP por dentro.

    MCP_URL=http://127.0.0.1:7301/mcp python agente/agente.py

Sem LLM: o pedido chega em formato fixo e o agente decide por regra. O agente
nao conhece regra de sala (conflito, politica, alternativas): ele traduz
protocolo. A ponte entre os dois lados esta em `_concluir_ou_pausar` e em
`_retomar`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from a2a.helpers import new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import DefaultServerCallContextBuilder, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Task,
    TaskState,
    TaskStatus,
)
from starlette.applications import Starlette

from cliente_mcp import ClienteMCP, ErroMCP, Trace

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://{HOST}:{PORTA}")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

TOOL_RESERVA = "reservar_sala"
RESOURCE_POLITICA = "politica://uso"

PEDIDO = re.compile(r"^\s*reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+?)\s*$")
ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")
RECUSAR = "recusar"

# O a2a-sdk instrumenta com OpenTelemetry e, ao fechar a resposta bloqueante,
# loga "Failed to detach context" sem efeito no protocolo. Silenciado aqui.
logging.getLogger("opentelemetry.context").setLevel(logging.CRITICAL)


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


@dataclass
class Pausa:
    """O que o agente guarda de uma Task em TASK_STATE_INPUT_REQUIRED.

    `request_state` e opaco: guardado e ecoado byte a byte, nunca aberto. Vive
    so aqui, em memoria do agente, indexado pelo id da Task, e nao sai em
    nenhuma resposta A2A.
    """

    tool: str
    argumentos: dict[str, Any]
    chave: str
    campo: str
    opcoes: list[str]
    request_state: str
    politica: str
    trace: Trace


PAUSAS: dict[str, Pausa] = {}


def _texto_de(resultado: dict[str, Any]) -> str:
    return " ".join(c.get("text", "") for c in resultado.get("content", []) if c.get("type", "text") == "text")


def _alternativas(pausa: Pausa) -> str:
    return "alternativas: " + ", ".join(pausa.opcoes)


class AgenteCentralDeSalas(AgentExecutor):
    def __init__(self, mcp: ClienteMCP) -> None:
        self.mcp = mcp

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        trace = Trace.do_header(context.call_context.state.get("headers", {}).get("traceparent"))
        texto = context.get_user_input().strip()
        tarefa = context.current_task

        if tarefa is None:
            tarefa = Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message],
            )
            await event_queue.enqueue_event(tarefa)
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await self._novo_pedido(texto, updater, trace or Trace.novo())
            return

        # A continuacao pode chegar sem contextId: o da Task e o que vale.
        updater = TaskUpdater(event_queue, tarefa.id, tarefa.context_id)
        pausa = PAUSAS.get(tarefa.id)
        if tarefa.status.state != TaskState.TASK_STATE_INPUT_REQUIRED or pausa is None:
            await self._terminar(updater, TaskState.TASK_STATE_FAILED, "Task nao esta aguardando escolha.")
            return
        await self._retomar(texto, pausa, updater, trace or pausa.trace)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        PAUSAS.pop(context.task_id, None)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    # ------------------------------------------------------------------
    # Primeiro SendMessage: abre a Task, descobre, le a politica, chama.
    # ------------------------------------------------------------------

    async def _novo_pedido(self, texto: str, updater: TaskUpdater, trace: Trace) -> None:
        casamento = PEDIDO.match(texto)
        if not casamento:
            await self._terminar(
                updater,
                TaskState.TASK_STATE_FAILED,
                "Pedido invalido. Use: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>",
            )
            return
        pedido = casamento.groupdict()
        await updater.start_work()

        try:
            # Descoberta em runtime: a tool e os nomes dos argumentos vem do tools/list.
            tools = {t["name"]: t for t in await self.mcp.listar_tools(trace)}
            tool = tools.get(TOOL_RESERVA)
            if tool is None:
                await self._terminar(updater, TaskState.TASK_STATE_FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}.")
                return
            propriedades = tool.get("inputSchema", {}).get("properties", {})
            argumentos = {nome: pedido[nome] for nome in propriedades if nome in pedido}
            faltando = [n for n in tool.get("inputSchema", {}).get("required", []) if n not in argumentos]
            if faltando:
                await self._terminar(updater, TaskState.TASK_STATE_FAILED, f"Pedido sem os campos exigidos pela tool: {', '.join(faltando)}")
                return

            # Resource e escolha da aplicacao: o agente le a politica e guarda a versao.
            politica = (await self.mcp.ler_resource(RESOURCE_POLITICA, trace)).splitlines()[0].split(":", 1)[1].strip()

            resultado = await self.mcp.chamar_tool(TOOL_RESERVA, argumentos, trace)
        except ErroMCP as erro:
            await self._terminar(updater, TaskState.TASK_STATE_FAILED, erro.message)
            return

        await self._concluir_ou_pausar(resultado, TOOL_RESERVA, argumentos, politica, trace, updater)

    # ------------------------------------------------------------------
    # A ponte, ida: input_required do MCP vira TASK_STATE_INPUT_REQUIRED.
    # ------------------------------------------------------------------

    async def _concluir_ou_pausar(
        self,
        resultado: dict[str, Any],
        tool: str,
        argumentos: dict[str, Any],
        politica: str,
        trace: Trace,
        updater: TaskUpdater,
    ) -> None:
        if resultado.get("resultType") == "input_required":
            pedidos = resultado.get("inputRequests") or {}
            chave, pedido = next(iter(pedidos.items()), (None, None))
            schema = ((pedido or {}).get("params") or {}).get("requestedSchema") or {}
            propriedades = schema.get("properties") or {}
            if (
                len(pedidos) != 1
                or pedido.get("method") != "elicitation/create"
                or len(propriedades) != 1
                or not resultado.get("requestState")
            ):
                await self._terminar(updater, TaskState.TASK_STATE_FAILED, "O servidor MCP pediu uma entrada que o agente nao sabe repassar.")
                return
            campo, definicao = next(iter(propriedades.items()))
            opcoes = list(definicao.get("enum") or ([definicao["const"]] if "const" in definicao else []))
            pausa = Pausa(
                tool=tool,
                argumentos=argumentos,
                chave=chave,
                campo=campo,
                opcoes=opcoes,
                request_state=resultado["requestState"],
                politica=politica,
                trace=trace,
            )
            PAUSAS[updater.task_id] = pausa
            log(f"task {updater.task_id}: input_required do MCP -> TASK_STATE_INPUT_REQUIRED ({', '.join(opcoes)})")
            await updater.requires_input(self._msg(updater, _alternativas(pausa)))
            return

        PAUSAS.pop(updater.task_id, None)
        if resultado.get("isError"):
            # A mensagem da tool chega intacta ao historico da Task.
            await self._terminar(updater, TaskState.TASK_STATE_FAILED, _texto_de(resultado))
            return

        dados = resultado.get("structuredContent") or {}
        if dados.get("reservado") is False:
            await self._terminar(
                updater, TaskState.TASK_STATE_CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusada'}."
            )
            return

        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": politica,
        }
        await updater.add_artifact([new_text_part(json.dumps(reserva, ensure_ascii=False))], name="reserva")
        await self._terminar(
            updater, TaskState.TASK_STATE_COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}."
        )

    # ------------------------------------------------------------------
    # A ponte, volta: a escolha do cliente A2A vira inputResponses e o
    # tools/call original e repetido, com id novo e o requestState intacto.
    # ------------------------------------------------------------------

    async def _retomar(self, texto: str, pausa: Pausa, updater: TaskUpdater, trace: Trace) -> None:
        casamento = ESCOLHA.match(texto)
        valor = casamento["valor"] if casamento else None
        if valor == RECUSAR:
            resposta: dict[str, Any] = {"action": "decline"}
        elif valor in pausa.opcoes:
            resposta = {"action": "accept", "content": {pausa.campo: valor}}
        else:
            # Fora do enum: a Task continua pausada e a lista e repetida.
            await updater.requires_input(self._msg(updater, _alternativas(pausa)))
            return

        await updater.start_work()
        try:
            resultado = await self.mcp.chamar_tool(
                pausa.tool,
                pausa.argumentos,
                trace,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
        except ErroMCP as erro:
            PAUSAS.pop(updater.task_id, None)
            await self._terminar(updater, TaskState.TASK_STATE_FAILED, erro.message)
            return
        await self._concluir_ou_pausar(resultado, pausa.tool, pausa.argumentos, pausa.politica, trace, updater)

    @staticmethod
    def _msg(updater: TaskUpdater, texto: str):
        return updater.new_agent_message([new_text_part(texto)])

    @staticmethod
    async def _terminar(updater: TaskUpdater, estado: TaskState, texto: str) -> None:
        """Encerra a Task deixando a mensagem final tambem no `history`.

        O a2a-sdk so move `status.message` para o historico na transicao
        seguinte, e estado terminal nao tem transicao seguinte. A mensagem e
        publicada uma vez em WORKING e repetida (mesmo messageId) no terminal.
        """
        mensagem = updater.new_agent_message([new_text_part(texto)])
        await updater.update_status(TaskState.TASK_STATE_WORKING, message=mensagem)
        await updater.update_status(estado, message=mensagem)
        log(f"task {updater.task_id}: {TaskState.Name(estado)} | {texto}")


def agent_card() -> AgentCard:
    return AgentCard(
        name="Central de Salas",
        description="Reserva salas de reuniao da Hill Valley Tech.",
        provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(url=f"{URL_PUBLICA}/a2a", protocol_binding="JSONRPC", protocol_version="1.0"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="reservar-sala",
                name="Reservar sala",
                description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                tags=["salas", "agenda"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
                examples=[
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            )
        ],
    )


class ContextoA2A(DefaultServerCallContextBuilder):
    """Sem header `A2A-Version`, assume 1.0, a unica versao que o card declara.

    O a2a-sdk segue a letra da spec e trata header ausente como 0.3, recusando
    `SendMessage`/`GetTask` com VersionNotSupported. O validador do desafio fala
    v1.0 sem mandar o header; um cliente que mande `A2A-Version` continua
    passando pela checagem do SDK normalmente.
    """

    def build(self, request) -> ServerCallContext:
        contexto = super().build(request)
        headers = contexto.state.setdefault("headers", {})
        if not (headers.get("A2A-Version") or headers.get("a2a-version")):
            headers["a2a-version"] = "1.0"
        return contexto


def criar_app() -> Starlette:
    card = agent_card()
    mcp = ClienteMCP(MCP_URL)
    handler = DefaultRequestHandler(
        agent_executor=AgenteCentralDeSalas(mcp),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    rotas = create_agent_card_routes(card) + create_jsonrpc_routes(handler, rpc_url="/a2a", context_builder=ContextoA2A())

    @asynccontextmanager
    async def ciclo_de_vida(_app: Starlette):
        yield
        await mcp.fechar()

    return Starlette(routes=rotas, lifespan=ciclo_de_vida)


if __name__ == "__main__":
    log(f"agente A2A Central de Salas em {URL_PUBLICA}/a2a, falando MCP com {MCP_URL}")
    uvicorn.run(criar_app(), host=HOST, port=PORTA, log_level="warning")
