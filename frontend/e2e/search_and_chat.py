"""Vite browser checks: visible search failures, stream errors, cancellation and client deadlines.

Use an isolated preview with local auto login. Chat/search requests are intercepted; no documents are created.
"""

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect

BASE = os.environ.get("BIS_UI_URL", "http://127.0.0.1:5174").rstrip("/")
OUT = Path(
    os.environ.get("BIS_BROWSER_ARTIFACTS", "/workspace/artifacts/bis-search-browser")
)
OUT.mkdir(parents=True, exist_ok=True)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_default_timeout(8000)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(BASE + "/#/id/project", wait_until="networkidle")

        async def fail(route):
            await route.fulfill(status=500, json={"detail": "Тестовая ошибка поиска"})

        await page.route("**/api/workspace/documents?**", fail)
        await page.get_by_label("Поиск документов").fill("КЖ0")
        await page.get_by_role("button", name="Найти в списке", exact=True).click()
        await expect(page.get_by_role("alert")).to_have_text("Тестовая ошибка поиска")
        await expect(
            page.get_by_role("button", name="Найти в списке", exact=True)
        ).to_be_enabled()
        await page.screenshot(path=str(OUT / "visible-search-error.png"))
        await page.goto(BASE + "/#/id/chat", wait_until="networkidle")

        async def stream_error(route):
            await route.fulfill(
                status=200,
                content_type="text/event-stream",
                body="data: "
                + json.dumps({"type": "step", "label": "Проверяю подключение"})
                + "\r\n\r\ndata: "
                + json.dumps(
                    {"type": "error", "detail": "Тест: каталог модели не совпадает"}
                )
                + "\r\n\r\n",
            )

        await page.route("**/api/chat/stream", stream_error)
        await page.get_by_label("Сообщение Синапсу").fill(
            "Подготовь АООК по имеющимся актам"
        )
        await page.get_by_role("button", name="Отправить сообщение", exact=True).click()
        await expect(page.locator(".alert.error")).to_have_text(
            "Тест: каталог модели не совпадает"
        )
        await page.get_by_label("Сообщение Синапсу").fill("Следующий запрос")
        await expect(
            page.get_by_role("button", name="Отправить сообщение", exact=True)
        ).to_be_enabled()
        await page.unroute("**/api/chat/stream", stream_error)
        release = asyncio.Event()

        async def pending(route):
            await release.wait()
            with suppress(PlaywrightError):
                await route.abort()

        await page.route("**/api/chat/stream", pending)
        await page.get_by_label("Сообщение Синапсу").fill("Проверка остановки")
        await page.get_by_role("button", name="Отправить сообщение", exact=True).click()
        await page.get_by_role("button", name="Остановить запрос", exact=True).click()
        await expect(page.locator(".alert.error")).to_contain_text("Запрос остановлен")
        await page.get_by_label("Сообщение Синапсу").fill("Следующий запрос")
        await expect(
            page.get_by_role("button", name="Отправить сообщение", exact=True)
        ).to_be_enabled()
        release.set()
        result = await page.evaluate("""async () => {
   const {streamChat,api}=await import('/src/api.ts');
   const original=window.fetch;
   const results={};
   try {
    window.fetch=(_url,{signal}={})=>new Promise((resolve,reject)=>{
     if(signal?.aborted) reject(new DOMException('Aborted','AbortError'));
     else signal?.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
    });
    try {await streamChat({},()=>{},{timeoutMs:25});} catch(e){results.timeout=e.status===408;}
    window.fetch=async ()=>{
     const text='data: '+JSON.stringify({type:'done',reply:{text:'Готово'}})+'\\r\\n\\r\\n';
     const bytes=new TextEncoder().encode(text);
     return new Response(new ReadableStream({start(c){for(let i=0;i<bytes.length;i+=2)c.enqueue(bytes.slice(i,i+2));c.close();}}),{headers:{'Content-Type':'text/event-stream'}});
    };
    const answer=await streamChat({},()=>{});results.crlf=answer.text==='Готово';
    window.fetch=async()=>new Response('<html>Wrong server</html>',{status:200});
    try {await api('/brain/search');} catch(e){results.invalidJson=e.status===502;}
    return results;
   }finally{window.fetch=original;}
  }""")
        assert all(result.values()), result
        assert not errors, errors
        result.update(
            {
                "document_search_error_visible": True,
                "stream_error_visible": True,
                "cancel_unblocks_chat": True,
                "javascript_errors": errors,
            }
        )
        (OUT / "results.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(json.dumps(result, ensure_ascii=False))
        await browser.close()


asyncio.run(asyncio.wait_for(main(), 90))
