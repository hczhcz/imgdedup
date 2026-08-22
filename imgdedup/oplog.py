import json
import logging
import threading
import time

_lock = threading.Lock()
_logger = None


def init(log_file):
    global _logger
    with _lock:
        if _logger is not None:
            return
        logger = logging.getLogger("imgdedup")
        logger.setLevel(logging.INFO)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(sh)
        _logger = logger


def _emit(method, event, fields):
    payload = {"event": event, "ts": time.time(), **fields}
    method(json.dumps(payload, ensure_ascii=False))


def log(event, **fields):
    _emit(_logger.info, event, fields)


def error(event, **fields):
    _emit(_logger.error, event, fields)
