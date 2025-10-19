import os
import asyncio
import base64
import json
import numpy as np
import sounddevice as sd
import websockets
import sys

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
WS_URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"

SAMPLE_RATE = 24000   # Realtime の推奨に合わせる
CHANNELS = 1
BLOCK_SECONDS = 2.5   # 録音区間（短めでOK）

async def main():
    if not OPENAI_API_KEY:
        print("OPENAI_API_KEY を環境変数に設定してください。", file=sys.stderr)
        return

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    # ★ 重要: サブプロトコル 'realtime' を明示
    async with websockets.connect(
        WS_URL,
        additional_headers=headers,
        subprotocols=["realtime"],
        max_size=10 * 1024 * 1024,
    ) as ws:

        # --- セッション設定: 文字起こしON（出力はresponse側でtextのみ指定） ---
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "input_audio_transcription": {"model": "gpt-4o-transcribe"},
                # ここで "default" の出力モードは設定せず、毎回 response.create で text を指定
            }
        }))

        print("マイクから日本語で話してください（録音します）…")
        audio = sd.rec(int(BLOCK_SECONDS * SAMPLE_RATE),
                       samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16")
        sd.wait()
        pcm16 = audio.flatten().tobytes()
        b64audio = base64.b64encode(pcm16).decode("utf-8")

        # --- 音声チャンクを投入してコミット ---
        await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64audio}))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

        # --- 応答生成（テキストのみ）---
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "modalities": ["text"],                    # ★ テキストのみ
                "instructions": "日本語で簡潔に答えてください。"
            }
        }))

        # --- 受信ループ：代表的なイベントを全部拾う ---
        full_text = []
        while True:
            raw = await ws.recv()
            event = json.loads(raw)
            etype = event.get("type", "")

            # デバッグ: 何が来ているか最初は必ず見ましょう
            # print("EVENT:", etype, event)

            if etype == "response.text.delta":
                full_text.append(event.get("delta", ""))

            elif etype in ("response.text.done", "response.completed", "response.done"):
                break

            elif etype == "error":
                # サーバ側のエラー内容を表示して即終了
                print("ERROR:", event)
                break

            # 他に response.output_* 系が来る場合もありますが、text 以外は無視

        print("\n--- AIのテキスト応答（音声なし） ---")
        print("".join(full_text).strip())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
