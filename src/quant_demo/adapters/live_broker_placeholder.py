from __future__ import annotations


class LiveBrokerDisabled:
    """Fail-closed placeholder: this project cannot submit live orders."""

    async def submit_order(self, *_args, **_kwargs):
        raise RuntimeError(
            "Live trading is intentionally disabled. Use the local BacktestEngine paper venue."
        )

