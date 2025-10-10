import re
import json
import urllib.request
import threading
import asyncio
import time
from typing import Optional, Union

from command_selector import CommandSelector
from llm import LLM
from stt_azure import AzureSpeechToText
from stt_google import GoogleSpeechToText
from voicevox import VoiceVoxTTS

from helper.halo_helper import HaloHelper
from helper.filler import Filler
from helper.corr_gate import CorrelationGate
from helper.asr_coherence import ASRCoherenceFilter
from helper.vad import VAD
from helper.similarity import TextSimilarity
from halo_mcp.spotify_refresh import SpotifyRefresh
from motor_controller import MotorController
from halo_janome import JapaneseNounExtractor
from voicevox_pipelined import VoiceVoxTTSPipelined
from random_action import RandomAction

class Halo:
    def __init__(self):
        self.halo_helper = HaloHelper()
        self.config = self.halo_helper.load_config()

        self.owner_name: str = self.config["owner_name"]
        self.your_name: str = self.config["your_name"]
        self.run_timeout_sec: int = self.config["run_timeout_sec"]
        self.stt_max_len: int = self.config["stt_max_len"]
        self.stt_type: str = self.config["stt"]
        self.llm_model: str = self.config["llm"]
        self.tts_config: dict = self.config["voiceVoxTTS"]
        self.change_name: dict = self.config["change_text"]
        self.isfiller: bool = self.config["filler"]["use_filler"]
        self.is_need_wav_filler = True  #wav再生のフィラーが必要かどうか
        self.filler_dir: str = self.config["filler"]["filler_dir"]
        self.coherence_threshold: float = self.config["coherence_threshold"]

        self.wakeup_word: str = self.config["wakeup_word"]
        self.wakeup_word_pattern = re.compile(self.wakeup_word)
        self.similarity_threshold: float = self.config["similarity_threshold"]
        self.farewell_word: str = self.config["farewell_word"]
        self.farewell_word_pattern = re.compile(self.farewell_word)

        self.motor_controller = MotorController(self.config)
        self.command_selector = CommandSelector(general_config=self.config)
        self.llm = LLM()
        self.stt = self.load_stt(self.stt_type)
        self.asr_coherence_filter = ASRCoherenceFilter()
        self.similarity = TextSimilarity()
        self.filler = Filler(self.isfiller, self.filler_dir)
        self.system_content = self.halo_helper.load_system_prompt_and_replace(self.owner_name, self.your_name)
        print(self.system_content)
        self.janome = JapaneseNounExtractor()
        self.random_action = RandomAction(self.motor_controller, self.config)

        # 常時STT運用用の状態
        self.history: str = ""
        self.response: str = ""
        self.command: dict = {}
        # TTSをバックグラウンドで回すためのスレッド管理
        self.tts_thread: Optional[threading.Thread] = None
        self.tts_lock = threading.Lock()
        # STT安定化用カウンタ
        self._stt_fail_count: int = 0
        # fake_memory用
        self.fake_memory_text = self.get_fake_diary_text(self.config)
        self.fake_summary_text = self.get_fake_summary_text(self.config)
        
        self.corr_gate = self.init_corr_gate(self.config.get("vad", {}))
        self.tts = self.init_tts(self.tts_config)
        self.init_spotify()

        self.tts_pipelined = VoiceVoxTTSPipelined(base_url=self.tts_config["base_url"], speaker=89, max_len=80)
        self.tts_pipelined.set_params(speedScale=1.0, pitchScale=0.0, intonationScale=1.0)
        self.tts_pipelined.start_stream(motor_controller=self.motor_controller, corr_gate=self.corr_gate, filler=self.filler, synth_workers=3, autoplay=False)
        
        
        # ウォームアップ
        self.pre_warm_up(self.stt, self.llm, self.llm_model, self.system_content)
        

    # ---------- init ----------
    def init_corr_gate(self, cfg: dict) -> CorrelationGate:
        # 相関ゲート（TTS由来の音を抑制）をアプリ全体で共有
        corr_gate = CorrelationGate(
            sample_rate=cfg.get("samplereate", 16000),
            frame_ms=cfg.get("frame_duration_ms", 20),
            buffer_sec=1.0,
            corr_threshold=cfg.get("corr_threshold", 0.60),
            max_lag_ms=cfg.get("max_lag_ms", 95),
        )
        return corr_gate

    def init_tts(self, tts_config: dict) -> VoiceVoxTTS:
        tts = VoiceVoxTTS(
            base_url=tts_config["base_url"],
            speaker=tts_config["speaker"],
            max_len=tts_config["max_len"],
            queue_size=tts_config["queue_size"],
        )
        tts.set_params(
            speedScale=tts_config["speedScale"],
            pitchScale=tts_config["pitchScale"],
            intonationScale=tts_config["intonationScale"],
        )
        return tts

    def init_spotify(self):
        try:
            spotify_refresh = SpotifyRefresh().refresh()
        except Exception as e:
            print(f"Spotify refresh でエラー: {e}")
        return

    # ---------- get fake memory ----------
    def get_fake_diary_text(self, config: dict) -> str:
        if not config["fake_memory"]["use_fake_memory"]:
            return ""
        url = f"{config['fake_memory']['fake_memory_endpoint']}recent?days={config['fake_memory']['get_fake_memory_days']}"
        return self._get_fake_memory_text(config["fake_memory"]["use_fake_memory"], url)

    def get_fake_summary_text(self, config: dict) -> str:
        if not config["fake_memory"]["use_fake_memory"]:
            return ""
        url = f"{config['fake_memory']['fake_memory_endpoint']}summary/{self.halo_helper.get_today()}"
        return self._get_fake_memory_text(config["fake_memory"]["use_fake_memory"], url)

    
    def _get_fake_memory_text(self, use_fake_memory: bool, fake_memory_endpoint: str) -> str:
        if not use_fake_memory:
            return ""
        today = self.halo_helper.get_today_month_day()
        url = fake_memory_endpoint
        print(url)
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                body_text = resp.read().decode(charset, errors="replace")
                body_text = body_text.replace(today, f"今日")
            data = json.loads(body_text)
            fake_memory_text = data.get("content", "") or ""
            print(fake_memory_text)
            return fake_memory_text
        except Exception as e:
            print(f"fake_memory取得エラー: {e}")
            return

    # ---------- pre warm up ----------
    def pre_warm_up(self, stt, llm, llm_model, system_content):
        # プリウォーム
        try:
            stt.warm_up()
            response_text = llm.generate_text(llm_model, "日本語で会話", system_content, "")
            print(response_text)
        except Exception as e:
            print(f"STT warm_up でエラー: {e}")



    

    # ---------- main loop ----------
    def main_loop(self) -> None:
        self.speak_async("ハロ、起動した")
        self.run()
        time.sleep(1)
        self.speak_async("ハロ、待機モード")
        self.random_action.reset_timer()
        while True:
            self.random_action.random_action(self.config["random_action_time"])
            if not self.is_vad(
                self.config, 
                self.config["vad"]["waiting_min_consecutive_speech_frames"]):
                time.sleep(0.1)
                continue
            first_text = self.stt.listen_once_fast(motor_controller=self.motor_controller)
            if not self.wakeup_word_pattern.match(first_text):
                print("ウェイクアップキーワードが含まれていません")
                time.sleep(0.1)
                continue
            self.speak_async("ハロ、おしゃべりする！")
            self.run()
            time.sleep(1)
            self.speak_async("ハロ、待機モード")
            self.random_action.reset_timer()

    def run(self) -> None:
        print("========== 話しかけてください。Ctrl+Cで終了します。 ==========")
        time_out = time.time() + self.run_timeout_sec

        try:
            while True:
                try:
                    if time.time() >= time_out:
                        print(f"タイムアウト({self.run_timeout_sec}s)により終了します。")
                        break
                    
                    self.stop_led()

                    # VADで発話を検出
                    if not self.is_vad(
                        self.config, 
                        self.config["vad"]["min_consecutive_speech_frames"]):
                        time.sleep(0.1)
                        continue

                    self.is_need_wav_filler = True  #wav再生のフィラーをリセットしておく
                    is_kaigyo = False  #改行が含まれているかどうか

                    # ユーザー発話認識(キーワード取得)
                    user_text = self.listen_with_nouns()
                    if not user_text or user_text == "":
                        time.sleep(0.1)
                        continue
                    # ユーザー発話認識のテキスト変更(春→ハロなど)
                    user_text = self.halo_helper.apply_text_changes(user_text, self.change_name)
                    self.history = self.halo_helper.append_history(self.history, self.owner_name, user_text)

                    # 終了ワードのチェック
                    if self.check_farewell(user_text):
                        break
                    # 文章のチェックして、正しいユーザー発話ではない場合はcontinue
                    if self.check_sentence(user_text, self.response):
                        continue

                    # パイプライン再生開始
                    self.tts_pipelined.stop_play_object() # 前回の話を止めて割り込む場合
                    self.tts_pipelined.talk_resume()
                    # もし会話が走っていたら、その会話はスキップ

                    # フィラー再生
                    self.say_filler(self.is_need_wav_filler)

                    # コマンド直接実行の場合
                    try:
                        response = self.command_selector.select(user_text, self.fake_summary_text)
                        if response:
                            self.response = response
                            self.history = self.halo_helper.append_history(self.history, self.your_name, self.response)
                            self.speak_async(self.response)
                            continue
                    except Exception as e:
                        print(f"コマンド実行エラー: {e}")

                    print("LLMで応答を生成中...")
                    system_memory = self.system_content + self.fake_memory_text
                    self.response = ""
                    for delta in self.llm.stream_generate_text(self.llm_model, user_text, system_memory, self.history):
                        if not delta:
                            continue
                        self.response += delta

                        # 改行がきたらその時だけ流して、そのあとはパイプラインに流さない
                        if "\n" in delta:
                            is_kaigyo = True
                            delta = delta.replace("\n", "")
                            print(f"改行発生 : {delta}")
                            self.tts_pipelined.push_text(delta)
                        # 逐次テキスト断片をパイプラインへ投入
                        if not is_kaigyo:
                            self.tts_pipelined.push_text(delta)
                        print(f"[response] {self.response}")

                    # パイプライン再生中にpanを動かす
                    self.motor_controller.motor_pan_kyoro_kyoro(1, 2)
                    # パイプライン再生終了
                    self.tts_pipelined.talk_pause_after_flush(flush_ingest=False)

                    # コマンドの取り出しと履歴追加
                    self.response, self.command = self.halo_helper.get_halo_response(self.response)
                    self.history = self.halo_helper.append_history(self.history, self.your_name, self.response)
                    # コマンドがあれば実行
                    self.exec_command(self.command)
                    
                    '''
                    response_text = self.llm.generate_text(self.llm_model, user_text, system_memory, self.history)
                    self.response = response_text

                    # 応答読み上げは非同期で行う
                    #self.speak_async(self.response)
                    self.tts_pipelined.push_text(self.response)
                    '''
                    time_out = time.time() + self.run_timeout_sec    # タイムアウト時間を更新

                except KeyboardInterrupt:
                    print("\n\n音声認識ループが中断されました")
                    break
        except Exception as e:
            print(f"音声認識ループでエラーが発生しました: {e}")
        finally:
            try:
                self.stop_tts()
                if self.tts_thread and self.tts_thread.is_alive():
                    self.tts_thread.join(timeout=0.5)
            except Exception:
                pass
            try:
                self.stt.close()
            except Exception:
                pass
            try:
                self.motor_controller.led_stop_blink()
                self.motor_controller.stop_motor()
            except Exception:
                pass

    # ---------- stt ----------
    def listen_with_nouns(self) -> str:
        self.janome.reset_keyword_filler()
        # 途中結果の出力用ハンドラ
        def _on_interim(txt: str):
            try:
                print(f"[interim] {txt}")
                def _task():
                    try:
                        # 普通名詞・固有名詞でフィラーを生成
                        keyword_filler = asyncio.run(self.janome.make_keyword_filler_async(txt, self.your_name))
                        if keyword_filler != "":
                            print(f"[keyword_filler] {keyword_filler}")
                            if self.tts_pipelined.is_object_playing():
                                self.tts_pipelined.barge_in("バージイン", mode="hard_nonstop_wav") #バージンは初回言わないバグがあるための対応
                            self.tts_pipelined.push_text(keyword_filler)
                            self.is_need_wav_filler = False
                    except Exception:
                        pass
                threading.Thread(target=_task, daemon=True).start()
            except Exception:
                pass
        user_text = self.stt.listen_once_fast(motor_controller=self.motor_controller, on_interim=_on_interim)
        return user_text

    # ---------- tts control ----------
    def stop_tts(self) -> None:
        try:
            self.tts.stop()
        except Exception:
            pass

    def speak_async(self, text: str) -> VoiceVoxTTS:
        # 進行中があれば停止
        with self.tts_lock:
            self.stop_tts()
            self.motor_controller.led_stop_blink()
            self.motor_controller.stop_motor()
            # 再生停止の完了を待つ（短時間）
            try:
                # まずはスレッドの終了を待機
                if self.tts_thread and self.tts_thread.is_alive():
                    self.tts_thread.join(timeout=1.0)
            except Exception as e:
                print(f"TTSスレッド終了エラー: {e}")
            
            def _run():
                try:
                    self.motor_controller.motor_pan_kyoro_kyoro(1, 2)
                    self.tts.speak(text, self.motor_controller, corr_gate=self.corr_gate, filler=self.filler)
                except Exception as e:
                    print(f"TTSエラー: {e}")

            self.tts_thread = threading.Thread(target=_run, daemon=True)
            self.tts_thread.start()
        return self.tts

    # ----------メカ------------------
    def stop_led(self):
        """パイプライン再生が終了したらled停止"""
        if not self.tts_pipelined.is_object_playing():
            self.motor_controller.led_stop_blink() #led停止

    # ---------- コマンド実行 ----------
    def exec_command(self, command: str) -> str:
        if self.command == "":
            return
        command = command.replace("{summary}", self.fake_summary_text)
        print(command)
        fut = self.command_selector.exec_command(command)
        if fut:
            def _on_done(f):
                try:
                    result = f.result()
                    if result:
                        self.response = result['result']
                        self.history = self.halo_helper.append_history(self.history, self.your_name, self.response)
                        print(f"[command_response] {self.response}")
                        self.speak_async(self.response)
                except Exception as e:
                    print(f"[command_error] {e}")
            fut.add_done_callback(_on_done)


    # ---------- 会話ロジック ----------
    def is_vad(self, config: dict, min_consecutive_speech_frames: int) -> bool:
        print("VAD detection start")
        is_vad = VAD.listen_until_voice_webrtc(
            aggressiveness=config["vad"]["aggressiveness"],
            samplerate=config["vad"]["samplereate"],
            frame_duration_ms=config["vad"]["frame_duration_ms"],
            min_consecutive_speech_frames=min_consecutive_speech_frames,
            device=None,
            timeout_seconds=None,
            corr_gate=self.corr_gate,
            stop_event=None,
        )
        if is_vad:
            print("VAD detected")
        return is_vad

    def check_farewell(self, txt: str) -> bool:
        if self.farewell_word_pattern.match(txt):
            farewell = "バイバイ！"
            print(f"{self.your_name}: {farewell}")
            self.tts.speak(farewell, self.motor_controller, corr_gate=self.corr_gate)
            return True
        return False

    def check_sentence(self, user_text: str, response: str) -> bool:
        if len(user_text) > self.stt_max_len:
            print(f"ユーザー発話がしきい値を超えています :txt: {user_text} :threshold: {self.stt_max_len}")
            return True
        if self.is_similarity_threshold(user_text, response):
            print(f"類似度がしきい値を超えています :txt: {user_text} :response: {response}")
            return True
        """
        if self.is_coherence_threshold(user_text, self.coherence_threshold):
            print(f"破綻がしきい値を超えています :txt: {user_text} :threshold: {self.coherence_threshold}")
            return True
        """
        return False

    def say_filler(self, is_need_wav_filler: bool) -> bool:
        # ttsフィラーだった時は再生しない
        if not is_need_wav_filler:
            return False
        # 音声再生中だった時は再生しない
        if self.tts_pipelined.is_object_playing():
            return False
        if self.filler.say_filler():
            self.motor_controller.led_start_blink()
            self.motor_controller.motor_tilt_kyoro_kyoro(2)
            self.motor_controller.motor_pan_kyoro_kyoro(1, 2)
            return True
        return False

    def is_similarity_threshold(self, user_text: str, response: str) -> bool:
        # 類似度（ユーザ確定テキスト vs 応答テキストの一部）
        score = 0.0
        try:
            # print(f"類似度計算 :user_text: {user_text} :response: {self.response}")
            score, best_sub = self.similarity.calc_max_substring_similarity(user_text, self.response)
            print(f"類似度: {score * 100:.1f}%  一致抜粋: {best_sub[:80]}")
        except Exception as e:
            print(f"類似度計算エラー: {e}")
        return score >= self.similarity_threshold

    def is_coherence_threshold(self, txt: str, threshold: float) -> bool:
        # 文章の破綻度
        try:
            is_noisy, score = self.asr_coherence_filter.is_noisy(txt, threshold)
        except Exception as e:
            print(f"破綻度計算エラー: {e}")
            return False
        # 完全に0の時は文中にハテナがあるなど
        if score == 0.0:
            is_noisy = False
        if is_noisy:
            return True
        return False

    # ---------- STT ----------
    def load_stt(self,stt_type: str) -> Union[AzureSpeechToText, GoogleSpeechToText]:
        if stt_type == "azure":
            return AzureSpeechToText()
        elif stt_type == "google":
            return GoogleSpeechToText()
        else:
            raise ValueError(f"Invalid STT type: {stt_type}")

    

if __name__ == "__main__":
    halo = Halo()
    halo.main_loop()