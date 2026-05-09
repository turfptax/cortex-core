"""Plugin discovery and lifecycle for Cortex.

v0 (slice 2b1) — see plugin_api.py for the interface and
notes/plugin-interface-sketch.md for the full design.

Discovers plugins by scanning plugins/<name>/plugin.toml, parses each
manifest, then loads the plugin's entrypoint module and calls its
register(api) function. Builds a PluginAPI bundle per plugin (with the
plugin's own SQLite at plugins/<name>/data/<name>.db) and invokes the
plugin's lifecycle hooks (on_load / on_unload).

Crash isolation: any exception inside a plugin's import, register, or
on_load is caught at this boundary and logged. A broken plugin must
never take down core.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import tomllib
from pathlib import Path
from typing import Optional

from plugin_api import (
    Plugin,
    PluginAPI,
    PluginConfig,
    PluginManifest,
    SoundBus,
    _NullCoreMemoryRO,
    _NullDisplayBus,
    _NullEventBus,
    _NullLLMRouter,
)

logger = logging.getLogger(__name__)


def _default_plugins_dir() -> Path:
    """Default plugins directory: sibling of src/ inside cortex-core."""
    src_dir = Path(__file__).resolve().parent
    return src_dir.parent / "plugins"


def discover(plugins_dir: Optional[Path] = None) -> list[PluginManifest]:
    """Scan a directory for plugin.toml files and return parsed manifests."""
    plugins_dir = plugins_dir or _default_plugins_dir()
    if not plugins_dir.is_dir():
        logger.info("plugins directory %s does not exist; skipping discovery",
                    plugins_dir)
        return []

    manifests: list[PluginManifest] = []
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        toml_path = entry / "plugin.toml"
        if not toml_path.is_file():
            continue
        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            logger.error("failed to parse %s: %s", toml_path, e)
            continue
        meta = data.get("plugin", {})
        manifests.append(PluginManifest(
            name=meta.get("name", entry.name),
            version=meta.get("version", "0.0.0"),
            description=meta.get("description", ""),
            enabled=bool(meta.get("enabled", True)),
            entrypoint=meta.get("entrypoint", entry.name),
            folder=entry,
            capabilities=data.get("capabilities", {}),
            llm=data.get("llm", {}),
            hooks=list(data.get("hooks", {}).get("subscribe", [])),
            config=data.get("config", {}),
            dependencies=data.get("dependencies", {}),
        ))
    return manifests


def _import_plugin_module(manifest: PluginManifest):
    """Import a plugin's __init__.py as a module.

    Adds the plugin folder to sys.path first so imports inside the plugin
    (e.g. `from <module> import Foo` between sibling files) resolve
    correctly within the plugin folder.
    """
    folder_str = str(manifest.folder.resolve())
    if folder_str not in sys.path:
        sys.path.append(folder_str)

    init_path = manifest.folder / "__init__.py"
    if not init_path.is_file():
        raise FileNotFoundError(f"plugin entrypoint missing: {init_path}")

    spec = importlib.util.spec_from_file_location(
        f"plugin_{manifest.name}", str(init_path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PluginRegistry:
    """Holds loaded plugins and arbitrates their lifecycle."""

    def __init__(self, *, sound_manager=None, battery=None, cortex_db_path=None):
        self.plugins: list[Plugin] = []
        self.manifests: list[PluginManifest] = []
        self._sound_manager = sound_manager
        self._battery = battery
        self._cortex_db_path = cortex_db_path

    def discover_and_load(
        self, plugins_dir: Optional[Path] = None
    ) -> "PluginRegistry":
        """Discover plugins, then load each enabled one."""
        self.manifests = discover(plugins_dir)
        if not self.manifests:
            logger.info("no plugins discovered")
            return self

        enabled = [m for m in self.manifests if m.enabled]
        disabled = [m for m in self.manifests if not m.enabled]
        if disabled:
            logger.info("plugins disabled in manifest: %s",
                        [m.name for m in disabled])
        if enabled:
            logger.info("plugins discovered: %s",
                        [f"{m.name}@{m.version}" for m in enabled])

        for manifest in enabled:
            try:
                self._load_plugin(manifest)
            except Exception as e:
                logger.exception("failed to load plugin %s: %s",
                                 manifest.name, e)

        return self

    def _build_api(self, manifest: PluginManifest) -> PluginAPI:
        """Construct the PluginAPI bundle for a single plugin.

        Creates plugins/<name>/data/<name>.db (a CortexDB instance — yes it
        creates the full schema including non-pet tables; harmless. Slice 2c
        will give plugins a leaner DB layer if it matters).
        """
        from cortex_db import CortexDB

        plugin_data_dir = manifest.folder / "data"
        plugin_data_dir.mkdir(exist_ok=True)
        plugin_db_path = plugin_data_dir / f"{manifest.name}.db"

        return PluginAPI(
            db=CortexDB(str(plugin_db_path)),
            core_memory=_NullCoreMemoryRO(),
            llm=_NullLLMRouter(),
            display=_NullDisplayBus(),
            events=_NullEventBus(),
            config=PluginConfig(manifest.config),
            log=logging.getLogger(f"plugin.{manifest.name}"),
            sound=SoundBus(self._sound_manager, manifest.folder / "assets"),
            plugin_data=plugin_data_dir,
            plugin_assets=manifest.folder / "assets",
            core_db_path=self._cortex_db_path,
            battery=self._battery,
        )

    def _load_plugin(self, manifest: PluginManifest) -> None:
        api = self._build_api(manifest)
        module = _import_plugin_module(manifest)

        if not hasattr(module, "register"):
            logger.error("plugin %s has no register() function", manifest.name)
            return

        plugin = module.register(api)
        if not isinstance(plugin, Plugin):
            logger.error(
                "plugin %s register() returned %s; expected a Plugin instance",
                manifest.name, type(plugin).__name__,
            )
            return

        # Stamp the manifest name on the plugin so it can self-identify
        # (used when building plugin HTTP mount paths, MCP tool names, etc.)
        plugin.name = manifest.name

        try:
            plugin.on_load()
        except Exception as e:
            logger.exception("plugin %s on_load failed: %s", manifest.name, e)
            return

        self.plugins.append(plugin)
        logger.info("loaded plugin: %s@%s", manifest.name, manifest.version)

    def get_http_routes(self):
        """Collect HTTP routes from every loaded plugin.

        Returns:
            list of (method, mount_path, handler) tuples ready for the core
            HTTP server to register. Mount paths are auto-namespaced as
            `/plugins/<plugin_name><route.path>` so plugins can never clash
            with each other or with core routes.
        """
        out = []
        for plugin in self.plugins:
            try:
                routes = plugin.http_routes() or []
            except Exception as e:
                logger.exception("plugin %s http_routes() failed: %s",
                                 plugin.name or plugin.__class__.__name__, e)
                continue
            for route in routes:
                mount_path = "/plugins/{}{}".format(plugin.name, route.path)
                out.append((route.method.upper(), mount_path, route.handler))
        return out

    def unload_all(self) -> None:
        """Call on_unload on every loaded plugin, swallowing errors."""
        for plugin in self.plugins:
            try:
                plugin.on_unload()
            except Exception as e:
                logger.error("plugin %s on_unload failed: %s",
                             plugin.__class__.__name__, e)
        self.plugins = []
