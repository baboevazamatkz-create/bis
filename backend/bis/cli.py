"""Командная строка: bis <команда>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _alembic_upgrade() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    command.upgrade(cfg, "head")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bis", description="BIS — управление платформой")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="Применить миграции БД")
    sub.add_parser("seed-demo", help="Создать демо-организацию и данные")

    p = sub.add_parser("create-org", help="Создать организацию и администратора")
    p.add_argument("--name", required=True)
    p.add_argument("--inn")
    p.add_argument("--admin-email", required=True)
    p.add_argument("--admin-password", required=True)
    p.add_argument("--admin-name", default="Администратор")
    p.add_argument("--platform-admin", action="store_true")

    p = sub.add_parser("ingest", help="Загрузить файлы в базу знаний")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--kind", default="norm")
    p.add_argument("--org-inn", help="ИНН организации; без него — в общую нормативную библиотеку")
    p.add_argument("--status", default="active")

    p = sub.add_parser("worker", help="Фоновые задачи: контроль сроков, почта, план-факт")
    p.add_argument("--poll", type=int, default=60)
    p = sub.add_parser("run-jobs", help="Однократно выполнить фоновые задачи (для cron)")
    p.add_argument("names", nargs="*")

    p = sub.add_parser("eval", help="Оценка качества поиска, маршрутизации и ответов на наборе вопросов")
    p.add_argument("cases", type=Path, nargs="?", help="JSON-набор; по умолчанию демо-набор")
    p.add_argument("--org-inn", required=True)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--answers", action="store_true", help="Проверять ответы агентов (нужна модель)")

    p = sub.add_parser("local", help="Локальный запуск без Docker: встроенная БД, API, веб, фоновые задачи, Telegram")
    p.add_argument("--dir", type=Path, default=Path.cwd(), help="Папка с .env и данными")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--setup", action="store_true", help="Только подготовить базу и выполнить команду --then")
    p.add_argument("--then", nargs=argparse.REMAINDER, default=[], help="Команда bis после подготовки (seed-demo, create-org …)")
    sub.add_parser("cad-mcp", help="MCP-сервер для AutoCAD/nanoCAD (ИИ рисует в чертеже)")
    sub.add_parser("cad-check", help="Диагностика подключения к AutoCAD/nanoCAD")
    p = sub.add_parser("doctor", help="Диагностика BIS: база, данные, модели, поиск, голос, сканы, AutoCAD, журнал")
    p.add_argument("--dir", type=Path, default=None, help="Папка локальной установки (C:\\BIS) — запустит встроенную базу")
    p = sub.add_parser("import-archive", help="Загрузить архив организации из папки (документы, договоры, письма, схемы)")
    p.add_argument("folder", type=Path)
    p.add_argument("--org-inn", help="ИНН организации (по умолчанию — первая)")
    p.add_argument("--object", help="Шифр объекта для всех файлов (иначе — по папке и тексту)")
    p.add_argument("--no-contracts", action="store_true", help="Не разбирать договоры на обязательства")
    p = sub.add_parser("reindex", help="Смысловые векторы: досчитать недостающие (после смены модели — все)")
    p.add_argument("--all", action="store_true", help="Пересчитать все векторы заново")
    sub.add_parser("llm-check", help="Проверить подключение моделей: ответ, JSON, время")
    sub.add_parser("telegram-poll", help="Telegram-бот в режиме опроса (без входящих соединений, закрытый контур)")

    p = sub.add_parser("serve", help="Запустить API")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        _alembic_upgrade()
        print("Миграции применены")
        return 0
    if args.cmd in ("worker", "run-jobs"):
        import logging

        from bis.db import SessionLocal
        from bis.jobs import load_all_jobs, run_job, worker

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        jobs = load_all_jobs()
        if args.cmd == "worker":
            worker(SessionLocal, poll_seconds=args.poll)
            return 0
        for name in args.names or list(jobs):
            if name not in jobs:
                print(f"Неизвестная задача: {name}. Доступны: {', '.join(jobs)}", file=sys.stderr)
                return 1
            r = run_job(SessionLocal, name)
            print(f"{name}: {r.status}")
        return 0
    if args.cmd == "local":
        from bis import local

        if args.setup:
            local.prepare(args.dir.resolve())
            return main(args.then) if args.then else 0
        local.run(args.dir.resolve(), args.host, args.port)
        return 0
    if args.cmd == "cad-mcp":
        from bis.cad.mcp_server import main as cad_main

        cad_main()
        return 0
    if args.cmd == "doctor":
        from bis import doctor

        workdir = None
        if args.dir:
            from bis import local

            workdir = args.dir.resolve()
            local.prepare(workdir)
        report = doctor.run(workdir)
        print(report)
        if workdir:
            print(f"\nОтчёт сохранён: {workdir / 'doctor.txt'}")
        return 0
    if args.cmd == "cad-check":
        import json

        from bis.cad.bridge import diagnose

        print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "import-archive":
        from collections import Counter

        from sqlalchemy import select

        from bis.db import SessionLocal
        from bis.llm.gateway import get_gateway
        from bis.models import ConstructionObject, Organization
        from bis.rag.embeddings import get_embedder
        from bis.services.archive import import_file
        from bis.services.storage import get_storage

        with SessionLocal() as db:
            q = select(Organization)
            org = db.scalar(q.where(Organization.inn == args.org_inn) if args.org_inn else q.order_by(Organization.created_at))
            if org is None:
                print("Организация не найдена", file=sys.stderr)
                return 1
            obj_id = None
            if args.object:
                obj = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == org.id,
                                                                 ConstructionObject.code == args.object))
                obj_id = obj.id if obj else None
            files = [f for f in sorted(args.folder.rglob("*")) if f.is_file()]
            stats: Counter = Counter()
            for i, f in enumerate(files, 1):
                try:
                    r = import_file(db, get_storage(), get_gateway(), get_embedder(), org.id, data=f.read_bytes(),
                                    name=f.name, object_id=obj_id, folder=str(f.parent.relative_to(args.folder)),
                                    analyze_contracts=not args.no_contracts)
                    db.commit()
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    r = {"status": "error", "error": str(e)[:200]}
                stats[r.get("type") or r["status"]] += 1
                print(f"[{i}/{len(files)}] {f.name}: {r.get('type', r['status'])}" + (f" — {r['error']}" if r.get("error") else ""))
            print("Итого:", ", ".join(f"{k}: {v}" for k, v in stats.most_common()))
        return 0
    if args.cmd == "reindex":
        from sqlalchemy import select

        from bis.config import get_settings
        from bis.db import SessionLocal
        from bis.models import KnowledgeChunk, KnowledgeSource
        from bis.rag.embeddings import get_embedder
        from bis.rag.ingest import embedding_text

        emb = get_embedder()
        dim = getattr(emb, "dim", None) or get_settings().embedding_dim
        signature = f"{type(emb).__name__}:{getattr(emb, 'doc_model', getattr(emb, 'model', ''))}:{dim}"
        marker = Path(get_settings().storage_dir).resolve().parent / ".embedder"
        previous = marker.read_text(encoding="utf-8").strip() if marker.exists() else signature
        full = args.all or previous != signature  # сменилась модель — векторы несравнимы, считаем все
        with SessionLocal() as db:
            ids = [cid for cid, vec in db.execute(select(KnowledgeChunk.id, KnowledgeChunk.embedding)).all()
                   if full or vec is None or len(vec) != dim]
            print(("Пересчёт всех векторов" if full else "Досчёт недостающих векторов") + f": {len(ids)}")
            for i in range(0, len(ids), 64):
                part = db.execute(select(KnowledgeChunk, KnowledgeSource).join(
                    KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id).where(KnowledgeChunk.id.in_(ids[i:i + 64]))).all()
                vecs = emb.embed_documents([embedding_text(s.designation or s.title, c.section_path, c.heading, c.content)
                                            for c, s in part])
                for (c, _), v in zip(part, vecs, strict=True):
                    c.embedding = v
                db.commit()
                print(f"{min(i + 64, len(ids))}/{len(ids)}")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(signature, encoding="utf-8")
        return 0
    if args.cmd == "llm-check":
        from bis.llm.check import check_providers
        from bis.llm.gateway import get_gateway

        results = check_providers(get_gateway())
        if not results:
            print("Модели не настроены: работает офлайн-режим (правила). См. BIS_LLM_CONFIG_FILE.")
        for r in results:
            if r["ok"]:
                print(f"✓ {r['provider']} ({r['model']}): «{r['reply']}», {r['seconds']} с, JSON: "
                      f"{'да' if r['json'] else 'нет'}; задачи: {', '.join(r['routes']) or '—'}")
            else:
                print(f"✗ {r['provider']} ({r['model']}): {r['error']}" + (f"\n  → {r['hint']}" if r.get("hint") else ""))
        return 0 if results and all(r["ok"] for r in results) else 1
    if args.cmd == "telegram-poll":
        import logging

        from bis.connectors.telegram import poll

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        poll()
        return 0
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("bis.main:app", host=args.host, port=args.port)
        return 0

    from sqlalchemy import select

    from bis.db import SessionLocal
    from bis.models import Organization
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    with SessionLocal() as db:
        if args.cmd == "seed-demo":
            from bis.demo import DEMO_EMAIL, DEMO_PASSWORD, seed_demo

            org = seed_demo(db, get_storage(), get_embedder())
            db.commit()
            print(f"Демо-организация: {org.name}\nВход: {DEMO_EMAIL} / {DEMO_PASSWORD} (также pto@demo.bis, manager@demo.bis)")
        elif args.cmd == "create-org":
            from bis.demo import create_organization

            org, admin = create_organization(db, name=args.name, inn=args.inn, admin_email=args.admin_email,
                                             admin_password=args.admin_password, admin_name=args.admin_name,
                                             platform_admin=args.platform_admin)
            db.commit()
            print(f"Создана организация {org.name} ({org.id}), администратор {admin.email}")
        elif args.cmd == "eval":
            import json as _json

            from bis.evals import EvalCase, run_eval
            from bis.llm.gateway import get_gateway
            from bis.plugins.sdk import get_registry

            org = db.scalar(select(Organization).where(Organization.inn == args.org_inn))
            if org is None:
                print(f"Организация с ИНН {args.org_inn} не найдена", file=sys.stderr)
                return 1
            path = args.cases or Path(__file__).parent / "demo" / "eval_demo.json"
            cases = [EvalCase.model_validate(c) for c in _json.loads(path.read_text(encoding="utf-8"))]
            sup = get_registry().supervisor()
            answer_fn = None
            if args.answers:
                from bis.agents.base import AgentContext
                from bis.models import User

                admin = db.scalar(select(User).where(User.organization_id == org.id, User.role == "admin"))
                reg = get_registry()

                def answer_fn(q: str) -> str:
                    ctx = AgentContext(db=db, org_id=org.id, user=admin, gateway=get_gateway(), embedder=get_embedder(),
                                       storage=get_storage(), agents=reg.agents, tool_registry=reg.tools)
                    name, _ = sup.route(q, get_gateway())
                    return reg.agents[name].run(ctx, reg.tools, [], q).text

            report = run_eval(db, get_embedder(), org.id, cases, k=args.k, answer_fn=answer_fn,
                              route_fn=lambda q: sup.route(q)[0])
            db.rollback()  # оценка не меняет данные
            print(_json.dumps(report, ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "ingest":
            from bis.rag.ingest import ingest_bytes

            org_id = None
            if args.org_inn:
                org = db.scalar(select(Organization).where(Organization.inn == args.org_inn))
                if org is None:
                    print(f"Организация с ИНН {args.org_inn} не найдена", file=sys.stderr)
                    return 1
                org_id = org.id
            files = [f for p in args.paths for f in (sorted(p.rglob("*")) if p.is_dir() else [p]) if f.is_file()]
            for f in files:
                try:
                    src = ingest_bytes(db, get_storage(), get_embedder(), org_id=org_id, data=f.read_bytes(),
                                       filename=f.name, kind=args.kind, status=args.status)
                    db.commit()
                    print(f"✓ {f.name}: {src.designation or src.title} — фрагментов {src.chunk_count}")
                except Exception as e:  # noqa: BLE001 — продолжаем пакетную загрузку
                    db.rollback()
                    print(f"✗ {f.name}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
