"""Servidor MCP da Central de Salas.

Streamable HTTP stateless em :7301/mcp, com tres tools, um resource e o ciclo
de MRTR (input_required + requestState selado) na tool de reserva.

    REQUEST_STATE_SECRET=... python servidor-mcp/servidor.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, create_model

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORTA = int(os.environ.get("MCP_PORT", "7301"))

SAO_PAULO = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3
# O requestState vale 10 minutos: dentro da faixa de 5 a 30 exigida.
TTL_REQUEST_STATE = 600.0


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Dominio: dados do starter, carregados uma vez. Reservas vivem em memoria.
# --------------------------------------------------------------------------

SALAS: dict[str, dict[str, Any]] = {s["id"]: s for s in json.loads((DADOS / "salas.json").read_text())}
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text())
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text()
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def _proximo_id() -> str:
    maior = max((int(r["id"].split("-")[1]) for r in RESERVAS), default=0)
    return f"res-{maior + 1:04d}"


def _instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError as exc:
        raise ToolError(f"Data invalida: {valor}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SAO_PAULO)
    return dt


def validar_pedido(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as regras de sala e de politica, na ordem das mensagens do enunciado."""
    if sala not in SALAS:
        raise ToolError(f"Sala inexistente: {sala}")
    ini, fi = _instante(inicio), _instante(fim)
    if fi <= ini:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio")
    ini_sp, fim_sp = ini.astimezone(SAO_PAULO), fi.astimezone(SAO_PAULO)
    dentro = (
        ini_sp.date() == fim_sp.date()
        and JANELA_INICIO <= ini_sp.time() <= JANELA_FIM
        and JANELA_INICIO <= fim_sp.time() <= JANELA_FIM
    )
    if not dentro:
        raise ToolError("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError("Duracao acima do limite: a politica permite no maximo 2 horas")
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and _instante(r["inicio"]) < fi and ini < _instante(r["fim"])
    ]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade >= a pedida, por (capacidade, id), no maximo 3."""
    minima = SALAS[sala]["capacidade"]
    livres = [
        s for s in SALAS.values() if s["id"] != sala and s["capacidade"] >= minima and not conflitos(s["id"], ini, fi)
    ]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:MAX_ALTERNATIVAS]]


# --------------------------------------------------------------------------
# Schemas de saida
# --------------------------------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# --------------------------------------------------------------------------
# Servidor MCP
# --------------------------------------------------------------------------


def _seguranca_request_state() -> RequestStateSecurity:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        log("REQUEST_STATE_SECRET ausente ou com menos de 32 bytes.")
        log('Gere com: python3 -c "import secrets; print(secrets.token_hex(32))"')
        raise SystemExit(2)
    # AES-256-GCM do SDK: cifra e autentica o requestState, com expiracao,
    # vinculo ao metodo/tool/argumentos e audience = nome do servidor.
    return RequestStateSecurity(keys=[segredo], ttl=TTL_REQUEST_STATE)


mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=_seguranca_request_state(),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS.values()])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = validar_pedido(sala, inicio, fim)
    em_conflito = conflitos(sala, ini, fi)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in em_conflito],
    )


def _schema_de_escolha(opcoes: list[str]) -> type[BaseModel]:
    """Schema plano da elicitation: uma propriedade `sala` restrita as alternativas."""
    return create_model(
        "EscolhaDeSala",
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Any] | None:
    """Resolver do MRTR: so pergunta quando a sala pedida esta ocupada.

    Roda a cada rodada. Na primeira, devolve `Elicit` e o SDK encerra a resposta
    com `input_required` + `requestState`. No retry, o SDK confere o requestState
    (selo, expiracao, vinculo aos argumentos) e injeta a resposta do cliente.
    """
    ini, fi = validar_pedido(sala, inicio, fim)
    if not conflitos(sala, ini, fi):
        return None
    opcoes = alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError("Sem alternativas disponiveis no intervalo")
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", _schema_de_escolha(opcoes))


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult, Resolve(escolha_de_sala)],
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        return ReservaOut(reservado=False, motivo="recusado")
    destino = sala if escolha.data is None else escolha.data.sala
    ini, fi = validar_pedido(destino, inicio, fim)
    if conflitos(destino, ini, fi):
        raise ToolError(f"Sala ocupada no intervalo: {destino}")
    reserva = {"id": _proximo_id(), "sala": destino, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(reserva)
    log(f"reserva criada {reserva['id']} sala={destino} {inicio}..{fim} responsavel={responsavel}")
    return ReservaOut(
        reserva=reserva["id"],
        sala=destino,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA_VERSAO,
    )


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas. A primeira linha declara a versao.")
def politica_de_uso() -> str:
    return POLITICA_TEXTO


# --------------------------------------------------------------------------
# Log de cada request no stderr: metodo, id e traceparent do _meta.
# Fica na borda ASGI para registrar tambem o que o SDK recusa antes do handler.
# --------------------------------------------------------------------------


class LogDeRequests:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        corpo = b""
        mensagens = []
        while True:
            msg = await receive()
            mensagens.append(msg)
            corpo += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        self._registrar(corpo)

        async def replay() -> dict:
            return mensagens.pop(0) if mensagens else {"type": "http.disconnect"}

        await self.app(scope, replay, send)

    @staticmethod
    def _registrar(corpo: bytes) -> None:
        try:
            dados = json.loads(corpo)
        except ValueError:
            log("mcp request ilegivel")
            return
        for item in dados if isinstance(dados, list) else [dados]:
            if not isinstance(item, dict):
                continue
            params = item.get("params") or {}
            meta = params.get("_meta") or {} if isinstance(params, dict) else {}
            extra = ""
            if item.get("method") == "tools/call":
                extra = f" tool={params.get('name')}" + (" retry=sim" if params.get("requestState") else "")
            log(
                f"mcp method={item.get('method')} id={json.dumps(item.get('id'))}"
                f" traceparent={meta.get('traceparent', '-')}{extra}"
            )


app = LogDeRequests(mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True))


if __name__ == "__main__":
    log(f"servidor MCP central-de-salas em http://{HOST}:{PORTA}/mcp (politica {POLITICA_VERSAO})")
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
