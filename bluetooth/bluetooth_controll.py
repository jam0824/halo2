import asyncio
from bleak import BleakClient




class CarController:
    def __init__(self, address: str = "69:96:1C:04:00:58", char_uuid: str = "0000ffe1-0000-1000-8000-00805f9b34fb"):
        self.address = address
        self.char_uuid = char_uuid
        self.client: BleakClient | None = None

    async def connect(self):
        """Connect to the BLE car"""
        self.client = BleakClient(self.address)
        await self.client.__aenter__()
        print("Connected:", self.client.is_connected)

        # 通知コールバックを登録
        await self.client.start_notify(self.char_uuid, self._handle_rx)

    async def disconnect(self):
        """Disconnect from the BLE car"""
        if self.client:
            await self.client.stop_notify(self.char_uuid)
            await self.client.__aexit__(None, None, None)
            self.client = None
            print("Disconnected")

    async def _send(self, cmd: bytes):
        """Send raw command to the car"""
        if not self.client or not self.client.is_connected:
            raise RuntimeError("Car is not connected")
        await self.client.write_gatt_char(self.char_uuid, cmd)

    # ==== Control Methods ====
    async def forward(self):
        await self._send(b"A#")

    async def backward(self):
        await self._send(b"B#")

    async def left(self):
        await self._send(b"E#")

    async def right(self):
        await self._send(b"F#")

    async def stop(self):
        await self._send(b"0#")

    # ==== Receive Handler ====
    def _handle_rx(self, _: int, data: bytearray):
        """Parse and display incoming $DAT packets"""
        try:
            text = data.decode(errors="ignore").strip()
            if text.startswith("$DAT") and text.endswith("#"):
                payload = text[4:-1]  # 中身だけ抜き出す "23,101,78"
                parts = payload.split(",")
                if len(parts) == 3:
                    distance, sound, battery = parts
                    print(f"[DAT] Distance={distance} cm, Sound={sound}, Battery={battery}%")
                else:
                    print("RX (DAT malformed):", text)
            else:
                # その他の通知
                print("RX:", text)
        except Exception as e:
            print("RX error:", e)
