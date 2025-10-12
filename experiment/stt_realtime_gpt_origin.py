import asyncio
import websockets
import pyaudio
import numpy as np
import base64
import json
import os

API_KEY = os.environ.get('OPENAI_API_KEY')
#わからない人は、上の行をコメントアウトして、下記のように直接API KEYを書き下してもよい
#API_KEY = "sk-xxxxx"

# WebSocket URLとヘッダー情報
WS_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
HEADERS = {
    "Authorization": "Bearer "+ API_KEY, 
    "OpenAI-Beta": "realtime=v1"
}

# PCM16形式に変換する関数
def base64_to_pcm16(base64_audio):
    audio_data = base64.b64decode(base64_audio)
    return audio_data

# 簡易VAD（RMSベース）
def is_voice(pcm16_bytes: bytes, threshold: float = 500.0) -> bool:
    if not pcm16_bytes:
        return False
    data = np.frombuffer(pcm16_bytes, dtype=np.int16)
    if data.size == 0:
        return False
    rms = np.sqrt(np.mean(np.square(data.astype(np.float32))))
    return rms >= threshold

# 音声を送信する非同期関数（VADで区切ってcommit→response.createを送る）
async def send_audio(websocket, stream, CHUNK, RATE, mic_enabled_event: asyncio.Event, awaiting_response: asyncio.Event):
    def read_audio_block():
        """同期的に音声データを読み取る関数"""
        try:
            # マイク停止中は読み取らない
            if not stream.is_active():
                return None
            return stream.read(CHUNK, exception_on_overflow=False)
        except Exception as e:
            # ストリーム停止中の例外は無視
            msg = str(e)
            if mic_enabled_event.is_set() and "Stream closed" not in msg:
                print(f"音声読み取りエラー: {e}")
            return None

    print("マイクから音声を取得して送信中...")
    # VADパラメータ
    voice_started = False
    silence_ms_after_voice = 0.0
    chunk_ms = 1000.0 * CHUNK / RATE
    speech_ms = 0.0
    min_speech_ms = 250.0  # 最低発話長（誤起動抑制）

    while True:
        # アシスタント再生中は送信停止
        await mic_enabled_event.wait()
        # 再開時に物理ストリームが止まっていたら起動
        try:
            if not stream.is_active():
                stream.start_stream()
                # ドライバ反映待ちのごく短い猶予
                await asyncio.sleep(0.01)
        except Exception:
            # 起動失敗時は少し待って次ループ
            await asyncio.sleep(0.02)
            continue
        # マイクから音声を取得
        audio_data = await asyncio.get_event_loop().run_in_executor(None, read_audio_block)
        if audio_data is None:
            # 停止中や読み取り失敗時は待機
            await asyncio.sleep(0.01)
            continue  # 読み取りに失敗した場合はスキップ
        
        # PCM16データをBase64にエンコード
        base64_audio = base64.b64encode(audio_data).decode("utf-8")

        audio_event = {
            "type": "input_audio_buffer.append",
            "audio": base64_audio
        }

        # WebSocketで音声データを送信
        await websocket.send(json.dumps(audio_event))

        # 簡易VAD: 音声開始→終了でcommitし、応答生成を起動
        if not voice_started:
            if is_voice(audio_data):
                voice_started = True
                silence_ms_after_voice = 0.0
                speech_ms = 0.0
        else:
            if is_voice(audio_data):
                silence_ms_after_voice = 0.0
                speech_ms += chunk_ms
            else:
                silence_ms_after_voice += chunk_ms

            # 800ms程度の無音で区切る + 最低発話長 + 応答未待機
            if silence_ms_after_voice >= 800.0 and speech_ms >= min_speech_ms and not awaiting_response.is_set():
                # 1ターンの音声を確定
                await websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
                # 応答生成をリクエスト
                await websocket.send(json.dumps({"type": "response.create"}))
                # アシスタントが話す間はマイク停止
                mic_enabled_event.clear()
                # 物理的にも入力を止める
                try:
                    if stream.is_active():
                        stream.stop_stream()
                except Exception:
                    pass
                awaiting_response.set()
                # 状態リセット（次ターンのため）
                voice_started = False
                silence_ms_after_voice = 0.0
                speech_ms = 0.0

        await asyncio.sleep(0)

