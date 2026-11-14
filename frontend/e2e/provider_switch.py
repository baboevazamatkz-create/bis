"""A Claude key in the previously Yandex-selected form must save and check Claude."""
import asyncio
import os
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        page = await browser.new_page()
        errors, saved = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        status = dict(selected_provider='yandexgpt', selected_model='gpt://fixture/yandexgpt/latest',
                      selected_base_url='https://llm.api.cloud.yandex.net/v1', providers=[], model_error=None,
                      text_configured=True, ocr_configured=True, ocr_provider='yandex', vision_configured=False,
                      folder_id='fixture', local_setup_available=True, provider_key_configured=True,
                      key_configured=True, voice={'stt': False}, last_model_attempts=[])

        async def api(route):
            path = urlparse(route.request.url).path.removeprefix('/api')
            data = []
            if path == '/auth/me':
                data = {'user': {'id': 'u', 'organization_id': 'org', 'role': 'admin', 'full_name': 'Инженер'}}
            elif path == '/org':
                data = {'name': 'Тест'}
            elif path == '/libraries':
                data = dict(connections=[], allowed_roots=[], strict=True, platform_admin=True)
            elif path == '/workspace/reading-status':
                data = status
            elif path == '/workspace/ai-provider':
                body = route.request.post_data_json
                saved.append(body)
                assert body['provider'] == 'anthropic' and body['model'] == 'claude-opus-5-5'
                status.update(selected_provider='anthropic-claude', selected_model='claude-opus-5-5',
                              ocr_provider='anthropic', vision_configured=True)
                data = {'saved': True}
            elif path == '/ai/models/check':
                data = {'providers': [{'provider': 'anthropic-claude', 'ok': True, 'reply': 'Москва'}]}
            await route.fulfill(json=data)

        await page.route('**/api/**', api)
        await page.goto(os.environ.get('BIS_UI_URL', 'http://127.0.0.1:5178') + '/#/id/libraries')
        await page.get_by_text('ИИ и качество чтения', exact=True).click()
        await page.get_by_role('button', name='Выбрать языковую модель', exact=True).click()
        await expect(page.locator('#ai-model')).to_have_value('yandexgpt/latest')
        await page.locator('#ai-provider-key').fill('sk-ant-fixture-browser-key')
        await expect(page.locator('#ai-provider')).to_have_value('anthropic')
        await page.wait_for_timeout(8500)
        await expect(page.locator('#ai-provider')).to_have_value('anthropic')
        await page.get_by_role('button', name='Подключить Claude', exact=True).click()
        await expect(page.get_by_text('OCR: Claude Vision', exact=False)).to_be_visible()
        await page.get_by_role('button', name='Проверить ИИ', exact=True).click()
        await expect(page.get_by_text('anthropic-claude: Ответ получен', exact=True)).to_be_visible()
        assert len(saved) == 1 and not errors, errors
        print('PASS: Yandex form hydrates model, Claude key switches provider, polling preserves edits, save/check use Claude.')
        await browser.close()


asyncio.run(main())
