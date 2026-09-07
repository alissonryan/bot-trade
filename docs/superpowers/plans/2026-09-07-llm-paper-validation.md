# Validação do bot autônomo com LLM — plano de execução

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Medir se o nosso bot BTC/USDT spot, com decisões discricionárias de uma LLM e risco imposto pelo código, consegue gerar lucro líquido e valor incremental frente a alternativas simples, sem enviar ordens reais.

**Architecture:** Reutilizar `Brain → Collar → PaperHands → Store` no paper e `bot/backtest.py` no diagnóstico histórico. Manter o snapshot do Rafael imutável e todos os artefatos de avaliação separados dos bancos operacionais. Histórico é evidência exploratória; o paper prospectivo no mercado KCEX é a validação principal.

**Tech Stack:** Python existente em `.venv`, pytest, SQLite, OpenRouter, dados públicos KCEX, snapshot histórico Bybit fornecido pelo usuário.

---

## Status e autorização

**Proposta para aprovação.** Este documento não inicia processos, não autoriza gastos nem altera configurações operacionais. Planejamento não equivale a execução. Não há promessa de lucro, aprovação de live ou compromisso de escala de capital.

Objetivo confirmado pelo usuário: **uma LLM operando autonomamente para gerar lucro**. Isso significa não aprovar manualmente cada trade. O código continua dono dos limites de risco, tamanho, stop, isolamento e halts; retirar essas proteções não é um experimento deste plano. SELL no spot fecha BTC pertencente ao bot; não abre short.

Não substituir a LLM pelos detectores Apex. O briefing do Rafael informa hipóteses e disciplina de avaliação, não será apresentado como estratégia implementada no bot atual.

## Decisão entre abordagens

1. **Só paper:** maior fidelidade ao ambiente atual e menor risco de conhecimento futuro, mas demora a acumular regimes e operações.
2. **Só histórico:** diagnóstico rápido e repetível, mas sujeito a diferenças de venue, execução, relógio e conhecimento prévio do modelo.
3. **Combinado — escolhido:** validar software, iniciar paper após o smoke e executar diagnóstico histórico isolado durante a observação. Não esperar terminar dois anos de backtest para começar a coletar evidência prospectiva.

## O que já existe e o que não se pode afirmar

| Superfície | Evidência lida | Consequência para a avaliação |
|---|---|---|
| `bot/brain.py::SYSTEM`, `_user_payload`, `request_body` | Prompt genérico BTC spot; últimos 20 candles Min15 em OHLC, ATR, quotes e posição; volume não enviado | Congelar este contrato em B0. Adicionar volume/indicadores/prompt Apex seria um candidato diferente, não correção silenciosa da referência. |
| `bot/collar.py::decide` | Uma posição, teto de notional, risco em código, cooldown pós-perda opcional | Reutilizar; nenhum parâmetro é aumentado para fabricar lucro. |
| `bot/hands.py::PaperHands` | Ask/bid e slippage na execução; `fee=0`; ledger persistente | Não chamar PnL de líquido de taxas sem verificar taxa KCEX. Retomar não pode recarregar caixa. |
| `bot/cli.py::_loop` | Budget diário em memória, zerado no restart | Sem supervisor com restart automático; antes de retomar, reconciliar gasto real do dia e reduzir o saldo autorizado. O teto não é durável. |
| `bot/backtest.py::CachedBrain` | Offline por padrão; cache pelo corpo da requisição e modelo; teto acumulado por cache | Cache novo pode exigir gasto. Não substituir cache miss por HOLD nem reiniciar orçamento usando outro cache. |
| `bot/backtest.py::replay` | Uma decisão por abertura Min15; stop usa low/gap; TP/TTL locais só observam aberturas Min15 | Não reproduz `CYCLE_MINUTES=5`, wake por movimento, latência ou observação tick a tick. Nem mudar `CYCLE_MINUTES` faz o replay passar a decidir a cada 5m. |
| `bot/backtest.py::compare` | B&H total/cap de ordem; 30 embaralhamentos; sensibilidade de custos com intenções fixas | São controles exploratórios, não p-values nem novas trajetórias LLM. Reaplicar custo LLM ao comparar sensibilidade econômica. |
| Auditoria runtime | Snapshot compacto e resultados; não arquiva integralmente todos os candles enviados nem a resposta bruta | Não prometer reprodução exata de prompts antigos. Congelar template/config e medir comportamento/PnL; instrumentação adicional, se necessária, deve preceder a janela e não alterar decisões. |
| Paper SL/TP/TTL | Monitoramento local depende do processo e bloqueia durante chamadas síncronas | Downtime/latência são parte do resultado operacional; não assumir stop exchange-side no paper. |
| Live M1 | Saída discricionária bloqueada por falta de evidência terminal segura; demais invariantes seguem vigentes | Paper positivo não elimina o bloqueio. Live é outro gate, fora da execução deste plano. |

