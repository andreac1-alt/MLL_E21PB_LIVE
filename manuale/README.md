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

Regola pratica:

- ogni nuova decisione strutturale del workspace live va documentata qui
- gli insight restano in `INSIGHTS/`
- il manuale contiene verita' operative relativamente stabili
