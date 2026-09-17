# Paper de futuros KCEX com Jev como gatilho e LLM decisora

Data: 2026-09-17. Status: design aprovado em conversa, aguardando revisão desta spec.

## Objetivo

Operar o contrato perpétuo **BTC_USDT** da KCEX em **paper**, num horizonte de **segundos**, com long e short. O Jev (TypeSafe, System One) avalia o mercado a cada 2 s e decide quando acordar a LLM; a LLM dá a palavra final (`LONG`/`SHORT`/`CLOSE`/`HOLD`); o código dono do risco decide tamanho, stop e limites. Live só depois de edge demonstrado pelo critério fixo desta spec.

## Fora de escopo

- Qualquer ordem real, rota privada `/fapi/v1/private/...` ou uso de `KCEX_TOKEN`. As rotas privadas ainda não foram capturadas; serão outra spec, só após edge.
- Ordens limite/maker, várias posições, outros pares, alavancagem acima de 3x.
- Alterar o bot spot (`bot/`) além de reaproveitar peças de baixo nível.
- Treinar ou ajustar modelos.

## Decisões fixadas

| Tema | Decisão |
|---|---|
| Papel dos modelos | Jev filtra (a cada 2 s), LLM decide. O custo do atraso da LLM é medido. |
| Alavancagem | Configurável, padrão **1x**, teto fixo em código **3x**, margem isolada. Liquidação simulada mesmo em 1x. |
| Ordens | Mercado nas duas pontas (taker). |
| Proteções | Stop obrigatório + tempo máximo (5 min) + saída antecipada pela LLM, o que vier primeiro. |
| Dinheiro | Saldo inicial 450 USDT; margem 20 USDT por trade; máx. 5% do saldo; 1 posição; perda diária igual à do spot. |
| Chamada da LLM | Imediata ao disparo do Jev; uma por vez; 10 s de espera só após HOLD (sinal oposto ou saída ignoram); timeout 8 s; descarte se o preço andar > 5 bps entre disparo e resposta. |
| Edge | Critério rígido abaixo, fixado antes de começar. |
| Arquitetura | Pacote novo `fut/` ao lado de `bot/`; banco e lock próprios. |

Todos os números acima são configuráveis por env, exceto o teto de 3x.

## Fatos do contrato (lidos em 2026-09-16, GET públicos sem login)

`GET https://www.kcex.com/fapi/v1/contract/detail?symbol=BTC_USDT`:
`contractSize=0.0001` BTC, `minVol=1` contrato, `volScale=0`, `priceUnit=0.1`, `priceScale=1`, `takerFeeRate=0.0001`, `makerFeeRate=0`, `maintenanceMarginRate=0.005`, `initialMarginRate=0.008`, `minLeverage=1`, `maxLeverage=125`, `marketOrderPriceLimitRate2=0.005`, `apiAllowed=true`.

`GET .../contract/funding_rate/BTC_USDT`: `fundingRate`, `collectCycle=8` (horas), `nextSettleTime` (ms).

`GET .../contract/ticker?symbol=BTC_USDT`: `lastPrice`, `bid1`, `ask1`, `fairPrice` (marcação), `indexPrice`, `fundingRate`, `timestamp`.

Estes valores são lidos em runtime (`contract/detail` na partida e a cada hora) e nunca hardcoded, exceto como fixtures de teste. Ida e volta a mercado custa ~2 bps.

## Arquitetura

```
kcex/fapi.py        leituras públicas de futuros (GET, com retry)
kcex/fws.py         socket público wss://www.kcex.com/fapi/edge
fut/settings.py     configuração de futuros (env)
fut/types.py        FutSnapshot, JevVerdict, FutIntent, FutGate, FutPosition
fut/market.py       estado em memória: book, trades, CVD, mid, marcação, funding, barras 1 s / 1 min
fut/questions.py    perguntas do Jev e limiares (um arquivo só, para revisão)
fut/jev.py          cliente do Jev (typesafe-sdk) + mock determinístico
fut/llm.py          prompt, parser LONG/SHORT/CLOSE/HOLD, custo e orçamento
fut/dispatch.py     regras de chamada da LLM
fut/collar.py       risco e tamanho
fut/ledger.py       paper: fills, taxa, funding, liquidação, stop, tempo máximo
fut/shadow.py       baselines nos mesmos ticks
fut/report.py       relatório e verificação do critério de edge
fut/loop.py         loop principal
fut/cli.py          entrada, lock, logs, códigos de saída (python -m fut)
```

