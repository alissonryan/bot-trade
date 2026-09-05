# bot-trade

Bot de **spot BTC/USDT na KCEX** com LLM (OpenRouter). A modelo só diz comprar / vender / esperar. **Código** define tamanho, stop ATR e se a ordem é paper ou live.

A KCEX **não tem** conta demo nem API HMAC documentada (`/user/openapi` 404; `api.kcex.com` responde 403). Paper é um ledger local em cima do **preço real**. Live usa sessão web (`KCEX_TOKEN`).

Para agentes (Claude Code, Cursor, Codex, Grok): leia [AGENTS.md](AGENTS.md) e [CLAUDE.md](CLAUDE.md) **antes** de alterar código.

## Estado (2026-09-04, após a revisão de segurança do live)

| | |
| --- | --- |
| Paper | Roda. Preço por **WebSocket público da KCEX** (`miniTicker` + `deals` + `bookTicker`); REST só como fallback a cada 5 s. LLM a cada **5 min** (ou se o preço andar ~0,4 %). O caixa virtual é um ledger de verdade (debitado na compra, creditado na venda, persistido). |
| Live | **Ainda não rodou.** Precisa `python -m kcex.cli login` e `MODE=live`. Primeira ordem no **mínimo da exchange** (regras do símbolo), não em 20 USDT. |
| WebSocket KCEX | **Mapeado e verificado**: `wss://wbs.kcex.com/ws?platform=web` (default). A KCEX aceita a nomenclatura legada e a MEXC v3 no mesmo socket. Frames reais em `tests/fixtures/kcex_ws_frames.jsonl`. Não inventar **outra** URL sem capturar do Chrome. |
| Gráfico local | `python -m bot run --chart` → `http://127.0.0.1:8765/` (candles, só leitura, sem botão de compra/venda). |
| Coleira | 20 USDT por ordem, ~5 % do caixa, 1 posição, stop ATR na exchange (live), halt diário com PnL realizado **+ não realizado**, mínimo e escalas lidos da exchange. |
| Segurança live | Posição gravada **antes** do stop; fill confirmado por **saldo**; a linha só é apagada com **prova** de que a ordem não encheu (senão fica `PENDING` para o `reconcile`); a venda que falha re-lê o saldo antes de repor o stop; reconciliação com a exchange no boot e a cada ciclo, inclusive quando o bot se acha zerado; posição sem stop irrecuperável vira `UNPROTECTED` e para com exit 2; posição que o bot não consegue sair vira exit 5. |
| BTC que não é do bot | O `reconcile` guarda em `foreign_btc` (kv) quanto BTC da conta não é dele. Quando o **seu** stop de 0.00064 dispara, o bot reconhece que o que saiu não é do tamanho dele e mantém a própria posição em vez de lançar uma saída fantasma. |
| Observabilidade | Cada decisão grava no audit o snapshot (preço, bid, ask, ATR), o motivo do LLM (`ok`, `llm_budget`, `llm_timeout`, …), o custo real da chamada, os ids de ordem e o estado da posição. Log em `data/bot.log`. |
| Conta humana | Há um stop **manual** (0.00064 BTC @ 75722). O bot **não cancela** ids que não gravou. |
| Git | `PYTHONPATH=. python -m pytest tests -q`. |

Spec: [docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md](docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md) (ver o adendo no fim)  
APIs: [docs/kcex-spot-api.md](docs/kcex-spot-api.md)

## Como funciona

```text
KCEX WebSocket público (miniTicker + deals + bookTicker)  →  Hub  →  Eye  →  Snapshot   [REST a cada 5 s se o socket calar]
        ↓
LLM (OpenRouter) → BUY | SELL | HOLD   (ou o motivo da falha: llm_budget, llm_timeout, llm_http_429, …)
        ↓
Coleira (20 USDT, ATR, 1 posição, regras do símbolo, perda diária realizada + não realizada)
        ↓
  paper → SQLite (caixa, fills, posição)       live → market + stop-market na KCEX, confirmados por saldo
        ↓
Audit: snapshot + motivo/custo do LLM + regra da coleira + ids de ordem + estado da posição
```

