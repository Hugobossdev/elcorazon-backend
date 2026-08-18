"""Journalisation structurée.

Les journaux sortent en JSON sur la sortie standard : c'est le format que les
collecteurs (Loki, CloudWatch, ELK) ingèrent sans analyse syntaxique fragile,
et c'est ce qui rend un incident interrogeable par champ plutôt que par
expression régulière.
"""

from __future__ import annotations

import json
import logging
from typing import Any

# Attributs internes de LogRecord : tout le reste est une donnée métier ajoutée
# par l'appelant via `extra=`, et mérite de figurer dans le journal.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)
