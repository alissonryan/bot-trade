# bot-trade

Bot de **spot BTC/USDT na KCEX** com LLM (OpenRouter). A modelo só diz comprar / vender / esperar. **Código** define tamanho, stop ATR e se a ordem é paper ou live.

A KCEX **não tem** conta demo nem API HMAC que tenhamos encontrado (`/user/openapi` 404). Paper é um ledger local em cima do **preço real**. Live usa sessão web (`KCEX_TOKEN`).

Para agentes (Claude Code, Cursor, Codex, Grok): leia [AGENTS.md](AGENTS.md) e [CLAUDE.md](CLAUDE.md) **antes** de alterar código.

## Estado (2026-09-04)

| | |
| --- | --- |
| Paper | Roda. Ticker/book REST ~1s. LLM a cada **5 min** (ou se o preço andar ~0,4%). |
| Live | **Ainda não.** Precisa `python -m kcex.cli login` e `MODE=live`. |
| WebSocket KCEX | **Não mapeado.** Não inventar URL. |
| Coleira | 20 USDT por ordem, ~5% do caixa, 1 posição do bot, stop ATR na exchange (live). |
| Conta humana | Há um stop **manual** (0.00064 BTC @ 75722). O bot **não cancela** ids que não gravou. |
| Git | `main` após o plano de 11 tarefas. Testes: `pytest tests`. |

Spec: [docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md](docs/superpowers/specs/2026-09-04-kcex-llm-spot-bot-design.md)  
APIs: [docs/kcex-spot-api.md](docs/kcex-spot-api.md)

## Como funciona

```text
KCEX REST (preço real)
        ↓
LLM (OpenRouter) → BUY | SELL | HOLD
        ↓
Coleira (20 USDT, ATR, 1 posição)
        ↓
  paper → SQLite (data/bot.db)     live → market + stop-market na KCEX
```

Paper **não** envia ordem à corretora. Fill e stop são simulados no processo. Sem login KCEX o paper usa **450 USDT virtuais** (`PAPER_STARTING_USDT`).

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
| `WAKE_MOVE_PCT` | `0.004` | Acorda o LLM se o preço andar ~0,4% |
| `MAX_ORDER_USDT` | `20` | Teto por ordem |
| `MAX_PORTFOLIO_PCT` | `0.05` | Teto vs caixa livre |
| `MAX_DAY_LOSS_USDT` | `20` | Para **compras** novas no dia |
| `PAPER_STARTING_USDT` | `450` | Caixa virtual se o saldo KCEX falhar/zerar |
| `LLM_DAILY_BUDGET_USD` | `2` | Para novas chamadas OpenRouter (UTC) |
| `KCEX_TOKEN` | vazio | Sessão web; obrigatório só no live |
| `KCEX_WS_URL` | vazio | Deixe vazio até capturar no Chrome |

## Paper

```bash
PYTHONPATH=. python -m bot run           # loop
PYTHONPATH=. python -m bot run --once    # um ciclo
```

Log:

```bash
sqlite3 data/bot.db "select ts, action, rule from audit order by id desc limit 10;"
```

Não rode dois loops no mesmo `data/bot.db`. Se já houver um `python -m bot run`, mate o antigo antes.

## Live (quando for a hora)

1. `PYTHONPATH=. python -m kcex.cli login` — Chrome, captcha, Google Authenticator. Grava `KCEX_TOKEN` (~7 dias).
2. `MODE=live` no `.env`.
3. `PYTHONPATH=. python -m bot run`

Ordem: market buy ≤ 20 USDT, depois trigger stop-market ATR na KCEX. O bot **não cancela** stops que você já tem na conta (só ids que ele mesmo gravou).

401 → o processo para. Rode `login` de novo.

Primeira ordem real pode falhar por `needDolos` / `content-sign` (anti-bot da KCEX). Teste só com 20 USDT.

## Login / sessão (KCEX)

O login **já está no projeto**. Não precisa reabrir o DevTools nem colar token na mão.

```bash
PYTHONPATH=. python -m kcex.cli login    # Chrome do bot; você faz captcha + 2FA; grava KCEX_TOKEN
PYTHONPATH=. python -m kcex.cli auth
PYTHONPATH=. python -m kcex.cli ticker BTC_USDT
PYTHONPATH=. python -m kcex.cli balances
```

Opcional: `KCEX_EMAIL` e `KCEX_PASSWORD` só preenchem o form. Captcha e 2FA continuam manuais. Como a API trata o token: [docs/kcex-spot-api.md](docs/kcex-spot-api.md).

## Testes

```bash
PYTHONPATH=. python -m pytest tests -q
```

Nenhuma ordem live nos testes.

## Layout

| Path | Função |
| --- | --- |
| `bot/` | Olho, cérebro, coleira, mãos, loop |
| `kcex/` | Cliente REST + login Playwright |
| `tests/` | Pytest; mocks, sem live |
| `docs/kcex-spot-api.md` | Endpoints capturados |
| `data/bot.db` | Audit paper/live (gitignored) |
| `.env` | Segredos — nunca commitar |
| `AGENTS.md` / `CLAUDE.md` | Regras para a próxima sessão de agente |

## Próxima sessão

1. Ver o paper (`audit` no SQLite): HOLD vs BUY.
2. Mapear WebSocket da KCEX no Chrome (não inventar `wss://`).
3. Só então login live e um probe de 20 USDT — se o dono pedir.
