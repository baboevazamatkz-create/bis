"""Ассистент не расспрашивает: объекты и договоры — в подсказке, договор и период КС-2 подставляются сами."""

from sqlalchemy import select


def _ctx(db, demo):
    from bis.agents.base import AgentContext
    from bis.models import User
    from bis.services.storage import get_storage

    user = db.scalar(select(User).where(User.organization_id == demo.id, User.role == "admin"))
    return AgentContext(db=db, org_id=demo.id, user=user, gateway=None, embedder=None, storage=get_storage(), agents={})


def test_org_brief_and_closing_without_contract_or_period(db, demo):
    from bis.agents.base import org_brief
    from bis.agents.tools_works import ClosingArgs, closing_check_tool, create_ks2_ks3_tool
    from bis.models import Contract

    ctx = _ctx(db, demo)
    brief = org_brief(ctx)
    contract = db.scalars(select(Contract).where(Contract.organization_id == demo.id)).first()
    assert str(contract.id) in brief and "объект" in brief

    res = closing_check_tool.handler(ctx, ClosingArgs())
    assert res["contract"].startswith("№") and res["assumed"] and "период" in res["assumed"][-1]
    out = create_ks2_ks3_tool.handler(ctx, ClosingArgs())
    assert len(out["documents"]) == 2 and out["period"]
