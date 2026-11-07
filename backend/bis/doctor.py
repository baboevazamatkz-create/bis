"""Диагностика BIS одной командой: `bis doctor` — база, данные, модели, поиск, голос и сканы, AutoCAD, журнал ошибок.

Отчёт печатается и сохраняется в doctor.txt рядом с данными — его удобно переслать разработчику целиком.
Ключи и пароли в отчёт не попадают.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _timed(fn: Callable[[], Any]) -> tuple[Any, float, str | None]:
    t = time.perf_counter()
    try:
        return fn(), time.perf_counter() - t, None
    except Exception as e:  # noqa: BLE001 — диагностика собирает ошибки, а не падает
        return None, time.perf_counter() - t, f"{type(e).__name__}: {str(e)[:300]}"


def run(workdir: Path | None = None) -> str:
    from sqlalchemy import func, select, text

    from bis import __version__
    from bis.config import get_settings
    from bis.db import SessionLocal
    from bis.models import (
        ConstructionObject,
        Contract,
        Document,
        IntakeEvent,
        KnowledgeChunk,
        KnowledgeSource,
        Organization,
    )

    s = get_settings()
    lines: list[str] = []
    say = lines.append
    pkg = Path(__file__).resolve().parent
    built = max((p.stat().st_mtime for p in pkg.rglob("*.py")), default=0)
    say(f"BIS {__version__}, сборка от {time.strftime('%d.%m.%Y %H:%M', time.localtime(built))}")
    say(f"Модели: {Path(s.llm_config_file).name if s.llm_config_file else 'по умолчанию'}; векторы: {s.embedding_provider}; "
        f"голос: {s.stt_provider}; сканы: {s.ocr_provider}; pgvector: {'да' if s.vector_enabled else 'нет'}")

    # --- база и данные ---
    db = SessionLocal()
    _, dt, err = _timed(lambda: db.execute(text("select 1")).scalar())
    say(f"\n[{'OK' if not err else 'ОШИБКА'}] База данных: {dt * 1000:.0f} мс" + (f" — {err}" if err else ""))
    if err:
        return _finish(lines, workdir)
    count = lambda m, *w: db.scalar(select(func.count()).select_from(m).where(*w))  # noqa: E731
    say(f"  организаций {count(Organization)}, объектов {count(ConstructionObject)}, договоров {count(Contract)}, "
        f"документов {count(Document)} (из архива {count(Document, Document.source == 'archive')}, "
        f"без объекта {count(Document, Document.object_id.is_(None))}, без даты {count(Document, Document.date.is_(None))})")
    kinds = db.execute(select(Document.kind, func.count()).group_by(Document.kind).order_by(func.count().desc())).all()
    say("  по видам: " + ", ".join(f"{k} {n}" for k, n in kinds[:14]))
    chunks = count(KnowledgeChunk)
    with_vec = count(KnowledgeChunk, KnowledgeChunk.embedding.is_not(None))
    say(f"  база знаний: источников {count(KnowledgeSource)}, фрагментов {chunks}, со смысловыми векторами {with_vec}"
        + (" — ⚠ поиск по смыслу работает не по всем: выполните bis reindex" if chunks and with_vec < chunks * 0.9 else ""))
    no_text = count(Document, Document.source == "archive", Document.data["text_from"].astext == "none")
    if no_text:
        say(f"  ⚠ документов архива без текста: {no_text} (DWG, сканы без распознавания) — по содержимому их не найти")
    say(f"  приём с площадки: событий {count(IntakeEvent)}, с ошибкой {count(IntakeEvent, IntakeEvent.status == 'error')}")

    # --- модели ---
    from bis.llm.check import check_providers
    from bis.llm.gateway import get_gateway

    res, dt, err = _timed(lambda: check_providers(get_gateway()))
    if err:
        say(f"\n[ОШИБКА] Модели: {err}")
    else:
        say("\n[OK] Модели:" if all(r.get("ok") for r in res) else "\n[!] Модели:")
        for r in res:
            if r.get("ok"):
                say(f"  ✓ {r['provider']}: ответ за {r['seconds']} с, JSON {'да' if r.get('json') else 'нет'} "
                    f"({', '.join(r.get('routes') or [])})")
            else:
                say(f"  ✗ {r['provider']}: {r.get('error', '')[:200]} {r.get('hint') or ''}")

    # --- смысловой поиск ---
    from bis.rag.embeddings import get_embedder
    from bis.rag.search import hybrid_search

    emb = get_embedder()
    _, dt, err = _timed(lambda: emb.embed_query("армирование фундаментной плиты"))
    say(f"\n[{'OK' if not err else 'ОШИБКА'}] Смысловые векторы ({type(emb).__name__}): {dt:.2f} с" + (f" — {err}" if err else ""))
    org_id = db.scalar(select(Organization.id).order_by(Organization.created_at.desc()))
    for attempt in ("первый", "повторный"):
        hits, dt, err = _timed(lambda: hybrid_search(db, emb, "акт скрытых работ армирование", org_id=org_id, k=5))
        say(f"  поиск ({attempt}): {dt:.2f} с, найдено {len(hits or [])}" + (f" — {err}" if err else ""))

    # --- голос и сканы ---
    from bis import ocr, voice

    say(f"\n[{'OK' if ocr.available() else '!'}] Распознавание сканов: {'включено' if ocr.available() else 'не настроено'}")
    vs, _, err = _timed(voice.status)
    stt = bool(vs and vs.get("stt"))
    say(f"[{'OK' if stt else '!'}] Голос: {'распознавание включено' if stt else (err or 'не настроено (ключ или ffmpeg)')}")

    # --- AutoCAD / nanoCAD ---
    import sys

    if sys.platform == "win32":
        from bis.cad import bridge

        info, dt, err = _timed(lambda: bridge.in_com_thread(bridge.diagnose))
        if err:
            say(f"\n[ОШИБКА] CAD: {err}")
        else:
            ok = info.get("connected") not in (None, "dxf")
            say(f"\n[{'OK' if ok else '!'}] CAD: подключено {info.get('connected')}; запущено {info.get('running')}; "
                f"pywin32 {info.get('pywin32')}; BIS от администратора: {info.get('this_process_admin')}")
            for e in info.get("errors") or []:
                say(f"  ✗ {e}")
    else:
        say("\n[—] CAD: проверка доступна на Windows")
    db.close()
    return _finish(lines, workdir)


def _finish(lines: list[str], workdir: Path | None) -> str:
    log = (workdir / "logs" / "bis.log") if workdir else None
    if log and log.exists():
        tail = [ln.rstrip() for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
                if " WARNING " in ln or " ERROR " in ln][-25:]
        lines.append(f"\nЖурнал ({log}): последние предупреждения и ошибки" if tail else "\nЖурнал: ошибок нет")
        lines.extend("  " + ln[:400] for ln in tail)
    report = "\n".join(lines)
    if workdir:
        (workdir / "doctor.txt").write_text(report, encoding="utf-8")
    return report
