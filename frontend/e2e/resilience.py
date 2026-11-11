"""Slow and failed requests must not freeze unrelated pilot controls. Uses isolated preview."""

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path

from playwright.async_api import Error, async_playwright, expect

BASE = os.environ.get("BIS_UI_URL", "http://127.0.0.1:5174")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", args=["--no-sandbox"]
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(BASE, wait_until="networkidle")
        await page.get_by_role("link", name="Базы данных", exact=True).click()
        await page.get_by_text("ИИ и качество чтения", exact=True).click()
        await page.get_by_role("button", name="Подключить ИИ и OCR", exact=True).click()
        await page.get_by_label("Folder ID", exact=True).fill("fixturefolder")
        await page.get_by_label("API-ключ", exact=False).fill("fixture-not-a-secret")
        release = asyncio.Event()

        async def slow_model(route):
            await release.wait()
            with suppress(Error):
                await route.fulfill(
                    status=502, json={"detail": "Контрольная ошибка модели"}
                )

        await page.route("**/api/ai/models/check", slow_model)
        await page.get_by_role("button", name="Проверить ИИ", exact=True).click()
        await expect(
            page.get_by_role("button", name="Проверить ИИ", exact=True)
        ).to_be_disabled()
        await expect(
            page.get_by_role("button", name="Сохранить подключение", exact=True)
        ).to_be_enabled()
        await expect(
            page.get_by_role("button", name="Проверить OCR", exact=True)
        ).to_be_enabled()
        refresh_gate = asyncio.Event()
        reads = []

        async def slow_refresh(route):
            reads.append(route.request.url)
            await refresh_gate.wait()
            with suppress(Error):
                await route.fulfill(
                    status=500, json={"detail": "Список временно недоступен"}
                )

        await page.route("**/api/libraries", slow_refresh)
        await page.route(
            "**/api/workspace/ai-setup", lambda r: r.fulfill(json={"saved": True})
        )
        await page.get_by_role(
            "button", name="Сохранить подключение", exact=True
        ).click()
        await expect(
            page.get_by_text(
                "ИИ, OCR и голос настроены. Проверьте ответ модели и перечитайте базу.",
                exact=True,
            )
        ).to_be_visible()
        await page.get_by_role("button", name="Подключить ИИ и OCR", exact=True).click()
        await expect(
            page.get_by_role("button", name="Сохранить подключение", exact=True)
        ).to_be_enabled()
        await page.wait_for_timeout(8500)
        assert len(reads) == 1, f"Overlapping refresh requests: {reads}"
        release.set()
        await expect(page.get_by_role("alert")).to_contain_text(
            "Настройки изменились во время проверки"
        )
        await expect(
            page.get_by_role("button", name="Проверить ИИ", exact=True)
        ).to_be_enabled()
        refresh_gate.set()
        await expect(page.get_by_role("alert")).to_contain_text(
            "Список временно недоступен"
        )
        await page.unroute("**/api/libraries", slow_refresh)
        checks = await page.evaluate("""async () => {
          const {api,fetchBlob}=await import('/src/api.ts');
          const original=window.fetch;const result={};
          try {
            window.fetch=(_url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));
            try {await api('/test',{timeoutMs:25});}catch(e){result.headersDeadline=e.status===408;}
            window.fetch=async (_url,{signal})=>new Response(new ReadableStream({start(c){signal.addEventListener('abort',()=>c.error(new DOMException('Aborted','AbortError')));}}));
            try {await api('/test',{timeoutMs:25});}catch(e){result.bodyDeadline=e.status===408;}
            try {await fetchBlob('/test',{timeoutMs:25});}catch(e){result.fileDeadline=e.status===408;}
            window.fetch=async ()=>{throw new TypeError('Failed to fetch')};
            try {await api('/test');}catch(e){result.networkError=e.status===503;}
            return result;
          } finally {window.fetch=original;}
        }""")
        assert all(checks.values()), checks
        downloads = []

        async def document(route):
            downloads.append(route.request.url)
            await route.fulfill(
                body=b"fixture-docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={
                    "Content-Disposition": "attachment; filename*=UTF-8''act.docx"
                },
            )

        await page.route("**/api/fixture-file", document)
        async with page.expect_download() as event:
            await page.evaluate(
                "async () => {const {openFile}=await import('/src/api.ts');await openFile('/fixture-file','Act without extension');}"
            )
        assert (await event.value).suggested_filename == "act.docx"
        assert len(downloads) == 1
        checks["single_download_with_filename"] = True
        for label in (
            "Исполнительная документация",
            "Исполнительные схемы",
            "Проект",
            "Договоры",
        ):
            await page.get_by_role("link", name=label, exact=True).click()
            await expect(
                page.get_by_role("button", name="Найти в списке", exact=True)
            ).to_be_enabled()
            await page.get_by_role("button", name="Обновить список", exact=True).click()
            await expect(
                page.get_by_role("button", name="Найти в списке", exact=True)
            ).to_be_enabled()
            await page.get_by_label("Задание помощнику").fill("Проверь документы")
            await expect(
                page.get_by_role("button", name="Поручить Синапсу", exact=True)
            ).to_be_enabled()
        assert not errors, errors
        checks.update(
            independent_actions=True,
            refresh_does_not_lock=True,
            single_poll=True,
            lists=True,
            javascript_errors=errors,
        )
        Path("/workspace/artifacts/bis-resilience-results.json").write_text(
            json.dumps(checks, ensure_ascii=False, indent=2)
        )
        print(json.dumps(checks))
        await browser.close()


asyncio.run(main())
