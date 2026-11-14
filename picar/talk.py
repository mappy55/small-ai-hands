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
import os, sys, io, json, time, wave, array, argparse, subprocess, urllib.request, urllib.error, csv, uuid, shutil, tempfile, threading, queue

AMIVOICE_URL = "https://acp-api.amivoice.com/v1/nolog/recognize"    # ログ保存なし（家の中の声を送るので）
AMIVOICE_WS = "wss://acp-api.amivoice.com/v1/nolog/"                # ストリーミング。末尾の / を落とすと「ログ保存あり」になるので注意
OLLAMA = os.environ.get("OLLAMA_URL", "http://192.168.0.21:11434")
RATE = 16000
CHUNK = RATE // 10                       # 0.1 秒
ACTIONS = ["forward", "backward", "left", "right", "stop", "none"]
GAIN_TARGET = 0.7                        # 送る前にピークをここまで増幅する（0 で増幅しない）
TTS_RATE = 1.0                           # 読み上げの速さ（Open JTalk の -r。1.0 が標準）
TTS_PEAK = 0.9                           # 読み上げの声のピークをここまで増幅する（0 で増幅しない）
CHIME = 0.0                              # 読み上げの前の合図音（ピッ）の長さ（秒）。0 で鳴らさない
CHIME_LEVEL = 5000                       # 合図音の大きさ（16 ビットで ±5000 ＝ 約 -16dB）
LEAD_SILENCE = 0.25                      # 合図音と文の間（秒）
LEAD_LEVEL = 100                         # その間をつなぐ雑音の大きさ（±100 ＝ 約 -50dB。0 だと完全な無音）
LAST = {"peak": 0.0, "gain": 1.0}        # 直近の録音のピークと増幅率（表示用）
BILLED = {"s": 0.0}                      # AmiVoice が声として数えた秒数の累計（概算）

SYSTEM = (
    "あなたは小さな車型ロボット「ピカ」です。持ち主が声で話しかけてきます。音声認識の結果なので、多少の誤字や言い直しが混ざります。"
    "返事は日本語で、話し言葉で、1〜2 文で短く。記号や絵文字は使わない（読み上げるため）。"
    "体を動かす必要があるときだけ action を選ぶ: forward（前へ）, backward（後ろへ）, left（左へ曲がりながら前へ）, right（右へ曲がりながら前へ）, stop（止まる）。"
    "動く必要がなければ none。1 回の返事で動作は一種類だけ。"
    "times はその動作を繰り返す回数。回数を言われなければ 1。「2 回」「3 回」と言われたらその数。動かないときも 1。"
    "動けと言われたら、そのたびに必ず action を選ぶ。同じ命令を続けて言われても、前に動いたからといって none にしない（体はあなたの返事の action でしか動かない）。"
    "あいさつ、質問、雑談のように、動けと言われていないときは必ず none。迷ったら none。"
    "先に action を決めてから、それに合う say を書く。"
    "動くときの say は一言だけ（体は 1 秒で動き終わるので、長い返事は動きに間に合わない）。"
    'JSON だけで答える: {"action": "<上のどれか>", "times": <回数>, "say": "<読み上げる文>"}\n'
    '例: 「前に進んで」→ {"action": "forward", "times": 1, "say": "はい、前へ。"}\n'
    '例: 「左に2回曲がって」→ {"action": "left", "times": 2, "say": "はい、左へ2回。"}\n'
    '例: 「こんにちは」→ {"action": "none", "times": 1, "say": "こんにちは。"}\n'
    '例: 「名前は何ていうの」→ {"action": "none", "times": 1, "say": "ピカです。"}\n'
    '例: 「止まって」→ {"action": "stop", "times": 1, "say": "はい、止まる。"}'
)
# action を先に書かせる（返事の文に引っ張られて動作を選ぶのを避ける）
SCHEMA = {"type": "object",
          "properties": {"action": {"type": "string", "enum": ACTIONS}, "times": {"type": "integer"}, "say": {"type": "string"}},
          "required": ["action", "times", "say"]}
MAX_TIMES = 3                            # 繰り返しの上限。頭が何回と答えても、コードでここまでに縛る

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

FRAME = RATE // 50                       # 20 ミリ秒