A interface `python -m bot.backtest run --help` foi executada ao preparar este plano. **A suíte e os experimentos financeiros ainda não foram executados nesta etapa.** Reavaliar o código no início: há outras sessões trabalhando no projeto.

## Artefatos e isolamento

Raiz do checkout atual: `/Users/alissonryan/code/bot-trade`.

Raiz de resultados: `data/validation/llm-v1/` (local, não publicar nem commitar dados privados).

| Caminho | Responsabilidade |
|---|---|
| `manifest.json` | Versão/hash dos arquivos executados, versão Python/dependências, modelo exato, prompt/hash, Settings sem chave, regras da venue, período, autorização e orçamento. |
| `history-bybit-15m.db` | Projeção histórica separada no schema `History.bars`; nunca se passar por dados KCEX. |
| `history-manifest.json` | Proveniência, unidade de tempo, contagens, limites e hash da série projetada. |
| `historical.settings.json`, `rules-kcex.json` | Config congelada sem credenciais e regras capturadas, usadas em todos os replays. |
| `historical-decisions.db` | Um único cache para todo o orçamento histórico autorizado. |
| `historical-*.json`, `historical-*.md` | Resultados completos por janela, inclusive perdas, erros e interrupções. |
| `paper/` | Diretório de trabalho isolado do único loop paper; CLI escreve `paper/data/bot.db`, `bot.log` e `bot.lock`. |
| `daily/` | Backups SQLite consistentes, equity observada, custos, operação e resumo diário. |
| `final-report.md` | Decisão técnica/financeira, dados utilizados e limites explícitos. |

Não apagar nem reutilizar `data/bot.db`, `data/bot-live.db`, `.env`, o snapshot ou o histórico anterior. Um novo diretório não é permissão para rodar outro loop: **não pode existir paper/live concorrente**, mesmo quando os locks estão em diretórios diferentes. Antes de iniciar, inspecionar processos/terminais pelo gerenciador que os lançou. Se já houver bot ativo, não matar nem lançar segundo; resolver sua propriedade e usar a instância correta.

Antes de uma janela longa, congelar uma cópia executável da revisão avaliada, sem copiar secrets, bancos operacionais ou perfil Chrome; ou assegurar que os arquivos dessa revisão não serão alterados enquanto roda. Hash apenas de commit não basta com mudanças não commitadas. Um restart deve usar a mesma revisão e o mesmo ledger. Não compartilhar uma `.venv` sujeita a atualização durante a avaliação sem registrar/revalidar o ambiente.

## Baseline B0 proposta

Uma configuração explícita para testar a tese mínima da LLM, não otimizada por resultados:

| Campo | B0 |
|---|---|
| Mercado | `MODE=paper`, `SYMBOL=BTC_USDT`, `KCEX_TOKEN` vazio |
| Modelo | O identificador efetivamente configurado no início; registrar sem exibir a chave; não usar campeão de leaderboard como seleção posterior |
| Prompt e parser | Os atuais, congelados |
| Relógio prospectivo | `CYCLE_MINUTES=5`, `WAKE_MOVE_PCT=0.004` |
| Caixa inicial | `PAPER_STARTING_USDT=450`, somente em ledger novo |
| Tamanho | `MAX_ORDER_USDT=20`, `MAX_PORTFOLIO_PCT=0.05`, uma posição |
| Perda diária | `MAX_DAY_LOSS_USDT=20`; não desativar stop/collar |
| Stop | `ATR_PERIOD=14`, `ATR_MULT=2`, clamp 0.004–0.04 |
| Confiança mínima | `MIN_CONFIDENCE=0`, conforme default atual; medir em vez de calibrar antes dos dados |
| Slippage paper | `PAPER_SLIPPAGE_BPS=5`, além do spread bid/ask observado |
| Extensões | `TP_ATR_MULT=0`, `TIME_LIMIT_MINUTES=0`, `COOLDOWN_MINUTES=0`, `JOURNAL_ENABLED=0` |
| Decisão de saída | LLM SELL ou stop protetor; ausência de TTL deve aparecer como duração/exposição, não ser disfarçada por lucro realizado apenas |
| Tokens/JSON | Congelar valores existentes após smoke de completude; não aumentar continuamente para perseguir respostas desejadas |

