# Prompt Standard Per Nuova Chat Codex

Usa questo prompt quando inizi una nuova chat Codex nel workspace:

```text
Siamo nel workspace:
/Users/andreacecchini/SISTEMI DI TRADING/MLL1_E21PB_LIVE

Obiettivo del workspace:
costruire e mantenere la pipeline operativa live 2026 per MLL1_E21PB_BLUE.

Prima di lavorare, orientati leggendo questi file:
- manuale/README.md
- manuale/ARCHITETTURA.md
- manuale/RUNBOOK.md
- manuale/OUTPUT_STRUCTURE.md
- manuale/PORTFOLIO_LIVE_PLAN.md
- INSIGHTS/work_diary.csv

Stato architetturale importante:
la vecchia pipeline basata su run_trading_day.py, run_trading_day_live.py e output/trading_day e' stata archiviata in:
output/archivio/20260602_pipeline_legacy/

Non usare il vecchio layer trading_day come fonte operativa live, salvo per consultazione storica.

Pipeline canonica nuova:
0. scripts/run_live_sd.py
   - wrapper operativo consigliato
   - chiede la SD
   - suggerisce la prima SD senza screening completi
   - lancia step 1 sulla SD
   - risolve la seduta precedente alla SD via market calendar NYSE
   - lancia step 2 e step 3 sulla BD
   - comando:
     venv/bin/python scripts/run_live_sd.py

1. 1_run_day.py
   - step di screening
   - si lancia sulla SD
   - produce output in output/screening_day/

2. 2_run_trade_state_day.py
   - step di lifecycle trade live
   - si lancia alla chiusura della BD
   - comando base:
     venv/bin/python 2_run_trade_state_day.py --buy-date YYYY-MM-DD
   - produce full snapshot:
     output/trade_state/YYYY/MM/YYYYMMDD/trade_state_YYYYMMDD.csv

3. 3_build_portfolio_day.py
   - deve leggere trade_state_BD-1 e trade_state_BD
   - calcola moltiplicatori, risk_amount, quantity e target partial
   - deve aggiornare:
     portfolio_positions_daily.csv
     portfolio_actions_daily.csv
     portfolio_state_daily.csv
   - scrive:
     output/trade_state/YYYY/MM/YYYYMMDD/trade_sizing_YYYYMMDD.csv
   - comando base:
     venv/bin/python 3_build_portfolio_day.py --buy-date YYYY-MM-DD

App:
- comando da fuori workspace:
  cd "/Users/andreacecchini/SISTEMI DI TRADING/MLL1_E21PB_LIVE" && venv/bin/streamlit run app.py --server.port 8503
- tabs principali:
  - Overview
  - Market
  - First Screen
  - Second Screen
  - Portfolio
  - Operazioni
  - Trade Console
  - Mese
  - Anno
  - Consuntivo
  - Balance
  - Entry Context
- Trade Console:
  - usa semantica live SD -> BD
  - mostra sezioni Dati, Mercato, Moltiplicatori, ETF Filter ed Entry
  - i moltiplicatori devono leggere le stesse fonti canoniche dello step 3 portfolio
- Reporting app:
  - `Mese`, `Anno`, `Consuntivo` lavorano su soli trade chiusi attribuiti per `BD`
  - `Balance` mostra portfolio balance in `R`
  - `Entry Context` lavora su soli trade chiusi e classifica il contesto su `screen_date`

Terminologia obbligatoria:
- SD = screen date
- BD = buy date

Regola temporale da ricordare:
- nel sistema generale la `BD` canonica di una `SD` e' la prima seduta di mercato successiva
- dentro `scripts/run_live_sd.py` si usa invece la seduta precedente alla `SD` come data di update portfolio, perche' e' l'ultima seduta completamente osservabile

Non usare target_date quando stai parlando della pipeline live trade/portfolio, a meno che tu stia citando codice legacy che usa ancora quel nome.

Stati trade supportati da trade_state:
- SKIPPED
- OPEN
- PARTIAL_1
- PARTIAL_2
- CLOSED

Regole chiave:
- il trade_state e' lifecycle puro del trade
- il trade_state non contiene sizing, moltiplicatore o logica portfolio
- il moltiplicatore resta nel layer portfolio
- la quantity viene calcolata nello step 3, non nello step 2
- se la quantity calcolata e' inferiore a 3, si applica la regola minima di 3 azioni
- portfolio_actions_daily deve derivare dal delta tra trade_state_BD-1 e trade_state_BD
- SKIPPED significa solo setup selezionato dal second screen ma non attivato dalla policy di ingresso E21 buffer
- lo stop loss porta a CLOSED con close_reason = STOP_LOSS
- dopo PARTIAL_1 lo stop viene portato a break-even
- PARTIAL_1 viene venduto se High >= target_partial_price; prezzo di vendita = target_partial_price
- PARTIAL_2 puo' avvenire dopo PARTIAL_1 quando Close < EMA21; prezzo di vendita = close
- dopo l'acquisto il P&L in R e' cash P&L / risk_amount, non una misura ex ante basata direttamente su r_multiplier_final

File nuovi gia creati:
- 1_run_day.py
- 2_run_trade_state_day.py
- 3_build_portfolio_day.py
- scripts/run_live_sd.py
- scripts/run_live_bd.py
- app.py
- core/trade_state.py

Output di test gia prodotti:
- output/trade_state/2026/01/20260105/trade_state_20260105.csv
- output/trade_state/2026/01/20260106/trade_state_20260106.csv

Caso guida verificato:
- SD = 2026-01-02
- BD = 2026-01-05
- second_screen_passed = STX, TTMI, CELC, APGE
- BD lunedi valido perche BLUE ON = True
- snapshot 2026-01-05: 4 righe
- snapshot 2026-01-06: TTMI aggiornato a CLOSED / STOP_LOSS

Quando lavori:
- prima leggi il codice e i manuali, non assumere
- a fine giornata aggiorna `INSIGHTS/work_diary.csv` con una breve narrazione dei punti salienti del lavoro
- usa rg per cercare
- non ripristinare o cancellare file senza richiesta esplicita
- usa apply_patch per modifiche manuali
- tieni separati lifecycle trade e portfolio
- se implementi step 3, non ricalcolare entry/exit: consumali da trade_state

Prossimo lavoro naturale:
continuare i run live da SD e verificare dalla app la coerenza di trade_state,
trade_sizing, actions e portfolio_state.
```

Refactor desiderato:
- spostare i builder dei dataset fuori da `app.py`, ad esempio in `core/app_views/reporting.py`
- lasciare `app.py` come consumer della logica
