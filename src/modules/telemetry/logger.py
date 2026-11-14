"""Local file logger (winston-style) + console.

File:   logs/<dd-mm-yyyy>output.log   e.g. logs/14-11-2026output.log  (a new file every day, local time)
Line:   one JSON object per line, so the file is both readable and machine-parsable:
  {"date": "2026-11-14 22:31:05.123", "type": "info",
   "message": "Grounded-Card-RAG | src/modules/agent | agent.py | run_agent | requestId: 3f2c... | message: request_completed status=answered"}

How the caller is found: logging's own stack inspection (stacklevel=2) gives the file and function that
called log(), so call sites never pass them. The requestId comes from a ContextVar set by the HTTP
middleware (Idempotency-Key / x-request-id) and copied into worker threads (see helper/async_utils.py).

Privacy: never log raw customer ids, names, scores or message text — callers pass hashes / sizes.
"""
import json
import logging
import sys
import threading
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from src.const.config import config

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
in_http_var: ContextVar[bool] = ContextVar("in_http", default=False)            # True inside an API call
script_source_var: ContextVar[str] = ContextVar("script_source", default="script")  # req_res_log.source for script runs

_ROOT = config.root
_ROOT_NAME = _ROOT.name


def set_request_id(value: str):
    """Returns a token; pass it to reset_request_id() when the request ends."""
    return request_id_var.set(value or "-")


def reset_request_id(token) -> None:
    request_id_var.reset(token)


def _location(record: logging.LogRecord) -> tuple[str, str]:
    """-> ('src/modules/agent', 'agent.py'). Files outside the project (uvicorn, libraries) use the logger name."""
    path = Path(record.pathname)
    try:
        rel = path.resolve().relative_to(_ROOT)
        folder = rel.parent.as_posix()
        return (folder if folder != "." else "-"), rel.name
    except (ValueError, OSError):
        return record.name, path.name


def _fmt_value(v) -> str:
    if isinstance(v, str):
        return v if v and " " not in v else json.dumps(v)
    return json.dumps(v, default=str)


def format_message(record: logging.LogRecord) -> str:
    folder, filename = _location(record)
    text = record.getMessage()
    fields = getattr(record, "fields", None) or {}
    if fields:
        text += " " + " ".join(f"{k}={_fmt_value(v)}" for k, v in fields.items())
    return (f"{_ROOT_NAME} | {folder} | {filename} | {record.funcName} | "
            f"requestId: {getattr(record, 'request_id', None) or request_id_var.get()} | message: {text}")


class _JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname.lower()
        entry = {
            "date": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.") + f"{int(record.msecs):03d}",
            "type": "warn" if level == "warning" else ("error" if level == "critical" else level),
            "message": format_message(record),
        }
        if record.exc_info:
            entry["stack"] = self.formatException(record.exc_info)  # full traceback, file only
        return json.dumps(entry, ensure_ascii=False, default=str)


class DailyFileHandler(logging.Handler):
    """Writes to logs/<dd-mm-yyyy>output.log and switches file when the date changes (no restart needed)."""

    def __init__(self, directory: Path):
        super().__init__()
        self.directory = directory
        self._day: str | None = None
        self._stream = None
        self._io_lock = threading.Lock()

    def path_for_today(self) -> Path:
        return self.directory / f"{datetime.now().strftime('%d-%m-%Y')}output.log"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with self._io_lock:
                day = datetime.now().strftime("%d-%m-%Y")
                if day != self._day or self._stream is None:
                    if self._stream:
                        self._stream.close()
                    self.directory.mkdir(parents=True, exist_ok=True)
                    self._stream = open(self.path_for_today(), "a", encoding="utf-8")  # noqa: SIM115
                    self._day = day
                self._stream.write(line + "\n")
                self._stream.flush()  # WHY: a crash must not lose the line that explains it
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with self._io_lock:
            if self._stream:
                self._stream.close()
                self._stream = None
        super().close()


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = f"{datetime.fromtimestamp(record.created):%H:%M:%S} {record.levelname:<7} {format_message(record)}"
        return line + ("\n" + self.formatException(record.exc_info) if record.exc_info else "")


file_handler = DailyFileHandler(config.log_dir)
file_handler.setFormatter(_JsonLineFormatter())

_root = logging.getLogger("agent")
_root.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
_root.propagate = False
_root.addHandler(file_handler)
if config.log_to_console:
    _console = logging.StreamHandler(sys.stdout)
    _console.setFormatter(_ConsoleFormatter())
    _root.addHandler(_console)


def attach_to(logger_name: str) -> None:
    """Also send a third-party logger (uvicorn) to today's file, so startup errors are in one place."""
    lg = logging.getLogger(logger_name)
    if file_handler not in lg.handlers:
        lg.addHandler(file_handler)


def log(event: str, level: str = "info", exc_info=False, **fields) -> None:
    """log('doc_ingested', doc_id='POL-CRD-001', chunks=4)
    log('agent_crashed', level='error', exc_info=True)   # inside an except block: writes the traceback"""
    _root.log(getattr(logging, level.upper()), event, extra={"fields": fields}, exc_info=exc_info, stacklevel=2)