**Reaproveitado de `bot/`, sem cópia:** `bot/store.py` (`Store(path, mode="futures-paper")`, identidade de modo, `append_audit`, `kv_get/kv_set`, `reserve_budget`/`settle_budget`, `commit`/`rollback`) e os helpers de custo de `bot/brain.py` (`_cost_from`, `_cost_from_http_error`). Se algum helper precisar deixar de ser privado, a mudança é só de nome/exportação, com testes do spot passando.

**Não reaproveitado:** `bot/brain.think` (formato spot), `bot/collar.py` e `bot/hands.py` (premissas de spot).

**Banco:** `data/futures-paper.db`. **Lock:** `data/futures.lock`, independente de `data/bot.lock`. Mesma regra do spot: lock adquirido antes de qualquer efeito colateral, incluindo log em arquivo.

## Fluxo

A cada tick do WS (ou do REST de reserva):
`market.apply` → `ledger.mark` (liquidação, stop, tempo máximo, funding) → `shadow.mark`.

A cada 2 s:
`market.snapshot` → `jev.evaluate` → `questions.should_wake` → se sim, `dispatch.request` → `llm.decide` → `collar.check` → `ledger.execute`.

Toda etapa grava uma linha de auditoria com: snapshot, perguntas e respostas do Jev, request e resposta crus da LLM, preço no disparo e na resposta, gate do collar e resultado.

## Jev

**Estado enviado (compacto, relativo):** mid, spread em bps, desequilíbrio do book e profundidade a 5/10/25 bps por lado, retornos de 2 s/10 s/60 s/5 min, fluxo agressor em 30 s e 2 min (volume comprador, vendedor, CVD, VWAP), funding atual, desvio `lastPrice` vs `fairPrice` em bps, e posição (lado, segundos aberta, PnL não realizado em bps, distância em bps até stop e liquidação). Posição ausente é enviada como `flat`.

**Perguntas (uma chamada):**

| Chave | Tipo | Pergunta |
|---|---|---|
| `direction_60s` | choice `up`/`down`/`flat` | O mid estará acima, abaixo ou dentro de ±3 bps em 60 s? |
| `move_beats_cost` | noul | O movimento nos próximos 60 s supera 3 bps em alguma direção? |
| `flow_aligned` | noul | O fluxo agressor recente confirma a direção dominante do preço? |
| `regime` | choice `trend`/`range`/`volatile` | Regime atual. |
| `exit_now` | noul (só com posição) | A tese da posição aberta perdeu força? |

`instructions` e `criteria` usam JSON estruturado (`question`, `goal`, `inputs`, `what`, `not_for`), conforme a doc do TypeSafe.

**Regra de acordar a LLM** (limiares por env, padrão 0,6):
- sem posição: `direction_60s.choice != flat` e `direction_60s.confidence >= 0.6` e `move_beats_cost >= 0.6`;
- com posição: `exit_now >= 0.6`, ou `direction_60s` contrário ao lado da posição com `confidence >= 0.6`.

**Cliente:** `typesafe-sdk` (Python), `model` por env (padrão `jev-latest`), `RetryPolicy(max_retries=0)` e timeout de 2 s no caminho quente. O `model` retornado pela API é gravado (o alias pode mudar de versão). Custo estimado por `input_tokens × JEV_USD_PER_MTOK` (padrão 0,042) e contado no custo total.

**Mock:** com `JEV_MODEL=mock` ou sem `TYPESAFE_API_KEY`, um modelo determinístico (momentum + desequilíbrio + fluxo) substitui o Jev com a mesma interface. O relatório marca a sessão como `mock` e ela nunca conta para o critério de edge.

## LLM

**Entrada:** o mesmo estado do Jev, as respostas do Jev, o motivo do disparo (`entry_signal`, `exit_signal`, `reversal_signal`) e a posição.

**Saída exigida:** JSON `{"action": "LONG|SHORT|CLOSE|HOLD", "confidence": 0..1, "reason": "..."}`. Sem tamanho, stop ou alvo.

