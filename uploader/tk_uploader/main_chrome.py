# -*- coding: utf-8 -*-
import re
from datetime import datetime

from patchright.async_api import Playwright
from utils.profile_browser import async_playwright  # fork: per-account Chrome profiles
import os
import asyncio

from conf import LOCAL_CHROME_PATH, LOCAL_CHROME_HEADLESS
from uploader.tk_uploader.tk_config import Tk_Locator
from utils.base_social_media import set_init_script
from utils.files_times import get_absolute_path
from utils.log import tiktok_logger


async def cookie_auth(account_file):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=LOCAL_CHROME_HEADLESS)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://www.tiktok.com/tiktokstudio/upload?lang=en")
        await page.wait_for_load_state('networkidle')
        try:
            # 选择所有的 select 元素
            select_elements = await page.query_selector_all('select')
            for element in select_elements:
                class_name = await element.get_attribute('class')
                # 使用正则表达式匹配特定模式的 class 名称
                if re.match(r'tiktok-.*-SelectFormContainer.*', class_name):
                    tiktok_logger.error("[+] cookie expired")
                    return False
            tiktok_logger.success("[+] cookie valid")
            return True
        except:
            tiktok_logger.success("[+] cookie valid")
            return True


async def tiktok_setup(account_file, handle=False):
    account_file = get_absolute_path(account_file, "tk_uploader")
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            return False
        tiktok_logger.info('[+] cookie file is not existed or expired. Now open the browser auto. Please login with your way(gmail phone, whatever, the cookie file will generated after login')
        await get_tiktok_cookie(account_file)
    return True


async def get_tiktok_cookie(account_file):
    async with async_playwright() as playwright:
        options = {
            'args': [
                '--lang en-GB',
            ],
            'headless': LOCAL_CHROME_HEADLESS,  # Set headless option here
        }
        # Make sure to run headed.
        browser = await playwright.chromium.launch(**options)
        # Setup context however you like.
        context = await browser.new_context()  # Pass any options
        context = await set_init_script(context)
        # Pause the page, and start recording manually.
        page = await context.new_page()
        await page.goto("https://www.tiktok.com/login?lang=en")
        await page.pause()
        # 点击调试器的继续，保存cookie
        await context.storage_state(path=account_file)


# Fork: the Post button and the "Show more" switches as TikTok Studio renders them in 2026-10
# (taken from Publisher Studio's working adapter).
TK_POST = 'button[data-e2e="post_video_button"], div.btn-post > button, div.button-group > button:has-text("Post")'
TK_AIGC = '[data-e2e="aigc_container"]'
TK_ONLY_YOU = ("Only you", "Only me", "仅自己")
UPLOAD_TIMEOUT_S = 900
PUBLISH_TIMEOUT_S = 180


