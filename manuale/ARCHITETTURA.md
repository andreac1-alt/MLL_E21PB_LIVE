# Architettura

## Perimetro

`LIVE_MLL1` e' un workspace dedicato esclusivamente all'operativita' live di
`MLL1_E21PB_BLUE`.

Vincoli:

- focus temporale operativo: `2026`
- nessun layer frozen
- nessun laboratorio strategico multi-variante
- nessun uso di output prodotti da altri workspace

## Principi

- dati esterni condivisi letti da `MARKET_DATA_ROOT`
- output prodotti salvati internamente nel workspace
- semantica temporale esplicita: `SD -> BD`
- `SD` = screen date, cioe' la seduta appena chiusa usata per lo screening
- `BD` = buy date, cioe' la seduta operativa in cui il setup viene valutato
- separazione netta tra screening, lifecycle del trade e portfolio

## Blocchi principali

- `1_run_day.py`
- `2_run_trade_state_day.py`
- `3_build_portfolio_day.py`
- `scripts/run_live_sd.py`
- `scripts/run_live_bd.py`
- `run_day.py`
- `app.py`
- `core/trade_state.py`
- `core/portfolio/`
- `core/trade_console/`
- `core/market/`

## Pipeline Live

La pipeline live deve funzionare senza conoscere il futuro.

Alla chiusura di una `SD`:

- si lancia `1_run_day.py`
- vengono prodotti `first_screen` e `second_screen`
- i candidati finali sono nel `second_screen_passed`

Alla chiusura di una `BD`:

- si lancia `2_run_trade_state_day.py --buy-date YYYY-MM-DD`
- vengono aggiornati i trade gia' aperti
- vengono creati i nuovi trade derivati dalle `SD` che mappano a quella `BD`
- viene salvato uno snapshot completo del `trade_state`

Il portfolio legge il `trade_state` e applica sizing, moltiplicatore e viste aggregate.

Il wrapper operativo principale e' `scripts/run_live_sd.py`:

- chiede la `SD`
- propone la prima `SD` senza screening completi
- esegue lo screening sulla `SD`
- calcola la `BD` precedente alla `SD`
- aggiorna trade state e portfolio sulla `BD`
- verifica la continuita' della sequenza

Per rilanciare solo portfolio/trade state su una `BD` specifica resta disponibile
`scripts/run_live_bd.py`.

La prima variante operativa del nuovo layer e':

`portfolio_live_trade_state_2026_no_carry_in`

## Layer Legacy

Il vecchio layer `trading_day` e gli script `run_trading_day.py` /
`run_trading_day_live.py` sono stati archiviati in:

`output/archivio/20260602_pipeline_legacy/`

Motivo:

- erano utili per ricostruzioni ex-post
- non erano adatti a una pipeline live incrementale
- potevano usare implicitamente dati successivi alla chiusura operativa della `BD`

## Direzione

Il workspace deve evolvere come sistema operativo di supporto al trading reale:

- screening
- lifecycle incrementale dei trade
- aggiornamento portfolio live
- trade console
- app minimale dedicata

## App

L'app locale `app.py` e' una shell Streamlit operativa.

Comando standard:

`cd "/Users/andreacecchini/SISTEMI DI TRADING/MLL1_E21PB_LIVE" && venv/bin/streamlit run app.py --server.port 8503`

Tabs principali correnti:

- `Overview`
- `Market`
- `First Screen`
- `Second Screen`
- `Portfolio`
- `Operazioni`
- `Trade Console`

Responsabilita' principali:

- `Overview`: stato generale del workspace live
- `Market`: semaforo, `Blue On`, breadth e contesto mercato sulla `SD`
- `First Screen` e `Second Screen`: lettura degli artifact `output/screening_day/`
- `Portfolio`: stato portfolio e posizioni aperte
- `Operazioni`: una riga per ogni action di `portfolio_actions_daily.csv`, in ordine cronologico inverso
- `Trade Console`: console ticker-level per verifica baseline, mercato, moltiplicatori, ETF filter ed entry

La `Trade Console` deve restare allineata alla pipeline live:

- usa `SD` come data di screening e ricava la `BD`
- legge `screening_day`, `etf_context`, `trade_state` e `portfolio_live`
- i moltiplicatori mostrati devono usare le stesse fonti canoniche dello step 3 portfolio