**Validação:** `LONG`/`SHORT` só sem posição; `CLOSE` só com posição; qualquer outra combinação ou JSON inválido vira HOLD com `llm.reason` nomeado (`invalid_action`, `parse_error`, `timeout`, `http_error`, `budget`).

**Modelo:** `LLM_MODEL` do `.env` (hoje deepseek-v4-flash), raciocínio desligado como no candidato B0.1, timeout de 8 s. Orçamento diário durável: reserva antes do HTTP, acerto depois, timeout reserva o custo de fallback.

## Dispatch

- Nenhuma chamada em andamento → chama já.
- Chamada em andamento → disparo ignorado e gravado como `suppressed_inflight`.
- Última resposta foi HOLD há menos de 10 s → disparo de mesmo tipo e direção gravado como `suppressed_cooldown`. Direção oposta ou `exit_signal` não esperam.
- Resposta após 8 s → descartada (`stale_timeout`).
- `|mid_resposta − mid_disparo| / mid_disparo > 5 bps` → ação descartada (`stale_price`).
- Sem orçamento → `suppressed_budget`.

## Collar

Para `LONG`/`SHORT`, na ordem:
1. mercado fresco (último frame < 5 s) e contrato `state=0`;
2. sem posição aberta;
3. perda diária não atingida (realizado + não realizado + taxas + funding + custo de Jev e LLM do dia);
4. `confidence` finita e ≥ `FUT_MIN_CONFIDENCE`;
5. ATR de 1 min válido;
6. alavancagem entre 1 e min(3, `maxLeverage`);
7. preço executável: `ask1` para LONG, `bid1` para SHORT, com fallback para `lastPrice` só se o lado estiver ausente, multiplicado (LONG) ou dividido (SHORT) pelo slippage; slippage negativo recusado;
8. `nocional_alvo = min(FUT_MARGIN_USDT, FUT_MAX_BALANCE_PCT × saldo) × alavancagem`;
9. `contratos = floor(nocional_alvo / (preço × contractSize))`; `< minVol` → `dust`;
10. stop: distância `clamp(ATR_MULT × ATR, MIN_STOP_PCT × preço, MAX_STOP_PCT × preço)`, arredondado a `priceUnit` (LONG para baixo, SHORT para cima, mesma convenção do spot);
11. liquidação estimada (ver ledger); se a distância do stop **já arredondado** for `> 0,5 × distância_liquidação` → `liq_too_close`;
12. margem requerida ≤ saldo livre.

`CLOSE` passa sempre que há posição (nada no collar prende o bot numa posição).

## Ledger (paper)

- **Preço de fill a mercado:** abrir LONG / fechar SHORT no `ask1 × (1 + slip)`; abrir SHORT / fechar LONG no `bid1 × (1 − slip)`. A mesma função serve ao collar e ao fill.
- **Taxa:** `takerFeeRate × nocional` em cada ponta, debitada do saldo.
- **Margem:** `nocional / alavancagem`, bloqueada na abertura.
- **Liquidação (isolada):** LONG `entrada × (1 − 1/alav + mmr)`; SHORT `entrada × (1 + 1/alav − mmr)`, com `mmr = maintenanceMarginRate`. Checada contra `fairPrice`. Liquidar zera a posição e perde a margem inteira; motivo `liquidation`.
- **Stop:** dispara quando o preço relevante (bid para LONG, ask para SHORT, e `lastPrice`) cruza o stop; fill no pior entre stop e book, com slippage; motivo `stop`.
- **Tempo máximo:** `FUT_MAX_HOLD_SECONDS` (padrão 300); fill no book; motivo `time_limit`.
- **Funding:** ao passar `nextSettleTime` com posição aberta, `nocional_marcação × fundingRate`; LONG paga quando positivo, SHORT recebe.
- **Ordem de checagem por tick:** liquidação, stop, tempo máximo, funding.
- **Atomicidade:** fill, posição, saldo e taxa numa transação do `Store`; estado em memória só muda após o commit.
- **Reinício:** carrega posição e saldo do banco e remarca imediatamente.

## Shadow (baselines)

