"""`bis doctor`: отчёт о состоянии BIS сохраняется в doctor.txt и включает ошибки из журнала."""


def test_doctor_report(db, demo, tmp_path):
    from bis import doctor

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "bis.log").write_text("2026-11-07 10:00:00 WARNING bis.http: медленно: POST /api/chat — 12.0 с\n"
                                               "2026-11-07 10:00:01 INFO bis: обычное сообщение\n", encoding="utf-8")
    report = doctor.run(tmp_path)
    assert "[OK] База данных" in report and "документов" in report and "поиск (повторный)" in report
    assert "медленно: POST /api/chat" in report and "обычное сообщение" not in report
    assert (tmp_path / "doctor.txt").read_text(encoding="utf-8") == report
