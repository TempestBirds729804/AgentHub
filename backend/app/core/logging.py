import logging
from contextvars import ContextVar

run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get() or "-"
        record.user_id = user_id_var.get() or "-"
        return True


def setup_logging() -> None:
    root = logging.getLogger()
    if not any(handler.name == "agenthub-context" for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.name = "agenthub-context"
        handler.addFilter(ContextFilter())
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s [run=%(run_id)s user=%(user_id)s] %(name)s: %(message)s"
            )
        )
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("httpx", "httpx2", "httpcore", "urllib3", "arq.worker"):
        logging.getLogger(name).setLevel(logging.WARNING)
