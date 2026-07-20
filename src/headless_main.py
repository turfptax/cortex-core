#!/usr/bin/env python3
"""Cortex Core, headless entry point (cloud migration P0, 2026-07-20).

Boots the memory engine with NO Pi hardware: no WhisPlay board, display,
recorder, button, LED, STT, BLE, gamepad, battery, sound. What runs:

  - CortexDB          cortex.db, path from CORTEX_DB_PATH
  - PluginRegistry    overseer et al; data dir from CORTEX_PLUGIN_DATA_DIR
  - CortexProtocol    the CMD surface (shared with the HTTP server)
  - HTTP API server   Basic auth password from CORTEX_SERVICE_TOKEN

This is the entry point the cloud container will use (P2+). On the Pi,
main.py stays the entry point and is unchanged by this file.

Environment (all optional; unset values fall back to the Pi defaults
baked into config.py):
  CORTEX_DB_PATH          absolute path to cortex.db
  OVERSEER_DB_PATH        absolute path to overseer.db
  CORTEX_PLUGIN_DATA_DIR  base dir for plugin data (per-plugin subdirs)
  CORTEX_SERVICE_TOKEN    HTTP Basic password (username stays "cortex")
  CORTEX_HTTP_PORT        HTTP port (default 8420)
  CORTEX_TENANT_TZ        owner's IANA timezone, e.g. America/Chicago
  CORTEX_LOOP_MODE        "external" = no in-process loop; cron hits /tick-now
  CORTEX_LLM_BACKEND      default LLM backend override (e.g. "openrouter")
  CORTEX_LLM_FALLBACK     comma list; "" or "none" = no fallback (cloud)
  OPENROUTER_API_KEY      OpenRouter key (beats secrets.toml)
  CORTEX_EMBED_URL        embedding endpoint (default local llama-embed)

Run:  python3 src/headless_main.py
"""

import logging
import os
import signal
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-12s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)

_app_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _app_dir)

from config import CORTEX_DB_PATH, HTTP_ENABLED, PLUGINS_ENABLED  # noqa: E402
from cortex_db import CortexDB  # noqa: E402
from cortex_protocol import CortexProtocol  # noqa: E402

log = logging.getLogger("headless")


def main():
    log.info("Cortex Core headless boot (db=%s)", CORTEX_DB_PATH)
    cortex_db = CortexDB(CORTEX_DB_PATH)

    plugin_registry = None
    if PLUGINS_ENABLED:
        try:
            from plugins_runtime import PluginRegistry
            plugin_registry = PluginRegistry(
                sound_manager=None,
                battery=None,
                cortex_db_path=CORTEX_DB_PATH,
            ).discover_and_load()
        except Exception as e:
            log.exception("plugin runtime failed to initialize: %s", e)

    cortex = CortexProtocol(cortex_db, plugin_registry=plugin_registry)

    http_server = None
    if HTTP_ENABLED:
        from http_server import start_http_server
        plugin_routes = (plugin_registry.get_http_routes()
                         if plugin_registry is not None else [])
        _http_thread, http_server = start_http_server(
            cortex_protocol=cortex,
            context_fn=lambda: {"app_state": "headless"},
            plugin_routes=plugin_routes,
        )

    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("signal %s: shutting down", signum)
        stop.set()

    # SIGTERM is what a container runtime sends; SIGINT covers Ctrl-C.
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while not stop.is_set():
            time.sleep(1)
    finally:
        if http_server is not None:
            try:
                http_server.shutdown()
            except Exception:
                pass
        if plugin_registry is not None:
            try:
                plugin_registry.unload_all()
            except Exception:
                pass
        log.info("headless shutdown complete")


if __name__ == "__main__":
    main()
