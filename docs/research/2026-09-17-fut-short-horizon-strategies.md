# Estratégias de curto horizonte para o paper de futuros KCEX (Jev + LLM)

**Data:** 2026-09-17  
**Escopo:** pesquisa apenas. Nenhum código foi alterado, nenhum commit, nenhuma ordem, `.env` não foi lido.  
**Objeto:** o que pessoas de fato usam e publicam para perpétuos de cripto em **segundos a minutos**, e o que dá para implementar **agora** junto com o Jev no loop `fut/`.  
**Método:** cinco pesquisadores em paralelo (microestrutura, filtros de taxa, LLM/agentes, repositórios, evidência negativa) + buscas em X, Firecrawl, GitHub, arXiv/SSRN, Reddit e docs do TypeSafe. Preferência por fontes primárias. Toda afirmação abaixo traz URL e tipo de evidência.

**Tipo de evidência (rótulo):**
- **acadêmico-backtest** — paper com dados e métrica
- **live** — ordens reais ou paper com fills no book ao vivo
- **anecdótico** — post, diário, Discord
- **hype** — curso, afiliado, “bot que imprime”

---

## Resumo executivo

A primeira hora de paper (limiar 0,5; 29 trades, 6 vencedores; 16 holds ≤ 30 s **todos** perdedores; p50 da LLM 3,9 s; sombras `jev_only` e `random` também no vermelho) não é um bug de prompt. É o resultado esperado de um **taker** que paga ~2–6 bps ida e volta num horizonte em que o RMS de BTC em 30 s é da ordem do próprio custo.

A literatura de microestrutura é forte em **explicar o movimento do mesmo intervalo** (OFI, imbalance de fila, TFI/CVD). É fraca em **pagar um taker depois da taxa** nos próximos 10–60 s. O único padrão público que sobrevive ao contato com o book é: **modelo rápido no caminho quente, código dono do risco, pouquíssimas entradas, movimento esperado ≫ custo, saídas mecânicas**. O Jev encaixa no caminho quente. A LLM de 4 s, como decisora direcional, não.

### Top 5 — impacto esperado / esforço para *este* loop

| # | Ideia | Impacto | Esforço | Por quê agora |
|---|---|---|---|---|
| 1 | **Portão de custo no código:** não entrar se o movimento esperado < 2–3× o custo round-trip; cap de frequência; spread largo = HOLD | alto | baixo | Identidade aritmética + Albers (live Binance) + a nossa hora. Independente do cérebro. |
| 2 | **Persistência do Jev:** só acordar a LLM após N respostas iguais do mesmo lado + usar `regime` (já existe e **não entra** em `should_wake`) | alto | baixo | trade-jev (15 dias NQ): cada resposta isolada é ruído; 4 iguais + cutoff 0,7 vira sinal. Overfit explícito — testar, não copiar o 4. |
| 3 | **OFI de *eventos* (CKS) + TFI/CVD em 10 s**, não só imbalance estático de profundidade | médio | médio | Temos book incremental. Silantyev (BitMEX XBTUSD): em cripto o **fluxo de trades** explica melhor que o OFI do book em 1 s. Feature para o Jev, não alpha de taker. |
| 4 | **Microprice / mid ponderado vs mid** como “já está caro pagar o ask?” | médio | baixo | Stoikov / hftbacktest. Um número no snapshot; o collar recusa LONG se `ask − microprice` já come o orçamento. |
| 5 | **LLM como veto, não como decisor de segundos;** prompt com invalidação; magnitudes no código | médio | médio | DXAP (live): LLM sem edge direcional. jev-trader: Jev sozinho + **post-only**. Nossa p50 3,9 s come o horizonte de 60 s. |

**O que não está no top 5 de propósito:** market making / post-only (fora de escopo da spec; KCEX maker 0% é isca se o fill for tóxico — Albers); funding como alpha de 60 s; cascatas de liquidação como entrada; VPIN; debate multi-agente a cada 2 s; DeepSeek “ganhou o Alpha Arena”.

---

## 0. O loop que temos e o que a primeira hora mostrou

Arquitetura (spec `docs/superpowers/specs/2026-09-17-kcex-futures-paper-jev-llm-design.md`, código `fut/`):

```
WS 2 s → MarketState.snapshot → Jev (questions.py) → should_wake
       → LLM LONG/SHORT/CLOSE/HOLD (~4 s) → collar.check → ledger taker
sombra: flat / jev_only / random nos mesmos ticks
```

Sinais já no snapshot (`fut/market.py`): bid/ask/mid, `spread_bps`, imbalance estático nas bandas 5/10/25 bps, retornos 2/10/60/300 s, fluxo agressor 30 s e 120 s (buy/sell/CVD/VWAP), ATR 1 m, fair/index, funding.

Perguntas Jev (`fut/questions.py`): `direction_60s` (up/down/flat), `move_beats_cost`, `flow_aligned`, `regime` (trend/range/volatile), `exit_now` com posição. **Acordar a LLM hoje:** flat → direção ≠ flat **e** `direction_conf ≥ limiar` **e** `beats_cost ≥ limiar`. Com posição → `exit_now` ou reversão. `regime` **não participa**. Min hold (60 s) só silencia exit/reversão; stop, liq e max hold (5 min) continuam.

Custo: taker 1 bp/lado no contrato KCEX + slippage ~1–2 bps → **~2–6 bps RT**. LLM p50 3,9 s. Descarte de LONG/SHORT se o mid andar > 5 bps entre disparo e resposta.

**Aritmética que manda no resto do relatório.** Com custo \(C\) e alvo bruto \(W=L\):

\[
p^* = \tfrac12 + \frac{C}{2W}
\]

| Alvo bruto \(W=L\) | \(C=4\) bps | \(C=6\) bps |
|---|---|---|
| 5 bps | 90% | impossível |
| 10 bps | 70% | 80% |
| 20 bps | 60% | 65% |
| 30 bps | 57% | 60% |

