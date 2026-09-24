# Optional MT5 Docker terminal

The normal, simplest path for this repository is to run a Windows MT5 terminal
and execute `data_ingestion/export_mt5_csv.py`. The Docker service below is an
optional terminal setup for the community `nautilus_mt5` adapter.

1. Clone the adapter repository outside the project or set
   `NAUTILUS_MT5_REPO_DIR` in `.env`:

   `git clone https://github.com/alifnurc/nautilus_mt5 .external/nautilus_mt5`

2. Copy `.env.example` to `.env`, fill the demo account values, and run:

   `docker compose --env-file .env up --build -d mt5-wine`

3. Open `http://localhost:60832/vnc.html`, complete the terminal login and
   enable AutoTrading if the adapter documentation requires it.

4. Follow the adapter's own connection instructions before attempting a live
   adapter session. This project itself never starts a live trading node; its
   historical exporter remains the only path used by the backtest.

If Docker networking or the adapter image is inconvenient, use a local Windows
MT5 terminal instead. That path does not require `nautilus_mt5` and is enough
for `copy_rates_range()` CSV export.
