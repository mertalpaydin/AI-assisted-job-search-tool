import sys
from pathlib import Path
from loguru import logger


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Configure loguru for the application."""
    logger.remove()

    logger.add(
        sys.stderr,
        level=level.upper(),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True,
    )

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_path,
            level=level.upper(),
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} - {message}",
            rotation="10 MB",
            retention="7 days",
            encoding="utf-8",
        )