Paper **não** envia ordem à corretora. Fill e stop são simulados no processo, com o mesmo slippage na entrada e no stop. Sem login KCEX o paper usa **450 USDT virtuais** (`PAPER_STARTING_USDT`) na primeira execução; depois disso o caixa é o que sobrou.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chrome
cp .env.example .env
```

No `.env` (mínimo para paper):

```bash
MODE=paper
CYCLE_MINUTES=5
OPENROUTER_API_KEY=sk-or-...
LLM_MODEL=deepseek/deepseek-v4-flash-0731   # ou outro id do OpenRouter
```

Nunca commite `.env`.

### Variáveis úteis

| Variável | Padrão | Função |
| --- | --- | --- |
| `MODE` | `paper` | `paper` ou `live` |
| `SYMBOL` | `BTC_USDT` | Único par no v1 |
| `CYCLE_MINUTES` | `15` no código / **`5` no `.env.example`** | Intervalo do LLM |
| `WAKE_MOVE_PCT` | `0.004` | Acorda o LLM se o preço andar ~0,4 % |
| `MAX_ORDER_USDT` | `20` | Teto por ordem |
| `MAX_PORTFOLIO_PCT` | `0.05` | Teto vs caixa livre |
| `MAX_DAY_LOSS_USDT` | `20` | Para **compras** novas no dia (realizado + não realizado) |
| `MIN_CONFIDENCE` | `0` | Bloqueia BUY abaixo disso; nunca bloqueia SELL |
| `PAPER_STARTING_USDT` | `450` | Caixa virtual inicial do paper |
| `LLM_DAILY_BUDGET_USD` | `2` | Para novas chamadas OpenRouter (UTC); cobrado pelo custo real da resposta |
| `LLM_FALLBACK_COST_USD` | `0.02` | Custo por chamada quando a resposta não traz `usage.cost` |
| `LLM_MAX_TOKENS` | `200` | Teto de tokens por resposta |
| `LLM_JSON_MODE` | `0` | `1` pede `response_format: json_object` (nem todo modelo aceita) |
| `WS_ENABLED` | `1` | Socket público da KCEX como fonte de preço |
| `KCEX_WS_URL` | vazio = default confirmado (`wss://wbs.kcex.com/ws?platform=web`) | `-` força só REST (sem WS) |
| `POLL_SECONDS` | `5` | Intervalo do fallback REST quando o socket cala |
| `STALE_MS` | `30000` | Sem preço por mais que isso = mercado stale (bloqueia entradas) |
| `CHART_HOST` / `CHART_PORT` | `127.0.0.1` / `8765` | Onde `--chart` sobe o gráfico local (loopback só) |
| `FILL_CONFIRM_TRIES` / `FILL_CONFIRM_WAIT_S` | `6` / `0.5` | Confirmação de fill por saldo no live |
| `KCEX_USER_AGENT` | UA de Chrome | A WAF devolve 406 para o UA do curl |
| `KCEX_TOKEN` / `KCEX_TOKEN_AT` | vazio | Sessão web e quando foi capturada; obrigatório só no live |
| `LOG_LEVEL` | `INFO` | Log em stderr e `data/bot.log` |

## Paper

```bash
PYTHONPATH=. python -m bot run           # loop
PYTHONPATH=. python -m bot run --once    # um ciclo do LLM
PYTHONPATH=. python -m bot run --chart   # loop + gráfico local (http://127.0.0.1:8765/)
```

`--chart` sobe um servidor HTTP+WS **só em loopback** (`127.0.0.1`/`localhost`/`::1` — recusa qualquer outro host) com um gráfico de candles somente leitura (`chart/index.html`): candles vêm de REST (`/kline`, mesmo padrão do `poll_heavy`), o preço ao vivo vem do `Hub` compartilhado via WebSocket próprio (`/ws`, nosso JSON, nunca o frame cru da KCEX). Não abre uma segunda conexão com a KCEX e não tem nenhum controle de compra/venda. Se a porta já estiver em uso, falha na hora (sem porta alternativa).

Ler o que aconteceu:

```bash
sqlite3 data/bot.db "select ts, action, rule, json_extract(payload,'$.llm.reason') as llm, json_extract(payload,'$.snapshot.last') as last from audit order by id desc limit 10;"
sqlite3 data/bot.db "select ts, side, qty, price, pnl, source from fills order by id desc limit 10;"
sqlite3 data/bot.db "select * from position;"
```

Só um loop por `data/`: o segundo processo sai com código 3 (`data/bot.lock`).

### Medir o modelo

O audit agora guarda o preço e o ATR de cada decisão. Com algumas dezenas de decisões, dá para medir por decisão se o preço tocou `+1R` antes de `−1R` nas barras seguintes (R = distância do stop) e comparar com 50 %, com um sorteio aleatório e com comprar-e-segurar no mesmo período. Sem essa leitura, "HOLD vs BUY" no SQLite não diz se a modelo acerta.

## Live (quando for a hora)

