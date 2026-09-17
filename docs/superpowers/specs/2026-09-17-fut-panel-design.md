# Painel local do paper de futuros (`python -m fut panel`)

Data: 2026-09-17 · Dono: Alisson · Status: aprovado em conversa, aguardando revisão do texto

## Objetivo

Hoje o paper de futuros (`fut/`) só é acompanhado pelo terminal e por SQL. Queremos uma página local, em português simples, em que qualquer leigo entenda: quanto está o BTC, se o bot está vivo, se há posição aberta e como ela está, o que o bot acabou de decidir e por quê, e quanto ele ganhou ou perdeu.

## Decisões fechadas

| Tema | Decisão |
|---|---|
| Escopo | Só futuros paper. Spot fica de fora. |
| Acesso | Só neste Mac: bind em `127.0.0.1`/`localhost`/`::1`, qualquer outro host é recusado (mesma regra do `--chart`). |
| Arquitetura | Processo separado, somente leitura, sobre `data/futures-paper.db`. Não toca em `fut/loop.py` nem exige reiniciar o bot. |
| Transporte | HTTP com polling de 1 s. Sem WebSocket próprio. |
| Preço | Vem do `snapshot` das linhas `jev` que o bot já grava (~1 a cada 2,5 s). O painel **não** abre conexão com a KCEX. |
| Ações | Nenhuma. Só GET. Sem botão de operar, parar ou configurar. |

## Arquitetura

```
fut run ──escreve──▶ data/futures-paper.db
                          │  sqlite3 "file:<path>?mode=ro"
python -m fut panel ──▶ fut/panel/server.py  (127.0.0.1:8766)
                          ├─ GET /                     → panel/index.html
                          ├─ GET /api/state            → resumo atual
                          └─ GET /api/events?after=N   → eventos novos, já narrados
```

O painel não pega `data/futures.lock` (só lê), não constrói `FutStore` (que migra e carimba, ou seja, escreve), não lê `.env`, e não chama KCEX, Jev nem LLM.

## Componentes

### `fut/panel/reader.py`
Única porta para o banco. Abre `sqlite3.connect("file:<abs>?mode=ro", uri=True, timeout=0.5)` a cada leitura e fecha em seguida (conexão curta: nunca segura lock de leitura enquanto o bot quer commitar).

- `decision_facts(after_id, limit=2000) -> rows`: uma leitura limitada por busca `id > after_id ORDER BY id LIMIT ?`, retornando `id, ts_ms, kind, cost_usd, bid, ask, last, stale, spread_bps` e o high-water mark.
- `event_rows(after_id, upto_id, limit)`: eventos paginados por id; a primeira página fica limitada aos últimos 20.000 ids.
- `fills(book=None, since_ms=None) -> list[dict]`
- `position(book) -> dict | None`, `balances() -> dict[str, float]` (chaves `fut_balance:*` de `kv`)
- Erros: arquivo ausente → `PanelDbMissing`; `locked`, `busy` e falhas transitórias de journal/abertura → `PanelDbBusy`; demais erros operacionais → `PanelDbBroken`. Tabelas opcionais ausentes continuam sendo lidas como vazias.

### `fut/panel/cache.py`
`PanelCache` dobra os `decision_facts` incrementalmente por chave primária, com um refresher compartilhado por todas as abas. Mantém custos vitalícios e por dia UTC, o snapshot mais recente, e a série de preços das últimas 2 h; a série é reduzida para no máximo 600 pontos apenas ao servir. Durante o cold start, `loading=true` até uma página menor que o limite chegar; o painel informa que os totais ainda estão incompletos. Um `view()` protegido por lock entrega uma cópia consistente para `build_state`.

### `fut/panel/narrate.py`
Função pura `narrate(row) -> Event | None`, com `Event = {id, ts_ms, tipo, tom, texto}`; `tom ∈ {info, bom, ruim, alerta}`. Todo o vocabulário para leigo mora neste arquivo.

