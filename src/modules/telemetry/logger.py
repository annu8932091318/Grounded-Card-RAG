"""Structured JSON logs (stdlib only). Never log raw customer ids, names or scores — callers pass hashes."""
import json
import logging
import sys
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
_root = logging.getLogger("agent")
_root.addHandler(_handler)
_root.setLevel(logging.INFO)
_root.propagate = False


def log(event: str, level: str = "info", **fields) -> None:
    """log('corpus_injection_detected', doc_id='POL-OPS-012')"""
    _root.log(getattr(logging, level.upper()), event, extra={"fields": fields})
