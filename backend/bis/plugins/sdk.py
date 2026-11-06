"""SDK плагинов.

Плагин — Python-пакет с точкой входа в группе `bis.plugins`, возвращающей подкласс Plugin.
Плагин объявляет манифест (имя, версия, разрешения) и предоставляет инструменты и агентов.
Инструмент плагина может требовать только те права (scopes), что объявлены в манифесте.

Пример pyproject.toml плагина:
    [project.entry-points."bis.plugins"]
    estimates = "bis_estimates:EstimatesPlugin"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points

from bis.agents.base import Agent, Tool, ToolRegistry
from bis.agents.supervisor import Supervisor

log = logging.getLogger(__name__)
ENTRY_POINT_GROUP = "bis.plugins"


@dataclass
class PluginManifest:
    name: str
    version: str
    title: str
    description: str = ""
    permissions: list[str] = field(default_factory=list)


class Plugin:
    manifest: PluginManifest

    def tools(self) -> list[Tool]:
        return []

    def agents(self) -> list[Agent]:
        return []


class PluginError(Exception):
    pass


@dataclass
class PluginRegistry:
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    agents: dict[str, Agent] = field(default_factory=dict)
    plugins: dict[str, PluginManifest] = field(default_factory=dict)

    def load(self, plugin: Plugin) -> None:
        m = plugin.manifest
        if m.name in self.plugins:
            raise PluginError(f"Плагин {m.name} уже загружен")
        allowed = set(m.permissions)
        tools = plugin.tools()
        for t in tools:
            extra = set(t.scopes) - allowed
            if extra and "*" not in allowed:
                raise PluginError(f"Плагин {m.name}: инструмент {t.name} требует необъявленные права {sorted(extra)}")
        agents = plugin.agents()
        for t in tools:
            self.tools.register(t)
        for a in agents:
            if a.name in self.agents:
                raise PluginError(f"Агент {a.name} уже зарегистрирован")
            self.agents[a.name] = a
        self.plugins[m.name] = m

    def supervisor(self) -> Supervisor:
        return Supervisor(self.agents)


def discover(enabled: list[str] | None = None) -> PluginRegistry:
    registry = PluginRegistry()
    found: dict[str, type[Plugin]] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            found[ep.name] = ep.load()
        except Exception as e:  # noqa: BLE001 — сбой стороннего плагина не должен ронять платформу
            log.error("Не удалось загрузить плагин %s: %s", ep.name, e)
    if "core" not in found:
        from bis.plugins.core import CorePlugin

        found["core"] = CorePlugin
    order = ["core"] + sorted(n for n in found if n != "core")
    for name in order:
        if enabled is not None and name != "core" and name not in enabled:
            continue
        registry.load(found[name]())
    return registry


_registry: PluginRegistry | None = None


def get_registry() -> PluginRegistry:
    global _registry
    if _registry is None:
        from bis.config import get_settings

        _registry = discover(get_settings().enabled_plugins)
    return _registry
