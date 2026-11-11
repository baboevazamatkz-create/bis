"""Run against isolated pilot preview; BIS_DOC_FIXTURE points to a real Word 97 DOC.

The synthetic preview fixture is created by the local preview setup, not customer data.
"""

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main():
    fixture = json.loads(
        Path("/workspace/artifacts/bis-pilot-preview/fixture.json").read_text()
    )
    assert fixture["folder"].startswith("/workspace/artifacts/bis-pilot-preview/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", args=["--no-sandbox"]
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(
            os.environ.get("BIS_UI_URL", "http://127.0.0.1:5174"),
            wait_until="networkidle",
        )
        await page.select_option("#active-project", fixture["object_id"])
        await page.get_by_role("link", name="Базы данных", exact=True).click()
        async with page.expect_file_chooser() as chooser:
            await page.get_by_role("button", name="Загрузить файлы", exact=True).click()
        await (await chooser.value).set_files(os.environ["BIS_DOC_FIXTURE"])
        await expect(
            page.get_by_text("Файлы загружены. Началось чтение базы.", exact=True)
        ).to_be_visible(timeout=20000)
        row = (
            page.locator(".database-connection")
            .filter(
                has=page.locator(
                    "strong", has_text=Path(os.environ["BIS_DOC_FIXTURE"]).name
                )
            )
            .first
        )
        await expect(row).to_contain_text("Текст доступен", timeout=20000)
        async with page.expect_file_chooser() as chooser:
            await page.get_by_role("button", name="Загрузить папку", exact=True).click()
        async with page.expect_response(
            lambda r: (
                r.url.endswith("/api/libraries/upload") and r.request.method == "POST"
            )
        ) as response:
            await (await chooser.value).set_files(fixture["folder"])
        assert (await response.value).status == 201
        await expect(
            page.get_by_text("Файлы загружены. Началось чтение базы.", exact=True)
        ).to_be_visible(timeout=20000)
        await page.get_by_text("ИИ и качество чтения", exact=True).click()
        await page.get_by_role("button", name="Проверить OCR", exact=True).click()
        await expect(page.get_by_role("alert")).to_contain_text("API-ключ")
        await page.get_by_role("button", name="База знаний", exact=True).click()
        await expect(
            page.get_by_role("button", name="Загрузить папку", exact=True)
        ).to_be_enabled()
        assert not errors, errors
        await page.screenshot(
            path="/workspace/artifacts/bis-upload-browser.png", full_page=True
        )
        print(
            "PASS: file picker -> real Word 97 DOC upload -> indexed; OCR missing-key error visible; knowledge upload enabled; no JS errors"
        )
        await browser.close()


asyncio.run(main())
