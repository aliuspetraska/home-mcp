"""Service integrations.

Each module in this package exposes `register(mcp, settings)` and attaches
tools/resources to the shared FastMCP instance. To add a new service, create
a module here and add it to SERVICE_MODULES below.
"""

import logging

from fastmcp import FastMCP

from app.config import Settings

logger = logging.getLogger(__name__)

SERVICE_MODULES = (
    "app.services.myuplink",
    "app.services.meteo",
)


def register_all(mcp: FastMCP, settings: Settings) -> None:
    for module_path in SERVICE_MODULES:
        try:
            module = __import__(module_path, fromlist=["register"])
            module.register(mcp, settings)
            logger.info("Registered service: %s", module_path.rsplit(".", 1)[-1])
        except Exception:
            logger.exception("Failed to register service: %s", module_path)
