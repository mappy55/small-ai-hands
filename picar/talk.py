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

def listen_stream(dev, thr=None, gain=5.0, engine="-a-general", wait_sec=30.0, no_speech=2.5, max_utt=10.0, verbose=True, gate=True, on_interim=None, post_time=0):
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
        # postTime: 声の後にこの長さの無音が続いたら発話の終わりとみなす（AmiVoice の既定は 550 ミリ秒）
        ws.send(f"s LSB16K {engine} authorization={key}" + (f' segmenterProperties="postTime={int(post_time)}"' if post_time else ""))
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
        interim = []                                       # 途中経過（U イベント）: [(届いた時刻, 文)]。文が変わったときだけ記録
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
                    elif ev == "U":
                        # 認識の途中経過。0.1 秒ごとに届き、変化がないときは前の文の末尾に「.」が足される
                        try:
                            u = norm_text(json.loads(body).get("text", ""))
                        except Exception:
                            u = ""
                        if u and (not interim or interim[-1][1] != u):
                            interim.append((tm, u))
                        if u and on_interim:
                            on_interim(u, tm)
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
                "t_S": t_S, "t_E": t_E, "t_A": t_A, "interim": interim}
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
# ---- 記号で答えさせる方式（--code）: 動作は種類が少ないので、頭には短い記号を一つ選ばせ、返事の文はコードで決める。
# 頭の時間は「指示文を読む」と「答えを書く」がほぼ半々だった（実測 0.8 秒 ＋ 0.9 秒）。指示文を短くし、
# 書く量を {"action": "left", "times": 2, "say": "…"} から {"c":"L2","say":""} に減らして、両方を削る
HEAD_CODE = False
CODE_ACT = {"F": "forward", "B": "backward", "L": "left", "R": "right"}
CODE_SAY = {"F": "前へ", "B": "後ろへ", "L": "左へ", "R": "右へ"}
CODES = [f"{k}{n}" for k in "FBLR" for n in (1, 2, 3)] + ["S", "N"]
SYSTEM_CODE = (
    "あなたは小さな車型ロボット「ピカ」です。持ち主が声で話しかけてきます（音声認識の結果なので誤字が混ざります）。"
    "c に記号を一つ選ぶ。F=前へ B=後ろへ L=左へ曲がる R=右へ曲がる。後ろの数字は回数で、言われなければ 1、最大 3。S=止まる。N=動かない。"
    "動けと言われたときだけ F B L R S。あいさつ、質問、雑談は必ず N。迷ったら N。"
    "say は N のときだけ書く（話し言葉で一言。記号や絵文字は使わない）。それ以外は空。\n"
    '例: 「前に進んで」→ {"c":"F1","say":""}\n'
    '例: 「左に2回曲がって」→ {"c":"L2","say":""}\n'
    '例: 「バックして」→ {"c":"B1","say":""}\n'
    '例: 「こんにちは」→ {"c":"N","say":"こんにちは。"}\n'
    '例: 「止まって」→ {"c":"S","say":""}'
)
SCHEMA_CODE = {"type": "object", "properties": {"c": {"type": "string", "enum": CODES}, "say": {"type": "string"}}, "required": ["c", "say"]}

def decode(code):
    """記号 → (動作, 回数, コードで決めた返事 or None)。知らない記号は動かない"""
    code = (code or "").strip().upper()
    if code == "S":
        return "stop", 1, "はい、止まる。"
    if len(code) == 2 and code[0] in CODE_ACT and code[1].isdigit():
        n = max(1, min(MAX_TIMES, int(code[1])))
        return CODE_ACT[code[0]], n, f"はい、{CODE_SAY[code[0]]}" + (f"{n}回。" if n > 1 else "。")
    return "none", 1, None

def _clamp(action, asked):
    """頭の答え（動作・回数）をコードで縛る。戻り値: (動作, 実際の回数, 頭が答えた回数)"""
    if action not in ACTIONS:
        action = "none"
    try:
        asked = int(asked)
    except (TypeError, ValueError):
        asked = 1
    return action, (1 if action in ("none", "stop") else max(1, min(MAX_TIMES, asked))), asked

