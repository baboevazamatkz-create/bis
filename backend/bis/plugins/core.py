"""Базовый плагин: встроенные инструменты и агенты платформы."""

from __future__ import annotations

from bis import __version__
from bis.agents.base import Agent, Tool
from bis.plugins.sdk import Plugin, PluginManifest


class CorePlugin(Plugin):
    manifest = PluginManifest(
        name="core",
        version=__version__,
        title="Ядро BIS",
        description="Поиск по базе знаний, реестры, АОСР, письма; агенты Нормоконтроль, Секретарь, ПТО, Помощник.",
        permissions=["*"],
    )

    def tools(self) -> list[Tool]:
        from bis.agents.builtin_tools import BUILTIN_TOOLS
        from bis.agents.tools_control import CONTROL_TOOLS

        return [*BUILTIN_TOOLS, *CONTROL_TOOLS]

    def agents(self) -> list[Agent]:
        from bis.agents.builtin_agents import BUILTIN_AGENTS

        return list(BUILTIN_AGENTS)