RMS de BTC ~0,9 bp/s (vol ~50% a.a.): **30 s ≈ 4,9 bps**, **60 s ≈ 6,9 bps**. Um scalp de 30 s como taker tem movimento típico **da ordem do custo**. Os 16/16 perdedores ≤ 30 s são o caso base, não um azar.

---

## Ideia 1 — Portão de custo no código (quando *não* operar)

### O que é

Três regras **no collar / `should_wake`**, não no prompt:

1. **Spread.** Recusar LONG/SHORT se `(ask−bid)/mid` ≥ \(X\) bps (começar em 2–3; calibrar no Hub). Taker paga o spread duas vezes.
2. **Vol vs custo.** Recusar se `ATR_1m × √(hold_min)` < \(2C\) com \(C≈5\) bps. Fita quieta + scalp taker = pagar o book por ruído.
3. **Frequência.** Teto duro (ex.: 1 entrada nova / 5–15 min, ou N/hora). 29 RT/h × 5 bps ≈ **145 bps/h** de arrasto só de taxa.

O min-hold de 60 s **não substitui** isso: em 60 s o RMS ≈ \(C\). Ele só impede o “mudei de ideia em 8 s”. Stop/liq/max-hold continuam livres.

### Evidência

| Fonte | Tipo | O que diz |
|---|---|---|
| Albers, Cucuringu, Howison, Shestopaloff, *To Make, or to Take*, arXiv:2502.18625 (v1 2025-02-25; HTML 2026-08-24). Live Binance BTCUSDT perp, 232 897 ordens maker tamanho mínimo, 12–19 fev 2024. https://arxiv.org/html/2502.18625v1 | **live** | Taker de imbalance, hold ~15 s: **−1,96 bp/trade** (n=18 927), **líquido da taxa VIP 1,5 bp**. Maker ingênuo também perde (−0,43). Fila da frente −0,06 bp de markout vs **fundo da fila −0,78 bp**. Princípio: *facilidade de prever × facilidade de explorar < c*. |
| Kim & Hansen, pulso de 15 min em perps Binance, 2021-01-01→2024-10-31. Cobertura 2026-09-06/07: https://www.techflowpost.com/en-US/article/33789 | **acadêmico-backtest** | +26% trades / +32% volume nos primeiros 10 s de :00/:15/:30/:45. Gross **+0,51 bp**. Taker Binance **5 bp**. Previsível ≠ negociável. |
| Ernie Chan, 2013-10-24. http://epchan.blogspot.com/2013/10/how-useful-is-order-flow-and-vpin.html | **anecdótico** (practitioner) | Order flow em ES “often too small to overcome transaction costs”; precisa ~1,3 bp de lucro *depois* de ~2 bp RT + 0,3 bp de taxa. |
| Nossa hora de paper (limiar 0,5) | **live paper** | 16 holds ≤ 30 s, 16 perdas. Sombras `jev_only` e `random` também perdem → o **custo**, não só o cérebro. |
| Pomegra, 2026-08-16. https://pomegra.io/learn/library/track-e-trading-risk/technical-analysis/chapter-14-what-doesnt-work-and-the-data/transaction-costs-and-edge | **anecdótico** (regra) | Edge ≥ 2–3× custo. 1× não deixa erro de estimação. |
| @Trader_Bran, 2026-07-07. https://x.com/Trader_Bran/status/2074522230667522202 | **anecdótico** | “scalping Crypto makes zero sense cause the fees eat you alive” |
| @Ayden Cheers, 2026-05-10. https://x.com/biglad8963/status/2053613050746589596 | **anecdótico** | 90 dias paper multi-asset: TA scalp sem edge em 5 m/15 m/1 h. “Fees eat the asymmetry.” Próxima iteração: book + regime. |

### Mapeamento no código

| Arquivo | Mudança |
|---|---|
| `fut/collar.py` | Depois de `stale`/`already_open`, **antes** de sizing: `spread_too_wide`, `vol_lt_cost`. CLOSE continua passando sempre. |
| `fut/questions.py` `should_wake` | Não acordar `entry_signal` se o snapshot já falharia o portão (economiza LLM). |
| `fut/settings.py` | `FUT_MAX_SPREAD_BPS`, `FUT_MIN_MOVE_MULT` (default 2), `FUT_MAX_ENTRIES_PER_HOUR`. |
| `fut/shadow.py` | As sombras herdam o mesmo collar — senão a comparação mente. |

**Não** pedir isso ao Jev: magnitudes e aritmética são o ponto cego documentado do modelo (`docs.typesafe.ai/model-jaggedness/jev-1.13.md`, 2026-09-16).

### Custo / latência

Zero HTTP extra. Uma comparação no snapshot que já existe. Latência ≈ 0.

### Como medir contra as sombras

Mesmos ticks, mesmo collar. Relatar por trade: hold_s, fee_bps, slip_bps, MFE_bps, MAE_bps, `fee_share = fees / |gross|`. Sucesso da ideia 1: **queda forte de N trades/hora** e **média líquida dos que restam > média de `random`**. Se `random` continuar menos ruim que a LLM, o cérebro tem edge negativo e o filtro só atrasa o sangramento.

---

## Ideia 2 — Persistência do Jev + `regime` no wake

### O que é

Não acordar a LLM numa única leitura. Exigir **N ciclos consecutivos** (ex. 3–4 × 2 s ≈ 6–8 s) com o mesmo lado, `direction_conf` e `beats_cost` acima do limiar, e `regime != range` (ou `regime=trend` com confiança alta).

Isso é exatamente o que o TypeSafe pede: perguntas atômicas, composição **no código** (`docs.typesafe.ai`, Confidence + primitives, consultado 2026-09-17).

### Evidência

