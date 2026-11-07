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
