"""Optional: start a disposable server, then run this script with Playwright installed.
STUDIO_URL defaults to http://127.0.0.1:8000. Creates a test account/project.
"""
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('dialog', lambda dialog: dialog.accept())
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(os.getenv('STUDIO_URL', 'http://127.0.0.1:8000'))
        page.get_by_role('button', name='创建账户', exact=True).click()
        page.get_by_label('您的称呼').fill('测试家属')
        page.get_by_label('邮箱', exact=True).fill(f'browser-{time.time_ns()}@example.com')
        page.get_by_label('密码', exact=True).fill('browser-test-password')
        page.get_by_role('button', name='创建账户 →').click()
        expect(page.get_by_role('heading', name='我的故事')).to_be_visible()
        page.get_by_role('button', name='＋ 创建一本自传').click()
        page.get_by_label('讲述者姓名 / 称呼').fill('陈奶奶')
        page.get_by_label('自传书名').fill('江边的岁月')
        page.locator('input[name=consent]').check()
        page.get_by_role('button', name='创建人物档案 →').click()
        expect(page.get_by_role('heading', name='江边的岁月')).to_be_visible()
        page.locator('[data-view=interview]').first.click()
        page.get_by_label('写下或录下您的回答').fill('我在江边长大。母亲常带我走过一座石桥。')
        page.get_by_role('button', name='发送回答 ↑').click()
        expect(page.locator('.bubble.assistant').last).to_contain_text('谢谢您', timeout=15000)
        page.locator('[data-view=sources]').first.click()
        page.get_by_label('素材标题', exact=True).first.fill('童年的村庄')
        page.get_by_label('采访记录', exact=True).fill('我们家门前有一棵很大的树。我和邻居孩子经常在那里玩耍。')
        page.get_by_role('button', name='保存文字素材').click()
        expect(page.locator('[data-source-form]')).to_have_count(2)
        for i in range(2):
            page.locator('[data-source-form]').nth(i).get_by_role('button', name='核对无误，确认素材').click()
            expect(page.locator('[data-source-form]').nth(i)).to_contain_text('已确认')
        page.locator('[data-view=manuscript]').first.click()
        page.get_by_role('button', name='✧ 生成自传初稿').click()
        expect(page.locator('[data-chapter-form]')).to_have_count(2, timeout=15000)
        for i in range(2):
            form = page.locator('[data-chapter-form]').nth(i)
            form.locator('[name=questions]').fill('')
            form.get_by_role('button', name='已核实，确认定稿').click()
            expect(page.locator('[data-chapter-form]').nth(i)).to_contain_text('已确认定稿')
        page.get_by_role('button', name='将第 1 章下移', exact=True).click()
        expect(page.locator('[data-chapter-form]')).to_have_count(2)
        page.locator('[data-view=delivery]').first.click()
        expect(page.get_by_role('link', name='↓ 下载交付 ZIP')).to_have_count(0)
        page.get_by_label('讲述者或合法授权人的姓名').fill('陈奶奶')
        page.get_by_label('线下确认记录').fill('测试：当面通读当前版本后同意家庭留存。')
        page.locator('input[name=confirmed]').check()
        page.get_by_role('button', name='记录当前版本确认', exact=True).click()
        expect(page.get_by_role('link', name='↓ 下载交付 ZIP')).to_be_visible()
        page.locator('#toast').evaluate("el => el.className = ''")
        page.screenshot(path='docs/preview-delivery.png', full_page=True)
        with page.expect_download() as download:
            page.get_by_role('link', name='↓ 下载交付 ZIP').click()
        assert download.value.suggested_filename == 'life-story.zip'
        page.locator('[data-view=overview]').first.click()
        Path('docs').mkdir(exist_ok=True)
        page.locator('#toast').evaluate("el => el.className = ''")
        page.screenshot(path='docs/preview-desktop.png', full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.get_by_role('button', name='大字阅读', exact=True).click()
        page.locator('[data-view=interview]').first.click()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path='docs/preview-mobile.png', full_page=True)
        page.get_by_role('button', name='● 录一段话').click()
        expect(page.get_by_role('button', name='■ 结束并试听')).to_be_visible()
        page.wait_for_timeout(400)
        page.get_by_role('button', name='■ 结束并试听').click()
        page.get_by_role('button', name='确认并上传', exact=True).click()
        expect(page.locator('audio')).to_have_count(1, timeout=10000)
        expect(page.locator('.job.failed')).to_contain_text('演示模式不识别音频', timeout=15000)
        assert not errors, errors
        browser.close()
        print('PASS: browser register → create → online interview → offline text → confirm → generate → approve → ZIP; reorder and version-bound approval; browser microphone recording/upload; mobile overflow and JS errors checked')


if __name__ == '__main__':
    main()
