# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio do curso de MCP e A2A (MBA Engenharia de Software com IA, Full Cycle). Enunciado original: [devfullcycle/desafio-a2a-com-mcp](https://github.com/devfullcycle/desafio-a2a-com-mcp).

Dois processos, em Python:

| Processo | Porta | Endpoint | Papel |
|---|---|---|---|
| `servidor-mcp/servidor.py` | `7301` | `POST /mcp` (Streamable HTTP, stateless, JSON) | Servidor MCP da Central de Salas: 3 tools, 1 resource, MRTR na reserva |
| `agente/agente.py` | `7300` | `POST /a2a` (JSON-RPC 2.0) e `GET /.well-known/agent-card.json` | Agente A2A v1.0 por fora, host MCP por dentro, sem LLM |

```
cliente A2A ──SendMessage/GetTask──▶ agente (:7300) ──tools/list, resources/read, tools/call──▶ servidor MCP (:7301)
            ◀── Task (INPUT_REQUIRED, ◀── input_required + requestState
                COMPLETED, CANCELED,     (guardado no agente, ligado à Task)
                FAILED) + artifact   ──▶ retry com id novo + inputResponses + requestState
```

Stack, com versões travadas nos `pyproject.toml`:

- servidor MCP: `mcp==2.3.0` (SDK oficial v2, revisão `2026-07-28` da spec) e `uvicorn==0.54.0`
- agente: `a2a-sdk[http-server]==1.2.1` (SDK oficial do A2A v1.0), `httpx==0.28.1`, `starlette==1.7.0` e `uvicorn==0.54.0`. Nenhuma dependência de provedor de LLM

## Como rodar

Pré-requisito: Python 3.10 ou superior (testado com 3.11 e 3.14).

### 1. Clonar e instalar (uma vez)

```bash
git clone https://github.com/RobertaFortes/desafio-a2a-com-mcp.git
```

```bash
cd desafio-a2a-com-mcp
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install ./servidor-mcp ./agente
```

### 2. Terminal 1: servidor MCP

O servidor exige a chave de integridade do `requestState` em `REQUEST_STATE_SECRET`, com no mínimo 32 bytes. Sem ela, ele recusa subir. Gere uma chave nova e exporte no mesmo terminal em que o servidor vai rodar:

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

```bash
.venv/bin/python servidor-mcp/servidor.py
```

O stderr deste terminal é o log de requests MCP: uma linha por request, com método, id e `traceparent`. Para testar o `requestState` depois de um restart, pare o servidor (Ctrl+C) e suba de novo **no mesmo terminal**, para manter a mesma chave. Com outra chave, o `requestState` antigo é rejeitado com `-32602`, como deve ser.

A chave nunca vai para o repositório. Se preferir um arquivo, use `.env` (já está no `.gitignore`).

### 3. Terminal 2: agente

```bash
.venv/bin/python agente/agente.py
```

### 4. Terminal 3: validador

Com os dois processos **recém-iniciados**, porque as reservas criadas por uma execução mudam o resultado da seguinte:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

### Variáveis de ambiente (todas opcionais, exceto o segredo)

| Variável | Padrão | Uso |
|---|---|---|
| `REQUEST_STATE_SECRET` | (obrigatória) | Chave de integridade do `requestState`, no mínimo 32 bytes |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `7301` | Onde o servidor MCP escuta |
| `AGENTE_HOST` / `AGENTE_PORT` | `127.0.0.1` / `7300` | Onde o agente escuta |
| `AGENTE_URL` | `http://127.0.0.1:7300` | URL pública usada no Agent Card |
| `MCP_URL` | `http://127.0.0.1:7301/mcp` | Servidor MCP que o agente consome |

### Conferências manuais do fluxo do avaliador

Agent Card:

```bash
curl -s http://localhost:7300/.well-known/agent-card.json
```

Sala ocupada (passo 7), com o corpo do exemplo de wire. A Task fica em `TASK_STATE_INPUT_REQUIRED` com `alternativas: sala-fusca, sala-mirante`:

```bash
python3 -c "import json; print(json.dumps(json.load(open('exemplos/wire/08-a2a-send-message.json'))['request']['body']))" | curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' -d @-
```

Continuação (passo 8): troque `<TASK_ID>` pelo `id` devolvido acima. Use `escolha=recusar` para o passo 9:

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{"messageId":"msg-2","role":"ROLE_USER","taskId":"<TASK_ID>","parts":[{"text":"escolha=sala-mirante"}]}}}'
```

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":3,"method":"GetTask","params":{"id":"<TASK_ID>"}}'
```

Uma nova continuação na mesma Task, agora terminal, é recusada com erro JSON-RPC `-32004` (`UNSUPPORTED_OPERATION`).

## Onde a ponte acontece

