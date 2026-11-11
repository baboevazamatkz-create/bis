"""Shared project-reading methods for authorized specialists."""
import uuid

from pydantic import BaseModel, Field

from bis.agents.base import tool
from bis.models import Document
from bis.services import project_alignment, project_reading


class ReadProject(BaseModel):
    document_id: str
    page_budget: int = Field(4, ge=1, le=12)
    with_vision: bool | None = Field(None, description='None — использовать подключённую модель зрения автоматически; false — только текст')


@tool('read_project_document', 'Прочитать следующие страницы РД: штампы, строки спецификаций, элементы и ссылки. '
      'Возвращает цитаты и охват. Визуальное предположение не является подтверждённой позицией.', scopes=('documents:read',))
def read_project_document(ctx, args: ReadProject):
    vision = args.with_vision if args.with_vision is not None else bool(ctx.gateway and ctx.gateway.has_capability('vision', 'vision'))
    return project_reading.view(project_reading.read(ctx, args.document_id, budget=args.page_budget, with_vision=vision))


class CompareAct(BaseModel):
    document_id: str


@tool('compare_act_with_project', 'Сопоставить АОСР/АООК с прочитанной РД по месту, элементу, марке и объёму. '
      'Не суммировать разные позиции и не подменять фактические материалы проектными.', scopes=('documents:read',))
def compare_act_with_project(ctx, args: CompareAct):
    doc = ctx.db.get(Document, uuid.UUID(args.document_id))
    if doc is None or doc.organization_id != ctx.org_id or doc.kind not in ('aosr', 'aook') or not doc.object_id:
        raise LookupError('Акт выбранного проекта не найден')
    return project_alignment.compare_act(ctx, doc)


PROJECT_READING_TOOLS = [read_project_document, compare_act_with_project]
