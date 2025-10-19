import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import json
import base64
import asyncio
import signal
import sounddevice as sd
import websockets
from voicevox_pipelined import VoiceVoxTTSPipelined


class RealtimeTextOnlyTTS:
    def __init__(
        self,
        *,
        model: str | None = None,
        sample_rate: int = 16000,
        channels: int = 1,
        dtype: str = "int16",
        chunk_ms: int = 100,
        voicevox_base_url: str = "http://192.168.1.151:50021",
        voicevox_speaker: int = 89,
        voicevox_max_len: int = 80,
        voicevox_speed_scale: float = 1.0,
        voicevox_pitch_scale: float = 0.0,
        voicevox_intonation_scale: float = 1.0,
        voicevox_autoplay: bool = True,
    ) -> None:
        # OpenAI Realtime 設定
        self.model = model or os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
        self.ws_url = f"wss://api.openai.com/v1/realtime?model={self.model}"

        # 音声入出力設定
        self.sample_rate = sample_rate
        self.channels = channels
        self.dtype = dtype
        self.chunk_ms = chunk_ms

        # 音声合成パイプライン
        self.tts_pipelined = VoiceVoxTTSPipelined(
            base_url=voicevox_base_url,
            speaker=voicevox_speaker,
            max_len=voicevox_max_len,
        )
        self.tts_pipelined.set_params(
            speedScale=voicevox_speed_scale,
            pitchScale=voicevox_pitch_scale,
            intonationScale=voicevox_intonation_scale,
        )
        self.tts_pipelined.start_stream(
            motor_controller=None,
            corr_gate=None,
            filler=None,
            synth_workers=3,
            autoplay=voicevox_autoplay,
        )

        # 制御用フラグ
        self._stop = asyncio.Event()

    def _handle_sigint(self, *_):
        self._stop.set()

    async def session_update(self, ws):
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "modalities": ["text"],
                        "input_audio_format": "pcm16",
                        "input_audio_transcription": {
                            "model": "whisper-1",
                            "language": "ja",
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.6,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                            "create_response": True,
                        },
                        "instructions": "常に日本語で、簡潔に答えてください。",
                    },
                }
            )
        )

    async def audio_producer(self, q: asyncio.Queue):
        blocksize = int(self.sample_rate * self.chunk_ms / 1000)

        def callback(indata, frames, time, status):
            if status:
                print(f"[sd] {status}", flush=True)
            try:
                q.put_nowait(bytes(indata))
            except asyncio.QueueFull:
                pass

        with sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=self.dtype,
            blocksize=blocksize,
            callback=callback,
        ):
            await self._stop.wait()

    async def audio_uploader(self, ws, q: asyncio.Queue):
        while not self._stop.is_set():
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            b64 = base64.b64encode(chunk).decode("ascii")
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def event_consumer(self, ws):
        print("話しかけてください。無音で区切られるとモデルが応答します。Ctrl+C で終了。")
        listResponseDelta = []
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            etype = msg.get("type")

            if etype == "response.text.delta":
                delta = msg.get("delta", "")
                listResponseDelta.append(delta)
                print(delta, end="", flush=True)
                self.tts_pipelined.push_text(delta)
            elif etype == "conversation.item.input_audio_transcription.completed":
                text = msg.get("transcript") or msg.get("text") or ""
                if text:
                    print(f"\n[You] {text}")
            elif etype == "response.done":
                if listResponseDelta:
                    print("\n", end="", flush=True)
                    listResponseDelta.clear()
            elif etype == "error":
                print(f"\n[error] {msg}")

    async def run(self):
        signal.signal(signal.SIGINT, self._handle_sigint)
        api_key = os.environ["OPENAI_API_KEY"]
        headers = {"Authorization": f"Bearer {api_key}", "OpenAI-Beta": "realtime=v1"}

        async with websockets.connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=20,
        ) as ws:
            print("connected. 初期化中…")
            try:
                _ = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

            await self.session_update(ws)
            print("VAD準備OK。録音開始。")

            q = asyncio.Queue(maxsize=8)
            listTasks = [
                asyncio.create_task(self.audio_producer(q)),
                asyncio.create_task(self.audio_uploader(ws, q)),
                asyncio.create_task(self.event_consumer(ws)),
            ]
            await asyncio.wait(listTasks, return_when=asyncio.FIRST_COMPLETED)


async def main():
    app = RealtimeTextOnlyTTS()
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