import re
_EARLY_CODE = re.compile(r'"c"\s*:\s*"([A-Za-z]\d?)"')
_EARLY = re.compile(r'"action"\s*:\s*"([a-z_]+)"\s*,\s*"times"\s*:\s*(-?\d+)\s*[,}]')

def early_action(partial):
    """出力の途中（JSON の書きかけ）から、動作と回数が確定していれば取り出す。まだなら None。
    回数は次の区切り（, か }）が来るまで待つ（"1" の後に "0" が続いて 10 になるかもしれないので）"""
    if HEAD_CODE:
        m = _EARLY_CODE.search(partial)
        return decode(m.group(1))[:2] if m else None
    m = _EARLY.search(partial)
    return (m.group(1), int(m.group(2))) if m else None

def ask_llm(model, history, text, timeout=120, on_early=None, holder=None):
    """頭に聞く。戻り値: (答え, かかった秒数, 生の出力)
    on_early(action, times, 経過秒) を渡すと、答えを少しずつ受け取り、動作と回数が出た時点で一度だけ呼ぶ
    （返事の文ができあがるのを待たずに体を動かすため）。呼んだ時刻は答えの t_early に入る"""
    msgs = [{"role": "system", "content": SYSTEM_CODE if HEAD_CODE else SYSTEM}] + history + [{"role": "user", "content": text}]
    stream = on_early is not None
    body = {"model": model, "stream": stream, "format": SCHEMA_CODE if HEAD_CODE else SCHEMA, "options": {"temperature": 0, "num_ctx": 4096}, "messages": msgs}
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time(); t_early = None; early = None; last = {}; t_first = None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if holder is not None:
            holder["resp"] = resp                          # 途中でやめたいとき用（Spec）
        if not stream:
            last = json.loads(resp.read()); content = last.get("message", {}).get("content", "")
        else:
            content = ""
            for line in resp:                              # 1 行に 1 つの JSON（書きかけの答えのかけら）
                if not line.strip():
                    continue
                r = json.loads(line)
                if t_first is None:
                    t_first = time.time() - t0             # 最初のかけらが届くまで（読み込み＋指示文の処理＋通信）
                content += r.get("message", {}).get("content", "")
                if early is None:
                    e = early_action(content)
                    if e is not None:
                        act, times, _ = _clamp(*e)
                        early = (act, times); t_early = time.time() - t0
                        on_early(act, times, t_early)
                if r.get("done"):
                    last = r
                    break
    wall = time.time() - t0
    try:
        ans = json.loads(content)
    except json.JSONDecodeError:
        ans = {"say": "すみません、うまく考えられませんでした。", "action": "none"}
    if HEAD_CODE and "c" in ans:
        act, n, fixed = decode(ans.get("c"))
        ans.update({"action": act, "times": n, "code": ans.get("c")})
        if fixed is not None:
            ans["say"] = fixed                             # 動くときの返事はコードで決める（頭には書かせない）
        elif not str(ans.get("say", "")).strip():
            ans["say"] = "はい。"
    ans["action"], ans["times"], ans["times_asked"] = _clamp(ans.get("action"), ans.get("times", 1))
    if early is not None:
        ans["action"], ans["times"] = early                # 体はもう動き始めている。途中で読んだ内容を正とする
        ans["t_early"] = t_early
    if ans["action"] not in ("none", "stop") and ans["times_asked"] > MAX_TIMES:
        # 頭の返事は「10回曲がります」のままなので、体がやることと口が言うことが食い違う。縛ったときは返事もコードで差し替える
        ans["say"] = f"{MAX_TIMES}回までにします。"
    ans.setdefault("say", "")
    # 頭の中の内訳（Ollama の申告。単位はナノ秒）: モデルの読み込み / 指示文と質問の処理 / 答えの生成
    ns = lambda k: last.get(k, 0) / 1e9
    ans["stats"] = (f"load {ns('load_duration'):.2f}s, prompt {last.get('prompt_eval_count', 0)} tok {ns('prompt_eval_duration'):.2f}s, "
                    f"gen {last.get('eval_count', 0)} tok {ns('eval_duration'):.2f}s, total {ns('total_duration'):.2f}s"
                    + (f", first chunk {t_first:.2f}s" if t_first is not None else ""))
    return ans, wall, content