| Linha no banco | Evento |
|---|---|
| `jev` com `wake=None` | nenhum (só alimenta o preço; são ~1.500/h) |
| `jev` com `wake=entry_signal` | "Jev viu chance de ALTA/QUEDA (72%)" + sufixo por `dispatch`: `dispatched` → "perguntando à LLM…"; `suppressed_inflight` → "LLM ainda ocupada"; `suppressed_cooldown` → "LLM acabou de dizer para esperar"; `suppressed_budget` → "orçamento de IA do dia acabou" |
| `jev` com `wake=exit_signal`/`reversal_signal` | "Jev acha que é hora de sair" / "Jev virou contra a posição" |
| `jev` com `gate` preenchido | "Sinal ignorado: spread alto" / "movimento esperado não paga o custo" |
| `jev_ab` | Linha observacional A/B com `variant`, `model`, `error`, `latency_ms`, `input_tokens`, `cost_usd`, `answers`, `probabilities` e `snapshot`; fica em `SILENT_KINDS`, não vira evento nem alimenta preço/snapshot/streak de falhas, mas seu `cost_usd` entra nos custos de Jev. |
| `jev` com `error` | "Jev falhou (timeout)" — tom alerta |
| `jev` com `error` | 529, `overloaded` ou `high traffic` → "Jev sobrecarregado (servidor da TypeSafe com excesso de demanda)"; qualquer outro 5xx → "Jev com erro no servidor da TypeSafe"; timeout → "Jev demorou demais para responder"; `Connection`/`connect` → "Jev sem conexão"; 429/rate → "Jev recusou por limite de uso"; 401/403 → "Jev recusou a chave de acesso". Demais erros removem URLs que casem com `https?://\S+` e o prefixo da classe de exceção antes de truncar em 80 caracteres. O evento recebe `grupo=jev_erro:<texto>` para o feed agrupar repetições sem expor identificadores técnicos. |
| `llm` `outcome=opened` | "ENTROU COMPRADO/VENDIDO a 76.150 · stop 76.020" + motivo da LLM entre aspas |
| `llm` `outcome=closed` | "LLM mandou FECHAR" + motivo |
| `llm` ação HOLD | "LLM decidiu ESPERAR" + motivo |
| `llm` `verdict=stale_timeout`/`stale_price` | "Resposta da LLM chegou tarde / preço já andou — entrada descartada" |
| `llm` `outcome=gate_*` | "Entrada barrada pela trava de risco: <regra traduzida>" |
| `llm` sem intent | "LLM não respondeu direito (<reason>)" — tom alerta |
| `exit` | "SAIU por STOP / TEMPO MÁXIMO / LIQUIDAÇÃO / funding cobrado" |
| `unmonitored` | "BOT PAROU: posição aberta sem preço" — tom alerta |

Resultado em dinheiro de cada saída vem do trade pareado (pnl − taxa de abertura − funding − taxa de fechamento), pareado por `ts_ms` no `state`, não inventado no `narrate`. Regra desconhecida cai num texto genérico que mostra o valor cru — nunca levanta exceção.

### `fut/panel/state.py`
Função pura `build_state(cache, now_ms, settings_view) -> dict`, lendo a visão consistente do `PanelCache`:

- `bot`: `{vivo: bool, ultimo_sinal_s}` — vivo se a última linha de `fut_decisions` está dentro de `max(10 s, 5 × FUT_JEV_EVERY_SECONDS)`.
- `jev`: `{ok, falhas_seguidas, desde_ms, motivo}` — saúde incremental das linhas `jev`; sucesso zera a sequência e o painel avisa a partir de três falhas seguidas.
- `preco`: `{mid, bid, ask, spread_bps, ts_ms, velho: bool}` do último snapshot.
- `posicao` (book `main`): `None` ou `{lado, entrada, stop, liq, contratos, aberto_ha_s, fecha_em_s, resultado_bps, resultado_usd}` marcado ao `mid` do último snapshot. `fecha_em_s` usa `FUT_MAX_HOLD_SECONDS` lido do ambiente do processo do painel, com padrão 300; o campo é rotulado "estimado".
- `dia` (UTC, igual ao bot): `{bruto, taxas, funding, custo_jev, custo_llm, liquido}`.
- `placar`: por carteira (`main`, `shadow:jev_only`, `shadow:random`): saldo, nº de trades, líquido. `main` com custo de IA; o rótulo explica a diferença.
- `trades`: últimas 50 operações fechadas do `main` (`entrada`, `saida`, `lado`, `motivo`, `liquido_usd`, `duracao_s`), pareando `open`→`close` como `fut/report.py::book_totals`.
- `serie`: `price_series` das últimas 2 h + marcadores `{ts_ms, preco, tipo: entrada_long|entrada_short|saida}`.

