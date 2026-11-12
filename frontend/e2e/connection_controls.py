"""Connection settings and cancellation stay available during delayed status/probes."""

import asyncio
import os
from contextlib import suppress

from playwright.async_api import Error, async_playwright, expect


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", args=["--no-sandbox"]
        )
        page = await browser.new_page()
        release = asyncio.Event()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        async def pending(route):
            await release.wait()
            with suppress(Error):
                await route.abort()

        for url in (
            "**/api/workspace/reading-status",
            "**/api/ai/models/check",
            "**/api/workspace/ocr-check",
        ):
            await page.route(url, pending)
        await page.goto(
            os.environ.get("BIS_UI_URL", "http://127.0.0.1:5174"),
            wait_until="networkidle",
        )
        await page.get_by_role("link", name="Базы данных", exact=True).click()
        await page.get_by_text("ИИ и качество чтения", exact=True).click()
        await expect(
            page.get_by_role("button", name="Подключить ИИ и OCR", exact=True)
        ).to_be_visible()
        await page.get_by_role("button", name="Проверить ИИ", exact=True).click()
        await page.get_by_role("button", name="Проверить OCR", exact=True).click()
        await page.get_by_role("button", name="Подключить ИИ и OCR", exact=True).click()
        await expect(page.get_by_label("Folder ID", exact=True)).to_be_visible()
        await page.get_by_role("button", name="Остановить проверки", exact=True).click()
        await expect(
            page.get_by_role("button", name="Проверить ИИ", exact=True)
        ).to_be_enabled()
        await expect(
            page.get_by_role("button", name="Проверить OCR", exact=True)
        ).to_be_enabled()
        await expect(
            page.get_by_role("button", name="Остановить проверки", exact=True)
        ).to_have_count(0)
        release.set()
        assert not errors, errors
        print(
            "PASS: settings available during stalled status; both probes cancel and unlock; no JS errors"
        )
        await browser.close()


asyncio.run(main())
