# サービスを置く場所
```
/etc/systemd/system/
```

# 環境ファイルを作成する
環境ファイルを作成（例 /etc/default/halo）
```
# 引用符なしで書きます
OPENAI_API_KEY=sk-xxxx
```


# 起動確認
```
sudo systemctl daemon-reload
sudo systemctl enable halo.service      # 次回起動時に自動実行
sudo systemctl start halo.service       # いま試しに実行
sudo systemctl status halo.service      # 状態確認
```

# ログの確認
```
journalctl -u halo.service -e
```

# serviceの修正時
```
sudo systemctl daemon-reload
sudo systemctl restart halo.service
```

# serviceを止めたいとき
```
sudo systemctl stop halo.service
```
自動起動を止めたい場合
```
sudo systemctl disable halo
```


# ユーザーサービス化するなら以下（今はしていない）
ユーザーセッションの PulseAudio に確実に乗ります。
```
mkdir -p /home/pi/.config/systemd/user
cp /etc/systemd/system/halo.service /home/pi/.config/systemd/user/halo.service
# 中身は User= 行を消し、WantedBy=default.target に変更（他は同等）
systemctl --user daemon-reload
systemctl --user enable --now halo.service
sudo loginctl enable-linger pi  # ブート時にユーザーサービス起動
```

# ユーザーサービスで動かしていて止めたい場合
```
systemctl --user stop halo.service
systemctl --user disable halo.service
```


# pipeWireの設定
PipeWire で WebRTC AEC を使う（推奨）
Raspberry Pi OS BookwormならPipeWireが標準です。以下はシンプルな有効化例です。

必要パッケージ
```
sudo apt update
sudo apt install pipewire-audio pipewire-pulse libspa-0.2-modules
```

※ libspa-0.2-modules に WebRTC AEC 実装が入っています。

フィルタノードを追加（ユーザー設定）
~/.config/pipewire/filter-chain.conf.d/aec.conf を作成して、概念的にはこんな内容を置きます（デバイス名は後述のコマンドで確認して置き換え）:

```
context.modules = [
  { name = libpipewire-module-filter-chain
    args = {
      node.description = "Echo Cancel (WebRTC)"
      media.name = "Echo Cancel (WebRTC)"
      filter.graph = {
        nodes = [
          { type = builtin name = aec plugin = webrtc-aec }
        ]
      }
      # マイク（先に判明していたキャプチャ）
      capture.props  = { node.name = "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono-67" }
      # スピーカー（今回わかった再生デバイス）
      playback.props = { node.name = "alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo" }
      node.props = { node.name = "echo-cancel.webrtc" }
    }
  }
]

```

再起動またはPipeWire再読み込み
```
systemctl --user restart pipewire pipewire-pulse
```

ソース/シンクの確認
```
pw-cli ls Node | grep -i echo
pw-top
```

出てきた「Echo Cancel」ソースを録音デバイスとして選択します（アプリ側でデバイス名を指定）。

補足: アプリがPulseAudio APIを使っている場合でも、pipewire-pulse互換で動くはずです。

設定解説リンク
https://chatgpt.com/share/e/68eb3faf-ebdc-8009-8626-a01d9d539645