### `fut/panel/server.py`
`ThreadingHTTPServer` com `require_loopback(host)` importado de `bot/chart_server.py`. Um único refresher em background atualiza o `PanelCache` e publica bytes de estado a cada segundo (0,05 s durante o cold start); todas as abas servem os mesmos bytes cacheados em `/api/state`. Antes da primeira atualização, `/api/state` responde `{"estado": "carregando"}`. Rotas: `/`, `/api/state`, `/api/events` (`after` inteiro ≥ 0; inválido → 400). Qualquer outro caminho → 404; qualquer método além de GET → 405. `PanelDbMissing` → 200 com `{"estado": "sem_banco"}`; `PanelDbBusy` durante o refresher republica a última leitura bem-sucedida com fatos de banco cacheados, campos de relógio/cache recalculados, `banco_ocupado=true` e `banco_ocupado_desde_ms` no primeiro busy da sequência, sem outra leitura do banco; o próximo sucesso limpa esses campos. O estado cacheado continua sendo servido em `/api/state`, enquanto uma leitura busy direta de `/api/events` responde 503. A página dimma os cards, mostra `banco ocupado desde HH:MM:SS — mostrando a última leitura` e, após 60 s, o aviso vermelho `o banco não responde há mais de 1 min — confira se o bot está rodando`. Falha inesperada do refresher → `{"estado": "erro_painel", "detalhe": "<tipo>"}` e o loop continua; `PanelDbBroken` → 200 com `{"estado": "banco_invalido"}`. `Cache-Control: no-store`. Verificação de `Origin`/`Host` loopback nas rotas `/api/*` (anti DNS-rebinding), reaproveitando `_origin_is_loopback`.

### `panel/index.html`
Um arquivo, HTML/CSS/JS puros, sem CDN e sem build. Blocos: faixa de status (vivo/parado), preço grande, cartão de posição, gráfico de linha em `<canvas>` com ▲ ▼ ✕, terminalzinho (feed monoespaçado, mais novo em cima, máx. 300 linhas, cores por `tom`), placar das três carteiras, tabela de operações. Polling de 1 s; `carregando` mostra o histórico incompleto, `erro_painel` mostra aviso âmbar e data da última leitura, e dados normais com mais de 10 s mostram o painel travado em vermelho. Banco ocupado dimma os cards e avisa a duração da ocupação. Falhas Jev repetidas são agrupadas no feed e, a partir de três, aparecem sob o status; sem posição aberta, o aviso diz que novas entradas não estão sendo avaliadas, enquanto uma posição aberta mantém a frase de proteção por stop e tempo máximo. Em cinco falhas consecutivas mostra "Painel sem resposta do banco — tentando de novo…" e continua. Tema escuro, legível em tela pequena.

### `fut/cli.py`
Novo subcomando `panel [--port 8766] [--host 127.0.0.1]`. Não adquire o lock, não carrega `.env`, não cria o banco. Porta ocupada → mensagem clara e código 1.

## Testes (TDD, sem rede)

- `narrate`: tabela com um caso por linha da tabela acima + payload desconhecido/malformado não levanta.
- `state`: banco sintético — posição long/short marcada certo, dia UTC, custos de IA separados, pareamento de trades, bot vivo/parado, sem posição, sem snapshot.
- `reader`: hash do arquivo idêntico antes/depois; funciona em banco sem `kv`/carimbo de modo (prova que não usa `FutStore`); banco ausente e banco travado viram as exceções nomeadas.
- `server`: recusa host não-loopback; 404/405; `after` inválido → 400; paginação por `after`; `Origin` externo → 403; 503 quando ocupado.
- `cli`: `panel` não cria arquivo nem pega o lock.

`./scripts/test` inteiro continua verde.

## Limitações declaradas

- Preço atualiza a cada ~2,5 s (cadência do Jev), não tick a tick; com o bot flat e o WS parado o bot não grava linha `jev`, então o preço congela e a tela avisa "preço velho".
- `fecha_em_s` é estimativa: o painel não lê a configuração real do processo do bot.
- O painel mostra o banco inteiro, misturando sessões com configurações diferentes — mesma limitação do `report` (achado A4 da revisão).
- O placar herda os defeitos conhecidos das carteiras shadow até as correções do worktree `fut-review-fixes` entrarem.

## Fora de escopo

Spot, acesso fora do loopback, login, botões de ação, preço tick a tick, alertas/notificações, histórico em gráfico de candles.

## Documentação a atualizar

`CLAUDE.md` (§ Futures paper e bloco Run), `AGENTS.md` (§ Futures paper): comando, porta, somente leitura, loopback, sem conexão KCEX.
