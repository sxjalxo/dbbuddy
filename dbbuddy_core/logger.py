"""Centralized logging system for DBBuddy.

This module provides a unified logging interface for the entire system,
replacing scattered print statements with proper logging.

Phase 16.1 of the production polish upgrade.
"""

import logging
import sys


def setup_logger(debug: bool = False, log_file: str = None) -> logging.Logger:
    """Setup and configure the centralized logger.

    Args:
        debug: Whether to enable debug logging
        log_file: Optional file path to write logs to

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger("dbbuddy")

    # Clear existing handlers
    logger.handlers.clear()

    # Set log level
    level = logging.DEBUG if debug else logging.WARNING
    logger.setLevel(level)

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # Prevent propagation to root logger
    logger.propagate = False

    return logger


def get_logger() -> logging.Logger:
    """Get the configured logger instance.

    Returns:
        Logger instance
    """
    return logging.getLogger("dbbuddy")
