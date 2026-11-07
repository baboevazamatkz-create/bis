import json

import httpx
import pytest

from bis.config import ProviderConfig
from bis.llm.gateway import LLMGateway, ProviderEntry
from bis.llm.pii import PiiMasker, contains_pii
from bis.llm.providers.anthropic_provider import to_anthropic_messages
from bis.llm.providers.gigachat import GigaChatProvider
from bis.llm.providers.offline import ScriptedProvider
from bis.llm.providers.openai_compat import OpenAICompatProvider
from bis.llm.types import LLMError, LLMResponse, Message, ToolCall, ToolSpec


def test_pii_mask_roundtrip():
    text = "Звонить Петрову +7 (916) 123-45-67, petrov@mail.example, паспорт 45 06 123456, ИНН 770123456789. ИНН организации 7701000001."
    m = PiiMasker()
    masked = m.mask(text)
    assert "916" not in masked and "petrov@" not in masked and "123456" not in masked
    assert "770123456789" not in masked
    assert "7701000001" in masked  # ИНН юрлица не маскируется
    assert "паспорт ⟦PASSPORT_1⟧" in masked
    assert m.unmask(masked) == text
    assert contains_pii(text)
    assert not contains_pii("Бетон B25, защитный слой 25 мм")


class Recorder(ScriptedProvider):
    pass


def make_gateway(**providers):
    entries = {name: ProviderEntry(provider=p, max_sensitivity=level, mask_pii=mask)
               for name, (p, level, mask) in providers.items()}
    return LLMGateway(entries=entries, routes={"default": list(providers)})


def test_gateway_routes_confidential_to_local_only():
    cloud = Recorder(["cloud"], name="cloud")
    local = Recorder(["local"], name="local")
    gw = make_gateway(cloud=(cloud, "internal", False), local=(local, "confidential", False))
    assert gw.complete([Message("user", "Вопрос про бетон")]).provider == "cloud"
    r = gw.complete([Message("user", "Договор")], sensitivity="confidential")
    assert r.provider == "local"


def test_gateway_masks_pii_for_cloud():
    cloud = Recorder([Message("assistant", "Позвоню ⟦PHONE_1⟧")], name="cloud")
    gw = make_gateway(cloud=(cloud, "internal", True))
    r = gw.complete([Message("user", "Телефон прораба +7 916 123-45-67")])
    sent = cloud.calls[0]["messages"][0].content
    assert "916" not in sent and "⟦PHONE_1⟧" in sent
    assert r.message.content == "Позвоню +7 916 123-45-67"


def test_gateway_no_provider_for_confidential():
    gw = make_gateway(cloud=(Recorder(name="cloud"), "internal", False))
    with pytest.raises(LLMError):
        gw.complete([Message("user", "x")], sensitivity="confidential")


def test_gateway_fallback_on_error():
    class Failing:
        name, model = "bad", "bad"

        def complete(self, *a, **k):
            raise LLMError("down")

    good = Recorder(["ok"], name="good")
    gw = make_gateway(bad=(Failing(), "internal", False), good=(good, "internal", False))
    assert gw.complete([Message("user", "x")]).provider == "good"


def _client(handler):
    return httpx.Client(base_url="http://llm", transport=httpx.MockTransport(handler))


def test_openai_compat_tool_call_roundtrip():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "qwen", "choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "search_knowledge", "arguments": json.dumps({"query": "засыпка"})}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    cfg = ProviderConfig(name="local", kind="openai_compat", base_url="http://llm", model="qwen")
    p = OpenAICompatProvider(cfg, client=_client(handler))
    history = [Message("system", "sys"), Message("user", "q"),
               Message("assistant", tool_calls=[ToolCall("c0", "find_objects", {})]),
               Message("tool", '{"objects": []}', tool_call_id="c0", name="find_objects")]
    r = p.complete(history, tools=[ToolSpec("search_knowledge", "d", {"type": "object", "properties": {}})])
    assert r.stop_reason == "tool_calls"
    assert r.message.tool_calls[0].arguments == {"query": "засыпка"}
    msgs = seen["body"]["messages"]
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "find_objects"
    assert msgs[3] == {"role": "tool", "tool_call_id": "c0", "content": '{"objects": []}'}
    assert seen["body"]["tools"][0]["function"]["name"] == "search_knowledge"


