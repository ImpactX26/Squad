import logging


def setup_logging(app_env: str) -> None:
    logging.basicConfig(
        level=logging.DEBUG if app_env == "development" else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # asyncpg / httpx / watchfiles are noisy at DEBUG
    for noisy in ("asyncio", "httpx", "httpcore", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)