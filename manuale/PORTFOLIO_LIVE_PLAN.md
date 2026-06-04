# Portfolio Live Plan

## Obiettivo

Costruire e aggiornare il portfolio live 2026 di `MLL1_E21PB_BLUE` senza usare dati futuri.

Il live e' un layer operativo incrementale:

- parte dal 2026 senza carry-in da anni precedenti
- usa `SD` come screen date
- usa `BD` come buy date
- separa screening, lifecycle trade e portfolio
- non legge output del layer frozen o legacy

## Pipeline Canonica

### Step 1 - Screening SD

Script:

`1_run_day.py`

Output principali:

- `output/screening_day/YYYY/MM/YYYYMMDD/first_screen_*.csv`
- `output/screening_day/YYYY/MM/YYYYMMDD/second_screen_*.csv`
- `output/screening_day/YYYY/MM/YYYYMMDD/second_screen_passed_YYYYMMDD.csv`
- `output/breadth/history/universe_breadth_daily.csv`

Responsabilita':

- selezionare i candidati alla chiusura della `SD`
- aggiornare la breadth storica usata dal moltiplicatore portfolio

### Step 2 - Trade State BD

Script:

`2_run_trade_state_day.py --buy-date YYYY-MM-DD`

Output:

`output/trade_state/YYYY/MM/YYYYMMDD/trade_state_YYYYMMDD.csv`

Responsabilita':

- creare i nuovi trade derivati dalle `SD` che mappano alla `BD`
- aggiornare i trade gia' aperti
- salvare uno snapshot completo dello stato trade a fine `BD`

Il `trade_state` non contiene sizing, moltiplicatori o aggregazioni portfolio.
Non deve calcolare target di partial che dipendono dalla `quantity`, perche' la
`quantity` nasce nello step 3 dopo moltiplicatori e regole di sizing.

Stati supportati:

- `SKIPPED`
- `OPEN`
- `PARTIAL_1`
- `PARTIAL_2`
- `CLOSED`

### Step 3 - Portfolio BD

Script:

`3_build_portfolio_day.py --buy-date YYYY-MM-DD`

Output portfolio:

- `output/portfolio_live/full/EMA21_SMA50/portfolio_live_trade_state_2026_no_carry_in/portfolio_positions_daily.csv`
- `output/portfolio_live/full/EMA21_SMA50/portfolio_live_trade_state_2026_no_carry_in/portfolio_actions_daily.csv`
- `output/portfolio_live/full/EMA21_SMA50/portfolio_live_trade_state_2026_no_carry_in/portfolio_state_daily.csv`
- yearly slice sotto `output/portfolio_live/yearly/YYYY/EMA21_SMA50/portfolio_live_trade_state_2026_no_carry_in/`

Output diagnostico per BD:

`output/trade_state/YYYY/MM/YYYYMMDD/trade_sizing_YYYYMMDD.csv`

Responsabilita':

- leggere il `trade_state` di `BD` e lo snapshot precedente
- calcolare il moltiplicatore del portfolio
- calcolare `risk_amount` e `quantity` effettiva
- applicare la regola minima di 3 azioni
- calcolare i target di partial dipendenti dalla size reale
- aggiornare posizioni, azioni e stato portfolio
- salvare il breakdown sizing in `trade_sizing`
- aggiornare il `trade_state` del giorno con i dettagli finali necessari alla diagnostica
- aggiornare il momentum portfolio per i giorni successivi

## Variante Operativa

Valori canonici correnti:

- `strategy_id = EMA21_SMA50`
- `variant_id = portfolio_live_trade_state_2026_no_carry_in`
- `portfolio_id = EMA21_SMA50__portfolio_live_trade_state_2026_no_carry_in`

Chiave posizione:

`<strategy_id>__<variant_id>__<ticker>__<entry_date_yyyymmdd>__<entry_seq>`

Regola:

- per design ci puo' essere un solo setup per `ticker + SD`
- possono esistere piu' trade sullo stesso ticker in una `BD`, se hanno `trade_id` diversi

## Moltiplicatore

Il moltiplicatore appartiene allo step 3, non allo step 2.

Formula:

`r_multiplier_final = (1.0 + slope_add + momentum_add + breadth_add) * etf_multiplier`

Componenti:

