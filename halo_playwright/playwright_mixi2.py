# -*- coding: utf-8 -*-
# pip install playwright
# playwright install chromium
from playwright.sync_api import sync_playwright
import os

STORAGE = "storage_state.json"
START_URL = "https://mixi.social/home"


class MixiClient:
    def __init__(self, storage_path: str = STORAGE, start_url: str = START_URL, headless: bool = True, lang: str = "ja-JP"):
        self.storage_path = storage_path
        self.start_url = start_url
        self.headless = headless
        self.lang = lang
        self._p = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self):
        self._p = sync_playwright().start()
        self.browser = self._p.chromium.launch(headless=self.headless, args=[f"--lang={self.lang}"])
        if os.path.exists(self.storage_path):
            self.context = self.browser.new_context(storage_state=self.storage_path)
        else:
            self.context = self.browser.new_context()
        self.page = self.context.new_page()
        return self

    def goto_home(self):
        self.page.goto(self.start_url, wait_until="domcontentloaded")
        self.context.storage_state(path=self.storage_path)

    def post(self, text: str):
        # self.page.get_by_text("ポスト", exact=False).first.click()
        self.page.get_by_role("button", name="ポスト").click()
        self.page.locator('[data-placeholder="今なにしてる？"]').fill(text)
        self.page.locator('[aria-label="送信"]').click()
        self.context.storage_state(path=self.storage_path)

    def close(self):
        try:
            if self.context:
                self.context.close()
        finally:
            try:
                if self.browser:
                    self.browser.close()
            finally:
                if self._p:
                    self._p.stop()

    def run_once(self, text: str):
        try:
            self.start()
            self.goto_home()
            self.post(text)
        finally:
            self.close()

if __name__ == "__main__":
    client = MixiClient(headless=False)
    client.run_once("test")
