"""Configure the installed local pilot through stdin, without its HTTP server or cloud calls."""
import os
import sys
from pathlib import Path


def configure(root: Path, key: str) -> None:
    from bis.local import load_env

    root = root.resolve()
    if not (root / '.env').is_file():
        raise RuntimeError('BIS .env not found')
    os.chdir(root)
    load_env(root / '.env', prefer_file=True)
    os.environ['BIS_LOCAL_DIR'] = str(root)
    from bis.config import get_settings

    get_settings.cache_clear()
    from bis.api.workspace import AIProviderSetup, _save_ai_provider
    from bis.services.local_config import LOCK

    with LOCK:
        result = _save_ai_provider(AIProviderSetup(provider='openai', model='gpt-4.1-mini', api_key=key or None))
        settings = get_settings()
        providers = {p.name: p for p in settings.llm_providers()}
        if settings.llm_routes()['default'][0] != 'openai' or not providers['openai'].resolved_api_key():
            raise RuntimeError('OpenAI configuration verification failed')
    if not result['saved']:
        raise RuntimeError('Configuration was not saved')
    print('OPENAI SAVED: provider=openai; model=gpt-4.1-mini; key present; OCR=OpenAI Vision.', flush=True)
    print('This confirms saved settings. API access is checked separately in BIS.', flush=True)


if __name__ == '__main__':
    supplied_key = sys.stdin.readline().strip()
    try:
        configure(Path(sys.argv[1]), supplied_key)
    except Exception as exc:  # noqa: BLE001 — print a sanitized error without a secret-bearing traceback
        message = str(getattr(exc, 'detail', exc))
        for value in [supplied_key, *(v for k, v in os.environ.items() if 'KEY' in k or 'TOKEN' in k)]:
            if value and len(value) >= 8:
                message = message.replace(value, '[REDACTED]')
        print('CONFIGURATION ERROR: ' + message, file=sys.stderr)
        sys.exit(1)
