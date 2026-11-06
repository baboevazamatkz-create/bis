from bis.config import ProviderConfig
from bis.llm.types import LLMProvider


def build_provider(cfg: ProviderConfig) -> LLMProvider:
    if cfg.kind == "openai_compat":
        from bis.llm.providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(cfg)
    if cfg.kind == "gigachat":
        from bis.llm.providers.gigachat import GigaChatProvider

        return GigaChatProvider(cfg)
    if cfg.kind == "anthropic":
        from bis.llm.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)
    if cfg.kind == "offline":
        from bis.llm.providers.offline import OfflineProvider

        return OfflineProvider(cfg.name)
    if cfg.kind == "scripted":
        from bis.llm.providers.offline import ScriptedProvider

        return ScriptedProvider(name=cfg.name)
    raise ValueError(f"Неизвестный тип провайдера: {cfg.kind}")
