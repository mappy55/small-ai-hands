#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cu_loop.py — スクショ方式の手足。Claude の computer use（computer_toolset_20260801）で、依頼文から直接 Firefox を操作させる。
木を読む loop.py と同じ依頼で並べて、秒数・手数・成否・トークンを比べるためのもの。

  python3 smallhands/cu_loop.py "Firefox で Wikipedia の東京駅を開いて、歴史の節を読んで" --cond "url:#歴史" --launch

- 画面: GNOME のショートカット Shift+Print を ydotool で押して保存させ、その PNG を拾う（必要なら縮小）。座標は元の解像度に戻してから押す。
- 操作: ydotool（mousemove --absolute / click / key / type）。キーボードの打ち方は loop.py のものを使う。
- 判定: 成功したかどうかは loop.py と同じ達成条件（--cond）を AT-SPI で見る。Claude の「できました」は信じない。
- 必要: ANTHROPIC_API_KEY、ydotoold、python3-pil、GNOME の Shift+Print（全画面を保存）が有効なこと。
  VirtualBox ではマウス統合を OFF にする（ON だとホストのマウス位置に引き戻されてクリックがずれる）。run_cu.sh が通知バナーとポインタ加速を切る。
"""
import sys, os, re, json, time, argparse, base64, io, subprocess, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loop as L                     # kb_type / kb_combo / ydo / condition_met / quick_state / find_apps など

from PIL import Image

API = "https://api.anthropic.com/v1/messages"

SYSTEM = ("You are operating a Linux desktop (GNOME) through screenshots. The user's request is in Japanese; the web pages are Japanese Wikipedia. "
          "Work step by step: take a screenshot, decide one action, act, and take a screenshot again to verify. "
          "Prefer navigating by typing a URL into the Firefox address bar (click it, or press ctrl+l, then type and press Return) over searching. "
          "Non-ASCII text cannot be typed with the keyboard: write Japanese parts of a URL percent-encoded (UTF-8). "
          "When the request is fully achieved, stop and reply with a short confirmation in one line. Do not ask the user questions.")

# ydotool のキー名 → キーコード（computer use の key アクション用）
XKEY = {"return": 28, "enter": 28, "tab": 15, "escape": 1, "esc": 1, "backspace": 14, "delete": 111, "space": 57,
        "up": 103, "down": 108, "left": 105, "right": 106, "home": 102, "end": 107, "page_up": 104, "pageup": 104,
        "page_down": 109, "pagedown": 109, "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "super": 125, "meta": 125,
        "f5": 63, "f6": 64, "f11": 87}
XKEY.update({c: L._LETTERS[c] for c in L._LETTERS})
XKEY.update({d: L._DIGITS[d] for d in L._DIGITS})

def ydo(*args):
    env = dict(os.environ, YDOTOOL_SOCKET=L.YDOTOOL_SOCKET)
    subprocess.run(["ydotool", *args], env=env, check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _shots_dir():
    try:
        pics = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        pics = ""
    pics = pics or os.path.expanduser("~/Pictures")
    for name in ("Screenshots", "スクリーンショット"):
        d = os.path.join(pics, name)
        if os.path.isdir(d):
            return d
    d = os.path.join(pics, "Screenshots"); os.makedirs(d, exist_ok=True); return d

SHOTS_DIR = None

def screenshot(scale):
    """GNOME Shell のショートカット Shift+Print（全画面を即保存）を ydotool で押し、保存された PNG を拾う。
    gnome-screenshot と org.gnome.Shell.Screenshot は GNOME 41 以降の Wayland では外部から使えない。
    戻り値: (base64 png, 元サイズ, 送るサイズ)"""
    global SHOTS_DIR
    SHOTS_DIR = SHOTS_DIR or _shots_dir()
    before = set(os.listdir(SHOTS_DIR))
    t0 = time.time()
    ydo("key", "42:1", "99:1", "99:0", "42:0")          # shift+print
    path = None
    while time.time() - t0 < 4.0:
        new = [f for f in os.listdir(SHOTS_DIR) if f not in before and f.lower().endswith(".png")]
        if new:
            path = os.path.join(SHOTS_DIR, sorted(new)[-1])
            # 書き終わるのを待つ（サイズが 2 回続けて同じ）
            last = -1
            for _ in range(20):
                sz = os.path.getsize(path)
                if sz > 0 and sz == last:
                    break
                last = sz; time.sleep(0.05)
            break
        time.sleep(0.05)
    if path is None:
        raise RuntimeError("screenshot not saved (Shift+Print) — check GNOME keyboard shortcuts")
    im = Image.open(path).convert("RGB")
    try:
        os.remove(path)                                     # 溜めない
    except OSError:
        pass
    full = im.size
    if scale != 1.0:
        im = im.resize((round(full[0] * scale), round(full[1] * scale)), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode(), full, im.size

def to_full(coord, scale):
    return [round(coord[0] / scale), round(coord[1] / scale)]

def mouse_move(x, y):
    ydo("mousemove", "--absolute", "-x", str(x), "-y", str(y))

def click(x, y, button="left", times=1):
    mouse_move(x, y); time.sleep(0.05)
    code = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}[button]
    for _ in range(times):
        ydo("click", code); time.sleep(0.08)

def key_combo(text, repeat=1):
    """'ctrl+l' / 'Return' / 'Page_Down' のような指定を ydotool key で"""
    names = [n.strip().lower() for n in text.split("+")]
    codes = []
    for n in names:
        if n not in XKEY:
            raise ValueError(f"unknown key {n!r}")
        codes.append(XKEY[n])
    for _ in range(repeat):
        ydo("key", *[f"{c}:1" for c in codes], *[f"{c}:0" for c in reversed(codes)]); time.sleep(0.05)

def scroll(x, y, direction, amount):
    mouse_move(x, y); time.sleep(0.05)
    # ydotool の wheel は縦のみ（負で下）。横は ctrl 無しのキーで代用しない（Firefox では shift+wheel が横）
    step = -amount if direction == "down" else amount
    ydo("mousemove", "--wheel", "-y", str(step))

def do_action(name, inp, scale):
    """computer toolset の 1 手を実行して (text or None, error) を返す。screenshot は呼び出し側で撮る"""
    c = inp.get("coordinate")
    if name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
        if c is None:
            raise ValueError("coordinate required")
        x, y = to_full(c, scale)
        btn = {"right_click": "right", "middle_click": "middle"}.get(name, "left")
        times = {"double_click": 2, "triple_click": 3}.get(name, 1)
        if inp.get("text"):                      # 修飾キーつきクリック
            codes = [XKEY[n.strip().lower()] for n in inp["text"].split("+")]
            ydo("key", *[f"{k}:1" for k in codes]); click(x, y, btn, times); ydo("key", *[f"{k}:0" for k in reversed(codes)])
        else:
            click(x, y, btn, times)
        return f"clicked ({x},{y})"
    if name == "mouse_move":
        x, y = to_full(c, scale); mouse_move(x, y); return "moved"
    if name == "left_click_drag":
        x0, y0 = to_full(inp["start_coordinate"], scale); x1, y1 = to_full(c, scale)
        mouse_move(x0, y0); ydo("click", "0x40"); mouse_move(x1, y1); ydo("click", "0x80"); return "dragged"
    if name == "type":
        raw = inp.get("text", "")
        enter = raw.endswith("\n")
        text = " ".join(raw.strip("\n").split("\n"))
        if text:
            L.kb_type(text)
        if enter:
            time.sleep(0.2); key_combo("Return")
        return f"typed {len(text)} chars" + (" + Return" if enter else "")
    if name == "key":
        key_combo(inp["text"], int(inp.get("repeat", 1))); return f"key {inp['text']}"
    if name == "scroll":
        x, y = to_full(c, scale) if c else (640, 400)
        scroll(x, y, inp.get("scroll_direction", "down"), int(inp.get("scroll_amount", 3))); return "scrolled"
    if name == "wait":
        d = min(float(inp.get("duration", 1)), 10); time.sleep(d); return f"waited {d:.0f}s"
    if name == "cursor_position":
        return "unknown"
    if name in ("left_mouse_down", "left_mouse_up", "hold_key", "zoom"):
        raise ValueError(f"{name} not supported here")
    raise ValueError(f"unknown action {name}")

def call_api(key, model, messages, max_tokens=1024):
    body = {"model": model, "max_tokens": max_tokens, "system": SYSTEM,
            "tools": [{"type": "computer_toolset_20260801"}], "messages": messages}
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            r = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Anthropic API {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
    return r, time.time() - t0

def judge(app, cond):
    if not cond or app is None:
        return None
    try:
        windows, items, docs = L.quick_state(app) if L.USE_COLLECTION else L.collect(app)
        return L.condition_met(cond, windows, items, docs)
    except Exception:
        return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("request")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--cond", default="", help="成功の判定条件（loop.py と同じ語彙。例 url:#歴史、heading:丸ノ内線）")
    ap.add_argument("--app", default="firefox", help="判定に使う AT-SPI 上のアプリ名")
    ap.add_argument("--launch", action="store_true", help="最初に Firefox を起動する（loop.py と同じプロファイル、about:blank）")
    ap.add_argument("--scale", type=float, default=1.0, help="スクショの縮小率（1280x800 ならそのままでよい）")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--log", default="logs/cu_log.jsonl")
    ap.add_argument("--save-shots", default="", help="各手のスクショを保存するディレクトリ（'' で保存しない）")
    a = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY") or sys.exit("ANTHROPIC_API_KEY が設定されていません")
    os.makedirs(os.path.dirname(a.log) or ".", exist_ok=True)
    if a.save_shots:
        os.makedirs(a.save_shots, exist_ok=True)

    app = None
    t_run0 = time.time()
    if a.launch:
        app, t_l = L.step_launch("firefox --profile $HOME/ffprof about:blank", a.app, "window:mozilla firefox")
        print(f"[launch] firefox in {t_l*1000:.0f}ms"); time.sleep(1.0)
        # 画面全体を撮るので、手前に端末などが残っていると Claude はそれを見てしまう → Firefox を最大化して覆う（GNOME: Super+↑）
        ydo("key", "125:1", "103:1", "103:0", "125:0"); time.sleep(0.8)
    else:
        app = L.find_app(a.app)

    shot, full, sent = screenshot(a.scale)
    print(f"== model {a.model}  screen {full[0]}x{full[1]} -> sent {sent[0]}x{sent[1]}  cond {a.cond or '-'}")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": f"Request (Japanese): {a.request}\n\nHere is the current screen."},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": shot}}]}]
    tokens = {"in": 0, "out": 0}
    step = 0; n_shots = 1; final_text = ""; stop = "max_steps"
    while step < a.max_steps:
        r, t_api = call_api(key, a.model, messages)
        u = r.get("usage", {}); tokens["in"] += u.get("input_tokens", 0); tokens["out"] += u.get("output_tokens", 0)
        content = r.get("content", [])
        messages.append({"role": "assistant", "content": content})
        texts = [b["text"] for b in content if b.get("type") == "text"]
        uses = [b for b in content if b.get("type") == "tool_use"]
        if texts:
            print(f"      claude: {' '.join(texts)[:160]!r}")
        if not uses:
            final_text = " ".join(texts); stop = r.get("stop_reason"); break
        results = []
        for b in uses:
            step += 1
            name, inp = b["name"], b.get("input", {})
            t0 = time.time(); err = None; out = ""
            try:
                if name in ("screenshot", "zoom"):
                    out = "(screenshot)"
                else:
                    out = do_action(name, inp, a.scale)
            except Exception as e:
                err = str(e)
            t_act = time.time() - t0
            # 操作のあとは少し待ってから撮る（画面が追いつく時間。Claude は自分で screenshot も要求するが、結果に画像を付けて往復を節約）
            if name not in ("screenshot", "zoom"):
                time.sleep(0.8)
            shot, full, sent = screenshot(a.scale); n_shots += 1
            if a.save_shots:
                with open(os.path.join(a.save_shots, f"step{step:02d}_{name}.png"), "wb") as f:
                    f.write(base64.b64decode(shot))
            print(f"[{step}] api {t_api*1000:.0f}ms -> {name} {json.dumps(inp, ensure_ascii=False)[:100]} | act {t_act*1000:.0f}ms"
                  + (f" | !! {err}" if err else ""))
            res = {"type": "tool_result", "tool_use_id": b["id"], "toolset_name": "computer",
                   "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": shot}}]}
            if err:
                res["content"] = [{"type": "text", "text": f"error: {err}"}]; res["is_error"] = True
            elif name not in ("screenshot", "zoom"):
                res["content"].insert(0, {"type": "text", "text": out})
            results.append(res)
            with open(a.log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), "step": step, "action": name, "input": inp, "api_ms": round(t_api*1000),
                                    "act_ms": round(t_act*1000), "err": err, "in": u.get("input_tokens"), "out": u.get("output_tokens")},
                                   ensure_ascii=False) + "\n")
        messages.append({"role": "user", "content": results})
    ok = judge(app, a.cond)
    t_run = time.time() - t_run0
    print(f"== run total {t_run:.1f}s, {step} actions, {n_shots} screenshots, stop={stop}, "
          f"condition {a.cond or '-'}: {'MET' if ok else ('not met' if ok is not None else '-')}, "
          f"api tokens in={tokens['in']} out={tokens['out']}")
    if final_text:
        print(f"== claude said: {final_text[:300]!r}")

if __name__ == "__main__":
    main()
