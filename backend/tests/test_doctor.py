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


def test_cli_finds_local_install_and_starts_embedded_db(tmp_path, monkeypatch):
    """bis.exe import-archive на компьютере с BIS: встроенная база поднимается сама, как у ярлыка BIS."""
    from bis import cli, local

    monkeypatch.delenv("BIS_DATABASE_URL", raising=False)
    monkeypatch.setenv("BIS_HOME", str(tmp_path))
    assert cli._local_home() is None  # без .env и data — не установка BIS
    (tmp_path / ".env").write_text("BIS_ENV=prod\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    assert cli._local_home() == tmp_path
    started = []
    monkeypatch.setattr(local, "prepare", lambda home: started.append(home))
    cli._auto_local("import-archive")
    cli._auto_local("cad-mcp")  # MCP для CAD базу не трогает
    assert started == [tmp_path]
    monkeypatch.setenv("BIS_DATABASE_URL", "postgresql+psycopg://x@server/bis")
    cli._auto_local("import-archive")  # сервер задан явно — его и используем
    assert started == [tmp_path]
