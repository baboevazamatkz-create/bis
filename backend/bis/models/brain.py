"""«Синапс»: связи между документами (АОСР → сертификат, письмо → ответ, изменение РД → прежнее)."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from bis.db import Base, IdMixin, TenantMixin, TimestampMixin


class DocumentLink(IdMixin, TimestampMixin, TenantMixin, Base):
    """from_document ссылается на to_document (to_document может ещё не быть в архиве — тогда только номер)."""

    __tablename__ = "document_links"
    __table_args__ = (UniqueConstraint("from_document_id", "relation", "ref_number", name="uq_document_link"),)

    from_document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"),
                                                        index=True)
    to_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True),
                                                             ForeignKey("documents.id", ondelete="SET NULL"), index=True)
    # quality | protocol | scheme | letter (ответ на) | contract | act | rd (лист РД) | revision (прежнее изменение)
    relation: Mapped[str] = mapped_column(String(30))
    ref_number: Mapped[str] = mapped_column(String(200), default="")
    label: Mapped[str | None] = mapped_column(String(300))
