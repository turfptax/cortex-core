"""Plugin interface types for Cortex.

v0 sketch — see notes/plugin-interface-sketch.md for the full design.

Pure interfaces and dataclasses. No implementations live here. The runtime
in plugins_runtime.py loads plugins and wires them to concrete services.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Screen:
    """A display screen a plugin registers with the core display bus.

    priority: higher wins focus when multiple screens compete.
    render: called by core display loop with the surface to draw on.
    """
    name: str
    priority: int
    render: Callable[..., Any]


@dataclass
class Tool:
    """An MCP tool a plugin exposes.

    Auto-namespaced by core to plugin__<plugin_name>__<tool_name>.
    """
    name: str
    handler: Callable[..., Any]
    schema: dict = field(default_factory=dict)


@dataclass
class Route:
    """An HTTP route a plugin registers.

    Mounted under /plugins/<plugin_name>/<path> by the core HTTP server.
    """
    method: str
    path: str
    handler: Callable[..., Any]


@dataclass
class Task:
    """A long-running background task started after on_load.

    restart=True means core respawns it if it exits unexpectedly.
    """
    name: str
    func: Callable[..., Any]
    restart: bool = True


@dataclass
class PluginAPI:
    """Bundle of services core hands to a plugin at registration time.

    All fields are populated by plugins_runtime.py. v0 holds slots for the
    services the sketch describes; concrete implementations land slice by
    slice as plugins are added.
    """
    db: Any                      # PluginDB — plugin's own SQLite handle
    core_memory: Any             # CoreMemoryRO — read-only access to cortex.db
    llm: Any                     # LLMRouter — ondevice / openrouter / lmstudio
    display: Any                 # DisplayBus — request_focus / release / draw
    events: Any                  # EventBus — on / emit
    config: Any                  # PluginConfig — plugin's [config] section
    log: logging.Logger
    sound: Any                   # SoundBus — play(path)
    plugin_data: Path            # absolute path to plugins/<name>/data/
    plugin_assets: Path          # absolute path to plugins/<name>/assets/
    # Added in slice 2b2:
    core_db_path: Any = None     # path to cortex.db (for one-time migrations)
    battery: Any = None          # core BatteryMonitor or None


class Plugin:
    """Base class plugins extend.

    Override the methods relevant to the plugin's capabilities. The runtime
    only invokes what the plugin declares in plugin.toml's [capabilities].

    The `name` attribute is stamped by plugins_runtime after register() so
    the plugin can reference its own manifest name (e.g. when building log
    prefixes, tool namespaces, or HTTP route mount paths).
    """

    name: str = ""

    def __init__(self, api: PluginAPI):
        self.api = api

    def on_load(self) -> None:
        """One-time setup after API is wired. Override for init."""

    def on_unload(self) -> None:
        """Graceful teardown on shutdown. Override for cleanup."""

    def screens(self) -> list[Screen]:
        return []

    def mcp_tools(self) -> list[Tool]:
        return []

    def http_routes(self) -> list[Route]:
        return []

    def background_tasks(self) -> list[Task]:
        return []

    def contribute_to_context(self) -> dict:
        """Optionally return a dict merged into cortex_get_context's response.

        Plugins implementing this surface their state in the standard
        context payload that AI sessions read at startup. The returned
        dict is merged at the TOP LEVEL of get_context() — so a plugin
        returning {"working_memory": {...}} adds context["working_memory"].

        Per locked overseer design: the overseer plugin uses this hook
        to inject its cached working_memory artifact (zero-latency, no
        LLM call). Other plugins are free to add their own keys; clashes
        between plugins go to whoever loaded last (manifest order).

        Default: contribute nothing.
        """
        return {}


@dataclass
class PluginManifest:
    """Parsed plugin.toml for a discovered plugin."""
    name: str
    version: str
    description: str
    enabled: bool
    entrypoint: str              # python module name under plugins/<name>/
    folder: Path                 # absolute path to plugins/<name>/
    capabilities: dict
    llm: dict
    hooks: list                  # event names from [hooks].subscribe
    config: dict                 # raw [config] section
    dependencies: dict


# ── Concrete service classes used by plugins_runtime to build PluginAPI ──
# Real implementations land slice by slice as plugins demand them. v0 stubs
# (the _Null* classes) raise on use or no-op so a plugin that touches an
# unimplemented service gets a clear signal instead of a silent failure.


class PluginConfig:
    """Read-only view of a plugin's [config] section from plugin.toml."""

    def __init__(self, data):
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        return key in self._data

    def __repr__(self):
        return f"PluginConfig({len(self._data)} keys)"


class SoundBus:
    """Plugin-facing wrapper around the core SoundManager.

    play(name): play a sound by short name. Resolution order:
        1) plugins/<name>/assets/sounds/<name>.wav (plugin-owned)
        2) core SoundManager's own SOUND_DIR (existing behavior)
    """

    def __init__(self, sound_manager, plugin_assets_dir):
        self._sm = sound_manager
        self._plugin_sounds = Path(plugin_assets_dir) / "sounds"

    def play(self, name):
        if self._sm is None:
            return
        plugin_path = self._plugin_sounds / f"{name}.wav"
        if plugin_path.is_file() and hasattr(self._sm, "play_path"):
            try:
                self._sm.play_path(str(plugin_path))
                return
            except Exception:
                pass
        # Fall back to core SoundManager.play(name) which looks in SOUND_DIR
        try:
            self._sm.play(name)
        except Exception:
            pass


class _NullEventBus:
    """No-op event bus for v0. Real bus arrives when overseer plugin needs it."""

    def on(self, event, handler):
        pass

    def emit(self, event, payload=None):
        pass


class _NullDisplayBus:
    """Stub display bus. Real focus arbitration lands in slice 2c."""

    def request_focus(self, screen_name):
        return False

    def release_focus(self, screen_name):
        pass


class _NullLLMRouter:
    """Stub LLM router. Pet keeps calling llama-server directly via urllib
    in 2b1; the router becomes real when the overseer plugin arrives.
    """

    def complete(self, prompt, *, backend=None, max_tokens=256, temperature=0.8):
        raise NotImplementedError(
            "LLMRouter not implemented yet — plugins should call llama-server "
            "or LM Studio directly until the overseer slice"
        )


class _NullCoreMemoryRO:
    """Stub for read-only access to cortex.db. Real impl in slice 2c."""

    def query(self, sql, params=()):
        return []

    def read_plugin(self, plugin_name, sql, params=()):
        return []
