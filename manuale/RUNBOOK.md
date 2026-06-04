# Runbook

## Obiettivo

Definire i run operativi canonici del workspace `LIVE_MLL1`.

## Flussi attesi

1. aggiornamento dati condivisi disponibili tramite `MARKET_DATA_ROOT`
2. `1_run_day.py` sulla `SD`
3. `2_run_trade_state_day.py` sulla `BD`
4. `3_build_portfolio_day.py` sulla `BD`
5. review operativa tramite app / trade console

## Wrapper Operativo Consigliato

Comando dalla root del workspace:

`venv/bin/python scripts/run_live_sd.py`

Lo script:

- chiede la `SD` con prompt
- suggerisce la prima `SD` per cui non vede screening completi
- lancia `1_run_day.py` sulla `SD`
- calcola la `BD` come seduta NYSE precedente alla `SD`
- lancia `2_run_trade_state_day.py` sulla `BD`
- lancia `3_build_portfolio_day.py` sulla `BD`
- controlla la continuita' della sequenza `BD`, permettendo il rerun di una `BD` gia' prodotta

Questo wrapper e' il flusso normale quando si opera "oggi": si processa la `SD`
appena disponibile e si aggiorna il portfolio fino alla `BD` precedente.

## Step 1 - Screening

Comando:

`venv/bin/python 1_run_day.py`

Input:

- `SD`

Output:

- `output/screening_day/YYYY/MM/YYYYMMDD/first_screen_*.csv`
- `output/screening_day/YYYY/MM/YYYYMMDD/second_screen_*.csv`

## Step 2 - Trade State

Comando:

`venv/bin/python 2_run_trade_state_day.py --buy-date YYYY-MM-DD`

Input:

- `BD`

Lo script ricava da solo:

- lo snapshot `trade_state` della seduta precedente
- le `SD` che mappano alla `BD`
- i `second_screen_passed` collegati a quelle `SD`
- le barre prezzo necessarie dalla cache locale

Output:

- `output/trade_state/YYYY/MM/YYYYMMDD/trade_state_YYYYMMDD.csv`

Lo snapshot e' completo: contiene tutti i trade noti a fine `BD`.

Lo step 2 resta lifecycle puro:

- non calcola `quantity`
- non calcola moltiplicatori
- non calcola target di partial dipendenti dalla size reale
- non produce aggregazioni portfolio

## Step 3 - Portfolio

Comando:

`venv/bin/python 3_build_portfolio_day.py --buy-date YYYY-MM-DD`

Input:

- `BD`

Responsabilita':

- leggere il `trade_state` di `BD-1` e `BD`
- calcolare moltiplicatori e size effettiva
- applicare la regola minima di 3 azioni
- calcolare il target di `PARTIAL_1` dopo avere la `quantity`
- derivare azioni BUY/SELL dal delta tra snapshot trade
- aggiornare `portfolio_positions_daily.csv`
- aggiornare `portfolio_actions_daily.csv`
- aggiornare `portfolio_state_daily.csv`
- scrivere il diagnostico `trade_sizing_YYYYMMDD.csv`

Output:

- `output/portfolio_live/full/<strategy_id>/<variant_id>/portfolio_positions_daily.csv`
- `output/portfolio_live/full/<strategy_id>/<variant_id>/portfolio_actions_daily.csv`
- `output/portfolio_live/full/<strategy_id>/<variant_id>/portfolio_state_daily.csv`

Variante di test del nuovo layer:

`portfolio_live_trade_state_2026_no_carry_in`

## App Operativa

Comando da fuori workspace:

`cd "/Users/andreacecchini/SISTEMI DI TRADING/MLL1_E21PB_LIVE" && venv/bin/streamlit run app.py --server.port 8503`

L'app espone:

- `Overview`: stato dei run e contesto generale
- `Market`: semaforo, `Blue On`, breadth e diagnostica mercato
- `First Screen`: artifact del primo screening sulla `SD`
- `Second Screen`: artifact del secondo screening, widget chart ed ETF context
- `Portfolio`: ultimo portfolio disponibile e posizioni aperte
- `Operazioni`: tabella action-level da `portfolio_actions_daily.csv`
- `Trade Console`: vista operativa ticker-level con dati, mercato, moltiplicatori, ETF filter ed entry

Nota app:

- la tab `Operazioni` mostra una riga per ogni operazione effettiva
- l'ordinamento di default e' cronologico inverso, con le piu' recenti prima
- nella `Trade Console` i moltiplicatori devono riflettere le stesse fonti usate da `3_build_portfolio_day.py`

## Stati Trade

Stati supportati da `trade_state`:

- `SKIPPED`
- `OPEN`
- `PARTIAL_1`
- `PARTIAL_2`
- `CLOSED`

`SKIPPED` indica solo un setup selezionato che non ha validato la policy di ingresso.

## Nota Operativa

Il moltiplicatore e il sizing non appartengono allo step 2. Restano nel layer portfolio.

Il P&L dopo l'acquisto non usa piu' il moltiplicatore ex ante: viene misurato ex post come cash P&L diviso `risk_amount`.
