from __future__ import annotations

import importlib
import os
from typing import Any, Protocol

import pandas as pd


class IndicatorPlugin(Protocol):
    plugin_id: str
    version: str

    def enrich(self, frame: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        """Return a frame with one or more new indicator columns."""
        ...


class IndicatorRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, IndicatorPlugin] = {}

    def register(self, plugin: IndicatorPlugin) -> None:
        if not plugin.plugin_id or not plugin.version:
            raise ValueError("Indicator plugin must have plugin_id and version")
        if plugin.plugin_id in self._plugins:
            raise ValueError(f"Duplicate indicator plugin: {plugin.plugin_id}")
        self._plugins[plugin.plugin_id] = plugin

    def metadata(self) -> list[dict[str, str]]:
        return [
            {"plugin_id": item.plugin_id, "version": item.version}
            for item in self._plugins.values()
        ]

    def apply(self, frame: pd.DataFrame, definitions: list[dict[str, Any]]) -> pd.DataFrame:
        output = frame
        for definition in definitions:
            plugin_id = str(definition["plugin_id"])
            plugin = self._plugins.get(plugin_id)
            if plugin is None:
                raise ValueError(f"Indicator plugin is not registered: {plugin_id}")
            output = plugin.enrich(output, dict(definition.get("params", {})))
        return output


def load_external_plugins(registry: IndicatorRegistry) -> list[str]:
    """Load trusted local modules listed in QUANT_PLUGIN_MODULES.

    Each module must expose ``register_indicators(registry)``. Newly loaded
    plugins are observe-only until a signal rule explicitly references their
    output columns.
    """
    loaded: list[str] = []
    raw = os.getenv("QUANT_PLUGIN_MODULES", "")
    for module_name in (item.strip() for item in raw.split(",")):
        if not module_name:
            continue
        module = importlib.import_module(module_name)
        register = getattr(module, "register_indicators", None)
        if not callable(register):
            raise ValueError(f"{module_name} must define register_indicators(registry)")
        register(registry)
        loaded.append(module_name)
    return loaded

