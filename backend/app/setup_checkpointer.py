import asyncio
import logging
import sys

from app.agent.checkpoint import setup_checkpointer_once


def main() -> None:
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(setup_checkpointer_once())
    logging.getLogger(__name__).info("Checkpointer tables ready")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
