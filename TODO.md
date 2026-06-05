# TODO

- Definire il perimetro operativo ufficiale di `LIVE_MLL1`: solo `MLL1_E21PB_BLUE`, solo `2026`, nessun layer frozen o sperimentale.
- Stabilire la struttura canonica di `output/` con distinzione chiara tra `trading_day`, `portfolio_live`, `trade_console`, `logs`, `app`.
- Adattare i path helper del workspace per leggere solo dati condivisi da `MARKET_DATA_ROOT` e salvare tutto il prodotto internamente.
- Portare nel workspace la logica strategica ufficiale `MLL1_E21PB_BLUE`, con semantica esplicita `screen_date -> buy_date`.
- Verificare che il motore `run_day` e `run_trading_day_live` possa rigenerare localmente gli artifact necessari nel nuovo schema `output/`.
- Ridurre i moduli importati da `backtest2` al solo perimetro operativo live, eliminando dipendenze non necessarie o orientate allo studio.
- Creare una prima `app.py` minimale per il workspace live con tab `Run`, `Market`, `Portfolio`, `Trade Console`.
- Definire la variante portfolio live iniziale e i relativi `strategy_id` / `variant_id` ufficiali.
- Verificare il flusso completo `screening -> trading_day -> portfolio_live` su una finestra reale del `2026`.
- Spostare i builder dei dataset in un modulo tipo `core/app_views/reporting.py`.
- Lasciare `app.py` solo come consumer della logica.
