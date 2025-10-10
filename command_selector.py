import json
import asyncio
import threading
from concurrent.futures import Future
import re
from pathlib import Path
from typing import Dict, List, Optional, Pattern, Tuple

from halo_mcp.mcp_call import MCPClient
from halo_playwright.playwright_mixi2 import MixiClient
from bluetooth.bluetooth_controll import CarController



class CommandSelector:
    """
    - 初期化時に command.json を読み込む
    - 文字列を正規表現で評価し、最初にヒットした key を返す
    - key に応じて処理を分岐するセレクター（現状は print のみ）
    """

    def __init__(self, config_path: str = "command.json", general_config: dict = {}) -> None:
        self.mixi_client = MixiClient(headless=True)
        self.config_path: str = config_path
        self.general_config: dict = general_config
        self.use_bluetooth: bool = self.general_config["bluetooth"]["use_bluetooth"]
        self.bluetooth_address: str = self.general_config["bluetooth"]["bluetooth_address"]
        self.bluetooth_char_uuid: str = self.general_config["bluetooth"]["bluetooth_char_uuid"]
        self.listRules: List[Tuple[str, Pattern[str]]] = []
        self._load_config()
        self.mcp_client = MCPClient()
        self.car_controller = CarController(
            self.bluetooth_address, 
            self.bluetooth_char_uuid
        )
        self._loop = None  # type: Optional[asyncio.AbstractEventLoop]
        self._loop_thread = None  # type: Optional[threading.Thread]
        self._car_connected: bool = False

    def _ensure_loop(self) -> None:
        if not self.use_bluetooth:
            return
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._loop_thread.start()

    def _ensure_car_connected(self) -> None:
        """BLE車両への接続を一度だけ張る。必要なら接続を確立する。"""
        self._ensure_loop()
        if not self._car_connected:
            try:
                fut = asyncio.run_coroutine_threadsafe(self.car_controller.connect(), self._loop)
                # 接続完了を待機（環境に応じて調整可能）
                fut.result(timeout=10)
                self._car_connected = True
            except Exception:
                self._car_connected = False
                raise

    def _load_config(self) -> None:
        path = Path(self.config_path)
        if not path.exists():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {path}")
        with path.open("r", encoding="utf-8") as f:
            data: Dict[str, Dict[str, str]] = json.load(f)

        dictCommands: Dict[str, str] = data.get("command", {})  # type: ignore[assignment]
        # JSON の順序を尊重して評価する
        for key, regexp in dictCommands.items():
            try:
                compiled = re.compile(regexp)
            except re.error as e:
                raise ValueError(f"正規表現が不正です: key={key}, pattern={regexp}, err={e}") from e
            self.listRules.append((key, compiled))

    def match_key(self, text: str) -> Optional[str]:
        for key, pattern in self.listRules:
            if pattern.search(text):
                return key
        return None

    def select(self, user_text: str, command: str) -> Optional[str]:
        response = None
        key = self.match_key(user_text)
        if key is None:
            print("[selector] マッチするコマンドがありませんでした")
            return None
        elif key == "sns":
            self.mixi_client.run_once(command)
            response = "ハロ、投稿した。" + command
        elif key == "forward":
            if not self.use_bluetooth:
                return None
            self._ensure_car_connected()
            asyncio.run_coroutine_threadsafe(self.car_controller.forward(), self._loop)
            response = "ハロ、出る！"
        elif key == "backward":
            if not self.use_bluetooth:
                return None
            self._ensure_car_connected()
            asyncio.run_coroutine_threadsafe(self.car_controller.backward(), self._loop)
            response = "ハロ、戻る。"
        elif key == "left":
            if not self.use_bluetooth:
                return None
            self._ensure_car_connected()
            asyncio.run_coroutine_threadsafe(self.car_controller.left(), self._loop)
            response = "ハロ、左に行く"
        elif key == "right":
            if not self.use_bluetooth:
                return None
            self._ensure_car_connected()
            asyncio.run_coroutine_threadsafe(self.car_controller.right(), self._loop)
            response = "ハロ、右に行く"
        elif key == "stop":
            if not self.use_bluetooth:
                return None
            self._ensure_car_connected()
            asyncio.run_coroutine_threadsafe(self.car_controller.stop(), self._loop)
            response = "ハロ、止まる"
        return response
    
    def exec_command(self, command) -> Future:
        """コマンドを非同期ループに投げて concurrent.futures.Future を返す。

        - `halo.py` 側の `add_done_callback` で受け取れるよう、結果は `{ "result": <str> }` 形式で返す。
        - `command` が dict の場合は、代表テキストを推測して抽出する。
        """
        self._ensure_loop()

        async def _task():
            out = await self.mcp_client.call(command)
            return {"result": out}

        # ループスレッド上で実行し、concurrent.futures.Future を返す
        return asyncio.run_coroutine_threadsafe(_task(), self._loop)


if __name__ == "__main__":
    import sys

    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "ライトをつけて"
    selector = CommandSelector()
    selector.select(text)