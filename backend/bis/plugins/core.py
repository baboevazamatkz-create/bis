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
        description="База знаний, реестры, документы; агенты: Нормоконтроль, Секретарь, ПТО, Договорной, "
        "Контроль исполнения, Контроль работ, Материалы, Закрытие работ, Помощник.",
        permissions=["*"],
    )

    def tools(self) -> list[Tool]:
        from bis.agents.builtin_tools import BUILTIN_TOOLS
        from bis.agents.tools_control import CONTROL_TOOLS
        from bis.agents.tools_id import ID_TOOLS
        from bis.agents.tools_pd import PD_TOOLS
        from bis.agents.tools_works import WORKS_TOOLS

        return [*BUILTIN_TOOLS, *CONTROL_TOOLS, *WORKS_TOOLS, *PD_TOOLS, *ID_TOOLS]

    def agents(self) -> list[Agent]:
        from bis.agents.builtin_agents import BUILTIN_AGENTS

        return list(BUILTIN_AGENTS)
