"""File-backed candle cache to reduce /info calls.

Daily candles only change once per day, so a several-hour TTL is fine. Cache
lives under data/candles/ and is gitignored.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..utils.logger import get

log = get(__name__)


class CandleCache:
    def __init__(self, cache_dir: str, ttl_sec: int):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl_sec = ttl_sec

    def _path(self, symbol: str, interval: str) -> Path:
        safe = symbol.replace("/", "_").replace(":", "_")
        return self.dir / f"{safe}_{interval}.json"

    def get(self, symbol: str, interval: str) -> list[dict] | None:
        p = self._path(symbol, interval)
        if not p.exists():
            return None
        age = time.time() - p.stat().st_mtime
        if age > self.ttl_sec:
            return None
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    def put(self, symbol: str, interval: str, candles: list[dict]) -> None:
        if not candles:
            return
        p = self._path(symbol, interval)
        try:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(candles, separators=(",", ":")))
            tmp.replace(p)
        except Exception as e:
            log.warning("candle cache write failed", extra={"symbol": symbol, "err": repr(e)})
