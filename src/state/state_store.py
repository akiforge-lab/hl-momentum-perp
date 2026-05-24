from __future__ import annotations

import json
from pathlib import Path

from ..portfolio.account_state import AccountState


class StateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def save(self, *, account: AccountState, extra: dict | None = None) -> None:
        payload = {"account": account.to_dict()}
        if extra:
            payload.update(extra)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        tmp.replace(self.path)
