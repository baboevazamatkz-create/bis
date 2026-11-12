"""An old status response must not undo the result of saving AI settings."""

import asyncio
import os

from playwright.async_api import async_playwright, expect


async def main():
    base = os.environ.get("BIS_UI_URL", "http://127.0.0.1:5174")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", args=["--no-sandbox"]
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        waiting, release = asyncio.Event(), asyncio.Event()
        count = 0
        status = {
            "providers": [],
            "model_error": None,
            "text_configured": True,
            "vision_configured": False,
            "ocr_configured": True,
            "folder_id": "fixturefolder",
            "local_setup_available": True,
            "voice": {"stt": True},
            "key_configured": True,
            "last_model_attempts": [],
        }

        async def reading(route):
            nonlocal count
            count += 1
            if count == 2:
                waiting.set()
                await release.wait()
                await route.fulfill(
                    json={
                        **status,
                        "text_configured": False,
                        "ocr_configured": False,
                        "key_configured": False,
                    }
                )
            else:
                await route.fulfill(json=status)

        await page.route("**/api/workspace/reading-status", reading)
        await page.route(
            "**/api/workspace/ai-setup", lambda r: r.fulfill(json={"saved": True})
        )
        await page.goto(base, wait_until="networkidle")
        await page.get_by_role("link", name="Базы данных", exact=True).click()
        await page.get_by_text("ИИ и качество чтения", exact=True).click()
        await page.get_by_role("button", name="Подключить ИИ и OCR", exact=True).click()
        await page.get_by_label("API-ключ", exact=False).fill(
            "fixture-key-not-a-secret"
        )
        await asyncio.wait_for(waiting.wait(), 15)
        await page.get_by_role(
            "button", name="Сохранить подключение", exact=True
        ).click()
        await expect(
            page.get_by_text(
                "ИИ, OCR и голос настроены. Проверьте ответ модели и перечитайте базу.",
                exact=True,
            )
        ).to_be_visible()
        release.set()
        await page.wait_for_timeout(500)
        panel = page.locator("details.panel").filter(
            has=page.locator("summary", has_text="ИИ и качество чтения")
        )
        await expect(panel).to_contain_text("Языковая модель: настройки сохранены")
        await expect(panel).to_contain_text("Ключ Яндекса: задан")
        await page.reload(wait_until="networkidle")
        await page.get_by_text("ИИ и качество чтения", exact=True).click()
        await expect(panel).to_contain_text("Ключ Яндекса: задан")
        assert not errors, errors
        print(
            "PASS: stale status ignored after save; reload retains configured state; no JS errors"
        )
        await browser.close()


asyncio.run(main())
