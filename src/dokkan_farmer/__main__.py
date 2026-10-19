"""Entry point: ``python -m dokkan_farmer`` (what the Docker image runs)."""

from __future__ import annotations

import logging
import sys

import uvicorn

from . import __version__
from .context import AppContext
from .logging_setup import setup_logging
from .settings import Settings
from .web import create_app

log = logging.getLogger("dokkan_farmer")


def main() -> None:
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    setup_logging(settings.log_level, settings.logs_dir)
    log.info(
        "Dokkan Idle Farmer %s (backend=%s, game=%s, data=%s)",
        __version__,
        settings.device_backend,
        settings.game_package,
        settings.data_dir,
    )
    context = AppContext(settings)
    app = create_app(context)
    uvicorn.run(
        app,
        host=settings.web_host,
        port=settings.web_port,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
