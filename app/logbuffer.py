import logging
from collections import deque
from datetime import datetime, timezone

MAX_ENTRIES = 500

_buffer: deque[dict] = deque(maxlen=MAX_ENTRIES)


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        _buffer.append({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "msg": self.format(record),
        })


def get_logs() -> list[dict]:
    return list(_buffer)


def install():
    handler = BufferHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
