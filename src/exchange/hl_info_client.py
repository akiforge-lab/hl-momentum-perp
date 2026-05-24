"""Read-only Hyperliquid Info endpoint client.

Mainnet: https://api.hyperliquid.xyz/info
Docs:   https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint

This module makes ONLY POST requests to /info (public read endpoint).
It never touches /exchange. There is no signing code here.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from ..utils.logger import get

log = get(__name__)

DEFAULT_BASE_URL = "https://api.hyperliquid.xyz"


class HLInfoClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, payload: dict) -> Any:
        r = await self._client.post(f"{self.base_url}/info", json=payload)
        r.raise_for_status()
        return r.json()

    # --- universe / market metadata ---------------------------------------

    async def meta(self) -> dict:
        """Perp universe metadata. Returns {'universe': [{name, szDecimals, maxLeverage, ...}, ...]}."""
        return await self._post({"type": "meta"})

    async def meta_and_asset_ctxs(self) -> tuple[dict, list[dict]]:
        """Meta + per-asset context (mark, mid, funding, openInterest, dayNtlVlm)."""
        resp = await self._post({"type": "metaAndAssetCtxs"})
        return resp[0], resp[1]

    async def all_mids(self) -> dict[str, str]:
        return await self._post({"type": "allMids"})

    # --- candles ----------------------------------------------------------

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        end_ms = end_ms or int(time.time() * 1000)
        return await self._post({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        })

    # --- user state (read-only) -------------------------------------------

    async def clearinghouse_state(self, address: str) -> dict:
        return await self._post({"type": "clearinghouseState", "user": address})
