"""The same fact ledger and investigator are available to every allowed specialist."""
from pydantic import BaseModel, Field

from bis.agents.base import tool
from bis.agents.resolve import OBJ_HINT
from bis.services import project_facts as ledger


class FactQuery(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)
    predicate: str | None = Field(None, description='material, work_start, work_end, batch, quantity, project_ref, certificate_number')
    location: str | None = None
    work: str | None = None
    limit: int = Field(100, ge=1, le=300)


class Investigation(BaseModel):
    question: str = Field(min_length=3, max_length=20000)
    object_id: str | None = Field(None, description=OBJ_HINT)
    budget: int = Field(12, ge=1, le=30)


@tool('query_project_facts', 'Прочитать версионные факты выбранного проекта с цитатами. Плановые материалы не доказывают монтаж. '
      'Пустой реестр не означает отсутствие данных в ещё не разобранных файлах.', scopes=('documents:read',))
def query_project_facts(ctx, args: FactQuery):
    return ledger.query(ctx, args.object_id, predicate=args.predicate, location=args.location, work=args.work, limit=args.limit)


@tool('investigate_project', 'Сначала найти и дочитать источники, извлечь факты и сопоставить противоречия. '
      'Возвращает план, охват, недостающие поля и точные основания. Не заменяет отсутствующее значение догадкой.', scopes=('documents:read',))
def investigate_project(ctx, args: Investigation):
    from bis.brain.orchestrator import investigate
    return investigate(ctx, args.question, args.object_id, budget=args.budget)


FACT_TOOLS = [query_project_facts, investigate_project]
