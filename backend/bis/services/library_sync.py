"""Read-only, incremental folder snapshots. Publication and retiring old versions are atomic."""
from __future__ import annotations

import datetime as dt
import hashlib
import mimetypes
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select

from bis.config import get_settings
from bis.models import Document, KnowledgeSource
from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion
from bis.rag.chunking import detect_designation
from bis.rag.ingest import ingest_pages
from bis.rag.parsers import UnsupportedFormat
from bis.services import archive
from bis.services.documents import save_document

MAX_FILE = 100 * 1024 * 1024
SUPPORTED = {".pdf", ".doc", ".rtf", ".docx", ".xlsx", ".xlsm", ".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def checked_path(value):
    path = Path(value.strip().strip('"')).expanduser().resolve()
    roots = [Path(p).expanduser().resolve() for p in get_settings().library_roots]
    if not any(path == root or root in path.parents for root in roots):
        raise PermissionError("Папка вне разрешённых каталогов BIS_LIBRARY_ROOTS")
    return path


def infer_form(name):
    low = name.lower()
    for kind, words in {"aosr": ("аоср",), "aook": ("аоок",), "letter": ("письм",),
                        "work_journal": ("журнал",), "id_registry": ("реестр",)}.items():
        if any(w in low for w in words):
            return kind
    return None


def is_form(path, level="knowledge"):
    low = path.lower()
    return any(x in low for x in ("шаблон", "бланк", "формы", "forms", "templates")) or (
        level == "knowledge" and infer_form(low) in ("aosr", "aook"))


def retire(db, version):
    if version and version.source_id:
        source = db.get(KnowledgeSource, version.source_id)
        if source:
            source.status = "replaced"
    if version and version.document_id:
        doc = db.get(Document, version.document_id)
        if doc:
            doc.status = "replaced"


def scan(db, storage, embedder, gateway, connection, *, force=False, checkpoint=None):
    root = checked_path(connection.path)
    if not root.is_dir():
        raise FileNotFoundError("Папка недоступна. Проверьте путь и подключение диска.")
    # Finish enumeration before making any deletion decisions. Never follow symlinks.
    paths = []
    def walk_error(error):
        raise error
    for base, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = [d for d in dirs if not d.startswith('.') and not (Path(base) / d).is_symlink()]
        for name in files:
            path = Path(base) / name
            if name.startswith(('.', '~$')) or path.is_symlink():
                continue
            paths.append(path)
    known = {f.relative_path: f for f in db.scalars(select(LibraryFile).where(LibraryFile.connection_id == connection.id))}
    seen, counts = set(), {"updated": 0, "unchanged": 0, "errors": 0, "removed": 0}
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        seen.add(relative)
        item = known.get(relative)
        if item is None:
            item = LibraryFile(connection_id=connection.id, relative_path=relative)
            db.add(item)
            db.flush()
        try:
            with db.begin_nested():
                if path.suffix.lower() not in SUPPORTED:
                    raise UnsupportedFormat("Формат не поддерживается; сохраните документ в PDF, DOCX или XLSX")
                resolved = path.resolve(strict=True)
                if root not in resolved.parents:
                    raise PermissionError("Файл выходит за пределы подключённой папки")
                before = path.stat()
                if before.st_size > MAX_FILE:
                    raise ValueError("Файл больше 100 МБ")
                data = path.read_bytes()
                after = path.stat()
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    raise ValueError("Файл меняется во время чтения; повторите синхронизацию")
                digest = hashlib.sha256(data).hexdigest()
                old = db.get(LibraryVersion, item.active_version_id) if item.active_version_id else None
                if old and old.sha256 == digest and item.state == 'ready' and not force:
                    counts['unchanged'] += 1
                    continue
                pages, method = archive._pages(data, path.name, mimetypes.guess_type(path.name)[0])
                if not any(p.text.strip() for p in pages):
                    raise ValueError(next((p.warning for p in pages if p.warning),
                                          "Нет извлечённого текста. Нужен OCR или проверка файла"))
                text = '\n'.join(p.text for p in pages)
                form = is_form(relative, connection.level)
                # Initial indexing must not wait for a cloud classifier for each file.
                # Unknown types stay visible for review; semantic enrichment runs separately.
                dtype, _ = archive.detect_type(None, text, path.name)
                kind = 'norm' if connection.level == 'knowledge' and not form else (
                    'project_doc' if dtype == 'project_doc' else 'org_document')
                versions = db.scalars(select(LibraryVersion).where(LibraryVersion.file_id == item.id)).all()
                version = LibraryVersion(file_id=item.id, revision=max((v.revision for v in versions), default=0) + 1,
                    sha256=digest, file_key=storage.put(data, path.name, prefix=f'libraries/{connection.id}'),
                    mime=mimetypes.guess_type(path.name)[0], form_kind=infer_form(relative) if form else None,
                    mapping={'format': 'unprepared'} if form else {})
                db.add(version)
                db.flush()
                src = ingest_pages(db, embedder, org_id=connection.organization_id, pages=pages, kind=kind,
                                   title=path.stem)
                src.file_key, src.file_name, src.mime = version.file_key, path.name, version.mime
                src.status = 'unknown' if kind == 'norm' else 'active'
                src.designation = detect_designation(text, path.name) if kind == "norm" else None
                src.meta = {**(src.meta or {}), 'library_version_id': str(version.id), 'relative_path': relative,
                            'level': connection.level, 'text_from': method, 'reading': archive.reading_report(pages, method)}
                version.source_id = src.id
                if connection.organization_id:
                    doc = save_document(db, storage, org_id=connection.organization_id,
                        kind=archive.KIND.get(dtype, 'other') if not form else 'other', title=path.stem,
                        data={'library_scope': connection.level, 'library_version_id': str(version.id),
                              'text_from': method, 'reading': archive.reading_report(pages, method)},
                        file_bytes=data, file_name=path.name, created_by='system:library',
                        object_id=connection.object_id, doc_date=archive.doc_date(text, dtype),
                        mime=version.mime or 'application/octet-stream')
                    doc.source, doc.status = 'library', 'registered'
                    if dtype == 'contract':
                        doc.data = {**doc.data, 'contract_kind': archive.contract_kind(text)}
                    src.document_id, version.document_id = doc.id, doc.id
                retire(db, old)
                item.active_version_id, item.state, item.error = version.id, 'ready', None
                counts['updated'] += 1
        except (OSError, ValueError, UnsupportedFormat) as exc:
            item.state, item.error = 'error', str(exc)[:1000]
            counts['errors'] += 1
        except Exception as exc:  # noqa: BLE001 — isolate parser/model errors to one file
            item.state, item.error = 'error', f'Ошибка чтения: {type(exc).__name__}'
            counts['errors'] += 1
        if checkpoint:
            connection.error = f"Прочитано: {counts['updated']}; ошибок: {counts['errors']}"
            checkpoint()
    for relative, item in known.items():
        if relative not in seen and item.state != 'removed':
            retire(db, db.get(LibraryVersion, item.active_version_id) if item.active_version_id else None)
            item.state, item.error = 'removed', None
            counts['removed'] += 1
    connection.status = 'partial' if counts['errors'] else 'ready'
    connection.error = f"Не прочитано файлов: {counts['errors']}" if counts['errors'] else None
    connection.synced_at = dt.datetime.now(dt.UTC)
    return counts


@contextmanager
def connection_lease(connection_id):
    """Keep folder ownership across per-file commits without holding a row transaction."""
    from sqlalchemy import text

    from bis.db import get_engine

    key = int.from_bytes(connection_id.bytes[:8], 'big', signed=True)
    with get_engine().connect() as lease:
        acquired = bool(lease.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}))
        lease.commit()
        try:
            yield acquired
        finally:
            if acquired:
                lease.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
                lease.commit()


def execute(connection_id, *, force=False):
    from bis.db import SessionLocal
    from bis.rag.embeddings import get_query_embedder
    from bis.services.storage import get_storage

    with connection_lease(connection_id) as acquired:
        if not acquired:
            return
        with SessionLocal() as db:
            connection = db.scalar(select(LibraryConnection).where(LibraryConnection.id == connection_id,
                LibraryConnection.enabled.is_(True)))
            if connection is None:
                return
            connection.status, connection.error = 'reading', None
            db.commit()
            try:
                scan(db, get_storage(), get_query_embedder(), None, connection, force=force, checkpoint=db.commit)
            except Exception as exc:  # noqa: BLE001 — an unavailable mount must not delete its contents
                db.rollback()
                connection = db.get(LibraryConnection, connection_id)
                connection.status, connection.error = 'error', str(exc)[:1000]
            db.commit()
