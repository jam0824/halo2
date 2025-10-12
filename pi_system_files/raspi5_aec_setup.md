# Raspberry Pi 5 音声認識/音声合成向け WebRTC AEC 設定メモ

最終更新: 2025-10-12 (JST)

このメモは、**Raspberry Pi 5 + Debian 12 (bookworm) + PipeWire/Pulse互換 + WebRTC AEC** で、
マイクがスピーカー音を拾わないように構成した手順の記録です。

---

## 0. 環境の確認

```bash
cat /etc/os-release
# → PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
systemctl --user status pipewire
# → Active: active (running)
```

PipeWire は稼働済み。PulseAudio 互換は `pipewire-pulse`。

---

## 1. 必要パッケージの導入（済）

```bash
sudo apt update
sudo apt install -y \
  pipewire pipewire-audio pipewire-pulse pipewire-alsa \
  libspa-0.2-modules wireplumber pulseaudio-utils
```

- `libspa-0.2-modules` に WebRTC AEC 実装が含まれる
- `pulseaudio-utils` は `parec` などを提供

---

## 2. 物理デバイス名の確認（例）

```bash
pactl list short sources
# 例）alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono

pactl list short sinks
# 例）alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo
```

> **今回の実デバイス**
>
> - **Mic (source_master)**: `alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono`  
> - **Speaker (sink_master)**: `alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo`

---

## 3. AEC の一時ロード（手動テスト）

```bash
pactl load-module module-echo-cancel aec_method=webrtc \
  source_master=alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono \
  sink_master=alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo \
  source_name=EC.source sink_name=EC.sink
```

作成された仮想デバイスの確認：

```bash
pactl list short sources | grep -E 'EC\.source|echo'
pactl list short sinks   | grep -E 'EC\.sink|echo'
```

---

## 4. 既定デバイスの固定（コード変更なしで配線）

```bash
pactl set-default-source EC.source
pactl set-default-sink   EC.sink
pactl info | egrep 'Default (Source|Sink)'
# → Default Source: EC.source / Default Sink: EC.sink
```

> simpleaudio は出力先選択が難しいため、**既定Sink=EC.sink** にしておくのが確実。  
> Azure STT は既定ソース（= EC.source）を参照する設定にする。

---

## 5. 永続化（再起動後も自動で AEC 有効）

`~/.config/pulse/default.pa` を作成/編集して、末尾に以下を追記：

```pa
load-module module-echo-cancel aec_method=webrtc \
  source_master=alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono \
  sink_master=alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo \
  source_name=EC.source sink_name=EC.sink
```

再読み込み：

```bash
systemctl --user restart pipewire pipewire-pulse
```

---

## 6. 動作確認

Pulse経由で録音：

```bash
parec -d EC.source --file-format=wav test.wav
aplay test.wav
```

（arecord は ALSA直のため `EC.source` を直接は参照しない→Pulse経由の `parec` を使う）

---

## 7. マイク/スピーカーを差し替えた時の変更点

1) 新しい物理名を確認：

```bash
pactl list short sources | grep alsa_input
pactl list short sinks   | grep alsa_output
```

2) AEC を新デバイスで再ロード：

- **手動運用**:
  ```bash
  MID=$(pactl list modules short | awk '/module-echo-cancel/{print $1}')
  [ -n "$MID" ] && pactl unload-module "$MID"
  pactl load-module module-echo-cancel aec_method=webrtc \
    source_master=<NEW_alsa_input_xxx> \
    sink_master=<NEW_alsa_output_xxx> \
    source_name=EC.source sink_name=EC.sink

  pactl set-default-source EC.source
  pactl set-default-sink   EC.sink
  ```

- **永続設定**:
  `~/.config/pulse/default.pa` の `source_master=` / `sink_master=` を新名に置換 →
  ```bash
  systemctl --user restart pipewire pipewire-pulse
  ```

---

## 8. 便利コマンド集

```bash
# 現在の既定入出力
pactl info | egrep 'Default (Source|Sink)'

# 現在のソース/シンク（簡易）
pactl list short sources
pactl list short sinks

# 稼働中のストリームを EC.sink / EC.source へ移動
pactl list short sink-inputs      # 出力ストリームのIDを確認
pactl move-sink-input <ID> EC.sink
pactl list short source-outputs   # 入力ストリームのIDを確認
pactl move-source-output <ID> EC.source

# PipeWire ログ
journalctl --user -u pipewire -b | grep -i -E 'echo|webrtc|aec|error|fail'
```

---

## 9. アプリ側の要点（参考）

- **STT (Azure Speech SDK)**: 既定ソースを使う or `AudioConfig(device_name="EC.source")`
- **TTS (VOICEVOX + simpleaudio)**: 既定Sink (= `EC.sink`) に音を出すのが確実
- 追加の堅牢化:
  - **半二重**: TTS 再生中は ASR 一時停止 → 終了後 100–200ms 待って再開
  - **48kHz 固定**: AEC 相性が良い
  - **音量調整**: スピーカー音量を必要最小限に

---

## 10. トラブルシューティング

| 症状 | 対処 |
|---|---|
| `arecord: Unknown PCM EC.source` | `arecord` は ALSA 直。`parec -d EC.source` を使う |
| `EC.source/EC.sink が出ない` | `libspa-0.2-modules` 未導入、`pipewire-pulse` 未起動、または `load-module` 未実行 |
| AEC しても回り込みが残る | スピーカー音量を下げる / 半二重併用 / マイク位置や指向性の見直し |
| 既定変更が効かない | 既に動作中のストリームは影響を受けない → `pactl move-xxx` or 再起動 |

---

## 付録：現在の代表値（参考）

- Mic (source_master):  
  `alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono`

- Speaker (sink_master):  
  `alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo`

- AEC 仮想：  
  - Source: `EC.source`  
  - Sink:   `EC.sink`

