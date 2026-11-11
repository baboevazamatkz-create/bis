"""Six-section pilot browser validation against the isolated synthetic preview only."""

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright, expect

BASE = os.environ.get("BIS_UI_URL", "http://127.0.0.1:5174").rstrip("/")
ROOT = Path("/workspace/artifacts/bis-pilot-preview")
OUT = Path("/workspace/artifacts/bis-pilot-ui")
OUT.mkdir(parents=True, exist_ok=True)


async def main():
    fixture = json.loads((ROOT / "fixture.json").read_text())
    assert str(ROOT) in fixture["folder"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_default_timeout(15000)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(BASE, wait_until="networkidle")
        objects = await (await page.request.get(BASE + "/api/objects")).json()
        assert any(
            o["id"] == fixture["object_id"] and o["name"].startswith("Учебный:")
            for o in objects
        )
        await expect(page.locator("#app-navigation .nav-item")).to_have_count(6)
        assert await page.locator("input[type=email]").count() == 0
        await page.select_option("#active-project", fixture["object_id"])
        await page.get_by_role("link", name="Базы данных", exact=True).click()
        await page.get_by_role("button", name="Подключить базу", exact=True).click()
        await page.get_by_label("Папка на компьютере с BIS").fill(fixture["folder"])
        await page.get_by_role(
            "button", name="Подключить и прочитать", exact=True
        ).click()
        await expect(page.get_by_text("Файлов: 3", exact=False)).to_be_visible(
            timeout=25000
        )
        libraries = await (await page.request.get(BASE + "/api/libraries")).json()
        next(
            c for c in libraries["connections"] if c["path"] == fixture["folder"]
        )
        if not libraries["strict"]:
            await page.get_by_role(
                "button", name="Использовать только мои базы", exact=True
            ).click()
        await page.get_by_role(
            "link", name="Исполнительная документация", exact=True
        ).click()
        await expect(page.locator(".workspace-document")).to_have_count(1)
        await expect(page.locator(".workspace-document")).to_contain_text("АОСР_17")
        assert not await page.get_by_label("Номер акта", exact=True).count()
        await page.get_by_role("link", name="Исполнительные схемы", exact=True).click()
        await expect(
            page.get_by_role("heading", name="Какие схемы ещё нужны", exact=True)
        ).to_be_visible()
        await expect(
            page.get_by_role("button", name="Поручить подготовку схемы", exact=True)
        ).to_be_visible()
        assert not await page.get_by_label("Проектная отметка, м", exact=True).count()
        await page.get_by_role("link", name="Проект", exact=True).click()
        await expect(page.locator(".workspace-document")).to_have_count(1)
        await page.get_by_label("Поиск документов").fill("125")
        await page.get_by_role("button", name="Найти в списке", exact=True).click()
        await expect(page.locator(".workspace-document")).to_contain_text(
            "Спецификация"
        )
        await page.get_by_label("Поиск документов").fill("does-not-exist")
        await page.get_by_role("button", name="Найти в списке", exact=True).click()
        await expect(page.locator(".workspace-document")).to_have_count(0)
        await page.get_by_role("link", name="Договоры", exact=True).click()
        await page.get_by_role("button", name="Субподряд", exact=True).click()
        await expect(page.locator(".workspace-document")).to_have_count(1)
        await page.get_by_role("button", name="Задать вопрос", exact=True).click()
        await page.get_by_label("Задание помощнику").fill(
            "Какой срок окончания работ по этому договору?"
        )
        await page.get_by_role("button", name="Поручить Синапсу", exact=True).click()
        await expect(page.locator(".msg.assistant")).to_contain_text(
            "17 ноября 2026", timeout=30000
        )
        await expect(
            page.get_by_role("button", name="Остановить запрос", exact=True)
        ).to_have_count(0)
        await page.screenshot(path=str(OUT / "contract-answer.png"))
        await page.get_by_role("link", name="Базы данных", exact=True).click()
        await page.get_by_role("button", name="Очистить базу", exact=True).click()
        await page.get_by_role(
            "button", name="Удалить данные этой базы?", exact=True
        ).click()
        await expect(
            page.get_by_text("База очищена и отключена.", exact=False)
        ).to_be_visible()
        assert len(list(Path(fixture["folder"]).iterdir())) == 3
        await page.get_by_role("button", name="Обновить базу", exact=True).click()
        await expect(page.get_by_text("Файлов: 3", exact=False)).to_be_visible(
            timeout=25000
        )
        await (
            page.locator("details")
            .filter(has=page.locator("summary").filter(has_text="ИИ и качество чтения"))
            .first.locator("summary")
            .click()
        )
        await expect(page.get_by_text("OCR: не подключён", exact=False)).to_be_visible()
        await page.get_by_role("button", name="Подключить ИИ и OCR", exact=True).click()
        await page.get_by_label("Folder ID", exact=True).fill("fixturefolder")
        await page.get_by_label(
            "API-ключ (пусто — сохранить текущий)", exact=True
        ).fill("fixture-not-a-real-key")
        await page.get_by_role(
            "button", name="Сохранить подключение", exact=True
        ).click()
        await expect(
            page.get_by_text("ИИ, OCR и голос настроены.", exact=False)
        ).to_be_visible()
        assert not await page.locator("input[type=password]").count()

        async def failed_model(route):
            await route.fulfill(
                status=200,
                json={
                    "providers": [
                        {
                            "provider": "yandexgpt",
                            "ok": False,
                            "error": "HTTP 403: fixture folder mismatch",
                            "hint": "Тестовая проверка: проверьте каталог ключа",
                        }
                    ]
                },
            )

        await page.route("**/api/ai/models/check", failed_model)
        await page.get_by_role("button", name="Проверить ИИ", exact=True).click()
        await expect(
            page.get_by_text("HTTP 403: fixture folder mismatch", exact=True)
        ).to_be_visible()
        await page.screenshot(path=str(OUT / "databases-desktop.png"))
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.screenshot(path=str(OUT / "databases-mobile.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        results = {
            "navigation_sections": 6,
            "login_form_removed": True,
            "folder_connection": True,
            "aosr_is_act": True,
            "scheme_tasks": True,
            "ordinary_search": True,
            "selected_contract_answer": True,
            "clear_preserves_originals": True,
            "force_reread": True,
            "ai_setup_ui": True,
            "model_errors_visible": True,
            "mobile_no_overflow": True,
            "javascript_errors": errors,
        }
        (OUT / "results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2)
        )
        print(json.dumps(results, ensure_ascii=False))
        await browser.close()


asyncio.run(asyncio.wait_for(main(), 150))