def _open_mic(dev):
    return subprocess.Popen(["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

def _read_frame(p):
    b = p.stdout.read(FRAME * 2)
    if len(b) < FRAME * 2:
        return None
    a = array.array("h"); a.frombytes(b); return a

def _gate(p, fixed, wait_sec, verbose, factor=3.0, floor_min=200, need=8):
    """門番: 音がするまで待つ。戻り値: (取り置きのフレーム, 床, しきい値) / 時間切れやマイク切断は None

    このマイクの雑音は「常に」でなく「突発的」: 静かなときの床は rms 50〜100 だが、40 ミリ秒ほどのプチッ（rms 500〜1500）が入る。
    だから 1〜2 フレーム跳ねただけでは反応せず、「直近 240 ミリ秒のうち 160 ミリ秒以上がしきい値超え」で開く。
    床は直近 3 秒の下位 20% を追い続ける（読み上げの直後は残響で跳ねるので、決め打ちにしない）。"""
    recent, pre, win = [], [], []
    t0 = time.time(); shown = False
    while True:
        c = _read_frame(p)
        if c is None:
            return None
        level = rms(c)
        recent.append(level); recent = recent[-150:]
        pre.append(c); pre = pre[-35:]                    # 声の頭を落とさないよう 0.7 秒ぶん持っておく
        if len(recent) < 25:
            continue
        floor = sorted(recent)[len(recent) // 5]
        thr = fixed or max(floor * factor, floor_min)
        if verbose and not shown:
            print(f"   (listening… floor {floor:.0f}, threshold {thr:.0f})", flush=True); shown = True
        win.append(level > thr); win = win[-12:]
        if sum(win) >= need:
            return pre, floor, thr
        if time.time() - t0 > wait_sec:
            return None

def listen(dev, thr=None, max_sec=8.0, end_silence=0.8, wait_sec=30.0, verbose=True):
    """自作の切り出し（--asr http 用）: 門番が開いてから、黙るまでを録音する。
    戻り値: (wav バイト列, 録音の秒数, 0, しきい値) / 声が無ければ None"""
    p = _open_mic(dev)
    try:
        g = _gate(p, thr, wait_sec, verbose)
        if g is None:
            return None
        pre, floor, thr_now = g
        buf = list(pre); tail = []; ended = "max"
        end_thr = thr or max(floor * 2.0, 150)            # 終わりの判定は少し低めに（語尾は小さい）
        n_tail = int(end_silence * 50)
        LAST.update({"floor": floor, "thr": thr_now})
        while True:
            c = _read_frame(p)
            if c is None:
                break
            buf.append(c)
            tail.append(rms(c) > end_thr); tail = tail[-n_tail:]
            # 黙った = 直近 0.8 秒のうち、しきい値超えが 1 割以下（プチッの 2〜3 フレームは許す）
            if len(tail) >= n_tail and sum(tail) <= len(tail) // 10:
                ended = "quiet"; break
            if len(buf) >= int(max_sec * 50):
                break
        LAST["endwait"] = end_silence if ended == "quiet" else 0.0   # 話し終えてから録音を切り上げるまでに待った時間
        samples = array.array("h")
        for c in buf:
            samples.extend(c)
        # この USB マイクは最大ゲインでもピークが 3〜9% しか出ない。小さいまま送ると AmiVoice が信頼度不足で棄却することがあるので、
        # ピークが 70% になるよう増幅してから送る（上限 16 倍。ノイズも一緒に持ち上がる）
        peak = max(1, max(abs(x) for x in samples))
        gain = min(16.0, GAIN_TARGET * 32767 / peak) if GAIN_TARGET else 1.0
        if gain > 1.05:
            samples = array.array("h", (max(-32768, min(32767, int(x * gain))) for x in samples))
        LAST.update({"peak": peak / 32768, "gain": gain})
        bio = io.BytesIO()
        with wave.open(bio, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(samples.tobytes())
        return bio.getvalue(), len(samples) / RATE, 0.0, thr_now
    finally:
        p.kill(); p.wait()

def listen_stream(dev, thr=None, gain=5.0, engine="-a-general", wait_sec=30.0, no_speech=2.5, max_utt=10.0, verbose=True, gate=True):
    """ストリーミング（--asr ws）: 門番が開いたら AmiVoice に WebSocket でつなぎ、マイクの音を流す。
    声の始まり（S）と終わり（E）は AmiVoice のサーバーが決める。自作の「黙ったか」の判定は使わない。

    戻り値: None（門番が開かなかった） / dict
      {"none": True, ...}        AmiVoice が声を見つけなかった（雑音で門番が開いただけ。課金されない）
      {"result": {...}, ...}     認識結果（A イベントの JSON。同期 HTTP の応答と同じ形）
    """
    try:
        import websocket                                   # apt: python3-websocket（websocket-client）
    except ImportError:
        sys.exit("WebSocket の部品がありません: sudo apt install -y python3-websocket   （または --asr http で従来の方式）")
    key = os.environ.get("AMIVOICE_APPKEY") or sys.exit("AMIVOICE_APPKEY が設定されていません")
    p = _open_mic(dev); ws = None
    try:
        # ストリーミングでは門番をゆるくする: 声かどうかは AmiVoice が決め、声でなければ課金もされない。
        # 厳しい門番（床の 3 倍・160ms）だと「バックして」のような短く小さい発話で開かなかった
        # gate=False（--open-mic）: 門番を使わず、聞き始めたらすぐつないで流し続ける。走った後は雑音の床が上がり、
        # 門番が雑音で開いて 3 秒つないでは切る、を繰り返し、その切れ目に声が落ちて頭や全体を聞き逃した（実測）。
        # 課金は AmiVoice が声と判定した区間だけなので、つなぎっぱなしでも無音や雑音のぶんは数えられない
        if gate:
            g = _gate(p, thr, wait_sec, verbose, factor=2.2, floor_min=150, need=5)
            if g is None:
                return None
            pre, floor, thr_now = g
        else:
            pre, floor, thr_now = [], 0.0, 0.0
            no_speech = max(no_speech, 25.0)
            if verbose:
                print("   (listening… open mic)")
        t_gate = time.time()
        t_audio0 = t_gate - len(pre) * 0.02                # 送る音声の先頭が録られた時刻（おおよそ）
        ws = websocket.create_connection(AMIVOICE_WS, timeout=10)
        ws.send(f"s LSB16K {engine} authorization={key}")
        r = ws.recv()
        if (r or "").strip() != "s":
            raise RuntimeError(f"AmiVoice: {r}")
        t_conn = time.time() - t_gate
        q = queue.Queue()
        def reader():
            try:
                while True:
                    m = ws.recv()
                    q.put((time.time(), m if isinstance(m, str) else m.decode("utf-8", "replace")))
            except Exception:
                q.put((time.time(), None))
        threading.Thread(target=reader, daemon=True).start()
        sent = array.array("h")
        def send(frames):
            for c in frames:
                if gain and gain != 1.0:
                    c = array.array("h", (max(-32768, min(32767, int(x * gain))) for x in c))
                sent.extend(c)
                ws.send_binary(b"p" + c.tobytes())
        send(pre)
        t_S = t_E = t_A = t_e = None; result = None; closed = False
        t_stream = time.time()
        while result is None and not closed:
            try:
                while True:
                    tm, m = q.get_nowait()
                    if m is None:
                        closed = True; break
                    ev, body = m[:1], m[2:]
                    if ev == "S" and t_S is None:
                        t_S = tm
                    elif ev == "E" and t_E is None:
                        t_E = tm
                    elif ev == "A":
                        result = json.loads(body); t_A = tm
                    elif ev == "e":                        # セッション終了の応答。A が無ければ声は検出されなかった
                        closed = True
                        if body.strip():
                            raise RuntimeError(f"AmiVoice: e {body}")
                    elif ev == "p" and body.strip():
                        raise RuntimeError(f"AmiVoice: p {body}")
            except queue.Empty:
                pass
            if result is not None or closed:
                break
            now = time.time()
            if t_e is None:
                # 終わらせる条件: サーバーが声の終わりを知らせた / 声が始まらない / 長すぎる
                if t_E is not None or (t_S is None and now - t_stream > no_speech) or (now - (t_S or t_stream) > max(max_utt, 0 if t_S else no_speech + 1)):
                    ws.send("e"); t_e = now
                else:
                    c = _read_frame(p)
                    if c is None:
                        break
                    send([c])
            else:
                if now - t_e > 6.0:                        # 結果が来ない
                    break
                time.sleep(0.01)
        bio = io.BytesIO()
        with wave.open(bio, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(sent.tobytes())
        LAST.update({"floor": floor, "thr": thr_now, "peak": (max(abs(x) for x in sent) / 32768 / (gain or 1.0)) if sent else 0.0, "gain": gain or 1.0})
        info = {"wav": bio.getvalue(), "sent_s": len(sent) / RATE, "t_conn": t_conn, "t_audio0": t_audio0,
                "t_S": t_S, "t_E": t_E, "t_A": t_A}
        if result is None:
            info["none"] = True
        else:
            info["result"] = result
        return info
    finally:
        p.kill(); p.wait()
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

def parse_result(r):
    """AmiVoice の認識結果（同期 HTTP の応答 / WebSocket の A イベント）を読む。
    戻り値: (文 or None=棄却, [(表記, 信頼度)], 読み, 声として数えられた秒数)"""
    if r.get("code") == "o":                 # 信頼度がしきい値未満で棄却（声が小さい・雑音・途中で切れた）
        return None, [], "", 0.0
    if r.get("code"):
        raise RuntimeError(f"AmiVoice: {r.get('code')} {r.get('message')}")
    results = r.get("results", [])
    toks = [t for res in results for t in res.get("tokens", [])]
    tokens = [(t.get("written", ""), float(t.get("confidence", 0))) for t in toks]
    spoken = "".join(t.get("spoken", "") for t in toks)
    billed = sum(max(0, res.get("endtime", 0) - res.get("starttime", 0)) for res in results) / 1000.0
    return r.get("text", ""), tokens, spoken, billed

def meter(dev, seconds=15):
    """音量計: 0.5 秒ごとの音量を表示する。黙っているときと話しているときの差を見るため"""
    p = subprocess.Popen(["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for i in range(seconds * 2):
            b = p.stdout.read(RATE)                         # 0.5 秒
            if len(b) < RATE:
                break
            a = array.array("h"); a.frombytes(b)
            lv = rms(a); pk = max(abs(x) for x in a)
            print(f"   {i*0.5:5.1f}s  rms {lv:6.0f}  peak {pk:6d}  {'#' * min(60, int(lv / 20))}", flush=True)
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
    text, tokens, spoken, billed = parse_result(r)
    LAST["billed"] = billed
    return text, tokens, wall, spoken

# ---------------------------------------------------------------- 判定: 信頼度の低い単語を落とす
def clean_tokens(tokens, min_conf):
    """信頼度が min_conf 未満の単語を、文の末尾に続いているぶんだけ落とす。戻り値: (残した文, 落とした単語の一覧)

    落とすのは末尾だけ。実測では、途中で切れた発話のかけらは末尾に付き（「前に進んで前に進んで」＋「ない」0.49）、
    落とすと意味が正しくなる。一方、文の途中の低い単語は合っていることがあり（「左に曲がって」の「曲がっ」0.51）、
    落とすと「左にて」になって命令が壊れた。途中の単語は残し、表示だけする。"""
    n = len(tokens)
    while n > 0 and tokens[n - 1][1] < min_conf:
        n -= 1
    kept = "".join(w for w, _ in tokens[:n])
    dropped = [(w, c) for w, c in tokens[n:]]
    return kept, dropped

def low_tokens(tokens, min_conf):
    """残したが信頼度の低い単語（表示とログ用）"""
    return [(w, round(c, 2)) for w, c in tokens if c < min_conf]

# ---------------------------------------------------------------- 頭: ノート PC の Ollama
def ask_llm(model, history, text, timeout=120):
    msgs = [{"role": "system", "content": SYSTEM}] + history + [{"role": "user", "content": text}]
    body = {"model": model, "stream": False, "format": SCHEMA, "options": {"temperature": 0, "num_ctx": 4096}, "messages": msgs}
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        r = json.loads(resp.read())
    wall = time.time() - t0
    content = r.get("message", {}).get("content", "")
    try:
        ans = json.loads(content)
    except json.JSONDecodeError:
        ans = {"say": "すみません、うまく考えられませんでした。", "action": "none"}
    if ans.get("action") not in ACTIONS:
        ans["action"] = "none"
    try:
        asked = int(ans.get("times", 1))
    except (TypeError, ValueError):
        asked = 1
    ans["times_asked"] = asked                             # 頭が答えた回数（表示・ログ用）
    ans["times"] = 1 if ans["action"] in ("none", "stop") else max(1, min(MAX_TIMES, asked))
    return ans, wall, content

# ---------------------------------------------------------------- 口: Open JTalk
DIC = "/var/lib/mecab/dic/open-jtalk/naist-jdic"
VOICE = "/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice"

def enable_speaker():
    """Robot HAT のアンプを入れる。アンプの入り切りは GPIO 20 で、I2S の DAC を有効にするとこのピンが
    I2S の入力（a2）に取られて lo のまま＝アンプが切れた状態になる。出力・high にすると鳴る。"""
    try:
        subprocess.run(["pinctrl", "set", "20", "op", "dh"], check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"   (スピーカーのアンプを入れられませんでした: {e} — 音が出なければ `pinctrl set 20 op dh` を手で実行)")
        return False

def say(text, wav=None):
    """読み上げる。戻り値: (合成の秒数, 再生の秒数)"""
    if not text.strip():
        return 0.0, 0.0
    # 一時ファイルは利用者ごとに分ける（sudo で一度動かすと root の /tmp/say.wav が残り、通常ユーザーで上書きできなくなる）
    wav = wav or os.path.join(tempfile.gettempdir(), f"smallhands_say_{os.getuid()}.wav")
    t0 = time.time()
    subprocess.run(["open_jtalk", "-x", DIC, "-m", VOICE, "-r", str(TTS_RATE), "-ow", wav], input=text.encode(), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    # 文の頭が切れる件は、アンプが切れていた（GPIO 20 が lo）のと音量が 40% だったのが原因で、enable_speaker() と
    # amixer で直った。無音・小さな雑音・合図音を先頭に足す対策はどれも的外れだった。助走（--lead）と合図音（--chime）は
    # 残してあるが、合図音は既定で鳴らさない。
    if LEAD_SILENCE > 0 or CHIME > 0:
        import random, math
        with wave.open(wav, "rb") as w:
            params = w.getparams(); frames = w.readframes(w.getnframes())
        if params.sampwidth == 2:
            sr, ch = params.framerate, params.nchannels
            if TTS_PEAK > 0:                                  # Open JTalk の声は小さめなので、ピークをそろえて大きくする
                body = array.array("h"); body.frombytes(frames)
                pk = max(1, max(abs(x) for x in body)) if body else 1
                g = TTS_PEAK * 32767 / pk
                if g > 1.05:
                    frames = array.array("h", (max(-32768, min(32767, int(x * g))) for x in body)).tobytes()
            lead = array.array("h")
            n_chime = int(sr * CHIME)
            for k in range(n_chime):
                env = min(1.0, k / (sr * 0.01), (n_chime - k) / (sr * 0.03))        # 立ち上がり 10ms、減衰 30ms
                v = int(CHIME_LEVEL * env * math.sin(2 * math.pi * 880 * k / sr))
                lead.extend([v] * ch)
            lead.extend(random.randint(-LEAD_LEVEL, LEAD_LEVEL) if LEAD_LEVEL > 0 else 0 for _ in range(int(sr * LEAD_SILENCE) * ch))
            with wave.open(wav, "wb") as w:
                w.setparams(params); w.writeframes(lead.tobytes() + frames)
    t1 = time.time()
    subprocess.run(["aplay", "-q", wav], check=False, timeout=60)
    t2 = time.time()
    time.sleep(0.4)                          # スピーカーの残響が消えるのを待ってから聞き始める
    return t1 - t0, t2 - t1

# ---------------------------------------------------------------- 手足: picarx
class Car:
    """--move のときだけ本物を動かす。動作は種類・速さ・時間をコードで制限する（LLM には決めさせない）"""
    def __init__(self, move, speed=30, seconds=1.0):
        self.px = None; self.speed = speed; self.seconds = seconds
        if move:
            from picarx import Picarx
            self.px = Picarx()
    def act(self, action, times=1):
        if action in ("none", None):
            return 0.0
        t0 = time.time()
        times = max(1, min(MAX_TIMES, int(times)))         # ここでも縛る（呼び出し側を信用しない）
        if self.px is None:
            print(f"   (dry run: would {action} x{times} for {self.seconds:.1f}s each at speed {self.speed})")
            return 0.0
        px = self.px
        try:
            if action == "stop":
                px.stop()
            else:
                for k in range(times):
                    if k:                                  # 回と回のあいだは一度止めて、区切りが見えるようにする
                        px.stop(); time.sleep(0.3)
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
    ap.add_argument("--asr", choices=["ws", "http"], default="ws",
                    help="ws: AmiVoice のストリーミング（声の始まりと終わりをサーバーが決める）。http: 自作の切り出し＋同期 HTTP")
    ap.add_argument("--stream-gain", type=float, default=5.0, help="ws で送る音の増幅率（このマイクは小さいので既定 5 倍）")
    ap.add_argument("--wav", help="マイクの代わりにこの wav を 1 回だけ処理する（確認用。同期 HTTP で送る）")
    ap.add_argument("--meter", action="store_true", help="音量計だけ動かす（15 秒。読み上げ → 黙る → 話す、で床と声の差を見る）")
    ap.add_argument("--say-first", action="store_true", help="--meter の前に読み上げを 1 回鳴らす（スピーカーを使ったあとの床を見る）")
    ap.add_argument("--gain", type=float, default=0.7, help="送る前にピークをこの割合まで増幅する（0 で増幅しない）")
    ap.add_argument("--tts-rate", type=float, default=1.0, help="読み上げの速さ（1.0 が標準、1.3 で速め）")
    ap.add_argument("--tts-peak", type=float, default=0.9, help="読み上げの声のピークをこの割合まで増幅する（0 で増幅しない）")
    ap.add_argument("--chime", type=float, default=0.0, help="読み上げの前に鳴らす合図音の秒数（既定は鳴らさない。頭切れの原因はアンプが切れていたことで、合図音は不要だった）")
    ap.add_argument("--max-times", type=int, default=3, help="繰り返しの上限（頭が何回と答えても、ここまで）")
    ap.add_argument("--gate", action="store_true", help="以前の方式: 自作の門番が開いたときだけ AmiVoice につなぐ（既定は、聞いている間ずっとつないでおく）")
    ap.add_argument("--open-mic", action="store_true", help="（既定になったので指定不要。互換のために残してある）")
    ap.add_argument("--no-lead", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--act-after-say", action="store_true", help="以前の順番（読み上げが終わってから動く）に戻す")
    ap.add_argument("--lead", type=float, default=0.0, help="合図音と文の間の秒数")
    ap.add_argument("--lead-level", type=int, default=100, help="その間をつなぐ雑音の大きさ（0 で完全な無音）")
    ap.add_argument("--history", type=int, default=0, help="モデルに見せる過去のやりとりの数（往復）。0 だと毎回その一言だけで判断する")
    ap.add_argument("--keep", default="logs/talk_wavs", help="送った音声を残すディレクトリ（'' で残さない）。棄却された声を後で調べるため")
    ap.add_argument("--log", default="logs/talk_log2.csv")
    a = ap.parse_args()

    for tool in ("arecord", "aplay", "open_jtalk"):
        if not shutil.which(tool):
            sys.exit(f"{tool} が見つかりません")
    global MAX_TIMES, GAIN_TARGET, LEAD_SILENCE, LEAD_LEVEL, CHIME, TTS_PEAK, TTS_RATE
    TTS_RATE = a.tts_rate
    MAX_TIMES = max(1, a.max_times)
    TTS_PEAK = a.tts_peak
    GAIN_TARGET = a.gain
    LEAD_SILENCE = a.lead
    LEAD_LEVEL = a.lead_level
    CHIME = a.chime
    dev = mic_device()
    car = Car(a.move, a.speed, a.seconds)
    enable_speaker()                                          # Picarx の初期化のあとに入れる（初期化でピンが変わっても上書きする）
    if a.keep:
        os.makedirs(a.keep, exist_ok=True)
    n_utt = 0
    if a.meter:
        if a.say_first:
            print("== 音量計: 最初に読み上げを 1 回鳴らします。そのあと 5 秒黙って、それから話してください")
            say("準備できました。")
        else:
            print("== 音量計: 5 秒黙って、それから話してください（読み上げは鳴らしません。--say-first で鳴らしてから測る）")
        meter(dev); return
    os.makedirs(os.path.dirname(a.log) or ".", exist_ok=True)
    new = not os.path.exists(a.log)
    logf = open(a.log, "a", newline="", encoding="utf-8"); log = csv.writer(logf)
    if new:
        log.writerow(["time", "model", "asr", "speech_s", "heard_in_s", "llm_s", "tts_synth_s", "tts_play_s", "act_s", "heard", "dropped", "say", "action"])
    print(f"== mic {dev}  asr {a.asr}  model {a.model} @ {OLLAMA}  move={'ON' if a.move else 'off (dry run)'}  min_conf={a.min_conf}")

    # まず届くかを軽く確かめ、それからモデルを温める（初回の読み込みは 1〜数分かかることがあるので長めに待つ）
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=5) as resp:
            names = [m["name"] for m in json.loads(resp.read()).get("models", [])]
    except Exception as e:
        sys.exit(f"Ollama に届きません（{OLLAMA}）: {e}\n  ノート PC が起きているか、Ollama が動いているか、OLLAMA_URL が合っているかを確認")
    if a.model not in names:
        sys.exit(f"モデル {a.model} が Ollama にありません。あるもの: {', '.join(names)}")
    print("   (warming up the model — the first load can take a minute or more…)", flush=True)
    try:
        _, t_warm, _ = ask_llm(a.model, [], "こんにちは", timeout=600)
        print(f"   (model ready in {t_warm:.1f}s)")
    except Exception as e:
        sys.exit(f"モデルの読み込みが終わりません: {e}\n  ノート PC で `ollama ps` を見て、{a.model} が読み込まれているか、GPU に載っているかを確認")
    say("準備できました。")

    history = []
    try:
        while True:
            detail = ""
            if a.wav or a.asr == "http":
                if a.wav:
                    wav_bytes = open(a.wav, "rb").read(); speech = 0.0; LAST["endwait"] = 0.0
                else:
                    got = listen(dev, thr=a.thr or None)
                    if got is None:
                        continue
                    wav_bytes, speech, _, _ = got
                n_utt += 1
                if a.keep and not a.wav:
                    with open(os.path.join(a.keep, f"{time.strftime('%H%M%S')}_{n_utt:03d}.wav"), "wb") as f:
                        f.write(wav_bytes)
                try:
                    text, tokens, t_http, spoken = amivoice(wav_bytes)
                except Exception as e:
                    print(f"   !! {e}"); time.sleep(1); continue
                billed = LAST.get("billed", 0.0)
                # 話し終えてから文字になるまで = 自作の「黙った」判定の待ち（0.8 秒）＋ 送ってから返るまで
                t_asr = LAST.get("endwait", 0.0) + t_http
                detail = f"endpoint wait {LAST.get('endwait', 0.0):.1f}s + http {t_http:.2f}s, recorded {speech:.1f}s"
            else:
                try:
                    got = listen_stream(dev, thr=a.thr or None, gain=a.stream_gain, gate=a.gate)
                except Exception as e:
                    print(f"   !! {e}"); time.sleep(1); continue
                if got is None:
                    continue
                n_utt += 1
                if a.keep:
                    with open(os.path.join(a.keep, f"{time.strftime('%H%M%S')}_{n_utt:03d}.wav"), "wb") as f:
                        f.write(got["wav"])
                if got.get("none"):
                    # 門番は開いたが、AmiVoice は声を見つけなかった（雑音）。発話区間が無いので課金もされない
                    print(f"   (no speech found by AmiVoice: sent {got['sent_s']:.1f}s, conn {got['t_conn']:.2f}s)")
                    continue
                try:
                    text, tokens, spoken, billed = parse_result(got["result"])
                except Exception as e:
                    print(f"   !! {e}"); time.sleep(1); continue
                res = got["result"].get("results", [])
                speech = billed
                # 話し終えてから文字になるまで: 結果が届いた時刻 −（送った音声の先頭の時刻 ＋ 声の終わりの位置）
                end_ms = res[-1].get("endtime", 0) if res else 0
                t_asr = max(0.0, got["t_A"] - (got["t_audio0"] + end_ms / 1000.0)) if got.get("t_A") else 0.0
                e2a = (got["t_A"] - got["t_E"]) if got.get("t_A") and got.get("t_E") else None
                detail = (f"ws conn {got['t_conn']:.2f}s, sent {got['sent_s']:.1f}s"
                          + (f", E→A {e2a:.2f}s" if e2a is not None else ", no E event"))
            BILLED["s"] += billed
            lvl = f"speech {speech:.1f}s, peak {LAST['peak']*100:.0f}% x{LAST['gain']:.1f}, floor {LAST.get('floor', 0):.0f}; {detail}"
            if text is None:
                print(f"[heard] (rejected by AmiVoice: confidence too low)  heard in {t_asr:.2f}s ({lvl})")
                log.writerow([time.time(), a.model, a.asr, f"{speech:.2f}", f"{t_asr:.3f}", 0, 0, 0, 0, "(rejected)", "", "", ""]); logf.flush()
                if a.wav:
                    break
                continue
            heard, dropped = clean_tokens(tokens, a.min_conf)
            unsure = [x for x in low_tokens(tokens, a.min_conf) if x[0] in heard and (x[0], x[1]) not in [(w, round(c, 2)) for w, c in dropped]]
            print(f"[heard] {text!r}  heard in {t_asr:.2f}s ({lvl})"
                  + (f"  dropped tail {[(w, round(c, 2)) for w, c in dropped]} -> {heard!r}" if dropped else "")
                  + (f"  low-confidence kept {unsure}" if unsure else ""))
            if not heard.strip():
                if tokens:                                   # 何か聞こえたが全部あやしい → 動かずに聞き返す
                    say("すみません、もう一度お願いします。")
                if a.wav:
                    break
                continue
            # 終了の合図は表記でなく読みで見る（「おしまい」が「姉妹」と書かれて返ることがある）
            if any(k in heard for k in ("おしまい", "終わり", "さようなら", "バイバイ")) or any(k in spoken for k in ("おしまい", "しまい", "おわり", "さようなら", "ばいばい")):
                say("はい、おしまいにします。"); break
            # 過去のやりとりを見せると、gemma3:4b は同じ命令に「もう曲がりました」と答えて動作を選ばないことがある
            # （指示に書き足しても 3 回に 1 回起きた）。命令はその一言だけで判断させるのを既定にする
            try:
                ans, t_llm, _ = ask_llm(a.model, history[-2 * a.history:] if a.history > 0 else [], heard)
            except Exception as e:                       # ノート PC に届かない（Wi-Fi が切れた等）。動かずに知らせて、聞き直す
                print(f"   !! 頭に届きません: {e}")
                say("すみません、いま考えられません。")
                continue
            print(f"[think] llm {t_llm:.2f}s -> say {ans['say']!r}  action {ans['action']}"
                  + (f" x{ans['times']}" + (f"（頭の答えは {ans['times_asked']} 回、上限 {MAX_TIMES}）" if ans['times_asked'] != ans['times'] else "") if ans['action'] not in ("none", "stop") and (ans['times'] > 1 or ans['times_asked'] != ans['times']) else ""))
            # 動作は返事の読み上げと同時に始める。以前は読み上げ（約 3 秒）が終わってから動かしていたので、
            # 声の終わりから動き出しまでが 6 秒近くあった（表示していた「返事の出だしまで」には入っていなかった）
            if a.act_after_say:
                t_syn, t_play = say(ans["say"])
                t_act = car.act(ans["action"], ans["times"])
                t_move = t_asr + t_llm + t_syn + CHIME + LEAD_SILENCE + t_play + 0.4
            else:
                box = {}
                th = threading.Thread(target=lambda: box.update(t=car.act(ans["action"], ans["times"])), daemon=True)
                th.start()
                t_syn, t_play = say(ans["say"])
                th.join(); t_act = box.get("t", 0.0)
                t_move = t_asr + t_llm
            print(f"[done]  tts synth {t_syn:.2f}s play {t_play:.2f}s | act {t_act:.2f}s | 声の終わりから返事の出だしまで {t_asr + t_llm + t_syn + CHIME + LEAD_SILENCE:.2f}s"
                  + (f"、動き出しまで {t_move:.2f}s" if ans["action"] != "none" else ""))
            log.writerow([time.time(), a.model, a.asr, f"{speech:.2f}", f"{t_asr:.3f}", f"{t_llm:.3f}", f"{t_syn:.3f}", f"{t_play:.3f}", f"{t_act:.3f}",
                          heard, json.dumps(dropped, ensure_ascii=False), ans["say"], ans["action"] + (f" x{ans['times']}" if ans["times"] > 1 else "")]); logf.flush()
            history += [{"role": "user", "content": heard}, {"role": "assistant", "content": json.dumps(ans, ensure_ascii=False)}]
            if a.wav:
                break
    except KeyboardInterrupt:
        print("\n== bye")
    finally:
        car.close(); logf.close()
        # 声として数えられた秒数（認識結果の開始〜終了の合計）。汎用・ログ保存なしの単価 0.04125 円/秒で概算（無料枠・クーポンを引く前）
        print(f"== AmiVoice が声として数えた秒数（概算）: {BILLED['s']:.1f}s ≒ {BILLED['s'] * 0.04125:.2f} 円")

if __name__ == "__main__":
    main()
