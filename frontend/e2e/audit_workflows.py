"""Navigation, dialogue context, microphone cleanup and executive job visibility."""

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path

from playwright.async_api import Error, async_playwright, expect


async def main():
    base = os.environ.get("BIS_UI_URL", "http://127.0.0.1:5174")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", args=["--no-sandbox"]
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.add_init_script("""
          window.stoppedTracks=0;
          Object.defineProperty(navigator.mediaDevices,'getUserMedia',{value:async()=>({getTracks:()=>[{stop:()=>window.stoppedTracks++}]})});
          window.MediaRecorder=class {state='inactive';mimeType='audio/webm';start(){this.state='recording';}stop(){this.state='inactive';this.ondataavailable?.({data:new Blob(['audio'])});this.onstop?.();}};
          window.AudioContext=class {createAnalyser(){return {fftSize:1024,getFloatTimeDomainData:b=>b.fill(.1)}}createMediaStreamSource(){return {connect(){}}}close(){return Promise.resolve()}};
        """)
        old_doc = "10000000-0000-0000-0000-000000000001"
        history = "20000000-0000-0000-0000-000000000002"
        requests = []

        async def answer(route):
            requests.append(route.request.post_data_json)
            await route.fulfill(
                content_type="text/event-stream",
                body="data: "
                + json.dumps(
                    {
                        "type": "done",
                        "reply": {
                            "session_id": history,
                            "text": "Контрольный ответ",
                            "documents": [],
                            "citations": [],
                        },
                    }
                )
                + "\n\n",
            )

        await page.route("**/api/chat/stream", answer)
        await page.route(
            "**/api/chat/sessions?**",
            lambda r: r.fulfill(json=[{"id": history, "title": "Другой диалог"}]),
        )
        await page.route(
            f"**/api/chat/sessions/{history}?**",
            lambda r: r.fulfill(
                json=[{"role": "assistant", "content": "История другого диалога"}]
            ),
        )
        await page.route(
            "**/api/voice/status", lambda r: r.fulfill(json={"stt": True, "tts": False})
        )
        await page.goto(base + "/#/id/project", wait_until="networkidle")
        await page.evaluate(
            "async id=>{const {askAssistant}=await import('/src/modules/executive/assistantTask.ts');askAssistant('Вопрос по документу',id,'Выбранный документ')}",
            old_doc,
        )
        await expect(page.locator(".msg.assistant")).to_contain_text(
            "Контрольный ответ"
        )
        assert requests[-1]["document_id"] == old_doc
        await page.get_by_role("button", name="Другой диалог", exact=True).click()
        await expect(page.locator(".msg.assistant")).to_contain_text(
            "История другого диалога"
        )
        await page.get_by_label("Сообщение Синапсу").fill("Продолжи этот диалог")
        await page.get_by_role("button", name="Отправить сообщение", exact=True).click()
        await expect(
            page.get_by_role("button", name="Остановить запрос", exact=True)
        ).to_have_count(0)
        assert (
            requests[-1]["session_id"] == history
            and requests[-1]["document_id"] is None
        )
        gate = asyncio.Event()

        async def delayed(route):
            await gate.wait()
            with suppress(Error):
                await route.fulfill(
                    json=[{"role": "assistant", "content": "Старый запоздалый ответ"}]
                )

        await page.route(f"**/api/chat/sessions/{history}?**", delayed)
        await page.get_by_role("button", name="Другой диалог", exact=True).click()
        await page.get_by_role("button", name="Новый диалог", exact=True).click()
        gate.set()
        await page.wait_for_timeout(200)
        await expect(
            page.get_by_text("Старый запоздалый ответ", exact=True)
        ).to_have_count(0)
        await page.get_by_role("button", name="Голосовая команда", exact=True).click()
        await expect(page.locator("button.recording")).to_have_count(1)
        await page.get_by_role("link", name="Проект", exact=True).click()
        assert await page.evaluate("stoppedTracks") >= 1
        await page.get_by_role(
            "button", name="Сказать голосом", exact=False
        ).click()
        await expect(
            page.get_by_role("button", name="Остановить запись", exact=False)
        ).to_be_visible()
        await page.get_by_role("link", name="Договоры", exact=True).click()
        assert await page.evaluate("stoppedTracks") >= 2
        await page.route(
            "**/api/executive/runs",
            lambda r: r.fulfill(
                json=[
                    {
                        "id": "audit-run",
                        "text": "Проложен кабель на участке 123",
                        "status": "needs_data",
                        "revision": 1,
                        "gaps": [
                            {"field": "journal", "message": "Не найден журнал работ"}
                        ],
                        "documents": [{"id": "doc", "title": "Черновик"}],
                    }
                ]
            ),
        )
        await page.route(
            "**/api/executive/runs/audit-run/resume",
            lambda r: r.fulfill(
                status=202,
                json={
                    "id": "audit-run",
                    "text": "Проложен кабель на участке 123",
                    "status": "queued",
                    "revision": 2,
                },
            ),
        )
        await page.get_by_role(
            "link", name="Исполнительная документация", exact=True
        ).click()
        await page.get_by_text(
            "Нужны данные · Проложен кабель на участке 123", exact=True
        ).click()
        await expect(
            page.get_by_text("Не найден журнал работ", exact=True)
        ).to_be_visible()
        await expect(
            page.get_by_role("button", name="Скачать комплект", exact=True)
        ).to_be_enabled()
        await page.get_by_role(
            "button", name="Повторить проверку задания", exact=True
        ).click()
        await expect(
            page.get_by_text("В очереди · Проложен кабель на участке 123", exact=True)
        ).to_be_visible()
        assert not errors, errors
        result = {
            "history_document_isolation": True,
            "history_race_cancelled": True,
            "microphones_released": True,
            "jobs_visible_and_retryable": True,
            "javascript_errors": errors,
        }
        Path("/workspace/artifacts/bis-audit-workflows.json").write_text(
            json.dumps(result)
        )
        print(json.dumps(result))
        await browser.close()


asyncio.run(main())