As extensões ficam desligadas **por escolha experimental explícita**, não porque estejam erradas. Se alguma já estiver habilitada no uso anterior, preservar seu histórico e registrar B0 como nova configuração. B0 não pode ser vendido como equivalência bit a bit com esse uso anterior.

Não ajustar prompt, tamanho, thresholds ou modelo durante a janela confirmatória. Correção de bug de medição/segurança é permitida, mas precisa de versão, impacto e nova janela válida quando altera decisões/PnL.

## Orçamento proposto, sujeito a aprovação

- Histórico: **US$10 no total**, com piloto inicialmente limitado a **US$2**, no mesmo cache acumulativo.
- Paper: **US$1/dia UTC**, envelope de **US$30 nos primeiros 30 dias**.
- Primeira etapa completa: envelope de **US$40 em chamadas LLM**, sem ordens reais. Não autoriza 60 dias automaticamente; extensão de 30 dias requer novo envelope de US$30.
- Antes de cada fase paga, medir custo do piloto e projetar o custo total. Se não couber, interromper e renegociar escopo/orçamento; não trocar o modelo, cortar a janela após ver perdas ou inventar decisões.
- Esses são limites operacionais, **não garantias absolutas de cobrança do provedor**: custo só é conhecido após resposta, a última chamada pode exceder a reserva e erros podem não estar integralmente registrados. Conferir também o consumo do provedor, sem atribuir ao bot chamadas de outras aplicações.
- Histórico registra gasto incremental deste experimento e custo econômico das decisões separadamente. Cache hit custa zero agora, mas a decisão equivalente teria custo em produção.
- Paper: nenhuma retomada automática. Antes do restart, somar consumo atribuído ao run naquele UTC (inclusive processo anterior e falhas); autorizar somente o saldo restante. Se esgotado, não iniciar novas chamadas até o próximo UTC. Registrar o efeito de orçamento esgotado na cobertura e na capacidade de saída LLM.
- Budget esgotado, truncamento ou timeout **não é HOLD estratégico**. Se a verba não sustenta a política proposta, resultado é operacionalmente incompleto, não “estratégia conservadora”.

## Fase 1 — prontidão de software e registro da referência

**Arquivos existentes:** `bot/brain.py`, `bot/collar.py`, `bot/hands.py`, `bot/cycle.py`, `bot/cli.py`, `bot/store.py`, `bot/eye.py`, `bot/backtest.py`; testes em `tests/`.

- [ ] Registrar código efetivo e Settings seguros; nunca imprimir `.env`, token ou API key. Separar a credencial da configuração exportada. Registrar autorização monetária e data antes de qualquer chamada paga.
- [ ] Executar a suíte existente com clientes mockados e sem credenciais KCEX no ambiente:

```bash
MODE=paper KCEX_TOKEN= OPENROUTER_API_KEY= PYTHONPATH=. .venv/bin/python -m pytest tests -q
```

- [ ] Se falhar, investigar e corrigir a causa antes de usar o PnL; não remover teste de segurança nem alterar estratégia para passar. Não assumir um número fixo de testes. Após correções, executar a suíte uma vez no estado final.
- [ ] Conferir cobertura dos contratos observáveis: compra/stop/SELL paper, reinício preservando caixa/posição, exclusão mútua, paper/live, candle fechado, quote ausente/stale, custo/cache, cooldown e journal point-in-time, ausência de writes reais. Testes relevantes: `tests/bot/test_hands_paper.py`, `test_mode_isolation.py`, `test_cli.py`, `test_cycle.py`, `test_collar.py`, `test_brain.py`, `tests/test_backtest.py` e `tests/kcex/`.
- [ ] Fazer smoke descartável com `PaperHands` real e `Store` temporário: usar quotes controladas, comprar, fechar por stop e reiniciar; reconciliar caixa com fills. Isso prova execução, não capacidade de previsão. Não forçar BUY dentro da janela econômica para satisfazer um checklist.
- [ ] Consultar regras públicas KCEX, registrar data/payload público e criar `rules-kcex.json` no schema de `SymbolRules`. Não confiar automaticamente em `min_amount=1` e `fee=0` do CLI de backtest. Nunca autenticar ou enviar ordens para este passo.

