"""Run against the isolated PTO-* synthetic fixture server, with Python Playwright installed.

BIS_UI_URL=http://127.0.0.1:5174 python frontend/e2e/executive_pipeline.py
Creates draft runs only in projects explicitly labelled as synthetic fixtures.
"""
import asyncio
import io
import json
import os
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from playwright.async_api import async_playwright, expect

BASE = os.environ.get('BIS_UI_URL', 'http://127.0.0.1:5174').rstrip('/')
OUT = Path(os.environ.get('BIS_UI_ARTIFACTS', '/workspace/artifacts/bis-requirements-ui'))
CASES = ('complete', 'missing_protocol', 'wrong_batch', 'wrong_material', 'conflicting_dates', 'conditional_rule', 'old_rd')


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = await browser.new_page(viewport={'width': 1440, 'height': 1080})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.goto(BASE + '/#/id/pipeline', wait_until='networkidle')
        await page.wait_for_function("document.querySelectorAll('#active-project option').length > 7")
        options = await page.locator('#active-project option').evaluate_all('(es)=>es.map(e=>({id:e.value,name:e.textContent}))')
        projects = {}
        for case in CASES:
            matches = [o for o in options if o['name'].startswith('PTO-' + case + ' · Учебный:')]
            assert len(matches) == 1, f'Isolated synthetic fixture project missing: {case}'
            projects[case] = matches[0]['id']
        results = {}

        async def submit(project, text):
            await page.select_option('#active-project', project)
            await page.get_by_role('heading', name='Сообщить о выполненной работе').wait_for()
            new = page.get_by_role('button', name='Новое сообщение', exact=True)
            if await new.count():
                await new.click()
            await page.locator('#pipeline-report').fill(text)
            async with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/api/executive/runs')) as pending:
                await page.get_by_role('button', name='Собрать комплект ИД', exact=True).click()
            response = await pending.value
            assert response.status == 202, await response.text()
            run = await response.json()
            await page.locator('.package-readiness').wait_for(timeout=60000)
            headers = {'X-BIS-Project': project}
            latest = await page.request.get(BASE + '/api/executive/runs/' + run['id'], headers=headers)
            assert latest.ok, await latest.text()
            return await latest.json()

        for case in CASES:
            run = await submit(projects[case], 'Проложен кабель на участке 123.')
            ready = case in ('complete', 'old_rd')
            assert run['status'] == ('ready_for_review' if ready else 'needs_data'), (case, run.get('gaps'))
            expected = 'Комплект готов к проверке инженером' if ready else 'Выпуск требует устранения замечаний'
            await expect(page.locator('.package-readiness')).to_contain_text(expected)
            assert await page.locator('.requirement-row').count() >= 3
            results[case] = {'id': run['id'], 'object_id': projects[case], 'document_id': run.get('documents', [{}])[0].get('id'),
                             'status': run['status'], 'gaps': [g['field'] for g in run.get('gaps', [])]}
            if case == 'complete':
                proof = page.locator('.requirement-row').filter(has_text='Испытание').first
                await proof.locator('summary').click()
                await expect(proof.locator('blockquote')).to_contain_text('Испытания:')
                source = await page.request.get(BASE + '/api/documents/' + run['attachments'][0]['id'] + '/file', headers={'X-BIS-Project': projects[case]})
                assert source.ok
                async with page.expect_download() as download:
                    await page.get_by_role('button', name='Скачать черновик и приложения ZIP', exact=True).click()
                archive_path = OUT / 'Комплект_ИД.zip'
                await (await download.value).save_as(archive_path)
                with zipfile.ZipFile(archive_path) as archive:
                    manifest = json.loads(archive.read('manifest.json'))
                    assert manifest['id'] == run['id'] and manifest['plan']['work_end'] == '2026-11-05'
                    docx_name = next(n for n in archive.namelist() if n.endswith('.docx'))
                    with zipfile.ZipFile(io.BytesIO(archive.read(docx_name))) as docx:
                        text = ''.join(ET.fromstring(docx.read('word/document.xml')).itertext())
                        assert 'ВВГнг-LS 3х2,5' in text and '«05» ноября 2026' in text
                    assert len(archive.namelist()) == 6, archive.namelist()
                await page.evaluate('window.scrollTo(0, 0)')
                await page.screenshot(path=OUT / 'ready-desktop.png', full_page=True)
                await page.set_viewport_size({'width': 390, 'height': 844})
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await page.evaluate('window.scrollTo(0, 0)')
                await page.screenshot(path=OUT / 'ready-mobile.png', full_page=True)
                await page.set_viewport_size({'width': 320, 'height': 740})
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await page.set_viewport_size({'width': 1440, 'height': 1080})
            if case == 'wrong_material':
                await page.evaluate('window.scrollTo(0, 0)')
                await page.screenshot(path=OUT / 'wrong-material.png', full_page=True)

        # Request delay/error injection is confined to these race checks; the scenarios above use the real API.
        project = projects['complete']
        a = await submit(project, 'Проложен кабель на участке 123. Проверка А.')
        b = await submit(project, 'Проложен кабель на участке 123. Проверка Б.')
        async def select_run(text):
            await page.locator('.pipeline-runs button').filter(has_text=text).first.click()
            await page.locator('.package-readiness').wait_for()
            await expect(page.get_by_role('button', name='Проверить актуальность', exact=True)).to_be_enabled()

        for fail, return_to_a in ((False, False), (True, False), (False, True)):
            await select_run(a['text'])
            started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
            pattern = '**/api/executive/runs/' + a['id'] + '/readiness'
            marker = 'ЗАПОЗДАЛЫЙ ОТВЕТ ДРУГОГО ЗАДАНИЯ'
            async def delayed(route, request, *, started=started, release=release, finished=finished, fail=fail, marker=marker):
                response = await route.fetch()
                value = await response.json()
                started.set()
                await release.wait()
                if fail:
                    await route.fulfill(status=500, json={'detail': marker})
                else:
                    await route.fulfill(json={**value, 'state': 'blocked', 'note': marker, 'blockers': [{'field': 'race', 'message': marker}]})
                finished.set()
            await page.route(pattern, delayed, times=1)
            await page.get_by_role('button', name='Проверить актуальность', exact=True).click()
            await asyncio.wait_for(started.wait(), 15)
            await select_run(b['text'])
            if return_to_a:
                await select_run(a['text'])
            release.set()
            await asyncio.wait_for(finished.wait(), 15)
            await page.unroute(pattern, delayed)
            await page.wait_for_load_state('networkidle')
            await expect(page.locator('body')).not_to_contain_text(marker)
            await expect(page.get_by_role('heading', name='Черновик готов к проверке', exact=True)).to_be_visible()
            await expect(page.get_by_role('button', name='Проверить актуальность', exact=True)).to_be_enabled()
        results['race_checks'] = {'delayed_success_ignored': True, 'delayed_error_ignored': True, 'return_to_same_run_ignores_old_result': True}
        results['page_errors'] = errors
        assert not errors, errors
        (OUT / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
        await browser.close()
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
