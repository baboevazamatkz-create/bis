from pydantic import BaseModel

from bis.agents.base import Agent, Tool, ToolRegistry, _inline_refs
from bis.agents.supervisor import Supervisor
from bis.llm.gateway import LLMGateway, ProviderEntry
from bis.llm.providers.offline import ScriptedProvider
from bis.plugins.sdk import Plugin, PluginError, PluginManifest, PluginRegistry, discover


def test_supervisor_keywords_and_tie_break():
    reg = discover()
    sup = reg.supervisor()
    assert sup.route("Какие требования СП к опалубке?")[0] == "norms"
    assert sup.route("Подготовь письмо заказчику о сроках")[0] == "secretary"
    # равенство ключевых слов без модели — побеждает агент-исполнитель
    assert sup.route("Сделай АОСР по СП")[0] == "pto"
    assert sup.route("Привет")[0] == "assistant"


def test_supervisor_llm_route():
    reg = discover()
    llm = ScriptedProvider(['{"agent": "secretary"}'])
    gw = LLMGateway(entries={"x": ProviderEntry(llm, "confidential")})
    name, how = reg.supervisor().route("Нужно ответить заказчику", gw)
    assert (name, how) == ("secretary", "моделью")
    assert llm.calls[0]["json_mode"] is True


def test_inline_refs_removes_defs():
    class Inner(BaseModel):
        a: int

    class Outer(BaseModel):
        inner: Inner
        items: list[Inner]

    schema = _inline_refs(Outer.model_json_schema())
    assert "$defs" not in str(schema) and "$ref" not in str(schema)
    assert schema["properties"]["inner"]["properties"]["a"]["type"] == "integer"


class EmptyArgs(BaseModel):
    pass


def _tool(name, scopes=()):
    return Tool(name=name, description="d", args_model=EmptyArgs, handler=lambda ctx, a: {}, scopes=scopes)


def test_plugin_permissions_enforced():
    class Bad(Plugin):
        manifest = PluginManifest(name="bad", version="1", title="Bad", permissions=["knowledge:read"])

        def tools(self):
            return [_tool("bad_tool", ("documents:write",))]

    class Good(Plugin):
        manifest = PluginManifest(name="good", version="1", title="Good", permissions=["documents:write"])

        def tools(self):
            return [_tool("good_tool", ("documents:write",))]

        def agents(self):
            return [Agent(name="estimator", title="Сметчик", description="Сметы", instructions="", tools=["good_tool"])]

    reg = PluginRegistry()
    try:
        reg.load(Bad())
        raise AssertionError("ожидалась ошибка")
    except PluginError:
        pass
    reg.load(Good())
    assert reg.tools.get("good_tool") and "estimator" in reg.agents


def test_registry_rejects_duplicate_tools():
    reg = ToolRegistry()
    reg.register(_tool("x"))
    try:
        reg.register(_tool("x"))
        raise AssertionError("ожидалась ошибка")
    except ValueError:
        pass


def test_core_plugin_loaded():
    reg = discover()
    assert "core" in reg.plugins
    assert Supervisor(reg.agents).default == "assistant"
