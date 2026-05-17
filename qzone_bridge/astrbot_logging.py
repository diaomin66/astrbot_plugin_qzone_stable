"""Logging adapter for AstrBot-hosted and standalone daemon execution."""

from __future__ import annotations

import os

try:
    from astrbot.api import logger

    USING_ASTRBOT_LOGGER = True
except Exception:
    import logging

    logger = logging.getLogger("qzone_bridge")
    USING_ASTRBOT_LOGGER = False


def configure_standalone_logging(default_level: str = "INFO") -> None:
    if USING_ASTRBOT_LOGGER:
        return
    import logging

    logging.basicConfig(level=os.getenv("QZONE_DAEMON_LOG_LEVEL", default_level))
