from __future__ import annotations

import os
from pathlib import Path


class KillSwitch:
    def __init__(self, kill_file_path: str):
        self.kill_file = Path(kill_file_path)
        self._config_flag = False
        self._auto_triggered: str | None = None

    def set_config_flag(self, value: bool) -> None:
        self._config_flag = value

    def trip(self, reason: str) -> None:
        self._auto_triggered = reason

    def reset_auto(self) -> None:
        self._auto_triggered = None

    @property
    def file_present(self) -> bool:
        return self.kill_file.exists()

    def is_active(self) -> tuple[bool, str | None]:
        if self._config_flag:
            return True, "config:global_kill_switch=true"
        if self.file_present:
            return True, f"file:{self.kill_file}"
        if self._auto_triggered:
            return True, f"auto:{self._auto_triggered}"
        return False, None