| Fonte | Tipo | O que diz |
|---|---|---|
| justinhe16/trade-jev `results/FINDINGS.md`, 2026-09-17. https://github.com/justinhe16/trade-jev/blob/main/results/FINDINGS.md | **acadêmico-backtest** (Jev real, NQ L10, 15 dias, 22 401 chamadas, US$ 0,91) | Agir em **toda** resposta: **−US$ 128 590**, 6 941 trades. Com cutoff 0,7 + **4 respostas iguais** + stop 200 / alvo 100 ticks: **+US$ 20 795**, 12/15 dias, 178 trades. Imbalance rule −21 820; random −11 230. **Os filtros foram escolhidos entre 1 920 settings nos mesmos 15 dias** — lead, não edge provado. Autores: “not a calculator”; flip BUY/SELL snapshot a snapshot. |
| TypeSafe Confidence. https://docs.typesafe.ai/confidence | **vendor** | Confiança ≠ probabilidade. Três faixas: alto → agir; médio → cautela; baixo → não agir. Transferência (entrada) pede limiar maior que leitura. Noul **não** traz `confidence`. |
| DXAP, *What LLM Trading Agents Actually Do in Production*, arXiv:2609.05663, 2026-09-04. https://arxiv.org/abs/2609.05663 | **live** | Restrição > contexto extra. Bracket mecânico +39 bps/posição. Memória/reflexão ρ=−0,20 vs PnL. |
| Nossa spec | — | `regime` já é perguntado e **ignorado** em `should_wake`. |

### Mapeamento no código

| Arquivo | Mudança |
|---|---|
| `fut/questions.py` | Estado de persistência (contador de lado). `should_wake`: `entry_signal` só se `streak >= N` e `regime` ∈ {trend, volatile} com `regime_conf` (se Choice expuser). Range → não acorda. |
| `fut/jev.py` | Passar `regime` e probs no `JevVerdict` (se ainda não). |
| Prompt `fut/llm.py` | Dizer o *streak* e o regime. “Não perseguir spike de 1 minuto.” |

**Não** fundir as cinco perguntas numa só “devemos operar?”. TypeSafe: decompor; combinar no código.

### Custo / latência

Zero. O Jev já avalia as cinco em paralelo. N=4 atrasa o wake em ~6–8 s — barato comparado com 3,9 s da LLM, e corta wakes falsos.

### Como medir

Replay das linhas `jev` já gravadas (como trade-jev: **uma** rodada paga, **muitos** settings). Grade: N ∈ {1,2,3,4,6}, limiar ∈ {0,50, 0,55, 0,65, 0,70}, regime on/off. Registrar em hold-out (dias novos). Comparar main vs `jev_only` vs `random` **com o mesmo N**. Se só o N=4 escolhido in-sample ganha, é o overfit do FINDINGS.md.

---

## Ideia 3 — OFI de eventos (CKS) + TFI/CVD em 10 s

### O que é

Hoje o snapshot manda **imbalance estático** \((Q_b-Q_a)/(Q_b+Q_a)\) nas bandas 5/10/25 bps — OBI, não OFI. Cont–Kukanov–Stoikov (2014) definem OFI como a **soma assinada das mudanças do book** (melhoria de preço, Δ tamanho, recuo), inclusive cancels. CVD/TFI é o fluxo de *trades* agressores.

Em **ações**, OFI ≫ TFI no mesmo bucket. Em **perp de BTC**, o contrário aparece em 1 s: o book é raso e cheio de cancel.

### Evidência

| Fonte | Tipo | Números |
|---|---|---|
| Cont, Kukanov, Stoikov, *J. Fin. Econometrics* 12(1):47–88, 2014. Preprint arXiv:1011.6402. https://arxiv.org/abs/1011.6402 | **acadêmico-backtest** (NYSE, 50 ações) | Relação **linear contemporânea** \(\Delta P = \beta\,\mathrm{OFI}\), \(\beta \propto 1/\mathrm{depth}\). \(R^2\) médio ~65% em buckets de 10 s. **Não é previsão.** |
| Cont, Cucuringu, Zhang, arXiv:2112.13213v4, 13 jun 2023. https://arxiv.org/html/2112.13213v4 | **acadêmico-backtest** (Nasdaq top 100, 2017–2019) | OFI integrado (vários níveis) sobe o \(R^2\) **do mesmo minuto**. Forward 1 min OOS: \(R^2\) **≈ −0,37** (OFI próprio). PnL “econômico” **ignora taxas**. |
| Silantyev, *Digital Finance* 1:191–218, 2019 (BitMEX XBTUSD). https://doi.org/10.1007/s42521-019-00007-w · Medium 2018-05-04 https://medium.com/@eliquinox/order-flow-analysis-of-cryptocurrency-markets-b479a0216ad8 | **acadêmico-backtest** (crypto) | 1 s: TFI \(R^2\) **12,8%** > OFI **7,1%**. 10 s: OFI 40,5% vs TFI 37,3%. ≥ 10 s TFI vence de novo (1 h TFI 75% vs OFI ~55%). Book cripto: ~4,9 updates L1/s vs ES 57,7. |
| Kolm, Turiel, Westray, *Math. Finance* 2023 | **acadêmico-backtest** | Redes em order-flow batem book cru; horizonte efetivo **~2 mudanças de preço**. |
| Kethan S E, SSRN 7053198, 2026 (independente, não revisado) | **acadêmico-backtest** (fraco) | 10 s OFI walk-forward: IC +0,0044, Sharpe bruto +0,98, **líquido −1,73**; custo 164× o edge bruto. |
| Albers et al. 2025 (acima) | **live** | Imbalance **é** previsível e **não** paga taker. |
| Gould & Bonart, arXiv:1512.03492 | **acadêmico-backtest** | Imbalance de **fila** prevê o **próximo tick**, não 5 min. Célula forte = large-tick. BTC perp é small-tick relativo ao ATR. |
| CVD em X / YouTube | **hype / anecdótico** | @CounterScalp 2026-08-11: “se tiver que escolher só 1 seria CVD”. Sem backtest. Kalena: ~58% em divergência horária. r/OrderFlow_Trading: “CVD useless”. |