def test_gigachat_oauth_and_function_call():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if "oauth" in str(request.url):
            assert request.headers["Authorization"] == "Basic KEY"
            return httpx.Response(200, json={"access_token": "TOKEN", "expires_at": 9999999999999})
        assert request.headers["Authorization"] == "Bearer TOKEN"
        body = json.loads(request.content)
        if "functions" in body:
            assert body["functions"][0]["name"] == "search_knowledge"
        return httpx.Response(200, json={"model": "GigaChat-2-Max", "choices": [{"finish_reason": "function_call",
            "message": {"role": "assistant", "content": "", "function_call": {"name": "search_knowledge",
                        "arguments": {"query": "опалубка"}}, "functions_state_id": "st1"}}]})

    cfg = ProviderConfig(name="giga", kind="gigachat", api_key="KEY")
    p = GigaChatProvider(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))
    r = p.complete([Message("user", "q")], tools=[ToolSpec("search_knowledge", "d", {"type": "object"})])
    assert r.message.tool_calls[0].arguments == {"query": "опалубка"}
    assert r.message.raw == {"functions_state_id": "st1"}
    p.complete([Message("user", "q")])
    assert sum("oauth" in str(c.url) for c in calls) == 1  # токен переиспользуется


def test_anthropic_message_conversion_groups_tool_results():
    raw_blocks = [{"type": "thinking", "thinking": "", "signature": "s"},
                  {"type": "tool_use", "id": "t1", "name": "a", "input": {}}]
    msgs = [
        Message("user", "вопрос"),
        Message("assistant", tool_calls=[ToolCall("t1", "a", {}), ToolCall("t2", "b", {})], raw=raw_blocks,
                raw_provider="claude"),
        Message("tool", "r1", tool_call_id="t1"),
        Message("tool", "r2", tool_call_id="t2", is_error=True),
    ]
    out = to_anthropic_messages(msgs, "claude")
    assert out[1]["content"] is raw_blocks  # нативные блоки передаются без изменений
    assert out[2]["role"] == "user" and len(out[2]["content"]) == 2
    assert out[2]["content"][1]["is_error"] is True
    # история от другого провайдера пересобирается из нейтрального формата
    out2 = to_anthropic_messages(msgs, "other")
    assert [b["type"] for b in out2[1]["content"]] == ["tool_use", "tool_use"]


def test_scripted_returns_response_objects():
    resp = LLMResponse(Message("assistant", "x"), "end", "p", "m")
    assert ScriptedProvider([resp]).complete([]) is resp


def test_provider_config_expands_env_and_check_reports_hints(monkeypatch):
    from bis.config import ProviderConfig
    from bis.llm.check import check_providers
    from bis.llm.gateway import LLMGateway, ProviderEntry
    from bis.llm.providers.offline import ScriptedProvider
    from bis.llm.types import LLMError

    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gtest")
    cfg = ProviderConfig(name="y", kind="openai_compat", base_url="https://llm.api.cloud.yandex.net/v1",
                         model="gpt://${YANDEX_FOLDER_ID}/yandexgpt-lite/latest")
    assert cfg.model == "gpt://b1gtest/yandexgpt-lite/latest"

    class Denied:
        model = "gpt://b1gtest/yandexgpt/latest"

        def complete(self, *a, **kw):
            raise LLMError('y: HTTP 403: {"error":{"message":"rpc error: code = PermissionDenied desc = ..."}}')

    gw = LLMGateway(entries={"ok": ProviderEntry(ScriptedProvider(["Москва", '{"ok": true}'], name="ok")),
                             "denied": ProviderEntry(Denied()), "offline": ProviderEntry(ScriptedProvider(name="offline"))},
                    routes={"chat": ["ok", "denied"]})
    gw.entries["offline"].provider.model = "offline"
    res = {r["provider"]: r for r in check_providers(gw)}
    assert res["ok"]["ok"] and res["ok"]["reply"] == "Москва" and res["ok"]["json"] is True and res["ok"]["routes"] == ["chat"]
    assert not res["denied"]["ok"] and "ai.languageModels.user" in res["denied"]["hint"]
    assert "offline" not in res
