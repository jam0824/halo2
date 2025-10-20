import signal
import asyncio
from typing import Optional

import numpy as np
import sounddevice as sd


SR = 48000
IN_CHANNELS = 1
OUT_CHANNELS = 1
DTYPE = "float32"
DELAY_MS = 10


class MonoDelayBuffer:
    def __init__(
        self,
        *,
        samplerate: int,
        delay_ms: int,
        gain_direct: float = 0.707,
        gain_delayed: float = 0.707,
    ) -> None:
        self.samplerate = int(samplerate)
        self.delay_samples = max(1, int(self.samplerate * delay_ms / 1000))
        self.gain_direct = float(gain_direct)
        self.gain_delayed = float(gain_delayed)
        self._delay_line: Optional[np.ndarray] = None

    def process_float32(self, x: np.ndarray) -> np.ndarray:
        if self._delay_line is None:
            self._delay_line = np.zeros(self.delay_samples, dtype=np.float32)
        if x.dtype != np.float32:
            x = x.astype(np.float32, copy=False)
        concat = np.concatenate((self._delay_line, x))
        y_delayed = concat[: x.shape[0]]
        self._delay_line = concat[-self.delay_samples :]
        y_mix = self.gain_direct * x + self.gain_delayed * y_delayed
        np.clip(y_mix, -1.0, 1.0, out=y_mix)
        return y_mix

    def process_int16_bytes(self, pcm16_bytes: bytes) -> bytes:
        x_i16 = np.frombuffer(pcm16_bytes, dtype=np.int16)
        x_f32 = x_i16.astype(np.float32) / 32768.0
        y_f32 = self.process_float32(x_f32)
        y_i16 = np.clip(y_f32 * 32767.0, -32768, 32767).astype(np.int16)
        return y_i16.tobytes()


class MonoDelayMixPlayer:
    def __init__(
        self,
        *,
        samplerate: int = SR,
        delay_ms: int = DELAY_MS,
        gain_direct: float = 0.707,
        gain_delayed: float = 0.707,
    ) -> None:
        self.samplerate = samplerate
        self.delay_samples = max(1, int(self.samplerate * delay_ms / 1000))
        self._delay_line: Optional[np.ndarray] = None
        self._stop = asyncio.Event()
        self.gain_direct = float(gain_direct)
        self.gain_delayed = float(gain_delayed)

    def _handle_sigint(self, *_):
        self._stop.set()

    def _callback(self, indata, outdata, frames, time, status):
        if status:
            print(status, flush=True)

        x = indata[:, 0]
        if self._delay_line is None:
            self._delay_line = np.zeros(self.delay_samples, dtype=x.dtype)

        concat = np.concatenate((self._delay_line, x))
        y_delayed = concat[:frames]
        self._delay_line = concat[-self.delay_samples:]

        y_mix = self.gain_direct * x + self.gain_delayed * y_delayed
        # クリッピング制御（float32想定）
        np.clip(y_mix, -1.0, 1.0, out=y_mix)
        outdata[:, 0] = y_mix

    async def run(self) -> None:
        signal.signal(signal.SIGINT, self._handle_sigint)
        blocksize = max(1, int(self.samplerate * 0.02))

        with sd.Stream(
            samplerate=self.samplerate,
            dtype=DTYPE,
            channels=(IN_CHANNELS, OUT_CHANNELS),
            blocksize=blocksize,
            callback=self._callback,
        ):
            print(f"Running... Mono mix: direct + {DELAY_MS}ms delay. Ctrl+C to stop.")
            await self._stop.wait()


async def main() -> None:
    app = MonoDelayMixPlayer()
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())


