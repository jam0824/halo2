import os
import json
import base64
import asyncio
import signal
import sounddevice as sd
import websockets

MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
WS_URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"       # PCM16
CHUNK_MS = 100        # 100msごとに送る

_stop = asyncio.Event()
def _handle_sigint(*_): _stop.set()

async def session_update(ws):
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "modalities": ["text"],
            "input_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1", "language": "ja"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.6,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
                "create_response": True,
            },
            "instructions": "常に日本語で、簡潔に答えてください。",
        }
    }))

async def audio_producer(q: asyncio.Queue):
    blocksize = int(SAMPLE_RATE * CHUNK_MS / 1000)

    def callback(indata, frames, time, status):
        if status:
            print(f"[sd] {status}", flush=True)
        # CFFI バッファ -> bytes へ変換して積む
        try:
            q.put_nowait(bytes(indata))
        except asyncio.QueueFull:
            pass

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        blocksize=blocksize,
        callback=callback,
    ):
        await _stop.wait()

async def audio_uploader(ws, q: asyncio.Queue):
    while not _stop.is_set():
        try:
            chunk = await asyncio.wait_for(q.get(), timeout=0.1)
        except asyncio.TimeoutError:
            continue
        b64 = base64.b64encode(chunk).decode("ascii")
        await ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": b64
        }))
    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

async def event_consumer(ws):
    print("話しかけてください。無音で区切られるとモデルが応答します。Ctrl+C で終了。")
    buf = []
    while not _stop.is_set():
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        etype = msg.get("type")

        if etype == "response.text.delta":
            delta = msg.get("delta", "")
            buf.append(delta)
            print(delta, end="", flush=True)
        elif etype == "conversation.item.input_audio_transcription.completed":
            text = msg.get("transcript") or msg.get("text") or ""
            if text:
                print(f"\n[You] {text}")
        elif etype == "response.done":
            if buf:
                print("\n", end="", flush=True)
                buf.clear()
        elif etype == "error":
            print(f"\n[error] {msg}")

async def main():
    signal.signal(signal.SIGINT, _handle_sigint)
    api_key = os.environ["OPENAI_API_KEY"]
    headers = {"Authorization": f"Bearer {api_key}", "OpenAI-Beta": "realtime=v1"}

    async with websockets.connect(
        WS_URL,
        additional_headers=headers,
        ping_interval=20
    ) as ws:
        print("connected. 初期化中…")
        try:
            _ = await asyncio.wait_for(ws.recv(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        await session_update(ws)
        print("VAD準備OK。録音開始。")

        q = asyncio.Queue(maxsize=8)
        tasks = [
            asyncio.create_task(audio_producer(q)),
            asyncio.create_task(audio_uploader(ws, q)),
            asyncio.create_task(event_consumer(ws)),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

if __name__ == "__main__":
    asyncio.run(main())
