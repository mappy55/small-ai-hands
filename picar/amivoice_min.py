#!/usr/bin/env python3
"""AmiVoice API のストリーミング（WebSocket）の最小例。マイクの音を流し、聞き取った文を表示する。

  sudo apt install -y python3-websocket alsa-utils
  export AMIVOICE_APPKEY=（マイページの「接続情報」にある APPKEY）
  python3 picar/amivoice_min.py                 # Ctrl+C で終了
  MIC=plughw:CARD=Device,DEV=0 python3 picar/amivoice_min.py
"""
import json, os, subprocess, threading, websocket

KEY = os.environ["AMIVOICE_APPKEY"]
MIC = os.environ.get("MIC", "default")

ws = websocket.create_connection("wss://acp-api.amivoice.com/v1/nolog/")      # ログ保存なし。末尾の / まで含める
ws.send(f's LSB16K -a-general authorization={KEY}'
        ' segmenterProperties="postTime=300"'                                # 話し終わりとみなす無音（既定 550 ミリ秒）
        ' profileWords="ピカ ぴか"')                                         # 単語登録（表記 読み|表記 読み）
print("s コマンドの応答:", ws.recv())                                         # 「s」だけなら成功

def reader():
    while True:
        m = ws.recv()
        ev, body = m[:1], m[2:]
        if ev == "U":                                                         # 途中経過（0.1 秒ごと）
            print("  …", json.loads(body)["text"])
        elif ev == "A":                                                       # 確定
            r = json.loads(body)
            if r.get("code") == "o":
                print("（信頼度が低くて棄却）")
            else:
                toks = [(t["written"], t.get("confidence")) for x in r["results"] for t in x["tokens"]]
                print("確定:", r["text"], toks)
        elif ev in "SE":                                                      # 声の始まり・終わり（音声先頭からのミリ秒）
            print(ev, body)

threading.Thread(target=reader, daemon=True).start()
mic = subprocess.Popen(["arecord", "-q", "-D", MIC, "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"], stdout=subprocess.PIPE)
try:
    while True:
        ws.send_binary(b"p" + mic.stdout.read(640))                           # 20 ミリ秒ぶん（16kHz・16 ビット）ずつ送る
except KeyboardInterrupt:
    ws.send("e")                                                              # 終了
finally:
    mic.kill()