**Gate:** suíte aprovada, smoke de ledger aprovado, modelo configurado, zero caminho de ordem privada no paper e custo de execução documentado. Se taxa real for não zero, `PaperHands` atual não a debita: mostrar desconto ex-post apenas como diagnóstico de trajetória fixa; **antes de chamar a trajetória de fiel ao saldo/risco líquido, corrigir o simulador com regressão e começar uma nova B0**. Não fingir que um desconto no relatório corrige as decisões que dependeriam daquele caixa/PnL.

## Fase 2 — preparar o histórico do Rafael sem alterá-lo

**Fonte:** `/Users/alissonryan/Downloads/snapshot.db`, somente leitura. **Não abrir com `Store` do bot**, pois ele executa migrations.

- [ ] Revalidar `PRAGMA quick_check`, tabela/cobertura/unidades, OHLCV, gaps e fechamento de barras. Leitura anterior: 77.304 candles Min15, de 2024-04-08 22:00 UTC a 2026-06-23 03:45 UTC, sem gaps nesse intervalo. Isso não prova completude das últimas barras nem autenticidade dos preços na venue.
- [ ] Criar a projeção descartável abaixo, a partir da raiz do checkout. Executar o bloco via Python/Eval ou script temporário; não mudar o arquivo original. Nenhum pacote novo é necessário.

```python
import hashlib
import json
import math
import sqlite3
from pathlib import Path

source = Path('/Users/alissonryan/Downloads/snapshot.db')
root = Path('data/validation/llm-v1')
root.mkdir(parents=True, exist_ok=True)
dest = root / 'history-bybit-15m.db'
manifest = root / 'history-manifest.json'
if dest.exists() or manifest.exists():
    raise RuntimeError('Artefato existente: verificar proveniência, não sobrescrever')
src = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
src.execute('PRAGMA query_only=ON')
try:
    if src.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
        raise RuntimeError('Snapshot com erro estrutural')
    rows = src.execute('''SELECT timestamp,open,high,low,close,volume
        FROM candles_BTCUSDTUSDT_15m ORDER BY timestamp''').fetchall()
finally:
    src.close()
bars = []
for ms, o, h, low, c, v in rows:
    if not isinstance(ms, int) or ms % 900_000:
        raise ValueError('Timestamp não alinhado em milissegundos')
    if not all(math.isfinite(x) for x in (o, h, low, c, v)):
        raise ValueError('OHLCV não finito')
    if not 0 < low <= min(o, c) <= max(o, c) <= h or v < 0:
        raise ValueError('OHLCV inválido')
    bars.append((ms // 1000, o, h, low, c, v))
if len(bars) < 22 or any(b[0] - a[0] != 900 for a, b in zip(bars, bars[1:])):
    raise ValueError('Histórico insuficiente ou descontínuo')
# Recortes abaixo terminam antes do fim do arquivo; últimas barras não são
# presumidas fechadas só porque foram persistidas.
with dest.open('xb'):
    pass
out = sqlite3.connect(dest)
try:
    with out:
        out.execute('CREATE TABLE bars (t INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL)')
        out.executemany('INSERT INTO bars VALUES (?,?,?,?,?,?)', bars)
    if out.execute('SELECT COUNT(*) FROM bars').fetchone()[0] != len(bars):
        raise RuntimeError('Projeção incompleta')
finally:
    out.close()
metadata = {
    'source': str(source), 'source_market': 'Bybit BTC perpetual; conforme briefing Rafael',
    'source_table': 'candles_BTCUSDTUSDT_15m',
    'source_time_unit': 'milliseconds', 'output_time_unit': 'seconds',
    'rows': len(bars), 'first_t': bars[0][0], 'last_t': bars[-1][0],
    'series_sha256': hashlib.sha256(json.dumps(bars, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
    'claim': 'Proxy histórico, não KCEX spot e não reprodução operacional de 5 minutos',
}
with manifest.open('x') as f:
    json.dump(metadata, f, indent=2)
print(json.dumps(metadata))
```