Nos mesmos ticks, com o mesmo collar e ledger, cada um com saldo virtual e tabela própria:
- `flat`: nunca opera;
- `jev_only`: executa a direção do Jev sempre que ele acordaria a LLM, sem LLM;
- `random`: entra com probabilidade fixa no mesmo ritmo médio de entradas da LLM (semente gravada), mesmo stop e tempo máximo.

## Falhas

| Situação | Comportamento |
|---|---|
| WS sem frame > 5 s | Sem entradas; REST `ticker` a cada 1 s como reserva para liquidação, stop e tempo máximo. |
| WS e REST falhando > 60 s com posição aberta | Audit `unmonitored`, saída com código 8. |
| Jev erro ou > 2 s | Não acorda a LLM; audit `jev_error`. |
| LLM erro/timeout/JSON inválido | HOLD com motivo nomeado; custo incerto reservado. |
| Orçamento LLM esgotado | Sem chamadas à LLM; proteções seguem. |
| `BudgetStateCorrupt` | Sem chamadas à LLM; proteções seguem. |
| Identidade de banco divergente | Recusa abrir (`StoreIdentityMismatch`), código 9. |
| Lock ocupado | Código 3, como o spot. |

## Captura do WS de futuros (pré-requisito)

O host `wss://www.kcex.com/fapi/edge` e a mensagem `sub.ticker` estão registrados em `docs/kcex-spot-api.md`. Os canais de trades e book, o ping e o formato exato dos frames **ainda não foram capturados**. Antes de escrever `kcex/fws.py`: capturar frames reais (canal público, sem login), gravar como fixtures em `tests/fixtures/` e documentar em `docs/kcex-futures-api.md`. Nada será adivinhado. Se algum canal não existir publicamente, o fallback é REST (`depth`, `deals`) com a latência medida e registrada.

## Medição e critério de edge

Cada decisão registra: timestamp e mid no disparo do Jev, timestamp e mid na resposta da LLM, latências de Jev e LLM, ação final, e para trades: entrada, saída, motivo, taxas, funding, PnL bruto e líquido.

`python -m fut report` mostra: número de trades, PnL bruto e líquido, taxas, funding, custo de Jev e LLM, resultado por baseline, bootstrap (10.000 reamostragens, IC 95%) do PnL líquido por trade, distribuição de latência da LLM, contagem de `stale_timeout`/`stale_price`/`suppressed_*`, e verificação automática do critério:

1. ≥ 200 trades fechados e ≥ 14 dias corridos de paper com Jev real (sessões `mock` não contam);
2. PnL líquido total > 0 após taxas, funding, slippage e custo de Jev e LLM;
3. PnL líquido maior que `flat`, `jev_only` e `random`;
4. limite inferior do IC 95% do PnL líquido por trade > 0;
5. nenhum dia com perda acima do limite diário.

O critério não pode ser alterado depois de a rodada começar. Passar nele não autoriza live; só permite abrir a spec de live.

## Testes

TDD. Nenhum teste chama rede, Jev real ou LLM real.
- `fut/collar`: tamanho em contratos, alavancagem 1/3 e recusa acima, slippage negativo, `dust`, `liq_too_close`, stop LONG e SHORT, perda diária com taxas e funding.
- `fut/ledger`: fills LONG/SHORT com taxa, stop, tempo máximo, liquidação por `fairPrice`, funding pago e recebido, ordem de checagem, atomicidade (falha no meio não altera nada), reinício com posição aberta.
- `fut/dispatch`: uma por vez, espera após HOLD, bypass de saída e direção oposta, timeout, `stale_price`, orçamento.
- `fut/questions`: regra de acordar com e sem posição.
- `fut/llm`: parser e validação de ação por estado.
- `fut/shadow`: baselines independentes do saldo real.
- `kcex/fws`: parser com frames reais capturados.
- `fut/report`: critério de edge com dados sintéticos que passam e que falham em cada item.

A suíte do spot continua passando sem alteração.

## Documentação a atualizar junto com a implementação

- `CLAUDE.md` e `AGENTS.md`: futuros deixam de ser "não usar" e passam a ser paper em `fut/`, com a mesma proibição de live sem nova spec.
- `docs/kcex-futures-api.md`: novo, com endpoints públicos, frames do WS e o que ainda não foi capturado.
- `.env.example`: variáveis `FUT_*`, `JEV_*`, `TYPESAFE_API_KEY`.