**Conclusão honesta:** calcular OFI/TFI **vale** como estado para o Jev e como *gate* (“livro a favor do lado”). **Não** vale como regra taker “OFI>0 → LONG”. O \(R^2\) famoso é do **mesmo** intervalo. O \(R^2\) à frente de 1 min é negativo. Em cripto, **TFI 10 s** é o objeto mais honesto.

### Fórmulas (do book incremental que `kcex/fws.py` já mantém)

OFI CKS no melhor bid/ask, entre snapshots consecutivos \((n-1,n)\):

```
e_bid = +q_bid_n          se bid sobe
        +Δq_bid           se bid igual
        −q_bid_{n-1}      se bid recua

e_ask = −q_ask_n          se ask desce (melhora)
        −Δq_ask           se ask igual
        +q_ask_{n-1}      se ask recua (piora)

OFI_h = soma (e_bid − e_ask) no intervalo h
```

TFI/CVD_h = soma `side × vol` dos deals em h (já temos 30 s/120 s; **falta 10 s**).

**REST snapshot não é OFI CKS.** Só o WS incremental.

### Mapeamento no código

| Arquivo | Mudança |
|---|---|
| `fut/market.py` | Acumular OFI L1 a cada apply do book; janelas 2 s e 10 s. Flow window extra **10 s**. Microprice barato: `(ask·q_bid + bid·q_ask)/(q_bid+q_ask)`. |
| `fut/questions.py` `jev_state` | `ofi_10s`, `tfi_10s`, `cvd_10s`, `microprice_minus_mid_bps`. Buckets nomeados (`ofi_sign`, `tfi_agree`) — Jev não é calculadora. |
| `fut/questions.py` | Noul extra (latência ~0, perguntas em paralelo): `flow_and_book_agree` = sinal(OFI_10s)=sinal(TFI_10s)=sinal(returns_10s). Compor em `should_wake`. |
| Código de referência | https://github.com/sauloduttra/ofi-signal (MIT, fórmula limpa, LOB **sintético** — copiar a fórmula, não o \(R^2\) 3261×). https://github.com/nicolezattarin/LOB-feature-analysis (Apache-2.0, OFI multinível em ações). |

### Custo / latência

CPU local no apply do WS. Sem HTTP. Jev: mais um Noul, “adding questions barely changes the response time” (TypeSafe).

### Como medir

Para cada wake e para cada snapshot 2 s, gravar \(x_t \to r_{(t,t+f]}\) com \(f\in\{2,10,60,300\}\) s, **líquido de taxa+slip**. Se \(R^2\) líquido ≤ 0, o sinal fica como *contexto* do Jev, não como regra de entrada. Sombra extra opcional `shadow:ofi_taker` (entra quando OFI e TFI 10 s concordam) vs `jev_only`.

---

## Ideia 4 — Microprice / mid ponderado como “o take já está caro?”

### O que é

Mid = média do touch. Mid ponderado / VAMP = preço que pesa as filas:

\[
m^w = \frac{q_a P_b + q_b P_a}{q_b+q_a}
\]

Stoikov (*The Micro-Price*, QF 2018, SSRN 2970694): o microprice é um martingale do mid futuro dado \((I, \mathrm{spread})\). hftbacktest usa OBI padronizado para **enviesar quotes maker**, com rebate −0,5 bp — não para tomar.

Uso **nosso** (taker): se vamos comprar no ask, a distância `ask − m^w` já é parte do custo. Se isso + taxa ≥ movimento esperado, não entra.

### Evidência

| Fonte | Tipo | Nota |
|---|---|---|
| Stoikov, SSRN 2970694; GitHub https://github.com/sstoikov/microprice (★ ~478, congelado 2021) | **acadêmico-backtest** (ações) | Melhor que mid e weighted-mid *in-sample* em nomes US. PDF QF paywall — fórmula da palestra 2020: https://www.youtube.com/watch?v=0ZHypIAxYNo |
| hftbacktest tutorial “Market Making with Alpha — Order Book Imbalance”, consultado 2026-09-17. https://hftbacktest.readthedocs.io/en/latest/tutorials/Market%20Making%20with%20Alpha%20-%20Order%20Book%20Imbalance.html | **acadêmico-backtest** (Binance BTCUSDT, maker) | OBI padronizado + rebate −0,5 bp / taker +7 bp. May 2023: ReturnOverTrade **1,39 bp**. Feb 2025: **0,86 bp** (inclui rebate 0,5). Autor: “highlights the importance of rebates”. **Não replica como taker.** |
| aligrithm.com 2026-08-28, “Imbalance Is the MM's Optimal Response, Not Alpha” | **secundário** | Stoikov chamou imbalance de “worst-kept secret”: o MM **posta** o book torto porque já viu o preço eficiente. Observar \(I\) e tomar o ask é comprar o inventory do MM. |

### Mapeamento no código

| Arquivo | Mudança |
|---|---|
| `fut/market.py` | `microprice` (weighted mid L1; VAMP nas bandas 5/10 bps se o book estiver sincronizado). |
| `fut/collar.py` | `take_too_rich`: LONG se `(ask − microprice)/mid × 1e4 + taker_fee_bps > expected_move_bps`. |
| `fut/questions.py` | Estado: `micro_minus_mid_bps`. |

Calibração completa de Stoikov (grade \(s,I\)) é esforço alto e dados US; o proxy L1 basta para a v1.

### Custo / latência

Zero. Como medir: distribuição de `ask − micro` nos fills LONG vs HOLD recusados; PnL líquido dos que passariam o filtro vs todos.

---

## Ideia 5 — LLM como veto; Jev (e o código) no caminho quente

### O que é

Hoje: Jev filtra → LLM **decide** LONG/SHORT/CLOSE/HOLD com p50 3,9 s. A spec já descarta LONG/SHORT atrasados ou com mid andado > 5 bps; CLOSE nunca.

