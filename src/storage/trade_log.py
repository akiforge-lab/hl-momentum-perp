from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..utils.time import utcnow


class JsonlLog:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        rec = {"ts": utcnow().isoformat(), "kind": kind, **payload}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
