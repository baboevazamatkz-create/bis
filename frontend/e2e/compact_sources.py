"""Historical citation piles collapse to used documents/pages without renumbering references."""
import asyncio
import os
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        page = await browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        citations = [dict(chunk_id=str(i), document_id='contract-one', title='Договор', citation='Договор, стр. 8',
                          page=8 if i < 2 else 9, source_id='s', content='Текст источника ' + str(i), status='active', section_path=None)
                     for i in range(20)]
        citations[5].update(document_id='irrelevant', title='Ненужный проект')

        async def api(route):
            path = urlparse(route.request.url).path.removeprefix('/api')
            data = []
            if path == '/auth/me':
                data = {'user': dict(id='u', organization_id='org', role='admin', full_name='Инженер')}
            elif path == '/org':
                data = {'name': 'Организация'}
            elif path == '/voice/status':
                data = {'stt': False, 'tts': False}
            elif path == '/chat/sessions':
                data = [{'id': 'session', 'title': 'Проверка источников'}]
            elif path == '/chat/sessions/session':
                data = [{'role': 'assistant', 'content': 'Ответ по договору [1, стр. 8] [2] [7].\n\nИсточники: [1] Договор',
                         'meta': {'citations': citations, 'documents': [], 'context': {}}}]
            await route.fulfill(json=data)

        await page.route('**/api/**', api)
        await page.goto(os.environ.get('BIS_UI_URL', 'http://127.0.0.1:5178') + '/#/id/chat')
        await page.get_by_role('button', name='Проверка источников', exact=True).click()
        outer = page.locator('details.citations')
        await expect(outer.locator(':scope > summary')).to_have_text('Основания · 1 документ')
        await expect(outer).not_to_have_attribute('open', '')
        await expect(page.locator('.bubble')).not_to_contain_text('Источники:')
        await outer.locator(':scope > summary').click()
        await expect(outer.locator('button')).to_have_count(2)
        await expect(outer).not_to_contain_text('Ненужный проект')
        await expect(outer).to_contain_text('[1, 2]')
        await expect(outer).to_contain_text('[7]')
        assert not errors, errors
        print('PASS: 20 historical citations become one collapsed used document with two unique page links and original reference numbers.')
        await browser.close()


asyncio.run(main())