- [ ] Carregar recortes usando `History.load(start - 21*900, end)`; todos precisam de 21 candles anteriores. Recomputar ATR pelo código do bot, não copiar ATR TA-Lib do snapshot para mudar silenciosamente a estratégia.
- [ ] Não adicionar OI/liquidações/indicadores ao prompt B0. São dados disponíveis para candidatos posteriores; suas coberturas são menores e joins exigiriam as-of/recebimento, não apenas timestamp do evento.
- [ ] Registrar dois vieses irreduzíveis neste diagnóstico: futuros Bybit versus spot KCEX; e possível conhecimento do período no treinamento da LLM. Janela cronológica reservada impede ajuste do pesquisador, mas **não elimina vazamento de pré-treinamento**.

## Fase 3 — diagnóstico histórico com a LLM real

Sem treinamento/fine-tuning. Sem gerar sinais futuros e chamá-los de cache. Cada requisição deve usar apenas as barras anteriores; snapshot inclui posição/caixa resultantes daquela trajetória.

**Janelas pré-definidas (UTC; fim exclusivo):**

| Janela | Início → fim | Decisões Min15 |
|---|---|---:|
| Piloto de custo/parser | 2026-06-01 → 2026-06-02 | 96 |
| Diagnóstico A | 2024-05-01 → 2024-05-31 | 2.880 |
| Diagnóstico B | 2025-01-01 → 2025-01-31 | 2.880 |
| Diagnóstico C | 2026-05-01 → 2026-05-31 | 2.880 |
| Reservada ao encerramento do diagnóstico | 2026-06-02 → 2026-06-22 | 1.920 |

Total previsto: **10.656 decisões**, antes de retries/interrupções. Não executar a seleção completa se a projeção do piloto exceder o envelope. As datas não foram selecionadas pela rentabilidade da LLM. Cada janela começa com o mesmo caixa e encerra contabilmente a posição final; reportar essa liquidação `end_of_data` separada dos exits naturais. Não concatenar janelas como se fossem uma conta contínua.

- [ ] Exportar `historical.settings.json` via `Settings` com os valores B0, remover `openrouter_api_key`; identificar por hash. Relógio do replay continua Min15, mesmo com configuração de 5 minutos registrada.
- [ ] Depois da aprovação do gasto, executar o piloto com o comando existente. A chamada carrega a credencial local sem imprimi-la:

```bash
MODE=paper KCEX_TOKEN= PYTHONPATH=. .venv/bin/python -m bot.backtest run \
  --start 2026-06-01 --end 2026-06-02 \
  --history data/validation/llm-v1/history-bybit-15m.db \
  --cache data/validation/llm-v1/historical-decisions.db \
  --config data/validation/llm-v1/historical.settings.json \
  --rules data/validation/llm-v1/rules-kcex.json \
  --spread-bps 1 --slippage-bps 5 \
  --output data/validation/llm-v1/historical-pilot.json \
  --report data/validation/llm-v1/historical-pilot.md \
  --load-env --allow-network --max-cost-usd 2
```

- [ ] Verificar 96 decisões completas, falhas nomeadas, custo efetivo, parser e runtime. Projetar 10.656 × custo médio e também um cenário conservador de custo; o forecast não deve assumir só respostas válidas ou que futuras posições geram prompts idênticos.
- [ ] Reexecutar o mesmo piloto **sem `--allow-network` e sem `--load-env`**, com a config salva e a mesma variável de chave vazia. Resultado econômico deve reproduzir; gasto novo deve ser zero. Diferenças de arquivo ligadas a tempo/gasto desta execução não são diferenças de estratégia.
- [ ] Se o envelope comportar as outras janelas, repetir o comando por linha da tabela alterando período e nomes de saída; conservar o MESMO cache e elevar seu teto acumulado para 10. Não usar cinco caches com US$10 cada. Se não comportar, registrar bloqueio financeiro e solicitar alteração antes de prosseguir.
- [ ] Registrar PnL líquido de execução, `net_after_llm_usdt`, PF, drawdown, exposição, duração, motivos de HOLD e exits. Spread de 1 bp é cenário modelado, não medição histórica da Bybit/KCEX. Sensibilidades de spread 1/3/5/10 bps e slippage 0/2/5 bps existem; acrescentar cenário de stress de slippage 10 bps usando `replay` com intenções fixas e rotulá-lo como ablação de execução.
- [ ] Corrigir no relatório a comparação econômica: `compare` usa intenções fixas que carregam custo LLM zero. Para isolar custos de execução, subtrair de cada sensibilidade o custo LLM original. Se mudar memória/prompt/snapshots de forma que altere o raciocínio, precisa de nova trajetória com chamadas próprias; não reaproveitar intenções como prova de aprendizado.
- [ ] Usar B&H total e B&H limitado ao capital efetivamente permitido para uma ordem na abertura (`min(20, 0.05*450)`, sujeito a preço/taxa/precisão). B&H total é contexto, não risco equivalente. Embaralhamento de ações é diagnóstico condicional de timing, não prova estatística contra o acaso.

