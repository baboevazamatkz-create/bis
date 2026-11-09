"""Exercise the isolated PTO project-understanding fixtures with real API calls.

Requires fixture.json from the synthetic project-understanding preview, Playwright and Chromium.
Never run against a working project. BIS_UI_URL and BIS_PROJECT_ARTIFACTS override test locations.
"""

import asyncio
import io
import json
import os
import zipfile
from pathlib import Path

from playwright.async_api import async_playwright, expect

BASE = os.environ.get("BIS_UI_URL", "http://127.0.0.1:5174").rstrip("/")
OUT = Path(
    os.environ.get(
        "BIS_PROJECT_ARTIFACTS", "/workspace/artifacts/bis-project-understanding"
    )
)


async def main():
    fixture = json.loads((OUT / "fixture.json").read_text())
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(BASE + "/#/id/project-reading", wait_until="networkidle")
        await page.locator(
            f"#active-project option[value='{fixture['pdf']['object_id']}']"
        ).wait_for(state="attached")
        response = await page.request.get(BASE + "/api/objects")
        projects = await response.json()
        for kind, code in [
            ("pdf", "PTO-PROJECT"),
            ("xlsx", "PTO-XLSX"),
            ("unread", "PTO-UNREAD"),
        ]:
            assert any(
                obj["id"] == fixture[kind]["object_id"]
                and obj["code"] == code
                and obj["name"].startswith("Учебный:")
                for obj in projects
            ), "Only isolated synthetic fixtures are permitted"

        async def read_case(kind):
            info = fixture[kind]
            await page.select_option("#active-project", info["object_id"])
            await page.locator(
                '#project-source option[value="' + info["document_id"] + '"]'
            ).wait_for(state="attached")
            await page.select_option("#project-source", info["document_id"])
            await page.get_by_role(
                "button", name="Прочитать проект", exact=True
            ).click()
            await page.locator(".project-sheet").first.wait_for(timeout=60000)
            await expect(
                page.get_by_role("button", name="Прочитать проект", exact=True)
            ).to_be_enabled()
            return info

        info = await read_case("pdf")
        await expect(
            page.get_by_role("heading", name="PDF · страница 1 · лист 3", exact=True)
        ).to_be_visible()
        await expect(
            page.get_by_role("heading", name="PDF · страница 2 · лист 7", exact=True)
        ).to_be_visible()
        await expect(page.locator(".project-sheet table tbody tr")).to_have_count(2)
        await expect(
            page.locator(".project-sheet table tbody tr").first
        ).to_contain_text("100")
        headers = {"X-BIS-Project": info["object_id"]}
        design = await (
            await page.request.get(
                BASE + "/api/projects/items?location=участок%20123&element=КЛ-1",
                headers=headers,
            )
        ).json()
        assert len(design["items"]) == 1 and design["items"][0]["quantity"] == "100", (
            design
        )
        assert design["items"][0]["scope_evidence"][0]["page"] == 1
        reading_id = design["items"][0]["reading_id"]
        proof = await page.request.get(
            BASE + f"/api/projects/readings/{reading_id}/file", headers=headers
        )
        assert proof.status == 200 and (await proof.body())[:4] == b"%PDF"
        await page.evaluate("scrollTo(0,0)")
        await page.screenshot(path=OUT / "reader-desktop.png", full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        await page.screenshot(path=OUT / "reader-mobile.png", full_page=True)
        await page.set_viewport_size({"width": 1440, "height": 1080})
        await read_case("xlsx")
        await expect(page.locator(".project-sheet table tbody tr")).to_have_count(1)
        await expect(page.locator(".project-sheet table tbody tr")).to_contain_text(
            "120"
        )
        await read_case("unread")
        await expect(
            page.get_by_role("heading", name="Чтение неполное", exact=True)
        ).to_be_visible()
        await expect(page.locator(".project-reading-workspace")).to_contain_text(
            "Не прочитаны:"
        )
        await page.select_option("#active-project", info["object_id"])
        await page.goto(BASE + "/#/id/pipeline", wait_until="networkidle")

        async def submit(text):
            new = page.get_by_role("button", name="Новое сообщение", exact=True)
            if await new.count():
                await new.click()
            await page.locator("#pipeline-report").fill(text)
            async with page.expect_response(
                lambda r: (
                    r.request.method == "POST" and r.url.endswith("/api/executive/runs")
                )
            ) as pending:
                await page.get_by_role(
                    "button", name="Собрать комплект ИД", exact=True
                ).click()
            created = await (await pending.value).json()
            await page.locator(".package-readiness").first.wait_for(timeout=60000)
            run = await (
                await page.request.get(
                    BASE + "/api/executive/runs/" + created["id"], headers=headers
                )
            ).json()
            return run

        oversized = await submit(
            "Проложен кабель на участке 123, линия КЛ-1. Объем: 110 м."
        )
        assert oversized["status"] == "needs_data", oversized
        assert any(g["field"] == "project_quantity" for g in oversized["gaps"]), (
            oversized["gaps"]
        )
        await expect(page.locator(".pipeline-workspace")).to_contain_text(
            "Расхождение с РД"
        )
        await page.evaluate("scrollTo(0,0)")
        await page.screenshot(path=OUT / "quantity-conflict.png", full_page=True)
        ambiguous = await submit("Проложен кабель на участке 123.")
        assert ambiguous["plan"]["project_alignment"]["state"] == "ambiguous", ambiguous
        draft = ambiguous["documents"][0]["id"]
        await page.locator("#correct-element").fill("КЛ-1")
        await page.get_by_role(
            "button", name="Учесть уточнения и повторить проверку", exact=True
        ).click()
        await expect(
            page.get_by_role("heading", name="Черновик готов к проверке", exact=True)
        ).to_be_visible(timeout=60000)
        fixed = await (
            await page.request.get(
                BASE + "/api/executive/runs/" + ambiguous["id"], headers=headers
            )
        ).json()
        assert (
            fixed["documents"][0]["id"] == draft
            and fixed["plan"]["project_alignment"]["state"] == "aligned"
        )
        assert fixed["resolution_history"][-1]["values"]["element"] == "КЛ-1"
        ready = await submit("Проложен кабель на участке 123, линия КЛ-1. Объем: 90 м.")
        assert ready["status"] == "ready_for_review", ready.get("gaps")
        assert ready["plan"]["project_alignment"]["state"] == "aligned"
        comparison = await page.request.post(
            BASE + "/api/projects/acts/" + ready["documents"][0]["id"] + "/compare",
            headers=headers,
        )
        assert (
            comparison.status == 200 and (await comparison.json())["state"] == "aligned"
        ), await comparison.text()
        package = await page.request.get(
            BASE + "/api/executive/runs/" + ready["id"] + "/bundle", headers=headers
        )
        assert package.status == 200, await package.text()
        (OUT / "Комплект_по_проекту.zip").write_bytes(await package.body())
        with zipfile.ZipFile(io.BytesIO(await package.body())) as z:
            manifest = json.loads(z.read("manifest.json"))
            assert (
                manifest["plan"]["project_alignment"]["checks"][0]["candidates"][0][
                    "position"
                ]
                == "1"
            )
            assert (
                manifest["plan"]["project_alignment"]["checks"][0]["candidates"][0][
                    "page"
                ]
                == 2
            )
        # A re-reading invalidates the previous comparison even when the original PDF is retained.
        reread = await page.request.post(
            BASE + "/api/projects/documents/" + info["document_id"] + "/read",
            headers=headers,
            data={"force": True, "budget": 4},
        )
        assert reread.status == 200, await reread.text()
        await page.get_by_role(
            "button", name="Проверить актуальность", exact=True
        ).click()
        await expect(
            page.get_by_role(
                "heading", name="Комплект требует повторной проверки", exact=True
            )
        ).to_be_visible(timeout=15000)
        stale = await page.request.get(
            BASE + "/api/executive/runs/" + ready["id"] + "/bundle", headers=headers
        )
        assert stale.status == 409
        await page.get_by_role(
            "button", name="Обновить основания и повторить проверку", exact=True
        ).click()
        await expect(
            page.get_by_role("heading", name="Черновик готов к проверке", exact=True)
        ).to_be_visible(timeout=60000)
        other = await page.request.get(
            BASE + f"/api/projects/readings/{reading_id}/file",
            headers={"X-BIS-Project": fixture["xlsx"]["object_id"]},
        )
        assert other.status == 404
        assert not errors, errors
        result = {
            "pdf_pages": 2,
            "logical_sheets": ["3", "7"],
            "linked_spec_quantity": "100",
            "excel_quantity": "120",
            "unread_explicit": True,
            "oversized_volume_blocked": True,
            "ambiguous_line_resolved_in_same_draft": True,
            "ready_run": ready["id"],
            "reread_invalidates_bundle": True,
            "cross_project_status": 404,
            "page_errors": errors,
        }
        (OUT / "browser-results.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