1. `PYTHONPATH=. python -m kcex.cli login` — Chrome, captcha, Google Authenticator. Grava `KCEX_TOKEN` e `KCEX_TOKEN_AT` (~7 dias; o bot avisa no dia 6).
2. `MODE=live` no `.env`.
3. `PYTHONPATH=. python -m bot run`

O que o live faz por ordem:

1. Lê o saldo de BTC, envia o market buy e **grava a posição como `PENDING` antes de qualquer stop**.
2. Confirma o fill pela variação do saldo (fill parcial protege só o que foi comprado; sem fill, cancela e fica flat).
3. Coloca o trigger stop-market (2 tentativas). Se falhar, tenta zerar. Se zerar também falhar, marca `UNPROTECTED` e **para com exit 2**: a posição está na exchange sem stop, resolva à mão e reinicie.
4. SELL: cancela o stop, confirma o cancelamento, vende; se a venda falhar, recoloca o stop.
5. No boot e a cada ciclo do LLM, compara a posição local com saldo e ordens abertas: stop executado vira fill registrado; stop sumido é recolocado.

Códigos de saída: `1` sessão morta (rode `login` de novo), `2` posição sem stop, `3` já existe um bot rodando, `4` ciclo `--once` falhou.

Primeira ordem real pode falhar por `needDolos` / `content-sign` (anti-bot da KCEX). Teste com o mínimo da exchange.

## Login / sessão (KCEX)

```bash
PYTHONPATH=. python -m kcex.cli login    # Chrome do bot; você faz captcha + 2FA; grava KCEX_TOKEN e KCEX_TOKEN_AT
PYTHONPATH=. python -m kcex.cli auth
PYTHONPATH=. python -m kcex.cli ticker BTC_USDT
PYTHONPATH=. python -m kcex.cli balances
```

Opcional: `KCEX_EMAIL` e `KCEX_PASSWORD` só preenchem o form. Captcha e 2FA continuam manuais. O `.env` é gravado com permissão `600`. Como a API trata o token: [docs/kcex-spot-api.md](docs/kcex-spot-api.md).

## Testes

```bash
PYTHONPATH=. python -m pytest tests -q
```

Nenhuma ordem live nos testes. O cliente KCEX e o socket são simulados.

## Layout

| Path | Função |
| --- | --- |
| `bot/eye.py` | Preço (socket primeiro via `Hub`, REST fallback), klines, saldo, regras do símbolo |
| `bot/hub.py` | Guarda em processo do último tick do WS, compartilhada pelo `Eye` e pelo gráfico |
| `bot/brain.py` | OpenRouter; motivo e custo de cada chamada |
| `bot/collar.py` | Coleira: regras puras |
| `bot/hands.py` | `PaperHands` (ledger) / `LiveHands` (invariantes de stop, confirmação por saldo, reconciliação) |
| `bot/cycle.py` | Um passo do loop |
| `bot/cli.py` | `python -m bot run`; lock, log, códigos de saída |
| `bot/store.py` | SQLite: audit, fills, posição com estado, kv |
| `bot/chart_server.py` | Servidor do gráfico local (`--chart`), só loopback |
| `kcex/` | Cliente REST + WS público (`kcex/ws.py`, o único cliente de socket) + login Playwright |
| `chart/index.html` | Página do gráfico local (`--chart`), só leitura |
| `tests/` | Pytest; mocks, sem live |
| `docs/kcex-spot-api.md` | Endpoints e socket capturados |
| `data/` | `bot.db`, `bot.log`, `bot.lock` (gitignored) |
| `.env` | Segredos — nunca commitar |

## Histórico

**2026-09-04, revisão de segurança do live e observabilidade.** Posição persistida antes do stop; fill confirmado por saldo; stop restaurado se a venda falhar; reconciliação com a exchange; estado `UNPROTECTED` com parada; orçamento do LLM pelo custo real (o contador fixo esgotava às ~08:20 UTC); motivos distintos para falha do LLM; loop resiliente a erro de rede; WebSocket real no lugar do poll de 1 s; barra em formação fora do ATR; regras do símbolo; PnL não realizado no halt; caixa do paper como ledger; audit com snapshot; User-Agent explícito; `.env` com `chmod 600`; lock de instância; log em arquivo.

## Próxima sessão

1. Ver o paper: audit com motivo do LLM, fills, posição.
2. Quando houver decisões suficientes, medir (seção "Medir o modelo").
3. ~~Mapear WebSocket da KCEX no Chrome~~ — **feito**: `wss://wbs.kcex.com/ws?platform=web` confirmado e em uso por default. Não inventar **outra** URL `wss://` sem capturar do Chrome.
4. Só então login live e um probe no **mínimo da exchange** — se o dono pedir.