A evidência pública diz: **LLM direcional em segundos não tem edge**; o que paga (quando paga) é **sair mecânico** e **operar raro**. Dois rearranjos compatíveis com a spec, em ordem de ousadia:

**A (prompt + collar, sem mudar papéis).** Manter LLM como decisor, mas: (i) exigir invalidação em prosa que o código **não** executa — só disciplina o modelo; (ii) “não fechar discricionário perto do stop”; (iii) “não perseguir o último print”; (iv) limiar de entrada da LLM **maior** que o de CLOSE.

**B (inverter, ainda paper).** Jev escolhe o lado (como `jev_only`, mas com os filtros 1–2). LLM só pode **HOLD** (veto) ou **CLOSE**. Nunca inicia. Isso corta a latência de entrada de 3,9 s para a do Jev (~100 ms–2 s). CLOSE continua podendo esperar a LLM.

### Evidência

| Fonte | Tipo | Resultado |
|---|---|---|
| jev-trader, github.com/jarrodwatts/jev-trader, commit 2026-09-17 “Post-only limit orders… earn the spread instead of paying it”. ★ ~356, MIT | **live demo** | Um Choice Jev por bloco Monad (~300 ms). **Não tenta ser lucrativo** (SPEC). Trocou IOC taker por **post-only**. Late → hold, sem quote. p50 loop ~100 ms. Estado: mid, spread, imbalance, depth, returns, CVD. |
| DXAP arXiv:2609.05663, 2026-09-04 | **live** | ~100 agentes Hyperliquid. **Sem edge direcional.** Win rate ~41% vs dummy-down 50%. Bracket 2%/4% **+39 bps**. Modelos frontier **indistinguíveis** em 416 cenários. Alavancagem 5× em todo sextil de vol. 43% das posições veem +300 bps MFE; 49% dessas fecham vermelhas. |
| TradeRank, 2026-09-13. https://www.traderank.ai/llm-for-trading | **live paper** | 56 modelos, 9 temporadas, 2 826 trades. 46,2% das temporadas lucrativas. Rank **não persiste** (ρ ≈ 0,03). Concordância entre LLMs ≠ sinal. |
| Nof1 Alpha Arena, out–nov 2025, Hyperliquid US$ 10k. https://nof1.ai/blog/TechPost1 · https://www.crypto-news.net/deepseek-leads-nof1-ai-crypto-trading-contest-with-22-profit/ (2025-11-03) | **live**, n pequeno, uma fita | Organizador: “PnL was dominated by trading costs in early runs as agents over-traded and took quick, tiny gains that fees erased.” Gemini: centenas de trades, taxas >10% do capital, −60%. DeepSeek/Qwen lideraram com **poucas** posições longas, horas de hold, 10–20× — **não** scalp de 30 s. Rankings viraram no meio. **Hype se citado como prova de scalp.** |
| CryptoTrade, EMNLP 2024, arXiv:2407.09546 | **acadêmico-backtest** (diário) | Agente LLM **bate time-series, não MACD**. |
| QuantHarness arXiv:2509.09995 | **hype no título** | “HFT” avaliado em **1 h e 4 h**. |
| Survey agêntico arXiv:2605.19337, 2026-05 | **survey** | 19 estudos closed-loop: 2 com split temporal, **1 com custos**, 0 reprodutível. |
| Cheng et al., SSRN 6713620, 2026-05 | **acadêmico** | “Reasoning dividend vs deliberation tax.” Agente só na *julgamento*; unwind compilado. |
| Brett Harrison, 2026-09-14. https://x.com/BrettHarrison/status/2099491644173074470 | **anecdótico** | LLM TTFT 200 ms–2 s; resposta 2–30 s. HFT 1–50 µs. LLM “not fast enough… except longer-horizon”. |
| Dan Piechowsi, 2026-09-17. https://x.com/DanPiechowski/status/2100599420895027629 | **anecdótico** | “não entendo como um modelo (mesmo rápido como Jev) pode ser melhor em swing de milissegundo do que um algoritmo afinado.” |
| kojott/LLM-trader-test | **anecdótico** | DeepSeek em **15 m**, hierarquia 4 h/1 h/15 m. Aposentou ruído de 3 m. Avisos de drenar carteira. |

### Mapeamento no código

| Arquivo | Variante A (agora) | Variante B (se A não mover o PnL) |
|---|---|---|
| `fut/llm.py` `SYSTEM` | Incluir custo RT em bps, “HOLD se o movimento esperado < 2× custo”, “não perseguir spike”, “não sugerir tamanho/alavancagem”, “CLOSE só se a tese quebrou”. | LLM só vê posição aberta; ações permitidas CLOSE/HOLD. |
| `fut/questions.py` | Entrada: limiar ≥ 0,65–0,70; CLOSE: 0,55. | `should_wake` de entrada dispara **execução Jev** (já é `jev_only`); LLM só em `exit_signal`. |
| `fut/dispatch.py` | Manter descarte stale_price; considerar apertar 5 bps → ~2 bps (½ \(C\)). | Entrada sem esperar LLM. |
| `fut/collar.py` | Ignorar qualquer coisa no `reason` que pareça tamanho. | Igual. |

### Custo / latência

A: mesmo custo de LLM, menos wakes se combinada com ideia 2. B: **menos** chamadas LLM (só saídas), latência de entrada = Jev. Risco de B: virar `jev_only`, que já perde — só faz sentido **depois** das ideias 1–2 no mesmo `jev_only`.

### Como medir

A/B no paper com o mesmo dia: `main` (LLM decisor) vs `jev_gated` (filtros 1–2, LLM veto) vs `jev_only` vs `random`. Critério de edge da spec **não muda**. Métricas extras: taxa de `stale_price`/`stale_timeout`, Brier de `direction_60s` vs mid em t+60 s (como btc-jev-signal), PnL condicional em `llm.action != jev_side`.

