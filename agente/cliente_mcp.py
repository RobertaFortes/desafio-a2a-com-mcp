"""Cliente MCP do agente: JSON-RPC sobre Streamable HTTP, stateless.

Escrito sobre httpx em vez do `mcp.client.Client` do SDK por dois motivos,
ambos de protocolo:

- o `Client` do SDK, quando recebe `input_required`, despacha a elicitation para
  um callback local e refaz a chamada sozinho. Aqui o `input_required` precisa
  chegar cru ao agente, para virar pausa da Task A2A;
- com um callback de elicitation o SDK declara `{"form": {}, "url": {}}`, e o
  agente declara so o que sabe fazer: elicitation em form mode.

Cada request carrega o `_meta` completo (versao, clientInfo, capabilities e
traceparent) e os headers que o transporte espelha do corpo. Nada e inferido de
request anterior: o objeto vive entre chamadas, o estado de protocolo nao.
"""

from __future__ import annotations

import itertools
import secrets
import sys
from dataclasses import dataclass
from typing import Any

import httpx

PROTOCOLO = "2026-07-28"
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
CAPABILITIES = {"elicitation": {"form": {}}}


class ErroMCP(Exception):
    """Erro de protocolo (JSON-RPC `error`) devolvido pelo servidor MCP."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message


@dataclass
class Trace:
    """Contexto W3C: o trace-id e da Task, o span-id e novo a cada request."""

    trace_id: str
    flags: str = "01"

    @classmethod
    def do_header(cls, valor: str | None) -> Trace | None:
        partes = (valor or "").strip().split("-")
        if len(partes) == 4 and len(partes[1]) == 32 and partes[1] != "0" * 32:
            return cls(trace_id=partes[1].lower(), flags=partes[3])
        return None

    @classmethod
    def novo(cls) -> Trace:
        return cls(trace_id=secrets.token_hex(16))

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._http = httpx.AsyncClient(timeout=30)
        # Ids unicos no processo: o retry do MRTR e um request novo, com id novo.
        self._ids = itertools.count(1)

    async def fechar(self) -> None:
        await self._http.aclose()

    async def _request(self, metodo: str, params: dict[str, Any], trace: Trace, nome: str | None = None) -> dict:
        corpo = {
            "jsonrpc": "2.0",
            "id": f"agente-{next(self._ids)}",
            "method": metodo,
            "params": {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
                    "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                    "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
                    "traceparent": trace.traceparent(),
                },
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if nome is not None:
            headers["Mcp-Name"] = nome
        print(
            f"mcp -> {metodo} id={corpo['id']}{' ' + nome if nome else ''}"
            f"{' (retry)' if 'requestState' in params else ''} traceparent={corpo['params']['_meta']['traceparent']}",
            file=sys.stderr,
            flush=True,
        )
        resposta = await self._http.post(self.url, json=corpo, headers=headers)
        dados = resposta.json()
        if "error" in dados:
            erro = dados["error"]
            raise ErroMCP(erro.get("code", 0), erro.get("message", "erro MCP"))
        return dados["result"]

    async def listar_tools(self, trace: Trace) -> list[dict[str, Any]]:
        return (await self._request("tools/list", {}, trace))["tools"]

    async def ler_resource(self, uri: str, trace: Trace) -> str:
        resultado = await self._request("resources/read", {"uri": uri}, trace, nome=uri)
        return "".join(c.get("text", "") for c in resultado["contents"])

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace: Trace,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        """`tools/call`. Devolve o resultado cru: `complete` ou `input_required`."""
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state
        return await self._request("tools/call", params, trace, nome=nome)
