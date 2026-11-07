"""Исполнительные схемы: DXF в реестре документов, SVG-предпросмотр, опционально DWG через ODA File Converter."""

from __future__ import annotations

import logging
import subprocess
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from bis.cad.scheme import SchemeData, build_dxf, dxf_bytes, summary, svg_preview
from bis.config import get_settings
from bis.models import Document
from bis.services.documents import save_document
from bis.services.storage import Storage

log = logging.getLogger(__name__)


def convert_to_dwg(dxf: bytes) -> bytes | None:
    """DXF → DWG (AutoCAD 2018) через ODA File Converter, если он установлен (BIS_ODA_CONVERTER)."""
    exe = get_settings().oda_converter
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        Path(src, "scheme.dxf").write_bytes(dxf)
        try:
            subprocess.run([exe, src, dst, "ACAD2018", "DWG", "0", "1", "*.DXF"], check=True, timeout=120,
                           capture_output=True)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("ODA File Converter: %s", e)
            return None
        out = Path(dst, "scheme.dwg")
        return out.read_bytes() if out.exists() else None


def create_scheme(db: Session, storage: Storage, org_id: uuid.UUID, data: SchemeData, *, actor: str,
                  object_id: uuid.UUID | None = None) -> Document:
    doc_dxf, checks, scale = build_dxf(data)
    content = dxf_bytes(doc_dxf)
    info = summary(checks, data)
    preview_key = storage.put(svg_preview(doc_dxf).encode("utf-8"), "preview.svg", prefix=f"documents/{org_id}/previews")
    extra: dict = {"scale": scale, "summary": info, "checks": [c.model_dump() for c in checks],
                   "input": data.model_dump(mode="json"), "preview_key": preview_key}
    dwg = convert_to_dwg(content)
    if dwg:
        extra["dwg_key"] = storage.put(dwg, "scheme.dwg", prefix=f"documents/{org_id}")
    issues = [{"level": "warning", "field": "tolerance", "message": info["conclusion"]}] if info["out_of_tolerance"] else []
    safe = (data.code or "schema").replace("/", "-")
    return save_document(db, storage, org_id=org_id, kind="scheme", title=data.title[:900], data=extra, file_bytes=content,
                         file_name=f"{safe}.dxf", created_by=actor, number=data.code or None, object_id=object_id,
                         issues=issues, mime="image/vnd.dxf")