class TiktokVideo(object):
    def __init__(self, title, file_path, tags, publish_date, account_file, thumbnail_path=None,
                 visibility="public", ai_generated=False):
        self.title = title
        self.file_path = file_path
        self.tags = tags
        self.publish_date = publish_date
        self.thumbnail_path = thumbnail_path
        self.account_file = account_file
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = LOCAL_CHROME_HEADLESS
        self.locator_base = None
        # Fork: "private" = Who can watch → Only you; ai_generated turns on the AI-generated
        # content switch. Either one that cannot be confirmed stops before Post.
        if visibility not in ("public", "private"):
            raise ValueError(f"unsupported TikTok visibility: {visibility}")
        self.visibility = visibility
        self.ai_generated = ai_generated
        self.video_id = None

    async def dismiss_prompts(self, page):
        """Fork: one-time TikTok Studio dialogs cover the caption editor after a file is chosen.

        "Turn on automatic content checks?" is answered with Turn on (copyright and For You
        eligibility checks, both harmless to a post); feature tips are closed with Got it.
        """
        for _ in range(5):
            checks = page.locator('div.common-modal-body:has-text("automatic content checks")')
            if await checks.count() and await checks.first.is_visible():
                await page.locator('div.common-modal-footer button:has-text("Turn on")').first.click()
                tiktok_logger.info("  [-] turned on automatic content checks")
                await page.wait_for_timeout(1000)
                continue
            tip = page.get_by_role("button", name=re.compile(r"^(Got it|OK)$"))
            if await tip.count() and await tip.first.is_visible():
                await tip.first.click()
                await page.wait_for_timeout(500)
                continue
            await page.wait_for_timeout(1000)

    async def declare_ai(self, page):
        aigc = self.locator_base.locator(TK_AIGC)
        switch = aigc.locator('div.Switch__content')

        async def on() -> bool:
            return await switch.count() > 0 and await switch.first.get_attribute('aria-checked') == 'true'

        if not (await aigc.count() and await aigc.first.is_visible()):
            more = self.locator_base.locator('div.more-btn')
            if await more.count():
                await more.first.click()
                await page.wait_for_timeout(800)
        if not await aigc.count():
            raise RuntimeError("未找到 TikTok「AI-generated content」开关，未发布")
        if not await on():
            await aigc.locator('div.Switch__root').first.click()
            await page.wait_for_timeout(600)
            confirm = self.locator_base.locator('div.TUXModal').get_by_text(re.compile(r'^(Turn on|开启)$'))
            if await confirm.count():
                await confirm.first.click()
        for _ in range(16):
            if await on():
                tiktok_logger.info("  [-] AI-generated content switched on")
                return
            await page.wait_for_timeout(500)
        raise RuntimeError("TikTok AI 声明开关未打开，未发布")

    async def make_private(self, page):
        if self.visibility != "private":
            return

        async def chosen() -> bool:
            return await self.locator_base.locator('button, [role=button], div, span').filter(
                has_text=re.compile(r'^(' + '|'.join(TK_ONLY_YOU) + r')$')).evaluate_all(
                "els => els.some(e => e.getClientRects().length > 0 && !e.closest('[role=listbox],[role=option]'))")

        if await chosen():
            return
        current = self.locator_base.get_by_text(re.compile(r'^(Everyone|Public|所有人)$'))
        if not await current.count():
            raise RuntimeError("未找到 TikTok「Who can watch」选项，未发布")
        await current.first.click()
        await page.wait_for_timeout(600)
        option = self.locator_base.locator('[role=listbox], [role=option], ul').get_by_text(
            re.compile(r'^(' + '|'.join(TK_ONLY_YOU) + r')$'))
        if not await option.count():
            raise RuntimeError("未找到 TikTok「Only you」，未发布")
        await option.first.click()
        for _ in range(16):
            if await chosen():
                tiktok_logger.info("  [-] Who can watch = Only you")
                return
            await page.wait_for_timeout(500)
        raise RuntimeError("TikTok 未显示已选「Only you」，未发布")

    async def set_schedule_time(self, page, publish_date):
        schedule_input_element = self.locator_base.get_by_label('Schedule')
        await schedule_input_element.wait_for(state='visible')  # 确保按钮可见

        await schedule_input_element.click(force=True)
        if await self.locator_base.locator('div.TUXButton-content >> text=Allow').count():
            await self.locator_base.locator('div.TUXButton-content >> text=Allow').click()

        scheduled_picker = self.locator_base.locator('div.scheduled-picker')
        await scheduled_picker.locator('div.TUXInputBox').nth(1).click()

        calendar_month = await self.locator_base.locator(
            'div.calendar-wrapper span.month-title').inner_text()

        n_calendar_month = datetime.strptime(calendar_month, '%B').month

        schedule_month = publish_date.month

        if n_calendar_month != schedule_month:
            if n_calendar_month < schedule_month:
                arrow = self.locator_base.locator('div.calendar-wrapper span.arrow').nth(-1)
            else:
                arrow = self.locator_base.locator('div.calendar-wrapper span.arrow').nth(0)
            await arrow.click()

        # day set
        valid_days_locator = self.locator_base.locator(
            'div.calendar-wrapper span.day.valid')
        valid_days = await valid_days_locator.count()
        for i in range(valid_days):
            day_element = valid_days_locator.nth(i)
            text = await day_element.inner_text()
            if text.strip() == str(publish_date.day):
                await day_element.click()
                break
        # time set
        await scheduled_picker.locator('div.TUXInputBox').nth(0).click()

        hour_str = publish_date.strftime("%H")
        correct_minute = int(publish_date.minute / 5)
        minute_str = f"{correct_minute:02d}"

        hour_selector = f"span.tiktok-timepicker-left:has-text('{hour_str}')"
        minute_selector = f"span.tiktok-timepicker-right:has-text('{minute_str}')"

        # pick hour first
        await page.wait_for_timeout(1000)  # 等待500毫秒
        await self.locator_base.locator(hour_selector).click()
        # click time button again
        await page.wait_for_timeout(1000)  # 等待500毫秒
        # pick minutes after
        await self.locator_base.locator(minute_selector).click()

        # click title to remove the focus.
        # await self.locator_base.locator("h1:has-text('Upload video')").click()

    async def handle_upload_error(self, page):
        tiktok_logger.info("video upload error retrying.")
        select_file_button = self.locator_base.locator('button[aria-label="Select file"]')
        async with page.expect_file_chooser() as fc_info:
            await select_file_button.click()
        file_chooser = await fc_info.value
        await file_chooser.set_files(self.file_path)

    async def upload(self, playwright: Playwright) -> None:
        browser = await playwright.chromium.launch(headless=self.headless, executable_path=self.local_executable_path)
        context = await browser.new_context(storage_state=f"{self.account_file}")
        # context = await set_init_script(context)
        page = await context.new_page()

        # change language to eng first
        await self.change_language(page)
        await page.goto("https://www.tiktok.com/tiktokstudio/upload")
        tiktok_logger.info(f'[+]Uploading-------{self.title}.mp4')

        await page.wait_for_url("https://www.tiktok.com/tiktokstudio/upload", timeout=10000)

        try:
            await page.wait_for_selector('iframe[data-tt="Upload_index_iframe"], div.upload-container', timeout=10000)
            tiktok_logger.info("Either iframe or div appeared.")
        except Exception as e:
            tiktok_logger.error("Neither iframe nor div appeared within the timeout.")

        await self.choose_base_locator(page)

        upload_button = self.locator_base.locator(
            'button:has-text("Select video"):visible')
        await upload_button.wait_for(state='visible')  # 确保按钮可见

        async with page.expect_file_chooser() as fc_info:
            await upload_button.click()
        file_chooser = await fc_info.value
        await file_chooser.set_files(self.file_path)

        await self.dismiss_prompts(page)
        await self.add_title_tags(page)
        # detect upload status
        await self.detect_upload_status(page)
        if self.thumbnail_path:
            tiktok_logger.info(f'[+] Uploading thumbnail file {self.title}.png')
            await self.upload_thumbnails(page)

        if self.publish_date != 0:
            await self.set_schedule_time(page, self.publish_date)

        if self.ai_generated:
            await self.declare_ai(page)
        await self.make_private(page)

        await self.click_publish(page)
        try:
            self.video_id = await self.get_last_video_id(page)
        except Exception:
            self.video_id = None
        tiktok_logger.success(f"video_id: {self.video_id}")

        await context.storage_state(path=f"{self.account_file}")  # save cookie
        tiktok_logger.info('  [-] update cookie！')
        await asyncio.sleep(2)  # close delay for look the video status
        # close all
        await context.close()
        await browser.close()

    async def add_title_tags(self, page):

        editor_locator = self.locator_base.locator('div.public-DraftEditor-content')
        await editor_locator.click()

        await page.keyboard.press("End")

        await page.keyboard.press("ControlOrMeta+A")  # fork: Meta on macOS

        await page.keyboard.press("Delete")

        await page.keyboard.press("End")

        await page.wait_for_timeout(1000)  # 等待1秒

        await page.keyboard.insert_text(self.title)
        await page.wait_for_timeout(1000)  # 等待1秒
        await page.keyboard.press("End")

        await page.keyboard.press("Enter")

        # tag part
        for index, tag in enumerate(self.tags, start=1):
            tiktok_logger.info("Setting the %s tag" % index)
            await page.keyboard.press("End")
            await page.wait_for_timeout(1000)  # 等待1秒
            await page.keyboard.insert_text("#" + tag + " ")
            await page.keyboard.press("Space")
            await page.wait_for_timeout(1000)  # 等待1秒

            await page.keyboard.press("Backspace")
            await page.keyboard.press("End")

    async def upload_thumbnails(self, page):
        await self.locator_base.locator(".cover-container").click()
        await self.locator_base.locator(".cover-edit-container >> text=Upload cover").click()
        async with page.expect_file_chooser() as fc_info:
            await self.locator_base.locator(".upload-image-upload-area").click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(self.thumbnail_path)
        await self.locator_base.locator('div.cover-edit-panel:not(.hide-panel)').get_by_role(
            "button", name="Confirm").click()
        await page.wait_for_timeout(3000)  # wait 3s, fix it later

    async def change_language(self, page):
        # set the language to english
        await page.goto("https://www.tiktok.com")
        await page.wait_for_load_state('domcontentloaded')
        await page.wait_for_selector('[data-e2e="nav-more-menu"]')
        # 已经设置为英文, 省略这个步骤
        if await page.locator('[data-e2e="nav-more-menu"]').text_content() == "More":
            return

        await page.locator('[data-e2e="nav-more-menu"]').click()
        await page.locator('[data-e2e="language-select"]').click()
        await page.locator('#creator-tools-selection-menu-header >> text=English (US)').click()

    async def click_publish(self, page):
        # Fork: onboarding tips can cover Post; content checks may ask to confirm; give up after
        # PUBLISH_TIMEOUT_S instead of retrying forever (the result is then uncertain, not failed).
        for tip in ('Got it', '知道了'):
            hint = self.locator_base.get_by_text(tip, exact=True)
            if await hint.count():
                await hint.first.click()
        await page.keyboard.press('Escape')
        await page.wait_for_timeout(800)
        post = self.locator_base.locator(TK_POST)
        if not await post.count():
            raise RuntimeError("未找到 TikTok Post 按钮，未发布")
        await post.first.click()
        deadline = asyncio.get_running_loop().time() + PUBLISH_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            confirm = self.locator_base.get_by_text(re.compile(r'^(Post now|Post anyway|立即发布)$'))
            if await confirm.count():
                await confirm.first.click()
            if '/tiktokstudio/content' in page.url or '/tiktokstudio/manage' in page.url:
                tiktok_logger.success("  [-] video published success")
                return
            await asyncio.sleep(1)
        raise TimeoutError("已点击 Post，但未确认 TikTok 发布结果；请在 TikTok Studio 核对，不要重发")

    async def get_last_video_id(self, page):
        await page.wait_for_selector('div[data-tt="components_PostTable_Container"]')
        video_list_locator = self.locator_base.locator('div[data-tt="components_PostTable_Container"] div[data-tt="components_PostInfoCell_Container"] a')
        if await video_list_locator.count():
            first_video_obj = await video_list_locator.nth(0).get_attribute('href')
            video_id = re.search(r'video/(\d+)', first_video_obj).group(1) if first_video_obj else None
            return video_id


    async def detect_upload_status(self, page):
        deadline = asyncio.get_running_loop().time() + UPLOAD_TIMEOUT_S
        while True:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("TikTok 视频上传超时，未发布")
            try:
                # if await self.locator_base.locator('div.btn-post > button').get_attribute("disabled") is None:
                post = self.locator_base.locator(TK_POST).first
                if await post.get_attribute("disabled") is None and await post.get_attribute("aria-disabled") != "true" \
                        and await post.get_attribute("data-disabled") != "true":
                    tiktok_logger.info("  [-]video uploaded.")
                    break
                else:
                    tiktok_logger.info("  [-] video uploading...")
                    await asyncio.sleep(2)
                    if await self.locator_base.locator(
                            'button[aria-label="Select file"]').count():
                        tiktok_logger.info("  [-] found some error while uploading now retry...")
                        await self.handle_upload_error(page)
            except:
                tiktok_logger.info("  [-] video uploading...")
                await asyncio.sleep(2)

    async def choose_base_locator(self, page):
        # await page.wait_for_selector('div.upload-container')
        if await page.locator('iframe[data-tt="Upload_index_iframe"]').count():
            self.locator_base = page.frame_locator(Tk_Locator.tk_iframe)
        else:
            self.locator_base = page.locator(Tk_Locator.default) 

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
