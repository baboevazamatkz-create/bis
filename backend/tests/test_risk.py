"""Рейтинг рисков объектов: балл из фактов и объяснимые причины."""

import datetime as dt


def test_risk_ranking_explains_score(client, auth, db, demo):
    from bis.models import ConstructionObject, Task
    from bis.services.risk import ranking

    obj = db.query(ConstructionObject).filter_by(organization_id=demo.id).first()
    for i in range(2):
        db.add(Task(organization_id=demo.id, title=f"Просроченное {i}", status="open", object_id=obj.id,
                    due_date=dt.date.today() - dt.timedelta(days=3)))
    db.commit()
    rows = ranking(db, demo.id)
    top = next(r for r in rows if r["object_id"] == str(obj.id))
    assert top["factors"]["tasks_overdue"] >= 2 and top["score"] >= 6
    assert any(r.startswith("просрочено поручений") for r in top["reasons"])
    api_rows = client.get("/api/control/risks", headers=auth).json()
    assert api_rows[0]["score"] >= api_rows[-1]["score"]
    digest = client.get("/api/control/digest", headers=auth).json()
    assert "risks" in digest