**Gate histórico:** resultado completo, contabilmente consistente e robustez descrita. Lucro é sinal favorável exploratório; prejuízo não prova que o loop de 5m KCEX falhará, pois não são a mesma execução. Nada aqui aprova live. Custos que inviabilizem economicamente B0 também são resultado útil.

## Fase 4 — iniciar paper prospectivo e smoke real

Pode começar assim que Fase 1 e autorização financeira terminarem; não depende da conclusão das chamadas históricas. Um único candidato ativo; replay não instancia `LiveHands`, não toca o ledger paper e tem orçamento separado.

- [ ] Confirmar ausência de outro paper/live. Se usar Orca para terminais, ler a skill versionada e identificar a sessão exata. Serviços precisam de gerenciador de processos (`hub start` ou gerenciador Orca), nunca execução solta com `&`.
- [ ] Preparar diretório paper novo, config B0 e referência executável congelada. CLI usa `data/` relativo ao cwd e não possui `--db`: não inventar flag nem apenas trocar `PAPER_STARTING_USDT` para zerar saldo.
- [ ] No gerenciador, definir cwd como `/Users/alissonryan/code/bot-trade/data/validation/llm-v1/paper`, executável `.venv/bin/python` absoluto da revisão congelada, `PYTHONPATH` absoluto correspondente e argumentos `-m bot run --once`. Injetar B0 por ambiente, `MODE=paper`, `KCEX_TOKEN=` e `LLM_DAILY_BUDGET_USD=1`. Carregar chave OpenRouter sem logar seu valor. Não depender dos defaults mutáveis de outro `.env`.
- [ ] Inspecionar exit code, uma decisão real auditada, preço/ATR válidos, custo e regra. HOLD pode ser sucesso operacional; `llm_parse`, `llm_truncated`, `llm_budget` ou `llm_config` não são aprovação do smoke da LLM.
- [ ] Em smoke separado de produção econômica, observar entrada/saída/restart pelos cenários controlados da Fase 1. Não exigir que o mercado produza BUY no `--once` nem mandar ordem fictícia para o ledger da avaliação.
- [ ] Reconciliar custo do `--once` antes de iniciar `-m bot run` no mesmo ledger; não zerar caixa nem esquecer custo de chamada. Se iniciou posição no smoke real, preservá-la e incluir desde a primeira decisão no T0 econômico.
- [ ] Observar vários ciclos, persistência e conexão pública. Início de processo não prova saúde. Registrar PID/handle verificado, T0 UTC, caminho do banco e revisão. Não declarar “rodando” só porque o launcher aceitou.
- [ ] Manter máquina acordada e conectada. O plano não assume que fechar notebook preserva monitoramento. Registrar downtime e reinício; não migrar automaticamente para cloud/pagar infraestrutura.

**Gate das primeiras 24h:** ciclo autônomo com quotes atuais, respostas válidas e custos atribuídos, ledger reconciliado, zero ordem real, nenhuma alteração humana de trades. Se só houver HOLD genuíno, o motor pode estar operacional, mas lucro permanece não demonstrado.

## Fase 5 — observar e contabilizar 30 dias, estender somente com decisão

**Revisões:** após 24h, 7 dias, 14 dias e 30 dias. Revisões diagnosticam saúde e custo, não retunam parâmetros. Mais 30 dias confirmatórios são recomendados se houver sinal favorável; orçamento adicional é separado.