A ponte está inteira em [`agente/agente.py`](agente/agente.py), em dois métodos do executor A2A. **Ida:** em `_concluir_ou_pausar` ([agente/agente.py:172](agente/agente.py#L172)), quando o resultado cru do `tools/call` chega com `resultType` igual a `input_required` ([linha 181](agente/agente.py#L181)), o agente extrai a única entrada de `inputRequests` (a chave atribuída pelo servidor, o nome do campo e o `enum` ou `const` do `requestedSchema`), guarda num objeto `Pausa` o `requestState` opaco junto com a tool, os argumentos originais, a chave, as opções, a versão da política e o trace, indexado pelo id da Task em `PAUSAS` ([linha 206](agente/agente.py#L206)), e põe a Task em `TASK_STATE_INPUT_REQUIRED` com a mensagem exata `alternativas: <ids>`, na ordem do `enum` ([linha 208](agente/agente.py#L208)). **Volta:** em `_retomar` ([linha 242](agente/agente.py#L242)), o `SendMessage` de continuação traz o `taskId`; o agente recupera a `Pausa` daquela Task, traduz `escolha=<id>` em `{"action": "accept", "content": {"sala": <id>}}` e `escolha=recusar` em `{"action": "decline"}` (escolha fora do `enum` repete a pausa sem chamar o MCP) e repete o `tools/call` original com `inputResponses` na mesma chave e o `requestState` ecoado byte a byte ([linhas 260 e 261](agente/agente.py#L260)). O id de JSON-RPC é novo porque cada request do [`ClienteMCP`](agente/cliente_mcp.py#L76) consome o próximo valor de um contador do processo (`agente-1`, `agente-2`, ...). No stderr do servidor MCP, o par aparece assim (trecho de uma execução do validador):

```
mcp method=tools/call id="agente-6" traceparent=00-e794c5d1fdf3ef47f651db5684ce1c1e-0ae82283ae6b6c87-01 tool=reservar_sala
mcp method=tools/call id="agente-7" traceparent=00-e794c5d1fdf3ef47f651db5684ce1c1e-d85d82c599b5b60c-01 tool=reservar_sala retry=sim
```

Do lado do servidor, o MRTR é o mecanismo de primeira classe do SDK Python: o parâmetro `escolha` de `reservar_sala` é `Annotated[ElicitationResult, Resolve(escolha_de_sala)]` ([servidor-mcp/servidor.py:216](servidor-mcp/servidor.py#L216)). O resolver ([linha 199](servidor-mcp/servidor.py#L199)) valida o pedido, devolve `None` se a sala está livre e devolve `Elicit(...)` com um schema plano de uma propriedade `sala` restrita às alternativas quando há conflito. Com o protocolo `2026-07-28`, o SDK não abre canal de volta: ele termina a resposta com `input_required`, e no retry injeta a resposta do cliente, já verificada, no parâmetro da tool.

## Decisões técnicas

### Como o `requestState` é protegido

- **Mecanismo:** `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)` do SDK MCP ([servidor-mcp/servidor.py:165](servidor-mcp/servidor.py#L165)), o utilitário pronto que o enunciado aponta. Ele instala o `RequestStateBoundary`, que sela todo `requestState` que sai e verifica todo que volta, antes de qualquer handler rodar.
- **Criptografia:** AES-256-GCM, com a chave derivada do segredo por HKDF-SHA256. É AEAD: cifra e autentica. Trocar um caractere quebra a tag GCM, e o servidor responde `-32602 Invalid or expired requestState`, com o motivo real só no log.
- **Validade:** 10 minutos (`TTL_REQUEST_STATE = 600`, dentro da faixa de 5 a 30). A expiração (`exp`) vai dentro do envelope selado.
- **Vínculo ao pedido:** o envelope também sela o método, o nome da tool, um digest dos `arguments` e o audience (`central-de-salas`). Se o retry chegar com argumentos diferentes dos selados, o estado é rejeitado com `-32602`: a divergência não toma efeito. Foi o caminho "rejeitar" que o enunciado aceita.
- **Sem memória entre rodadas:** o servidor não guarda nada entre o `input_required` e o retry. O pedido original é reconstruído dos `arguments`, conferidos contra o digest selado, e a pergunta feita fica selada no `requestState` (digest da elicitation). Por isso o retry funciona depois de reiniciar o processo, desde que a chave seja a mesma.
- **Segredo fora do código:** o servidor lê `REQUEST_STATE_SECRET` do ambiente e recusa subir se ela faltar ou tiver menos de 32 bytes. Não existe valor padrão.
- **Opacidade no agente:** o agente nunca abre, decodifica ou reconstrói o `requestState`. Ele só existe no campo `Pausa.request_state` e não aparece no card, no artifact, nas mensagens nem nos logs do agente.

### Onde fica o estado das Tasks

- **Tasks A2A:** no `InMemoryTaskStore` do a2a-sdk, com histórico, status e artifacts. O `DefaultRequestHandler` do SDK faz a máquina de estados: `SUBMITTED → WORKING → INPUT_REQUIRED | COMPLETED | CANCELED | FAILED`. Ele também recusa `SendMessage` para Task em estado terminal.
- **Pausas:** num dicionário em memória do agente, `PAUSAS[task_id] → Pausa`. A entrada nasce no `input_required` e é removida quando a Task termina. Como a chave é o id da Task, duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`.
- **Reservas:** em memória no servidor MCP, carregadas de `dados/reservas.json` na subida. Elas não sobrevivem a restart, como o enunciado permite.

### Outras decisões

- **O cliente MCP do agente é escrito à mão, sobre `httpx`** ([agente/cliente_mcp.py](agente/cliente_mcp.py)). O `mcp.client.Client` do SDK tem dois comportamentos incompatíveis com a ponte. Com um callback de elicitation, ele responde o `input_required` sozinho e refaz a chamada, e a Task nunca pausaria. Com qualquer callback de elicitation, ele também declara `{"form": {}, "url": {}}`, enquanto o enunciado pede só form mode. O cliente próprio monta, em todo request, o `_meta` completo (`protocolVersion`, `clientInfo`, `clientCapabilities: {"elicitation": {"form": {}}}` e `traceparent`) e os headers espelhados (`MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` em `tools/call` e `resources/read`). Não existe sessão nem handshake: o `httpx.AsyncClient` vive entre chamadas, o estado de protocolo não.
- **Descoberta em runtime:** a cada Task nova, o agente faz `tools/list`, confere se a tool `reservar_sala` existe e monta os `arguments` a partir das `properties` e do `required` do `inputSchema` descoberto. Em seguida ele lê `politica://uso` e extrai a versão da primeira linha. No log do servidor, o `tools/list` sempre vem antes do `tools/call` daquela Task.
- **`traceparent`:** o trace-id do header `traceparent` da chamada A2A vai no `_meta.traceparent` de todo request MCP daquela Task, inclusive no retry, que reaproveita o trace guardado na `Pausa` se a continuação vier sem header. O span-id é novo a cada request. Sem header, o agente gera um trace-id por Task.
- **O agente não decide domínio.** Conflito, política, alternativas e ordem vêm do servidor MCP. O agente só valida o formato fixo do texto e se a escolha está no `enum` que o próprio servidor mandou.
- **Erros:** `isError: true` termina a Task em `TASK_STATE_FAILED`, com o texto da tool intacto no `status.message` e no `history`. O SDK prefixa com `Error executing tool reservar_sala: `, o que o enunciado aceita. `reservado: false` (recusa) termina em `TASK_STATE_CANCELED`.
- **Mensagem final no `history`:** o a2a-sdk só move `status.message` para o `history` na transição seguinte, e estado terminal não tem transição seguinte. Para a mensagem final (por exemplo, `Sala inexistente: ...`) ficar visível no histórico, como nos exemplos de wire, o agente publica a mensagem em `WORKING` e repete o mesmo `messageId` no estado terminal (`_terminar`).
- **Log do servidor MCP na borda ASGI** (`LogDeRequests`): assim aparecem no stderr também os requests que o SDK recusa antes de chegar ao handler, como `_meta` incompleto ou header divergente.

### Divergências de SDK documentadas

- **Header `A2A-Version`.** O a2a-sdk 1.2.1 segue a letra da spec A2A v1.0: sem o header `A2A-Version`, ele assume `0.3` e recusa `SendMessage`/`GetTask` com `VersionNotSupportedError`. O validador fala v1.0 sem mandar o header. Evidência, em `a2a/utils/version_validator.py`:

  ```python
  if not actual_version:
      return constants.PROTOCOL_VERSION_0_3
  ```

  A solução foi a classe `ContextoA2A` ([agente/agente.py:316](agente/agente.py#L316)), um `ServerCallContextBuilder` que assume `1.0` só quando o header está ausente. É a única versão que o card declara. Um cliente que mande `A2A-Version` continua passando pela checagem do SDK sem alteração.
- **Forma da resposta de `GetTask`.** O a2a-sdk devolve a Task direto em `result`, que é a forma da spec v1.0. O exemplo `09-a2a-get-task-input-required.json` mostra `result.task`. O validador aceita as duas formas (`tarefa_de`), e a SDK não foi forçada a imitar o exemplo. `SendMessage` devolve `result.task`, igual ao exemplo.
- **Ruído no stderr do agente.** Quando recusa um `SendMessage` para Task terminal (verificação 31), o a2a-sdk deixa uma task interna sem aguardar, e o asyncio imprime `Task was destroyed but it is pending!`. A resposta JSON-RPC de erro sai correta. O aviso é interno ao SDK e foi mantido visível de propósito. Já o `Failed to detach context` do OpenTelemetry do SDK foi silenciado, porque não tem nenhum efeito no protocolo.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: e794c5d1fdf3ef47f651db5684ce1c1e
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
