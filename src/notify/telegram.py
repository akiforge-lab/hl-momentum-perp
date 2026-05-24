from __future__ import annotations

import asyncio
import time

import httpx

from ..utils.logger import get

log = get(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None,
                 severities: list[str], rate_limit_sec: float = 2.0):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.severities = set(severities or [])
        self.rate_limit_sec = rate_limit_sec
        self._last_send_ts = 0.0
        self._client = httpx.AsyncClient(timeout=10.0)
        self.enabled = bool(bot_token and chat_id)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, severity: str, text: str) -> None:
        if not self.enabled:
            log.info("telegram disabled", extra={"severity": severity, "preview": text[:120]})
            return
        if severity not in self.severities:
            return
        wait = self._last_send_ts + self.rate_limit_sec - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            r = await self._client.post(url, json={
                "chat_id": self.chat_id, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": True,
            })
            self._last_send_ts = time.time()
            if r.status_code >= 400:
                log.warning("telegram error", extra={"status": r.status_code, "body": r.text[:200]})
        except Exception as e:
            log.warning("telegram exception", extra={"err": repr(e)})
