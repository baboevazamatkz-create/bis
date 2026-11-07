"""Обработчики API не должны блокировать сервер: долгие (ИИ, OCR, голос, загрузка) — только синхронные,
их FastAPI выполняет в пуле потоков. async-обработчик с синхронной работой внутри замораживает весь BIS."""

import ast
from pathlib import Path

ALLOWED = {("telegram.py", "webhook")}  # только принимает обновление и ставит обработку в фон


def test_no_async_endpoints_with_blocking_work():
    api = Path(__file__).resolve().parents[1] / "bis" / "api"
    bad = []
    for path in api.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef) and (path.name, node.name) not in ALLOWED:
                bad.append(f"{path.name}:{node.lineno} {node.name}")
    assert not bad, "async-обработчики блокируют сервер: " + ", ".join(bad)