---

## Outros sinais (não no top 5, mas honestos)

**Funding / basis.** @systematicls 2025-11-22 (https://x.com/systematicls/status/1992072241589457328): funding é **prêmio de risco** por ser contraparte, não glitch; basis explode em cascata e o 0,03%/8 h vira arredondamento. Horizonte: horas. No loop: estado de *crowding* (`fair_minus_last_bps`, `funding_rate` já vão ao Jev). Não acordar por funding extremo. **Não** montar o arb spot/perp (fora de escopo, várias venues, ADL).

**Liquidação.** Cascata é feedback **depois** que começou (Garcia Seuma arXiv:2608.03616). Usar como “não fadear o flush”, não como trigger de 5 min. Sem feed de liq da KCEX capturado, não inventar.

**Intensidade de trades.** Silantyev: o book cripto é esparso. z-score de \(N_{10s}\) como *gate* (não operar fita morta; não fadear burst). Hawkes é overkill.

**VPIN.** Easley–López de Prado–O’Hara (RFS 2012) vs Andersen–Bondarenko (JFM 2014): VPIN é em grande parte mecânico em volume/vol e **não** antecipou o flash crash. Não construir regra taker nisso.

---

## Tabela de repositórios

Stars e last-push em **2026-09-17** (GitHub API / páginas). Preferir MIT/Apache. AGPL/GPL/LGPL assinalados.

| Nome | URL | ★ | Atividade | Linguagem | Reusar | Licença |
|---|---|---|---|---|---|---|
| hftbacktest | https://github.com/nkaz001/hftbacktest | 4,7k | push 2025-12-23 | Rust + Python/Numba | Melhor backtest L2/L3 cripto: fila, latência, notebook OBI. **Ideias, não o engine** no `fut/` | MIT |
| Hummingbot | https://github.com/hummingbot/hummingbot | 20,0k | 2026-09-17 | Python | MM live (PMM, inventory). Não é HFT de fila | Apache-2.0 |
| NautilusTrader | https://github.com/nautechsystems/nautilus_trader | 29,1k | 2026-09-17 | Rust+Python | Engine de eventos tick; overkill | LGPL-3.0 |
| jev-trader | https://github.com/jarrodwatts/jev-trader | 356 | 2026-09-17 | TypeScript | Loop Jev 300 ms, post-only, late=hold, estado compacto | MIT |
| trade-jev | https://github.com/justinhe16/trade-jev | 1 | 2026-09-17 | Python | **Achado mais acionável:** persistência + cutoff. Replay de respostas | MIT |
| tardis-node | https://github.com/tardis-dev/tardis-node | 367 | 2026-09-17 | TypeScript | Replay L2 incremental | MPL-2.0 |
| tardis-python | https://github.com/tardis-dev/tardis-python | 148 | 2026-08-23 | Python | CSV/replay; histórico pago | MPL-2.0 |
| tardis-machine | https://github.com/tardis-dev/tardis-machine | 311 | 2026-08-23 | TypeScript | Cache local L2 | MPL-2.0 |
| Cryptofeed | https://github.com/bmoscon/cryptofeed | 2,9k | 2026-09-14 | Python | WS L2 normalizado. **Não copiar** (AGPL) | AGPL-3.0 |
| sstoikov/microprice | https://github.com/sstoikov/microprice | 478 | 2021-01-10 | Jupyter | Notebook canônico; ações; congelado | sem licença clara |
| LOB-feature-analysis | https://github.com/nicolezattarin/LOB-feature-analysis | 277 | 2022-04-12 | Python | `ofi_computation.py` CKS multinível | Apache-2.0 |
| ofi-signal | https://github.com/sauloduttra/ofi-signal | 0 | 2026-05-23 | Python | Fórmula CKS limpa; LOB sintético | MIT |
| crypto-lob-data-pipeline | https://github.com/kostyafarber/crypto-lob-data-pipeline | 25 | 2022-08-10 | Python | Deribit→Kafka→OFI; abandonado | MIT |
| Freqtrade | https://github.com/freqtrade/freqtrade | 54,5k | 2026-09-17 | Python | Candle/minutos. **Não é este horizonte** | GPL-3.0 |
| CCXT | https://github.com/ccxt/ccxt | 44,0k | 2026-09-17 | multi | REST/WS unificado. **Proibido substituir `kcex/`** | MIT |
| Jesse | https://github.com/jesse-ai/jesse | 8,5k | 2026-09-14 | Python | Backtest OHLCV, anti look-ahead | MIT |
| btc-jev-signal | https://github.com/WebGrga/btc-jev-signal | pequeno | 2026 | — | Forecast Jev 15 m–1 d, **não opera**; Brier. Bom KPI | — |
| kojott/LLM-trader-test | https://github.com/kojott/LLM-trader-test | — | ongoing | — | DeepSeek 15 m, não 2 s | — |

**Hype / pular:** `caspian-alpha-hft-microstructure-tracker` (0★, tabelas µs); dezenas de “HFT engine” de 0★ gerados por LLM. Freqtrade/Jesse/OctoBot = candles.

---

## O que **não** fazer (evidência negativa)

1. **Scalp taker de 5–10 bps / hold ≤ 30 s.** Identidade + Albers + nossa hora + 16/16. BE WR 70–100%.
2. **Achar que 60 s de min-hold cria edge.** Só mata o pior churn. RMS(60 s) ≈ \(C\).
3. **Subir alavancagem para “valer 5 bps”.** Taxa e slip escalam com nocional. A spec já cap 3×.
4. **Acordar a LLM porque o Jev “está acordado”.** Kim–Hansen: pulso real de 0,51 bp gross.
5. **Maker “para não pagar taxa” sem fila da frente e cancel em toxicidade.** Albers: maker ingênuo perde mesmo com rebate. jev-trader só foi para post-only **depois** de pagar o spread. Spec v1: **sem** limite/maker.
6. **Segunda ordem residente GE / OCO.** Spec: OCO não comprovado na KCEX; duas triggers no mesmo BTC podem vender as moedas do dono.
7. **OFI/CVD contemporâneo como previsão.** \(R^2\) do mesmo bar; forward 1 min negativo (CCZ). Book é teatro (Hasbrouck–Saar; spoof em 140 ms — Kalena, interessada no produto DOM).
8. **LLM raciocinando (DeepSeek-R1 CoT) no loop de 2 s.** TTFT.
9. **Debate multi-agente / notícia / FinGPT no caminho de segundos.** CryptoTrade é diário. DXAP: ferramentas extra não pagam.
10. **Copiar “DeepSeek +22–48% no Alpha Arena”.** Uma temporada, fita long, holds de **horas**, taxas enormes nos perdedores. TradeRank: rank não persiste.
11. **Treinar segundo juiz LLM.** Fora de escopo; DXAP: liga de modelos nula.
12. **Backtest em candle 1 m sem spread e sem delay de 4 s.** Chan (QuantCon 2015): o mesmo sistema morre no BBO.
13. **VPIN, “CVD é o único indicador”, Discord de order-flow pago.**
14. **CCXT no lugar de `kcex/`.** Travado no AGENTS.md.
15. **Aumentar frequência para “ter amostra”.** Quantopian 2018: arrasto linear no turnover. Graham Capital 2017: velocidades rápidas pagam 4–5× o custo das lentas.

**Censos (não cripto, mas o melhor dataset de day-trade varejo):**

- Barber, Lee, Liu, Odean, *J. Financial Markets* 2014: **< 1%** dos day traders de Taiwan com lucro anormal previsível líquido de taxa. https://www.sciencedirect.com/science/article/abs/pii/S1386418113000190
- Chague, De-Losso, Giovannetti, SSRN 3423101: **97%** dos que persistiram >300 dias no mini-Ibovespa perderam. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101

**Mesma estratégia, só muda a taxa (crypto):**

- Athayde / PANews 2026-01-12: ETH 15 m, 0,02% maker **+47%** → 0,06% taker **−14%**. https://www.panewslab.com/zh/articles/19807b56-e556-4b46-86d2-80b76aef2ddc
- Gate 2026-03-17: BTC 5 m mean-reversion, 36 008 trades, Sharpe 1,22 → **−13,10** com taxa+slip.

**Diários:** r/CryptoCurrency 2024-09-14 — scalp 1 m +14 USDT bruto, líquido negativo. r/MEXC 2025-01-20 — +US$ 4 146 de PnL, **−US$ 6 619** de taxa, 90% market BTC.

---

## Como medir (protocolo, sem mudar o critério de edge)

O critério da spec permanece: ≥ 200 trades, ≥ 14 dias, Jev real (não mock), líquido > 0 depois de taxa/funding/slip/Jev/LLM, **ganhar de flat / jev_only / random**, IC95 inferior do PnL/trade > 0, nenhum dia abaixo do day-loss.

Para **estas** ideias, no paper:

1. **Replay das linhas `jev` já gravadas** (padrão trade-jev): N, limiar, regime, portão de spread — sem gastar Jev de novo.
2. **Sombra extra opcional** `cost_gated` = `jev_only` + ideia 1. Se nem isso bater `flat`, não há o que a LLM salve.
3. Por trade: hold_s, fee_bps, MFE, MAE, `stale_*`, lado Jev vs lado LLM vs fill.
4. **Calibração Jev:** Brier/log-loss de `direction_60s` vs mid em t+60 s, congelado no instante da pergunta (btc-jev-signal). Não retreinar no meio da janela de edge.
5. Relatório: `python -m fut report` + fatia hold ≤ 30 s / 30–60 / 60–300.

Prior da literatura para um taker de segundos: **esperado líquido < 0**, a menos que o N de trades desabe e o win médio ≫ 15–30 bps.

---

## Lista completa de fontes

### Papers / preprints

- Cont, Kukanov, Stoikov (2014), *The Price Impact of Order Book Events*. arXiv:1011.6402. https://arxiv.org/abs/1011.6402
- Cont, Cucuringu, Zhang (2023), *Cross-Impact of Order Flow Imbalance in Equity Markets*. arXiv:2112.13213v4. https://arxiv.org/html/2112.13213v4
- Xu, Gould, Howison (2020), *Multi-Level Order-Flow Imbalance*. arXiv:1907.06230
- Gould & Bonart (2016), *Queue Imbalance as a One-Tick-Ahead Price Predictor*. arXiv:1512.03492
- Stoikov (2018), *The Micro-Price*. SSRN 2970694. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2970694
- Silantyev (2019), *Order flow analysis of cryptocurrency markets*. *Digital Finance* 1:191–218. https://doi.org/10.1007/s42521-019-00007-w
- Albers, Cucuringu, Howison, Shestopaloff (2025), *To Make, or to Take*. arXiv:2502.18625. https://arxiv.org/html/2502.18625v1
- Kolm, Turiel, Westray (2023), *Deep Order Flow Imbalance*. *Mathematical Finance*
- Barber, Lee, Liu, Odean (2014), *The cross-section of speculator skill*. *J. Financial Markets*. https://www.sciencedirect.com/science/article/abs/pii/S1386418113000190
- Chague, De-Losso, Giovannetti, *Day Trading for a Living?*. SSRN 3423101. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101
- Easley, López de Prado, O’Hara (2012), *Flow Toxicity…*. *RFS*
- Andersen & Bondarenko (2014), *VPIN and the Flash Crash*. *J. Financial Markets*
- DXAP (2026-09-04), *What LLM Trading Agents Actually Do in Production*. arXiv:2609.05663. https://arxiv.org/abs/2609.05663
- CryptoTrade (EMNLP 2024). arXiv:2407.09546. https://arxiv.org/abs/2407.09546
- FinGPT. arXiv:2306.06031
- TradingGPT. arXiv:2309.03736
- TradingAgents. arXiv:2412.20138
- QuantHarness. arXiv:2509.09995
- Survey agêntico. arXiv:2605.19337
- Cheng et al. (2026-05), *Agents Are Not Algorithms*. SSRN 6713620
- Garcia Seuma (2026), liquidation cascade. arXiv:2608.03616
- Kethan S E (2026), SSRN 7053198 — independente, tratar com ceticismo
- Kim & Hansen, pulso 15 min Binance — cobertura https://www.techflowpost.com/en-US/article/33789 (2026-09-07)

### Docs e código

- TypeSafe: https://docs.typesafe.ai · https://docs.typesafe.ai/confidence · https://docs.typesafe.ai/model-jaggedness/jev-1.13.md (2026-09-16)
- jev-trader: https://github.com/jarrodwatts/jev-trader
- trade-jev FINDINGS: https://github.com/justinhe16/trade-jev/blob/main/results/FINDINGS.md
- hftbacktest OBI: https://hftbacktest.readthedocs.io/en/latest/tutorials/Market%20Making%20with%20Alpha%20-%20Order%20Book%20Imbalance.html
- ofi-signal: https://github.com/sauloduttra/ofi-signal
- Spec local: `docs/superpowers/specs/2026-09-17-kcex-futures-paper-jev-llm-design.md`
- Código local: `fut/questions.py`, `fut/loop.py`, `fut/market.py`, `fut/collar.py`, `fut/llm.py`

### Blogs / practitioner (não afiliado, ou aritmética reutilizável)

- Chan 2013-10-24: http://epchan.blogspot.com/2013/10/how-useful-is-order-flow-and-vpin.html
- Chan 2012-11 (comentários 2014): http://epchan.blogspot.com/2012/11/the-importance-of-2-as-sharpe-ratio.html
- Quantopian webinar 2018-03-01: https://www.youtube.com/watch?v=LAUKOjZvvQQ
- Graham Capital, *Transaction Costs*, jul 2017: https://www.grahamcapital.com/wp-content/uploads/2023/08/Transaction-Costs_GCM-Research-Note_Jul-17.pdf
- Quant Memo CKS, 2026-07-13: https://quantmemo.com/writing/paper-cont-kukanov-stoikov-order-flow-imbalance
- Silantyev Medium, 2018-05-04: https://medium.com/@eliquinox/order-flow-analysis-of-cryptocurrency-markets-b479a0216ad8
- unCoded, 2026-05-24 (vende bot; a conta de taxa é independente): https://uncoded.ch/blogs/signal-noise-the-1-minute-chart-fee-trap
- DYOR Academy, 2026-05-10: https://dyor.net/academy/en/strategies/scalping-crypto
- Multicoin, adverse selection, 2026-02-17: https://multicoin.capital/2026/02/17/adverse-selection-rules-everything-around-me/
- MLQuants, MM Binance, 2025-08-11: https://mlquants.substack.com/p/empirical-notes-3-market-making-in
- TradeRank: https://www.traderank.ai/llm-for-trading
- Nof1: https://nof1.ai · https://nof1.ai/blog/TechPost1
- PANews taxas 2026-01-12: https://www.panewslab.com/zh/articles/19807b56-e556-4b46-86d2-80b76aef2ddc
- Gate slip 2026-03-17: https://www.gate.com/news/detail/slippage-the-most-underestimated-profit-killer-in-trading-19535894

### Reddit

- r/algotrading 2020-01-14: https://www.reddit.com/r/algotrading/comments/eokhra/do_fees_make_trading_on_small_time_frames/
- r/algotrading 2022-05-22: https://www.reddit.com/r/algotrading/comments/uv8bke/my_strategy_is_only_profitable_when_i_dont/
- r/algorithmictrading 2025-10-27: https://www.reddit.com/r/algorithmictrading/comments/1ohgs1f/trading_strategy_obliterated_by_fees/
- r/algotrading 2026-01-31 fee tiers: https://www.reddit.com/r/algotrading/comments/1qrnl7k/why_fee_tiers_matter_a_case_study
- r/CryptoCurrency 2024-09-14: https://www.reddit.com/r/CryptoCurrency/comments/1fgj0r3/is_1m_scalping_possible_in_crypto_or_are_the_fees/
- r/MEXC_official 2025-01-20: https://www.reddit.com/r/MEXC_official/comments/1i5fwcn/watch_out_for_market_vs_limit_orders/
- r/Daytrading 2025-03 (scalping doesn’t work): https://www.reddit.com/r/Daytrading/comments/1jd8u0m/scalping_doesnt_work_for_most_people_heres_why/

### X / Twitter (consultado 2026-09-17)

- @systematicls 2025-11-22 funding: https://x.com/systematicls/status/1992072241589457328
- @Trader_Bran 2026-07-07: https://x.com/Trader_Bran/status/2074522230667522202
- @biglad8963 2026-05-10: https://x.com/biglad8963/status/2053613050746589596
- @BrettHarrison 2026-09-14: https://x.com/BrettHarrison/status/2099491644173074470
- @DanPiechowski 2026-09-17: https://x.com/DanPiechowski/status/2100599420895027629
- @jarrodwatts / jev-trader buzz 2026-09-17 (vários posts “Jev trading bots so hot”) — **hype de produto**, não P&L
- CVD/OBI em X (ScalpX, CounterScalp, Whale Liquidity) — **anecdótico / Discord**; não usados como prova de edge

### Hype (citado para marcar)

- Kalena DOM/CVD/spoofing (produto)
- Trade Reclaim, OneKey, LiquidView, Coinperps (afiliado; a *conta* de bps é reutilizável)
- Alpha Arena como prova de scalp de segundos
- “Best Bitcoin scalping 2026” / GPTrader 300% ROI

---

*Fim da pesquisa. Nada disto autoriza live. O critério de edge da spec continua o único juiz.*