def norm_text(t):
    """途中経過と確定の文を比べるための正規化: 句読点・空白・途中経過の末尾に付く「.」を取る"""
    return "".join(ch for ch in (t or "") if ch not in " 　、。.．…,，!！?？")

class Spec:
    """先回り: AmiVoice の途中経過（まだ話し終えたと判定される前の文）で、先に頭に聞いておく。
    確定した文が同じなら、その答えをそのまま使う（話し終えてからの待ち時間と、頭の考える時間が重なる）。
    違っていたら捨てて聞き直す。体を動かすのは、文が確定して一致を確かめた後だけ。"""
    def __init__(self, model, settle=0.2, min_len=2):
        self.model = model; self.settle = settle; self.min_len = min_len
        self.cand = None; self.cand_t = 0.0; self.job = None; self.started = 0; self.wasted = 0
    def feed(self, text, tm):
        """途中経過が届くたびに呼ぶ。同じ文が settle 秒続いたら、その文で頭に聞き始める"""
        if text != self.cand:
            self.cand = text; self.cand_t = tm
            return
        if tm - self.cand_t < self.settle or len(text) < self.min_len:
            return
        if self.job is not None and self.job["text"] == text:
            return
        self._cancel()
        job = {"text": text, "t0": time.time(), "early": None, "ev": threading.Event(), "done": threading.Event(), "holder": {}, "ans": None, "err": None}
        def run():
            try:
                def on_early(act, times, t):
                    job["early"] = (act, times, time.time()); job["ev"].set()
                job["ans"], job["wall"], _ = ask_llm(self.model, [], text, on_early=on_early, holder=job["holder"])
            except Exception as e:
                job["err"] = e
            finally:
                job["t1"] = time.time(); job["done"].set(); job["ev"].set()
        self.job = job; self.started += 1
        threading.Thread(target=run, daemon=True).start()
    def _cancel(self):
        j, self.job = self.job, None
        if j is None or j["done"].is_set():
            return
        self.wasted += 1
        r = j["holder"].get("resp")                        # 接続を切ると、Ollama 側も生成をやめる
        try:
            import socket
            r.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            try:
                r.close()
            except Exception:
                pass
    def take(self, final_text):
        """確定した文と同じ文で聞いていた仕事があれば返す。なければ None（進行中のものは捨てる）"""
        j = self.job
        if j is not None and j["text"] == norm_text(final_text) and j["err"] is None:
            self.job = None
            return j
        self._cancel()
        return None
    def close(self):
        self._cancel()

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
    # 先回りは既定で使わない。実測では、文の全体が途中経過に出るのは確定の 0.05〜0.14 秒前で、稼げる時間がほとんど無い。
    # 一方、言葉の途中の間（「左に」で 0.6 秒止まる等）で先回りが始まり、捨てた問い合わせの後ろに本番が並んで、
    # 頭の返事が 0.8〜2 秒遅くなった（動き出し 1.8〜2.0 秒 → 2.6〜4.0 秒）
    ap.add_argument("--ahead", action="store_true", help="先回り（AmiVoice の途中経過で先に頭に聞く）を使う。実測では逆に遅くなった")
    ap.add_argument("--post-time", type=int, default=0, help="AmiVoice が発話の終わりとみなす無音の長さ（ミリ秒。0 で既定の 550）")
    # 記号で答えさせる方式を既定にする（実測: 頭 1.7 秒 → 1.0 秒、動き出し 1.8 秒 → 1.3 秒、6 回中 6 回正解）
    ap.add_argument("--no-code", dest="code", action="store_false", help="以前の方式: 頭に動作・回数・返事の文をすべて書かせる")
    ap.add_argument("--code", dest="code", action="store_true", help="（既定になったので指定不要）頭に短い記号（F1, L2, N など）で答えさせ、動くときの返事はコードで決める")
    ap.set_defaults(code=True)
    ap.add_argument("--no-early", action="store_true", help="頭の答えが全部そろってから動かす（以前の方式）")
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
    global HEAD_CODE, MAX_TIMES, GAIN_TARGET, LEAD_SILENCE, LEAD_LEVEL, CHIME, TTS_PEAK, TTS_RATE
    TTS_RATE = a.tts_rate
    HEAD_CODE = a.code
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
    n_utt = 0; spec = None
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
            got = None
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
                    if spec is not None:
                        spec.close()
                    spec = Spec(a.model) if (a.ahead and a.history == 0 and not a.act_after_say and not a.no_early) else None
                    got = listen_stream(dev, thr=a.thr or None, gain=a.stream_gain, gate=a.gate,
                                        on_interim=spec.feed if spec else None, post_time=a.post_time)
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
            if got is not None and got.get("interim") and got.get("t_A"):
                # 途中経過の移り変わり（確定の何秒前に、どの文になったか）
                print("        interim: " + " | ".join(f"{tm - got['t_A']:+.2f}s {u!r}" for tm, u in got["interim"][-6:]))
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
            # 頭は 動作 → 回数 → 返事 の順に書く。動作と回数が出た時点で体を動かし始め、返事の文は後から読み上げる
            box = {}; th = None
            t_ref = got["t_A"] if (got is not None and got.get("t_A")) else time.time()     # 文が確定した時刻
            def on_early(act, times, t):
                nonlocal th
                box["t_car"] = time.time()
                th = threading.Thread(target=lambda: box.update(t=car.act(act, times)), daemon=True)
                th.start()
            early_ok = not (a.act_after_say or a.no_early)
            job = spec.take(heard) if (spec is not None and got is not None) else None
            ahead = None
            try:
                if job is not None:
                    # 先回りが当たった: 動作が決まりしだい動かし、答えがそろうのを待つ
                    job["ev"].wait(120)
                    if job["early"] is not None:
                        on_early(job["early"][0], job["early"][1], 0)
                    job["done"].wait(120)
                    if job["err"] is not None or job["ans"] is None:
                        raise job["err"] or RuntimeError("no answer")
                    ans = job["ans"]; ahead = t_ref - job["t0"]            # 確定の何秒前に聞き始めていたか
                    t_llm = max(0.0, job["t1"] - t_ref)                    # 確定してから答えがそろうまで
                else:
                    ans, t_llm, _ = ask_llm(a.model, history[-2 * a.history:] if a.history > 0 else [], heard, on_early=on_early if early_ok else None)
            except Exception as e:                       # ノート PC に届かない（Wi-Fi が切れた等）。動かずに知らせて、聞き直す
                print(f"   !! 頭に届きません: {e}")
                say("すみません、いま考えられません。")
                continue
            print(f"[think] llm {t_llm:.2f}s" + (f"（先回り: 確定の {ahead:.2f}s 前から考え始めた）" if ahead is not None else (f"（動作は {ans['t_early']:.2f}s で確定）" if "t_early" in ans else ""))
                  + (f" [先回り {spec.started} 回、うち捨てた {spec.wasted} 回]" if spec is not None and got is not None and spec.started else "") + (f" [{ans['code']}]" if "code" in ans else "") + f" -> say {ans['say']!r}  action {ans['action']}"
                  + (f" x{ans['times']}" + (f"（頭の答えは {ans['times_asked']} 回、上限 {MAX_TIMES}）" if ans['times_asked'] != ans['times'] else "") if ans['action'] not in ("none", "stop") and (ans['times'] > 1 or ans['times_asked'] != ans['times']) else ""))
            print(f"        ({ans['stats']})")
            # 動作は返事の読み上げと同時に始める。以前は読み上げ（約 3 秒）が終わってから動かしていたので、
            # 声の終わりから動き出しまでが 6 秒近くあった（表示していた「返事の出だしまで」には入っていなかった）
            if a.act_after_say:
                t_syn, t_play = say(ans["say"])
                t_act = car.act(ans["action"], ans["times"])
                t_move = t_asr + t_llm + t_syn + CHIME + LEAD_SILENCE + t_play + 0.4
            else:
                if th is None:                             # 途中で読めなかった（--no-early を含む）。答えがそろってから動かす
                    box["t_car"] = time.time()
                    th = threading.Thread(target=lambda: box.update(t=car.act(ans["action"], ans["times"])), daemon=True)
                    th.start()
                t_syn, t_play = say(ans["say"])
                th.join(); t_act = box.get("t", 0.0)
                t_move = t_asr + max(0.0, box.get("t_car", time.time()) - t_ref)       # 声の終わり → 文の確定 → 車に指示を出すまで
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
