## 提案（リポジトリ全体に基づく新機能）

### まずは結論（優先度高の5案）
- **明瞭化ダッシュボード（Web UI）**
  - ログ/メトリクス/音量VU/ASR遅延/デバイス状態を可視化
  - `server/server.py` に REST/WS を追加し、ブラウザで監視・操作
- **ストリーミングTTS最適化とフェイルオーバー**
  - `tts_streaming/halo_stream_tts.py` と `voicevox_pipelined.py` を統合
  - チャンク出力の低遅延化＋VOICEVOX/クラウドTTSの自動切替
- **VAD/エコーキャンセル/AGCの音声前処理統合**
  - `helper/vad.py` を起点に AEC/NS/AGC（WebRTC系）を組み込み
  - 誤起動低減・ASR精度向上
- **ルーチン自動化（自然言語→マクロ）**
  - 「朝の準備して」で Bluetooth・モータ・ブラウザ操作を連鎖
  - `halo.py` のツール呼び出しを統一し、`halo_playwright/`・`bluetooth/`・`function_*` を編成
- **意味記憶の永続化と検索UI**
  - `fake_memory/` 群を Embeddings＋要約で統合、会話文脈へ自動注入
  - Web UIで検索・編集

### 他にも「こういう機能があればいいんじゃない？」
- **マルチASR自動切替**
  - `stt_google.py` / `stt_azure.py` / `experiment/stt_realtime_gpt.py` を品質/遅延で自動選択
  - 部分結果の早期提示
- **バージイン（割込み）とウェイクワード**
  - 強化VAD＋Wake Word（Porcupine等）でハンズフリー自然対話
- **会話感情→モータ/LEDアニメーション**
  - `function_motor.py`/`function_led.py` を会話状態フックに接続
- **Playwrightフローの自然言語化**
  - `halo_playwright/controller_browser.py` を拡張し、音声→安定フロー化＋自己回復分岐
- **Bluetoothプレゼンスと自動ハンドオーバー**
  - `bluetooth/bluetooth_controll.py` に検知・再接続・状態通知、入出力自動切替
- **MCPツール標準化とキャッシュ**
  - `halo_mcp/` を関数呼び出し規約で統一、レート制御・結果キャッシュ・タイムアウト健全化
- **サーバAPI強化とWebhook**
  - `server/server.py` にイベント→Webhook配信、キュー（遅延/再試行）、軽量認可
- **観測性（OpenTelemetry）**
  - 区間別遅延（VAD→ASR→LLM→TTS）・エラー率・リトライ数を計測しダッシュボードへ
- **音声リグレッションテスト**
  - Golden音源でVAD/ASR/合成の退行検知、短時間CI
- **デプロイ整備（Docker Compose）**
  - `server/` `fake_memory/` `voicevox` 連携のCompose＋ヘルスチェック（`pi_system_files/` と連動）
- **日本語特化の言語強化**
  - `janome_dictionary/user_dictionary.csv` を用いた固有表現拡張、SSMLでアクセント制御

### 実装の当たりどころ（既存構成に沿って）
- **音声I/O・前処理**: `helper/vad.py`, `sound_streaming/recive_pi_jitter.py`, `experiment/mic_vu.py`
- **ASR**: `stt_google.py`, `stt_azure.py`, `experiment/stt_realtime_gpt.py`
- **LLM/ストリーム**: `llm.py`, `tts_streaming/llm_stream.py`, `experiment/halo_realtime_gpt.py`
- **TTS**: `voicevox.py`, `voicevox_pipelined.py`, `tts_streaming/halo_stream_tts.py`
- **デバイス制御**: `function_motor.py`, `motor_controller.py`, `function_led.py`, `bluetooth/bluetooth_controll.py`
- **自動化/ブラウザ**: `halo_playwright/`
- **記憶**: `fake_memory/` 一式（要約・検索の統合候補）
- **サーバ**: `server/server.py`（API/WS/Webhookの拡張点）

### スモールスタートの提案（各1–2日のMVP）
- **ダッシュボードMVP**
  - REST: 現在状態/最後の発話/ASR遅延/音量レベル
  - UI: 単一ページでカード表示＋簡易ログストリーム
- **ストリーミングTTS改善**
  - 文/音素チャンク＋クロスフェード、VOICEVOX停止時はクラウドへ自動切替
- **AEC/NS/AGC導入**
  - WebRTC前処理を `helper/` に追加し、既存VADチェーンへ統合
- **ルーチン自動化（1フロー）**
  - 「ニュース読む」→ ブラウザ起動・スクロール・要約読み上げ（失敗時のやり直し込み）
- **メモリ検索UI**
  - `fake_memory/` をEmbeddingsで索引化、Web UIから全文/類似検索＋要約表示

### リスクと対策（要点）
- **低遅延と安定性のトレードオフ**
  - 逐次出力＋フェイルオーバー、メトリクス監視
- **Web UI と本体の責務分離**
  - Webは観測と操作に限定、コアは疎結合API化
- **外部API障害**
  - 明示的タイムアウト/指数バックオフ/キャッシュ

### 次アクション案
- 優先度高の5案から「ダッシュボードMVP → AEC/NS/AGC → TTS最適化」の順で着手
- 実装前に APIスキーマとイベント項目（ASR/LLM/TTS/デバイス）を先に合意


