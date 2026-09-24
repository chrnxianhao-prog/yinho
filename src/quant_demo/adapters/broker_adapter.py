from __future__ import annotations

from typing import Any, Protocol


class BrokerAdapter(Protocol):
    """Project-level seam for future paper/demo/live execution clients.

    NautilusTrader's BacktestEngine is the active paper implementation in this
    project. A future adapter should translate this project contract to a
    NautilusTrader execution client or a broker SDK, never to strategy code.
    """

    async def connect(self) -> None:
        ...

    async def submit_order(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        ...

    async def cancel_order(self, external_order_id: str) -> dict[str, Any]:
        ...

    async def replace_order(
        self,
        external_order_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    async def stream_events(self) -> Any:
        ...

