import logging
import os

NOISY_LOGGERS = ("httpx", "httpcore", "google_genai")


def setup_logging(level: str | None = None) -> None:
    """Configure root logging. Level comes from the arg, else LOG_LEVEL, else INFO.

    Also used as a ProcessPoolExecutor initializer: on Windows, workers are spawned
    fresh and would otherwise log nothing.
    """
    logging.basicConfig(
        level=(level or os.getenv("LOG_LEVEL", "INFO")).upper(),
        format="%(asctime)s %(levelname)-7s [%(processName)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
