"""Logging adapter for AstrBot-hosted and standalone daemon execution."""

from __future__ import annotations

import os

try:
    from astrbot.api import logger as _astrbot_logger

    USING_ASTRBOT_LOGGER = True
except ImportError:
    import logging

    _astrbot_logger = None
    USING_ASTRBOT_LOGGER = False


def get_logger(name: str = "qzone_bridge"):
    if USING_ASTRBOT_LOGGER and _astrbot_logger is not None:
        return _astrbot_logger
    import logging

    return logging.getLogger(name)


logger = get_logger("qzone_bridge")


def configure_standalone_logging(default_level: str = "INFO") -> None:
    if USING_ASTRBOT_LOGGER:
        return
    import logging

    logging.basicConfig(level=os.getenv("QZONE_DAEMON_LOG_LEVEL", default_level))
