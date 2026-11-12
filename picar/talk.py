#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
talk.py — PiCar-X と声で話す最小のループ（Raspberry Pi 側で動かす）

  耳   : USB マイク → AmiVoice API（同期 HTTP、ログ保存なし）
  頭   : ノート PC の Ollama（既定 gemma3:4b）。返事と動作を JSON で一つ返す
  判定 : コード。信頼度の低い単語を落とす、動作の種類と時間を制限する
  口   : Open JTalk → スピーカー
  手足 : picarx（--move を付けたときだけ実際に動かす）

  export AMIVOICE_APPKEY=...                     # キーはここだけ。リポジトリにもチャットにも書かない
  export OLLAMA_URL=http://192.168.0.21:11434    # ノート PC の Ollama
  python3 picar/talk.py                          # 動かさずに会話だけ（動作は表示のみ）
  sudo -E python3 picar/talk.py --move           # 実際に動かす（タイヤを浮かせて試すこと）

標準ライブラリだけで動く。段ごとの秒数を表示し、logs/talk_log.csv に残す。
"""
import os, sys, io, json, time, wave, array, argparse, subprocess, urllib.request, urllib.error, csv, uuid, shutil

AMIVOICE_URL = "https://acp-api.amivoice.com/v1/nolog/recognize"    # ログ保存なし（家の中の声を送るので）
OLLAMA = os.environ.get("OLLAMA_URL", "http://192.168.0.21:11434")
RATE = 16000
CHUNK = RATE // 10                       # 0.1 秒
ACTIONS = ["forward", "backward", "left", "right", "stop", "none"]

SYSTEM = (
    "あなたは小さな車型ロボット「ピカ」です。持ち主が声で話しかけてきます。音声認識の結果なので、多少の誤字や言い直しが混ざります。"
    "返事は日本語で、話し言葉で、1〜2 文で短く。記号や絵文字は使わない（読み上げるため）。"
    "体を動かす必要があるときだけ action を選ぶ: forward（前へ）, backward（後ろへ）, left（左へ曲がりながら前へ）, right（右へ曲がりながら前へ）, stop（止まる）。"
    "動く必要がなければ none。1 回の返事で動作は一つだけ。"
    'JSON だけで答える: {"say": "<読み上げる文>", "action": "<上のどれか>"}'
)
SCHEMA = {"type": "object",
          "properties": {"say": {"type": "string"}, "action": {"type": "string", "enum": ACTIONS}},
          "required": ["say", "action"]}

# ---------------------------------------------------------------- 耳: 録音（声が始まってから、黙るまで）
def rms(samples):
    return (sum(x * x for x in samples) / max(1, len(samples))) ** 0.5

def mic_device():
    """USB マイクの ALSA デバイス名。カード番号は起動ごとに変わるので、カードの ID で指定する"""
    if os.environ.get("MIC_DEVICE"):
        return os.environ["MIC_DEVICE"]
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5, env=dict(os.environ, LANG="C", LC_ALL="C", LANGUAGE="C")).stdout
        for line in out.splitlines():
            if line.startswith("card") and "USB" in line:
                card_id = line.split(":")[1].split("[")[0].strip()        # "card 3: Device [USB PnP Sound Device], ..."
                return f"plughw:CARD={card_id},DEV=0"
    except Exception:
        pass
    return "default"

def listen(dev, thr=None, max_sec=8.0, end_silence=0.8, wait_sec=30.0, verbose=True):
    """声が始まるまで待ち、黙ったら録音を終える。戻り値: (wav バイト列, 声の秒数, 待った秒数, しきい値) / 声が無ければ None"""
    p = subprocess.Popen(["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def read_chunk():
        b = p.stdout.read(CHUNK * 2)
        if len(b) < CHUNK * 2:
            return None
        a = array.array("h"); a.frombytes(b); return a
    try:
        # 最初の 0.5 秒で周りの音の大きさを測り、しきい値を決める（自動ゲインでノイズが常に乗っているため）
        noise = []
        for _ in range(5):
            c = read_chunk()
            if c is None:
                return None
            noise.append(rms(c))
        floor = sorted(noise)[len(noise) // 2]
        thr = thr or max(floor * 1.8, 400)
        if verbose:
            print(f"   (listening… noise {floor:.0f}, threshold {thr:.0f})", flush=True)
        pre, buf, started, quiet, hot = [], [], False, 0, 0
        t0 = time.time()
        while True:
            c = read_chunk()
            if c is None:
                break
            level = rms(c)
            if not started:
                pre.append(c); pre = pre[-3:]                 # 声の頭を落とさないよう 0.3 秒ぶん持っておく
                hot = hot + 1 if level > thr else 0
                if hot >= 2:
                    started = True; buf = list(pre); t_start = time.time()
                elif time.time() - t0 > wait_sec:
                    return None
            else:
                buf.append(c)
                quiet = quiet + 1 if level < thr else 0
                if quiet >= int(end_silence * 10) or len(buf) >= int(max_sec * 10):
                    break
        if not started:
            return None
        waited = t_start - t0
        samples = array.array("h")
        for c in buf:
            samples.extend(c)
        bio = io.BytesIO()
        with wave.open(bio, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(samples.tobytes())
        return bio.getvalue(), len(samples) / RATE, waited, thr
    finally:
        p.kill(); p.wait()

# ---------------------------------------------------------------- 耳: AmiVoice API（同期 HTTP）
def amivoice(wav_bytes, engine="-a-general"):
    key = os.environ.get("AMIVOICE_APPKEY") or sys.exit("AMIVOICE_APPKEY が設定されていません")
    bnd = "----smallhands" + uuid.uuid4().hex
    parts = []
    for name, val in (("u", key), ("d", engine)):                      # 音声 a は必ず最後（後ろのパラメータは無視される）
        parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="a"; filename="a.wav"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                 + wav_bytes + b"\r\n")
    parts.append(f"--{bnd}--\r\n".encode())
    req = urllib.request.Request(AMIVOICE_URL, data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"AmiVoice API {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    wall = time.time() - t0
    if r.get("code"):
        raise RuntimeError(f"AmiVoice: {r.get('code')} {r.get('message')}")
    tokens = [(t.get("written", ""), float(t.get("confidence", 0))) for res in r.get("results", []) for t in res.get("tokens", [])]
    return r.get("text", ""), tokens, wall

# ---------------------------------------------------------------- 判定: 信頼度の低い単語を落とす
def clean_tokens(tokens, min_conf):
    """信頼度が min_conf 未満の単語を落とす。戻り値: (残した文, 落とした単語の一覧)"""
    kept = [w for w, c in tokens if c >= min_conf]
    dropped = [(w, c) for w, c in tokens if c < min_conf]
    return "".join(kept), dropped

# ---------------------------------------------------------------- 頭: ノート PC の Ollama
def ask_llm(model, history, text):
    msgs = [{"role": "system", "content": SYSTEM}] + history + [{"role": "user", "content": text}]
    body = {"model": model, "stream": False, "format": SCHEMA, "options": {"temperature": 0.3, "num_ctx": 4096}, "messages": msgs}
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        r = json.loads(resp.read())
    wall = time.time() - t0
    content = r.get("message", {}).get("content", "")
    try:
        ans = json.loads(content)
    except json.JSONDecodeError:
        ans = {"say": "すみません、うまく考えられませんでした。", "action": "none"}
    if ans.get("action") not in ACTIONS:
        ans["action"] = "none"
    return ans, wall, content

# ---------------------------------------------------------------- 口: Open JTalk
DIC = "/var/lib/mecab/dic/open-jtalk/naist-jdic"
VOICE = "/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice"

def say(text, wav="/tmp/say.wav"):
    """読み上げる。戻り値: (合成の秒数, 再生の秒数)"""
    if not text.strip():
        return 0.0, 0.0
    t0 = time.time()
    subprocess.run(["open_jtalk", "-x", DIC, "-m", VOICE, "-ow", wav], input=text.encode(), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    t1 = time.time()
    subprocess.run(["aplay", "-q", wav], check=False, timeout=60)
    return t1 - t0, time.time() - t1

# ---------------------------------------------------------------- 手足: picarx
class Car:
    """--move のときだけ本物を動かす。動作は種類・速さ・時間をコードで制限する（LLM には決めさせない）"""
    def __init__(self, move, speed=30, seconds=1.0):
        self.px = None; self.speed = speed; self.seconds = seconds
        if move:
            from picarx import Picarx
            self.px = Picarx()
    def act(self, action):
        if action in ("none", None):
            return 0.0
        t0 = time.time()
        if self.px is None:
            print(f"   (dry run: would {action} for {self.seconds:.1f}s at speed {self.speed})")
            return 0.0
        px = self.px
        try:
            if action == "stop":
                px.stop()
            else:
                px.set_dir_servo_angle({"left": -25, "right": 25}.get(action, 0))
                (px.backward if action == "backward" else px.forward)(self.speed)
                time.sleep(self.seconds)
        finally:
            px.stop(); px.set_dir_servo_angle(0)
        return time.time() - t0
    def close(self):
        if self.px is not None:
            self.px.stop()

# ---------------------------------------------------------------- 本体
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("TALK_MODEL", "gemma3:4b"))
    ap.add_argument("--move", action="store_true", help="実際にモーターを動かす（付けなければ表示だけ）")
    ap.add_argument("--speed", type=int, default=30)
    ap.add_argument("--seconds", type=float, default=1.0, help="1 回の動作の長さ（秒）")
    ap.add_argument("--min-conf", type=float, default=0.6, help="この信頼度未満の単語は落とす")
    ap.add_argument("--thr", type=float, default=0, help="声と見なす音量のしきい値（0 で自動）")
    ap.add_argument("--wav", help="マイクの代わりにこの wav を 1 回だけ処理する（確認用）")
    ap.add_argument("--log", default="logs/talk_log.csv")
    a = ap.parse_args()

    for tool in ("arecord", "aplay", "open_jtalk"):
        if not shutil.which(tool):
            sys.exit(f"{tool} が見つかりません")
    dev = mic_device()
    car = Car(a.move, a.speed, a.seconds)
    os.makedirs(os.path.dirname(a.log) or ".", exist_ok=True)
    new = not os.path.exists(a.log)
    logf = open(a.log, "a", newline="", encoding="utf-8"); log = csv.writer(logf)
    if new:
        log.writerow(["time", "model", "speech_s", "asr_s", "llm_s", "tts_synth_s", "tts_play_s", "act_s", "heard", "dropped", "say", "action"])
    print(f"== mic {dev}  model {a.model} @ {OLLAMA}  move={'ON' if a.move else 'off (dry run)'}  min_conf={a.min_conf}")

    print("   (warming up the model…)", flush=True)
    try:
        _, t_warm, _ = ask_llm(a.model, [], "こんにちは")
        print(f"   (model ready in {t_warm:.1f}s)")
    except Exception as e:
        sys.exit(f"Ollama に届きません: {e}")
    say("準備できました。")

    history = []
    try:
        while True:
            if a.wav:
                wav_bytes = open(a.wav, "rb").read(); speech = 0.0
            else:
                got = listen(dev, thr=a.thr or None)
                if got is None:
                    continue
                wav_bytes, speech, _, _ = got
            try:
                text, tokens, t_asr = amivoice(wav_bytes)
            except Exception as e:
                print(f"   !! {e}"); time.sleep(1); continue
            heard, dropped = clean_tokens(tokens, a.min_conf)
            print(f"[heard] {text!r}  asr {t_asr:.2f}s (speech {speech:.1f}s)"
                  + (f"  dropped {[(w, round(c, 2)) for w, c in dropped]} -> {heard!r}" if dropped else ""))
            if not heard.strip():
                if tokens:                                   # 何か聞こえたが全部あやしい → 動かずに聞き返す
                    say("すみません、もう一度お願いします。")
                if a.wav:
                    break
                continue
            if any(k in heard for k in ("おしまい", "終わり", "さようなら")):
                say("はい、おしまいにします。"); break
            ans, t_llm, _ = ask_llm(a.model, history[-6:], heard)
            print(f"[think] llm {t_llm:.2f}s -> say {ans['say']!r}  action {ans['action']}")
            t_syn, t_play = say(ans["say"])
            t_act = car.act(ans["action"])
            print(f"[done]  tts synth {t_syn:.2f}s play {t_play:.2f}s | act {t_act:.2f}s | 声の終わりから返事の出だしまで {t_asr + t_llm + t_syn:.2f}s")
            log.writerow([time.time(), a.model, f"{speech:.2f}", f"{t_asr:.3f}", f"{t_llm:.3f}", f"{t_syn:.3f}", f"{t_play:.3f}", f"{t_act:.3f}",
                          heard, json.dumps(dropped, ensure_ascii=False), ans["say"], ans["action"]]); logf.flush()
            history += [{"role": "user", "content": heard}, {"role": "assistant", "content": json.dumps(ans, ensure_ascii=False)}]
            if a.wav:
                break
    except KeyboardInterrupt:
        print("\n== bye")
    finally:
        car.close(); logf.close()

if __name__ == "__main__":
    main()