- [ ] Fazer backup consistente via SQLite backup API, não copiar arquivo vivo ignorando WAL. Abrir a origem com `mode=ro`; nenhum avaliador deve instanciar `Store` sobre banco ativo.
- [ ] Registrar diariamente: caixa, quantidade BTC, preço de marcação executável, horário do quote, patrimônio, PnL, custo LLM acumulado, quantidade de chamadas/erros, motivos de bloqueio, volume negociado, stops/SELLs, tempo de posição, períodos sem observação e intervenções.
- [ ] Não usar só `SUM(fills.pnl)`: isso oculta posições perdedoras abertas. Patrimônio = caixa + BTC × bid com custo estimado de saída. PnL econômico = variação desse patrimônio − taxas ainda não debitadas − todas as chamadas LLM atribuídas − custos incrementais de infraestrutura/dados. Spread/slippage já embutidos nos fills não podem ser descontados duas vezes. Explicitar aproximação USD≈USDT.
- [ ] No fim do período, não emitir SELL apenas para mostrar resultado: marcar posição aberta pelo preço executável, incluindo custo de saída e idade; mostrar também cenário de liquidação contábil. O loop e seus sinais continuam intactos até decisão de encerramento.
- [ ] Reconciliar `paper_cash + posição` contra entradas/saídas desde T0. Preservar timestamps e fills legados do run; não recarregar conta após perdas. Run começa com ledger limpo e nunca apaga história.
- [ ] Custo LLM: somar `payload.llm.cost_usd` e, se vier a existir candidato com reflexão, `payload.reflection.cost_usd`; conferir cobranças de erros no provedor. Um log ausente não transforma cobrança em zero.
- [ ] Distinguir HOLD intencional, BUY bloqueado pelo collar e HOLD por falha. Usar `rule`, `llm.reason`, `exec_error` e `eye` da auditoria. Taxa de respostas válidas = respostas parseadas / chamadas efetivamente tentadas, não / ticks do loop.
- [ ] Reportar drawdown **amostrado**, com a resolução disponível. A auditoria de decisão não prova drawdown máximo intratick nem uptime de 99% sozinha. Não preencher períodos sem observação como mercado parado/execução perfeita.
- [ ] Comparar com caixa sem negociar e B&H (20 USDT e 450 USDT, restante em caixa quando aplicável) desde o mesmo T0, quotes e regras. Calcular lucro absoluto e retorno sobre 450; não usar retorno dividido por 20 como retorno da carteira. Comparar risco/exposição; B&H total não é condição binária de aprovação.

Consultas rápidas existentes (diagnóstico, não relatório econômico completo):

```sql
SELECT COUNT(*) AS fills, SUM(CASE WHEN side='SELL' THEN pnl ELSE 0 END) AS realized_pnl
FROM fills;

SELECT rule, COUNT(*) AS n FROM audit GROUP BY rule ORDER BY n DESC;

SELECT json_extract(payload,'$.llm.reason') AS reason, COUNT(*) AS n,
       SUM(COALESCE(json_extract(payload,'$.llm.cost_usd'),0)) AS llm_cost
FROM audit GROUP BY reason ORDER BY n DESC;

SELECT ts, side, qty, price, pnl, source FROM fills ORDER BY id DESC LIMIT 20;
SELECT * FROM position;
SELECT value FROM kv WHERE key='paper_cash';
```

## Decisão pré-registrada

Separar três conclusões: **software funciona**, **houve lucro no período**, **há evidência de vantagem replicável**. Uma não implica a próxima.

### Falha de medição/operação

Zero tolerância a ordens reais, mistura paper/live, ledger sem conciliação, entradas duplicadas, apagamento de perdas ou versões não identificadas. Corrigir causa, manter registro e repetir o período afetado sem esconder o original. Cobrança incompatível com budget ou orçamento esgotado recorrente impede chamar o plano de operação contínua validada.

Como meta operacional proposta, no mínimo 99% das tentativas de decisão devem resultar em resposta válida e ausência de buracos de observação deve ser demonstrada. É SLO escolhido, não propriedade provada. Qualquer outage relevante com posição aberta deve ser destacado mesmo se a meta agregada passar; downtime adverso não pode ser excluído para melhorar PnL.

### Candidato promissor, ainda sem aprovação live