- slope: `ema21_slope_pct_5 > 0.45` sulla `SD` -> `+0.25`
- momentum: portfolio momentum inv5d top half, lag 2 sedute -> `+0.25`
- breadth p20: `above_sma20_pct < above_sma20_pct_p20` sulla `SD` -> `+0.25`
- breadth p10: `above_sma20_pct < above_sma20_pct_p10` sulla `SD` -> `+0.50` totale
- ETF recommended -> `* 1.25`
- ETF not recommended -> `* 0.50`
- ETF non disponibile -> `* 1.00`

I trade gia' aperti mantengono il `r_multiplier` originale con cui sono entrati.
Il valore corrente dei moltiplicatori puo' comparire nei diagnostici, ma non
deve modificare retroattivamente la size o il rischio di trade gia' aperti.

## Regole Di Sizing E P&L

Sizing:

- lo step 3 parte dal rischio teorico per trade
- applica `r_multiplier_final`
- determina la `quantity`
- se la `quantity` risultante e' inferiore a 3, applica la regola minima di 3 azioni

P&L:

- dopo l'acquisto il risultato non viene piu' misurato con grandezze ex ante
- realized R = `(sell_price - entry_price) * shares_sold / risk_amount`
- unrealized R = `(close_price - entry_price) * shares_open / risk_amount`
- `r_multiplier_final` resta un input di sizing e diagnostica, non un moltiplicatore diretto del P&L realizzato

## Regole Partial

`PARTIAL_1`:

- il target viene calcolato nello step 3 dopo avere la `quantity`
- formula: `entry_price + (risk_amount * FIRST_TARGET_R_MULTIPLE / quantity)`
- trigger intraday: `High >= target_partial_price`
- prezzo di vendita: `target_partial_price`, non il close
- dopo `PARTIAL_1` lo stop viene portato a break-even

`PARTIAL_2`:

- puo' avvenire dopo `PARTIAL_1`
- trigger: `Close < EMA21`
- prezzo di vendita: close della `BD`

## Sorgenti Del Moltiplicatore

Slope:

- legge `second_screen_passed_YYYYMMDD.csv` della `SD`
- usa `ema21_slope_pct_5`

Breadth:

- legge `output/breadth/history/universe_breadth_daily.csv`
- usa `above_sma20_pct`, `above_sma20_pct_p20`, `above_sma20_pct_p10` della `SD`

Momentum:

- legge `portfolio_state_daily.csv`
- scrive `output/portfolio_live/full/EMA21_SMA50/portfolio_live_trade_state_2026_no_carry_in/market/momentum_inv5d_signal.csv`
- per una `BD` usa il segnale gia' disponibile con reference date a 2 sedute precedenti
- dopo il build portfolio della `BD`, aggiorna il segnale per i giorni successivi

ETF context:

- dati e reference restano in `MARKET_DATA_ROOT`
- il workspace salva il segnale operativo in `output/etf_context/YYYY/MM/YYYYMMDD/etf_context_YYYYMMDD.csv`
- la data e' sempre la `SD`

## File E Semantica

`trade_state_YYYYMMDD.csv`:

- lifecycle puro
- full snapshot a fine `BD`
- nessun sizing

`trade_sizing_YYYYMMDD.csv`:

- breakdown del moltiplicatore per `trade_id`
- scritto dallo step 3
- vive accanto al `trade_state` della stessa `BD`

`portfolio_positions_daily.csv`:

- contiene il valore finale `r_multiplier`
- serve per open heat, esposizione e mark-to-market

`portfolio_actions_daily.csv`:

- deriva dal delta tra `trade_state_BD-1` e `trade_state_BD`
- registra BUY, SELL partial e SELL close

`portfolio_state_daily.csv`:

- aggrega equity, realized, unrealized, drawdown e posizioni aperte

## Condizione Di Completezza

La pipeline live e' pronta al lancio sequenziale 2026 quando:

- step 1, 2 e 3 girano in sequenza per ogni giorno operativo
- `trade_sizing` viene scritto per ogni `BD`
- `portfolio_positions_daily.csv`, `portfolio_actions_daily.csv` e `portfolio_state_daily.csv` restano coerenti dopo rerun incrementali
- momentum viene aggiornato dopo ogni build portfolio
- i consumer/app leggono `portfolio_positions_daily.csv` per il valore finale e `trade_sizing` per il dettaglio diagnostico
