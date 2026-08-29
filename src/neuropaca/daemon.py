"""The daemon entry point (`neuropaca` console script; Architecture.md §10).

    NEUROPACA_CONFIG=/etc/neuropaca.toml neuropaca

Loads and validates `Config`, then hands off to `NeuroPACAOrchestrator.run()`,
which starts the event loop and blocks until `SIGTERM` / `SIGINT`.
"""

from __future__ import annotations

import asyncio
import os
import sys

from neuropaca.core.config import Config
from neuropaca.core.errors import ConfigError
from neuropaca.core.logging import configure, get_logger
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator

_DEFAULT_CONFIG_PATH = "neuropaca.toml"


def main() -> None:
    config_path = os.environ.get("NEUROPACA_CONFIG", _DEFAULT_CONFIG_PATH)
    try:
        config = Config.from_file(config_path)
    except ConfigError as exc:
        configure("ERROR")
        get_logger("daemon").error("cannot start: %s", exc)
        sys.exit(2)

    orchestrator = NeuroPACAOrchestrator(config)
    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