- PnL econômico positivo, incluindo posição aberta e todos os custos atribuídos.
- Resultado não explicado apenas por uma operação: mostrar concentração no maior trade e resultado removendo-o como análise de sensibilidade, sem apagar esse trade do resultado oficial.
- Comparação favorável ou justificável por menor risco frente aos controles com exposição compatível; caixa e buy-and-hold têm custos LLM zero.
- Pelo menos 30 dias observados para primeira revisão financeira; **60 dias e 60 operações encerradas naturalmente** são o piso proposto para discutir consistência, não prova automática nem incentivo a forçar trades. Poucas operações implicam continuar/inconclusivo.
- Para alegar evidência estatística, usar diferenças de PnL diário contra o controle pré-definido de B&H limitado ao cap e intervalo de incerteza por blocos temporais (não trades independentes). Fixar bootstrap circular de blocos de 7 dias, 10.000 reamostragens, seed 20260907 e intervalo percentil 95%; apresentar também sensibilidade com blocos de 3/14 dias. A amostra pode ser insuficiente; limite inferior não positivo significa **vantagem não demonstrada**, mesmo com lucro observado. Não usar o melhor bloco como novo resultado oficial.
- Janela confirmatória de 30 dias posteriores, sem retune, deve ser reportada separadamente; persistir perdas e posição ao atravessar o marco. Nenhum intervalo estatístico remove risco de mudança de regime ou viés de seleção.

### Não aprovado / inconclusivo

- Lucro bruto positivo, econômico negativo: não satisfaz objetivo do usuário.
- Empate com comprar/manter: não demonstrou que pagar a LLM agrega valor; ainda pode merecer análise de risco, não narrativa de alpha.
- Só HOLD, poucos trades ou regime único: inconclusivo.
- Falha em um histórico proxy: diagnóstico, não sentença sobre o loop prospectivo.
- Queda econômica >5% do caixa inicial (USDT22,50 com B0) ou consumo acima da autorização: checkpoint de interrupção do experimento e investigação, sem relaxar limites/recarregar conta. Esse é limite externo de pesquisa, não novo mecanismo live; a precisão da intervenção depende da supervisão disponível e deve ser informada.

## Fase 6 — um challenger apenas se B0 mostrar problema concreto

- [ ] Formular uma hipótese a partir dos dados, não da vontade de acrescentar features: exemplo, custo por decisão inviável, dados insuficientes ou exits inadequados. Escolher **uma** mudança: frequência, payload, prompt, modelo ou extensão opt-in — não todas.
- [ ] Preservar B0 e pré-registrar challenger, data e orçamento antes do resultado. Não selecionar o vencedor entre dezenas de prompts no mesmo período e chamar isso de confirmação.
- [ ] Comparar trajetórias independentes com a mesma informação temporal e regras. Memória/reflexão exige novas decisões condicionadas ao próprio histórico; intenções fixas não testam aprendizado.
- [ ] Não lançar múltiplos CLIs paper/live contornando locks. Um eventual champion/challenger prospectivo com feed comum e ledgers separados exige desenho explícito; não está implementado nem autorizado por este plano inicial. Até lá, usar replay controlado ou novas janelas versionadas, reconhecendo que comparação sequencial confunde regimes.

## Gate separado para live — fora do escopo desta execução

Mesmo com paper lucrativo: capturar e validar evidências KCEX de cancelamento/execução, resolver M1 sem remover seus bloqueios, verificar semântica de quantity/amount e preços/fees reais, revisar isolamento por conta e demais invariantes, testar recuperação e só então pedir autorização específica para probe na quantidade mínima da venue. Não assumir que 20 USDT é o mínimo atual. Aumento de capital/alavancagem não compensa ausência de edge.

## Entrega final e revisão deste plano

- [ ] Entregar comandos executados, contagem/resultados dos testes, hashes/configs, cobertura do snapshot, todos os resultados por janela, custos reais versus estimados, prova do loop paper e relatório em 24h/7d/14d/30d.
- [ ] Marcar claramente o que foi efetivamente observado, simulado, inferido e o que ficou incompleto. Nenhuma claim de aprovação baseada apenas em processo iniciado, número de dias ou saldo realizado.
- [ ] Após o smoke, remover apenas scripts descartáveis criados para o experimento que não precisem ser preservados como receita de reprodução; conservar ledger/cache/resultados e versões perdedoras.
- [ ] Aprovação do usuário antes de execução paga. Primeira ação após aprovação: Fase 1, não ligar live nem rodar o snapshot inteiro na API.
