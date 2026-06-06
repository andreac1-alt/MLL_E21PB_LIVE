# Manuale LIVE_MLL1

Questa cartella raccoglie la documentazione operativa del workspace
`MLL1_E21PB_LIVE`.

Obiettivo:

- mantenere separata la documentazione live da quella di `backtest2`
- descrivere solo flussi, regole e strumenti utili all'operativita' reale
- evitare materiali da laboratorio, frozen o analisi esplorativa non essenziale

Sezioni iniziali consigliate:

- `ARCHITETTURA.md`
- `OUTPUT_STRUCTURE.md`
- `RUNBOOK.md`
- `PORTFOLIO_LIVE_PLAN.md`
- `CODEX_START_PROMPT.md`

Comandi operativi principali:

- run live da `SD`: `venv/bin/python scripts/run_live_sd.py`
- run solo `BD`: `venv/bin/python scripts/run_live_bd.py`
- app locale: `cd "/Users/andreacecchini/SISTEMI DI TRADING/MLL1_E21PB_LIVE" && venv/bin/streamlit run app.py --server.port 8503`

Stato documentato al 2026-06-06:

- l'app include anche i tab `Mese`, `Anno`, `Consuntivo`, `Balance`, `Entry Context`
- la semantica di reporting dell'app e' stata uniformata verso `BD`
- nel sistema generale la `BD` canonica di una `SD` e' la prima seduta di mercato successiva e, viceversa, la `SD` canonica di una `BD` e' la seduta di mercato precedente che la genera; questa relazione va sempre risolta via market calendar NYSE
- `scripts/run_live_sd.py` usa ora `BD = SD`: fa screening sulla `SD` del prompt e aggiorna trade state e portfolio sulla stessa data

Regola pratica:

- ogni nuova decisione strutturale del workspace live va documentata qui
- gli insight restano in `INSIGHTS/`
- il manuale contiene verita' operative relativamente stabili
- a fine giornata va salvata in `INSIGHTS/work_diary.csv` una narrazione dei punti salienti del lavoro svolto
