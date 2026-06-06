# Output Structure

## Regola base

- input condivisi: fuori dal workspace, via `MARKET_DATA_ROOT`
- output del workspace: dentro `output/`

## Struttura target

- `output/screening_day/`
  - output dello step 1, organizzati per `SD`
- `output/trade_state/`
  - snapshot completi del lifecycle trade e sizing diagnostico, organizzati per `BD`
- `output/etf_context/`
  - contesto ETF operativo, organizzato per `SD`
- `output/portfolio_live/`
  - stato continuo del portfolio live 2026
- `output/trade_console/`
  - payload, snapshot e file di supporto operativo
- `output/app/`
  - cache leggere o file derivati dell'app
- `output/logs/`
  - log dei run e dei refresh

## Fuori da output

- `INSIGHTS/`
  - diario, insight, note evolutive del workspace
- `archivio/`
  - backup e snapshot storici fuori dal flusso operativo corrente

## Distinzione chiave

- `screening_day` = selezione candidati sulla `SD`
- `trade_state` = stato finale dei trade alla chiusura della `BD`
- `etf_context` = segnale ETF context materializzato sulla `SD`
- `portfolio_live` = stato continuo del libro con sizing e aggregazioni

## ETF Context

Path canonico:

`output/etf_context/YYYY/MM/YYYYMMDD/etf_context_YYYYMMDD.csv`

Regola:

- la data nel path e nel filename e' sempre la `SD`
- il layer dati/reference resta fuori dal workspace in `MARKET_DATA_ROOT`
- il file nel workspace e' il segnale operativo gia' calcolato per la pipeline live

## Trade State

Path canonico:

`output/trade_state/YYYY/MM/YYYYMMDD/trade_state_YYYYMMDD.csv`

Regola:

- la data nel path e nel filename e' sempre la `BD`
- il file e' un full snapshot
- include trade nuovi, trade gia' aperti, trade chiusi e trade skipped noti a fine `BD`

Path sizing diagnostico:

`output/trade_state/YYYY/MM/YYYYMMDD/trade_sizing_YYYYMMDD.csv`

Regola:

- la data nel path e nel filename e' sempre la `BD`
- viene scritto dallo step 3
- contiene breakdown di moltiplicatori, rischio, quantity e diagnostica di sizing
- non sostituisce `portfolio_positions_daily.csv`, che resta la fonte operativa aggregata del portfolio

Stati:

- `SKIPPED`
- `OPEN`
- `PARTIAL_1`
- `PARTIAL_2`
- `CLOSED`

## Legacy

Il precedente `output/trading_day/` e' legacy e non fa piu' parte del flusso live canonico.

I backup operativi locali vanno salvati in:

`archivio/`

Anche il vecchio `trade_timeline` legacy e la vecchia variante portfolio lunga
sono stati archiviati e non sono piu' fonti operative della pipeline live nuova.