# サーバーから音声を受信して再生する非同期関数
async def receive_audio(websocket, output_stream, input_stream, mic_enabled_event: asyncio.Event, awaiting_response: asyncio.Event):
    print("assistant: ", end = "", flush = True)
    loop = asyncio.get_event_loop()
    assistant_speaking = False
    while True:
        # サーバーからの応答を受信
        response = await websocket.recv()
        response_data = json.loads(response)

        # サーバーからの応答をリアルタイム（ストリーム）で表示
        if "type" in response_data and response_data["type"] == "response.audio_transcript.delta":
            print(response_data["delta"], end = "", flush = True)
        # サーバからの応答が完了したことを取得（テキストの区切り）
        elif "type" in response_data and response_data["type"] == "response.audio_transcript.done":
            print("\nassistant: ", end = "", flush = True)
            # 音声が無い応答（テキストのみ）の場合はここで終了扱い
            if not assistant_speaking:
                await websocket.send(json.dumps({"type": "input_audio_buffer.clear"}))
                awaiting_response.clear()
                mic_enabled_event.set()

        elif "type" in response_data and response_data["type"] == "response.completed":
            # 応答完了: サーバ側バッファをクリアし、マイク再開
            await websocket.send(json.dumps({"type": "input_audio_buffer.clear"}))
            awaiting_response.clear()
            mic_enabled_event.set()

        # サーバーからの応答に音声データが含まれているか確認
        if "delta" in response_data:
            if response_data["type"] == "response.audio.delta":
                base64_audio_response = response_data["delta"]
                if base64_audio_response:
                    pcm16_audio = base64_to_pcm16(base64_audio_response)
                    #音声データがある場合は、出力ストリームから再生
                    await loop.run_in_executor(None, output_stream.write, pcm16_audio)
                    if not assistant_speaking:
                        # 念のためここでもマイクを停止
                        try:
                            if input_stream.is_active():
                                input_stream.stop_stream()
                        except Exception:
                            pass
                    assistant_speaking = True

        # 音声出力の完了イベント
        if "type" in response_data and response_data["type"] in ("response.audio.done", "response.completed", "response.output_text.done"):
            await websocket.send(json.dumps({"type": "input_audio_buffer.clear"}))
            awaiting_response.clear()
            mic_enabled_event.set()
            assistant_speaking = False
            # マイク再開
            try:
                if not input_stream.is_active():
                    input_stream.start_stream()
            except Exception:
                pass

# マイクからの音声を取得し、WebSocketで送信しながらサーバーからの音声応答を再生する非同期関数
async def stream_audio_and_receive_response():
    # WebSocketに接続
    async with websockets.connect(WS_URL, additional_headers=HEADERS) as websocket:
        print("WebSocketに接続しました。")

        # セッション設定（最初は応答を生成しない & サーバの自動ターン検出を無効化）
        session_update = {
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "instructions": "あなたはおしゃべり上手です。話を盛り上げてください。",
                "voice": "cedar",
                "turn_detection": {"type": "server_vad"}
            }
        }
        await websocket.send(json.dumps(session_update))
        print("セッション設定を送信しました。")

        # 応答を確認
        while True:
            msg = await websocket.recv()
            data = json.loads(msg)
            etype = data.get("type")
            if etype == "session.updated":
                print("<< session.updated を受信しました")
                print(json.dumps(data, indent=2, ensure_ascii=False))
                break
            elif etype == "error":
                print("<< エラー:", data)
                break
        
        # PyAudioの設定
        CHUNK = 2048          # マイクからの入力データのチャンクサイズ
        FORMAT = pyaudio.paInt16  # PCM16形式
        CHANNELS = 1          # モノラル
        RATE = 24000          # サンプリングレート（24kHz）

        # PyAudioインスタンス
        p = pyaudio.PyAudio()

        # マイクストリームの初期化
        stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)

        # サーバーからの応答音声を再生するためのストリームを初期化
        output_stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, output=True, frames_per_buffer=CHUNK)

        print("マイク入力およびサーバーからの音声再生を開始...")

        # マイクON/OFF制御（初期はON）と応答待機フラグ
        mic_enabled_event = asyncio.Event()
        mic_enabled_event.set()
        awaiting_response = asyncio.Event()

        try:
            # 音声送信タスクと音声受信タスクを非同期で並行実行
            send_task = asyncio.create_task(send_audio(websocket, stream, CHUNK, RATE, mic_enabled_event, awaiting_response))
            receive_task = asyncio.create_task(receive_audio(websocket, output_stream, stream, mic_enabled_event, awaiting_response))

            # タスクが終了するまで待機
            await asyncio.gather(send_task, receive_task)

        except KeyboardInterrupt:
            # キーボードの割り込みで終了
            print("終了します...")
        finally:
            # ストリームを閉じる
            if stream.is_active():
                stream.stop_stream()
            stream.close()
            output_stream.stop_stream()
            output_stream.close()
            p.terminate()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(stream_audio_and_receive_response())
