# tts_pipelined_bargein_gate_flush_skip.py
import wave
import re
import queue
import threading
import time
from io import BytesIO
from typing import Dict, Optional

import requests
import simpleaudio as sa

from helper.filler import Filler

# 実機では本物の MotorController を使ってください
try:
    from motor_controller import MotorController
except Exception:
    class MotorController:
        def led_start_blink(self): pass
        def led_stop_blink(self): pass
        def motor_tilt_kyoro_kyoro(self, n:int=1): pass


class VoiceVoxTTSPipelined:
    """
    VOICEVOX 常駐ストリームTTS
      - 合成は N 並列（/audio_query → /synthesis）
      - 再生は seq で順序保証
      - barge_in(mode="hard"|"soft") で話題を即切替（エポック方式）
      - 再生ゲート: autoplay=False で起動→ talk_resume() で任意開始、talk_pause() で一時停止
      - talk_pause_after_flush(): いまのバックログを全部話し終えたら一時停止
      - skip_current(): 今の文だけ中断して次の文へ（軽量スキップ）
    """

    _SENT_END = re.compile(r"[、。．！？　!?]\s*$")  # 文末検出

    def __init__(
        self,
        base_url: str = "http://192.168.1.151:50021",
        speaker: int = 89,
        max_len: int = 80,
        queue_size_sent: int = 32,    # 文キューのサイズ
        request_timeout_query: int = 15,
        request_timeout_synth: int = 60,
        default_params: Optional[Dict] = None,
    ):
        # VOICEVOX設定
        self.base_url = base_url
        self.speaker = speaker
        self.max_len = max_len
        self.queue_size_sent = queue_size_sent
        self.request_timeout_query = request_timeout_query
        self.request_timeout_synth = request_timeout_synth

        self.params = {
            "speedScale": 1.0,
            "pitchScale": 0.0,
            "intonationScale": 1.0,
            "volumeScale": 1.0,
            "prePhonemeLength": 0.1,
            "postPhonemeLength": 0.1,
            "enableInterrogativeUpspeak": True,
        }
        if default_params:
            self.params.update(default_params)

        # 外部連携
        self._motor: Optional[MotorController] = None
        self._filler = None
        self._corr_gate = None

        # 再生
        self._play_obj: Optional[sa.PlayObject] = None

        # 制御フラグ
        self._stop_event = threading.Event()     # 即停止（緊急）
        self._closed_event = threading.Event()   # 入力終端（穏やかに終了）
        self._started = False
        self._is_speaking = False
        self._state_lock = threading.Lock()

        # 再生ゲート（autoplay=False なら閉じて開始）
        self._play_gate = threading.Event()      # set=再生OK / clear=待機

        # 入出力キュー
        self._in_q: "queue.Queue[str]" = queue.Queue(maxsize=1024)  # 断片投入
        # sent_q: (epoch, seq, sentence)
        self._sent_q: "queue.Queue[tuple[int, int, str]]" = queue.Queue(self.queue_size_sent)

        # エポック/順序管理
        self._epoch = 0                 # 新規に生成される文の世代
        self._player_epoch = 0          # プレーヤが再生中/待機中の世代
        self._seq_counter = 0           # 現エポックで新規付与する seq
        self._next_seq = 0              # プレーヤが次に欲しい seq
        self._flush_after_current = False  # soft 割り込み用：現行文の直後に一掃・切替
        self._synth_inflight = 0
        self._synth_inflight_lock = threading.Lock()

        # 合成結果（epoch ごとに {seq: wav_bytes}）
        self._results: Dict[int, Dict[int, bytes]] = {}
        self._results_cv = threading.Condition()  # _results / _player_epoch / _next_seq の同期

        # ingest のローカルバッファを捨てる合図（hard 割り込み時に使用）
        self._reset_ingest_buf = False

        # 「全部話し終わったら pause」のためのフラグ
        self._pause_when_idle = False
        # 句点待ちの途中断片も含め、即座に一度すべてフラッシュしたい時の合図
        self._force_ingest_flush = False

        # 現在文スキップ用
        self._skip_event = threading.Event()

        # スレッド
        self._th_ingest: Optional[threading.Thread] = None
        self._th_player: Optional[threading.Thread] = None
        self._th_synths: list[threading.Thread] = []

        # HTTPセッション（接続再利用で微速化）
        self._http = requests.Session()

        

    # ---------- Public API ----------
    def set_params(self, **kwargs):
        self.params.update(kwargs)

    def set_speaker(self, speaker: int):
        self.speaker = speaker

    def start_stream(
        self,
        motor_controller: MotorController,
        corr_gate=None,
        filler=None,
        synth_workers: int = 2,
        autoplay: bool = True,
    ):
        with self._state_lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            self._closed_event.clear()
            self._is_speaking = True

            self._motor = motor_controller
            self._corr_gate = corr_gate
            self._filler = filler

            if autoplay:
                self._play_gate.set()
            else:
                self._play_gate.clear()

            # ingest（断片→文）
            self._th_ingest = threading.Thread(target=self._run_ingest, name="tts_ingest", daemon=True)
            self._th_ingest.start()

            # 合成ワーカー（並列）
            self._th_synths = []
            for i in range(max(1, synth_workers)):
                th = threading.Thread(target=self._run_synth_worker, name=f"tts_synth_{i}", daemon=True)
                th.start()
                self._th_synths.append(th)

            # プレーヤ
            self._th_player = threading.Thread(target=self._run_player, name="tts_player", daemon=True)
            self._th_player.start()

    def talk_pause(self):
        """次の文から再生を止める（現在再生中の文は言い切る）。"""
        self._play_gate.clear()

    def talk_resume(self):
        """再生を許可（バッファ済みの先頭文から話し始める）。"""
        self._play_gate.set()

    def talk_pause_after_flush(self, flush_ingest: bool = True):
        """
        いま溜まっているメッセージ（バックログ）を話し切ったら一時停止する。
        - flush_ingest=True: ingest の内部バッファ/入力キューも強制フラッシュして文にする。
                             句点待ちをせず、早く止めたい時に便利。
        注意:
        - このメソッド呼び出し「後」に push_text() した分はバックログに追加されるため、
          追加が続くと停止も延びます（＝“完全に空になるまで話す”動作）。
        """
        self._pause_when_idle = True
        if flush_ingest:
            self._force_ingest_flush = True

    def skip_current(self):
        """
        今再生中の文（または gate 待ちで“次に再生予定”の文）をスキップし、次の文へ進む。
        - エポックは変えない／キューは捨てない
        - 再生ゲートが閉じている場合は、“次に再生予定”の文を飛ばして待機する
        """
        self._skip_event.set()
        with self._suppress_ex():
            if self._play_obj:
                self._play_obj.stop()  # 再生中なら即停止 → 次ループで次文へ

    def push_text(self, text: str):
        if not text:
            return
        if not self._started:
            raise RuntimeError("start_stream() を先に呼んでください。")
        try:
            self._in_q.put_nowait(text)
        except queue.Full:
            self._in_q.put(text)  # backpressure

    def close_stream(self):
        self._closed_event.set()

    def wait_until_idle(self, poll: float = 0.05):
        while self.is_playing():
            time.sleep(poll)

    def stop(self):
        """即時停止（すべて中断・破棄）"""
        self._stop_event.set()
        with self._suppress_ex():
            if self._play_obj:
                self._play_obj.stop()
        self._drain_queue(self._in_q)
        self._drain_queue(self._sent_q)
        with self._results_cv:
            self._results.clear()
            self._results_cv.notify_all()

    def shutdown(self, timeout: float = 2.0):
        """安全終了（アプリ終了時）"""
        self.close_stream()
        self.wait_until_idle()
        self._stop_event.set()
        for th in ([self._th_ingest] + self._th_synths + [self._th_player]):
            if th and th.is_alive():
                th.join(timeout=timeout)
        with self._state_lock:
            self._started = False
            self._is_speaking = False

    def is_playing(self) -> bool:
        if self._stop_event.is_set():
            return False
        if self._is_speaking:
            return True
        if self._play_obj is not None:
            try:
                return self._play_obj.is_playing()
            except Exception:
                return False
        return False

    def is_object_playing(self) -> bool:
        """現在読み上げ（合成/再生）中かどうか。"""
        if self._play_obj is not None:
            try:
                return self._play_obj.is_playing()
            except Exception:
                return False
        return False

    def stop_play_object(self):
        """再生オブジェクトを停止"""
        with self._suppress_ex():
            if self._play_obj:
                self._play_obj.stop()

    # --- 割り込み（barge-in） ---
    def barge_in(self, text: str, mode: str = "hard"):
        """
        mode="hard": 今の再生も即停止。旧世代のキュー/結果を破棄し、新規世代へ切替。
                     最初の文は ingest をバイパスして sent_q に即投入。
                     （※再生ゲートが閉じていれば、再開するまでしゃべりません）
        mode="soft": 今の文を言い切ってから旧世代を一掃→新規世代へ切替。
        mode="hard_nonstop_wav" : wav再生は止めないハードバージイン
        """
        if mode not in ("hard", "soft", "hard_nonstop_wav"):
            mode = "hard"

        # 新しいエポックを開始
        self._epoch += 1

        # 旧データを捨てる：入力断片/文キュー/結果
        self._drain_queue(self._in_q)
        self._drain_queue(self._sent_q)
        with self._results_cv:
            self._results.clear()
            self._results_cv.notify_all()

        # 新世代の採番リセット
        self._seq_counter = 0

        if mode == "hard":
            # 再生も即停止
            with self._suppress_ex():
                if self._play_obj:
                    self._play_obj.stop()

            # ingest のローカルバッファも確実に捨てる
            self._reset_ingest_buf = True

            # 再生側を新世代へ即切替
            with self._results_cv:
                self._player_epoch = self._epoch
                self._next_seq = 0
                self._flush_after_current = False
                self._results_cv.notify_all()

            # 最初の文は ingest を通さず sent_q に即投入（確実に先頭で再生させる）
            self._push_sentence_immediate(text)

        elif mode == "hard_nonstop_wav":
            # ingest のローカルバッファも確実に捨てる
            self._reset_ingest_buf = True

            # 再生側を新世代へ即切替
            with self._results_cv:
                self._player_epoch = self._epoch
                self._next_seq = 0
                self._flush_after_current = False
                self._results_cv.notify_all()

            # 最初の文は ingest を通さず sent_q に即投入（確実に先頭で再生させる）
            self._push_sentence_immediate(text)

        else:
            # soft：現在の文を言い切ってから切替
            with self._results_cv:
                self._flush_after_current = True
                self._results_cv.notify_all()
            # soft は通常どおり push_text（句点/長さで自然にフラッシュ）
            self.push_text(text)

    # ---------- Threads ----------
    def _run_ingest(self):
        buf = ""
        while not self._stop_event.is_set():
            # hard 割り込み直後はローカルバッファを捨てる
            if self._reset_ingest_buf:
                buf = ""
                self._reset_ingest_buf = False

            # ★ 要求があれば、今ある断片を即座に文に変換して流す
            if self._force_ingest_flush:
                # 入力キューを非ブロッキングでできるだけ吸い込む
                while True:
                    try:
                        piece2 = self._in_q.get_nowait()
                        buf += piece2
                    except queue.Empty:
                        break
                s2 = buf.strip()
                if s2:
                    epoch = self._epoch
                    seq = self._seq_counter; self._seq_counter += 1
                    self._sent_q.put((epoch, seq, s2))
                    buf = ""
                self._force_ingest_flush = False

            try:
                timeout = 0.1 if self._closed_event.is_set() else 1.0
                try:
                    piece = self._in_q.get(timeout=timeout)
                except queue.Empty:
                    piece = None

                if piece is None:
                    if self._closed_event.is_set():
                        tail = buf.strip()
                        if tail:
                            epoch = self._epoch
                            seq = self._seq_counter; self._seq_counter += 1
                            self._sent_q.put((epoch, seq, tail))
                        break
                    continue

                buf += piece
                if len(buf) >= self.max_len or self._SENT_END.search(buf):
                    s = buf.strip()
                    if s:
                        epoch = self._epoch
                        seq = self._seq_counter; self._seq_counter += 1
                        self._sent_q.put((epoch, seq, s))
                    buf = ""

            except Exception:
                if self._stop_event.is_set():
                    break

        # 最終フラッシュ（保険）
        tail = buf.strip()
        if tail:
            with self._suppress_ex():
                epoch = self._epoch
                seq = self._seq_counter; self._seq_counter += 1
                self._sent_q.put((epoch, seq, tail))

    def _run_synth_worker(self):
        while not self._stop_event.is_set():
            try:
                timeout = 0.1 if self._closed_event.is_set() else 1.0
                try:
                    epoch, seq, sent = self._sent_q.get(timeout=timeout)
                except queue.Empty:
                    # すべて閉じて空なら終了
                    if self._closed_event.is_set() and self._sent_q.empty():
                        break
                    continue

                with self._synth_inflight_lock:
                    self._synth_inflight += 1

                try:
                    # 合成（簡易リトライ 1 回）
                    query = self._audio_query(sent, self.speaker)
                    query.update(self.params)
                    wav_bytes = self._synth(query, self.speaker)

                    # AEC等：far-endへ16k/monoで供給（任意）
                    if self._corr_gate is not None:
                        with self._suppress_ex():
                            pcm = self._wav_to_int16_mono16k(wav_bytes)
                            self._corr_gate.publish_farend(pcm)

                    # 結果を登録（保存直前に epoch を確認）
                    with self._results_cv:
                        # hard 割り込みで player_epoch が進んでいたら旧世代は破棄
                        if epoch < self._player_epoch:
                            continue
                        bucket = self._results.setdefault(epoch, {})
                        # 文文字数も保存して、再生時のレイテンシログに出す
                        bucket[seq] = (wav_bytes, len(sent))
                        self._results_cv.notify_all()
                finally:
                    with self._synth_inflight_lock:
                        self._synth_inflight -= 1

            except Exception:
                if self._stop_event.is_set():
                    break

    def _run_player(self):
        start_time = time.perf_counter()
        try:
            while not self._stop_event.is_set():
                with self._results_cv:
                    # 再生対象は self._player_epoch / self._next_seq
                    while True:
                        bucket = self._results.get(self._player_epoch, {})
                        if self._next_seq in bucket:
                            val = bucket.pop(self._next_seq)
                            # 互換性のため、古い形式（bytesのみ）が来ても扱えるようにする
                            if isinstance(val, tuple) and len(val) == 2:
                                wav_bytes, _char_count = val
                            else:
                                wav_bytes = val
                                _char_count = None
                            break  # 再生へ

                        # 終了判定（クローズ & 何も残っていない）
                        all_empty = (
                            self._closed_event.is_set() and
                            self._in_q.empty() and
                            self._sent_q.empty() and
                            not any(self._results.values())
                        )
                        if all_empty:
                            return

                        # まだ来てない → 待機
                        self._results_cv.wait(timeout=0.2)
                        if self._stop_event.is_set():
                            return

                # --- 再生前スキップ（ここで指示が出ていればこの文を飛ばす） ---
                if self._skip_event.is_set():
                    self._skip_event.clear()
                    self._next_seq += 1
                    # soft-barge-in が保留なら「言い切った扱い」で即切替
                    with self._results_cv:
                        if self._flush_after_current:
                            self._flush_after_current = False
                            self._results.pop(self._player_epoch, None)
                            self._player_epoch = self._epoch
                            self._next_seq = 0
                            self._results_cv.notify_all()
                    continue

                # ★ 再生ゲート：開くまで待つ（stop/skip にも反応）
                skipped_before_play = False
                while not self._stop_event.is_set() and not self._play_gate.is_set():
                    # ゲート待ちの間にスキップ要求が来たら、この文を飛ばす
                    if self._skip_event.is_set():
                        self._skip_event.clear()
                        skipped_before_play = True
                        break
                    time.sleep(0.02)
                if self._stop_event.is_set():
                    return
                if skipped_before_play:
                    self._next_seq += 1
                    with self._results_cv:
                        if self._flush_after_current:
                            self._flush_after_current = False
                            self._results.pop(self._player_epoch, None)
                            self._player_epoch = self._epoch
                            self._next_seq = 0
                            self._results_cv.notify_all()
                    continue

                # 再生
                self._play(wav_bytes)

                # 初回からのレイテンシログ（必要なければ削除OK）
                end_time = time.perf_counter()
                elapsed = end_time - start_time
                if _char_count is not None:
                    print(f"[VoiceVox latency] {elapsed:.1f} s, chars={_char_count},speed={_char_count/elapsed:.1f}chars/s")
                else:
                    print(f"[VoiceVox latency] {elapsed:.1f} s")
                start_time = end_time

                # 文ごとに待つと自然
                if self._play_obj:
                    self._play_obj.wait_done()

                # 再生中に skip_current() が来て stop 済みの場合の掃除
                if self._skip_event.is_set():
                    self._skip_event.clear()

                # 次の文へ
                self._next_seq += 1

                # ソフト割り込み：現在の文が終わった直後に旧世代を一掃して切替
                with self._results_cv:
                    if self._flush_after_current:
                        self._flush_after_current = False
                        # 旧世代の残りを捨てる
                        self._results.pop(self._player_epoch, None)
                        # 新世代へ切替
                        self._player_epoch = self._epoch
                        self._next_seq = 0
                        self._results_cv.notify_all()

                # ★ ここで「全部しゃべり切っていれば」再生ゲートを閉じて一時停止
                if self._pause_when_idle and self._is_idle_now():
                    self._play_gate.clear()       # 以後の再生を停止
                    self._pause_when_idle = False

        finally:
            with self._suppress_ex():
                if self._play_obj:
                    self._play_obj.stop()
                if self._motor:
                    self._motor.led_stop_blink()
            self._is_speaking = False

    # ---------- VOICEVOX HTTP（簡易リトライ付き） ----------
    def _audio_query(self, text: str, speaker: int) -> Dict:
        for i in range(2):  # 1 リトライ
            try:
                r = self._http.post(
                    f"{self.base_url}/audio_query",
                    params={"text": text, "speaker": speaker},
                    timeout=self.request_timeout_query,
                )
                r.raise_for_status()
                return r.json()
            except Exception:
                if i == 0:
                    continue
                raise

    def _synth(self, query: Dict, speaker: int) -> bytes:
        for i in range(2):
            try:
                r = self._http.post(
                    f"{self.base_url}/synthesis",
                    params={"speaker": speaker},
                    json=query,
                    timeout=self.request_timeout_synth,
                )
                r.raise_for_status()
                return r.content
            except Exception:
                if i == 0:
                    continue
                raise

    def _play(self, wav_bytes: bytes):
        with wave.open(BytesIO(wav_bytes), "rb") as wf:
            wav = sa.WaveObject.from_wave_read(wf)
        if self._filler is not None:
            with self._suppress_ex():
                self._filler.stop_filler()
        if self._motor:
            with self._suppress_ex():
                self._motor.led_start_blink()
                self._motor.motor_tilt_kyoro_kyoro(2)
        self._play_obj = wav.play()

    # ---------- utils ----------
    def _push_sentence_immediate(self, text: str):
        """ingest を通さず、(epoch, seq, sentence) を sent_q に即投入する。"""
        s = (text or "").strip()
        if not s:
            return
        epoch = self._epoch
        seq = self._seq_counter; self._seq_counter += 1
        self._sent_q.put((epoch, seq, s))

    def _wav_to_int16_mono16k(self, wav_bytes: bytes):
        import numpy as np, io, wave as _wave
        with _wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            ch = wf.getnchannels()
            sr = wf.getframerate()
            n = wf.getnframes()
            pcm = np.frombuffer(wf.readframes(n), dtype=np.int16)
            if ch > 1:
                pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.int16)
        if sr != 16000:
            import numpy as np
            ratio = 16000 / sr
            x_old = np.arange(len(pcm))
            x_new = np.arange(0, len(pcm), 1/ratio)
            pcm = np.interp(x_new, x_old, pcm.astype(np.float32)).astype(np.int16)
        return pcm

    def _is_idle_now(self) -> bool:
        with self._results_cv:
            pending_results = any(bucket for bucket in self._results.values())
        with self._synth_inflight_lock:
            inflight = self._synth_inflight
        return (self._in_q.empty() and self._sent_q.empty()
                and not pending_results and inflight == 0)

    @staticmethod
    def _drain_queue(q: "queue.Queue"):
        with VoiceVoxTTSPipelined._suppress_ex():
            while not q.empty():
                try:
                    q.get_nowait()
                except Exception:
                    break

    @staticmethod
    def _suppress_ex():
        class _Ctx:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return True
        return _Ctx()


# ---- 使い方サンプル ----
if __name__ == "__main__":
    import time
    

    motor = MotorController()
    tts = VoiceVoxTTSPipelined(base_url="http://127.0.0.1:50021", speaker=89, max_len=80)
    tts.set_params(speedScale=1.0, pitchScale=0.0, intonationScale=1.0)

    tts.start_stream(motor_controller=motor, synth_workers=2, autoplay=True)

    tts.push_text("一文目です。")
    tts.push_text("二文目です。")
    tts.push_text("三文目です。")

    time.sleep(0.3)        # 1文目の途中で
    tts.skip_current()     # 1文目を中断 → 2文目へ

    # ゲートを閉じて“次の文”を待機状態に
    tts.talk_pause()
    tts.push_text("四文目です。（ゲート閉じているのでまだ鳴らない）")
    time.sleep(0.5)
    tts.skip_current()     # 次に鳴る予定の文（四文目）をスキップ
    tts.talk_resume()      # 再開 → 五文目以降があればそこから鳴る

    # バックログを全部吐いて止める
    tts.talk_pause_after_flush(flush_ingest=True)

    tts.close_stream()
    tts.wait_until_idle()
    tts.shutdown()
