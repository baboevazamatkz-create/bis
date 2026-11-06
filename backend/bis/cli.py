"""Командная строка: bis <команда>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _alembic_upgrade() -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
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

    p = sub.add_parser("serve", help="Запустить API")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        _alembic_upgrade()
        print("Миграции применены")
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
