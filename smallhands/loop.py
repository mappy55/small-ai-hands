#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
loop.py — 小さなローカル LLM にデスクトップアプリを操作させるループ（AT-SPI2 + ydotool + Ollama）

  計画ファイル（1 行 1 サブ目標、'目標文||達成条件'）を上から順に処理する。
   1. AT-SPI2 で「いま見えている」要素を集め（Collection が使えれば一括、だめなら木を歩く）、間引く
   2. 要素ごとに click / type の選択肢を番号付きで作る（末尾に DONE / WAIT）
   3. LLM（Ollama）に JSON スキーマ付きで「番号（と入力文字列）」だけ答えさせる
   4. AT-SPI2 の Action / EditableText で実行。書けない欄（Firefox の URL バーなど）は ydotool で打つ
   5. 達成条件（window / nowindow / title / heading / url / doc / text / text@<win>）が満たされるまで待つ
   6. 各段階の時間を CSV に、プロンプトと答えを logs/prompts/ に記録

  計画ファイルの特別な行:
   launch <cmd>||<条件>   アプリを起動して条件を待つ（LLM には聞かない）
   read[@見出し] ...||doc:<名前>   本文（document web）の見出しと段落を表示する（LLM には聞かない）

使い方（ゲスト側）:
    python3 smallhands/loop.py --plan plans/plan_tokyo_station.txt --log logs/run.csv
    bash scripts/run_batch.sh qwen3:4b-instruct 3 plans/plan_tokyo_history.txt

前提: Ollama はホストで OLLAMA_HOST=0.0.0.0（VirtualBox NAT ならゲストから 10.0.2.2:11434、環境変数 OLLAMA_URL で変更可）。
      ydotoold が動いていること（scripts/setup_ydotool.sh）。ydotool は前面のウィンドウに打つので、実行中は他のウィンドウをクリックしない。
"""
import sys, os, re, json, time, argparse, csv, hashlib, urllib.request, urllib.error, signal, subprocess, shutil

STEP_TIMEOUT = 60
def _watchdog(signum, frame):
    print(f"!! watchdog: a step exceeded {STEP_TIMEOUT}s (AT-SPI likely wedged) — aborting this run", flush=True)
    os._exit(4)
signal.signal(signal.SIGALRM, _watchdog)

import pyatspi

OLLAMA = os.environ.get("OLLAMA_URL", "http://10.0.2.2:11434")
YDOTOOL_SOCKET = os.environ.get("YDOTOOL_SOCKET", "/tmp/.ydotool_socket")

CLICKABLE = {"button", "push button", "toggle button", "menu button", "menu item",
             "check menu item", "radio menu item", "check box", "radio button",
             "list item", "tree item", "page tab", "link", "table cell"}
TYPEABLE = {"text", "entry", "password text"}
KEYTYPEABLE = {"combo box"}          # editable な combo box（Firefox の URL バー）はキーボードで打つ

# 間引き
MAX_LINKS = 25
MAX_ITEMS = 60
DROP_NAMES = {"minimize", "maximize", "restore down", "close tab", "firefox view",
              "open a new tab (ctrl+t)", "list all tabs", "open application menu", "sidebars",
              "view site information", "enter reader view", "edit this bookmark (ctrl+d)",
              "vpn", "account", "extensions", "import bookmarks…", "try private translations in firefox - beta"}
MAX_NAME = 60      # これより長い名前の list item / table cell は本文の断片なので落とす
FOOTNOTE = re.compile(r"^\[[^\]]*\]$")       # [1] [注釈 1] など

# ---------------------------------------------------------------- AT-SPI 読み取り
def _rect(acc):
    try:
        x, y, w, h = acc.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        return (int(x), int(y), int(w), int(h))
    except Exception:
        return None

def _has(acc, st):
    try:
        return acc.getState().contains(st)
    except Exception:
        return False

def _showing(acc):
    try:
        st = acc.getState()
        return st.contains(pyatspi.STATE_SHOWING) and st.contains(pyatspi.STATE_VISIBLE)
    except Exception:
        return False

def _sensitive(acc):
    try:
        return acc.getState().contains(pyatspi.STATE_SENSITIVE)
    except Exception:
        return True

def _text(acc, limit=120):
    try:
        t = acc.queryText()
        return t.getText(0, min(t.characterCount, limit))
    except Exception:
        return ""

def _full_text(acc, depth=0):
    """段落の ￼（埋め込みリンク等）を子要素のテキストで順に埋める"""
    try:
        t = acc.queryText().getText(0, -1)
    except Exception:
        t = acc.name or ""
    if "￼" not in t or depth > 5:
        return t
    parts, ci = [], 0
    for ch in t:
        if ch == "￼":
            try:
                parts.append(_full_text(acc.getChildAtIndex(ci), depth + 1))
            except Exception:
                parts.append("")
            ci += 1
        else:
            parts.append(ch)
    return "".join(parts)

def _label_name(acc):
    try:
        for rel in acc.getRelationSet():
            if rel.getRelationType() == pyatspi.RELATION_LABELLED_BY and rel.getNTargets() > 0:
                t = rel.getTarget(0)
                if t is not None and t.name:
                    return t.name
    except Exception:
        pass
    return ""

def find_app(needle):
    desktop = pyatspi.Registry.getDesktop(0)
    needle = needle.lower()
    for i in range(desktop.childCount):
        app = desktop.getChildAtIndex(i)
        if app is not None and needle in (app.name or "").lower():
            return app
    return None

def find_apps(needle):
    desktop = pyatspi.Registry.getDesktop(0)
    needle = needle.lower()
    out = []
    for i in range(desktop.childCount):
        app = desktop.getChildAtIndex(i)
        if app is not None and needle in (app.name or "").lower():
            out.append(app)
    return out

def _role_const(name):
    return getattr(pyatspi, "ROLE_" + name.upper().replace(" ", "_"), None)

_ITEM_ROLES = [r for r in (_role_const(n) for n in sorted(CLICKABLE | TYPEABLE | KEYTYPEABLE)) if r is not None]

def _in_view(acc, view):
    """要素の矩形がウィンドウの矩形と縦に重なっているか（Firefox は画面外の見出しにも VISIBLE を付けるので座標で見る）"""
    r = _rect(acc)
    return bool(r and r[2] > 0 and r[3] > 0 and view and r[1] + r[3] > view[1] and r[1] < view[1] + view[3])

def _n_actions(acc):
    try:
        return acc.queryAction().nActions
    except Exception:
        return 0

def _item_from(acc, role, name, desc, win_name):
    try:
        st = acc.getState()
    except Exception:
        return None
    sensitive = st.contains(pyatspi.STATE_SENSITIVE)
    if role in CLICKABLE and sensitive and (name or desc):
        if _n_actions(acc) == 0:        # 押せないクリック要素（Wikipedia の固定ヘッダーにある同名ボタンの複製など）は見せない
            return None
        return {"acc": acc, "kind": "click", "role": role, "name": name or desc, "win": win_name}
    if role in TYPEABLE and sensitive:
        label = name or desc or _label_name(acc)
        return {"acc": acc, "kind": "type", "role": role, "name": label or "text field", "win": win_name, "text": _text(acc)}
    if role in KEYTYPEABLE and st.contains(pyatspi.STATE_EDITABLE):
        return {"acc": acc, "kind": "keytype", "role": role, "name": name or desc or "text field", "win": win_name, "text": _text(acc)}
    return None

def collect_collection(w, win_name, items, docs):
    """Collection インターフェースで一括検索（木を歩かない。Firefox は対応、GTK は非対応で例外）"""
    col = w.queryCollection()
    def matches(states, match, roles):
        rule = col.createMatchRule(states, match, {}, col.MATCH_NONE, roles, col.MATCH_ANY, [], col.MATCH_NONE, False)
        return col.getMatches(rule, col.SORT_ORDER_CANONICAL, 0, True)
    view = _rect(w)
    for acc in matches(pyatspi.StateSet(pyatspi.STATE_SHOWING, pyatspi.STATE_VISIBLE), col.MATCH_ALL, _ITEM_ROLES):
        try:
            role = acc.getRoleName(); name = acc.name or ""
            desc = ""
            if not name:
                try: desc = acc.description or ""
                except Exception: pass
            it = _item_from(acc, role, name, desc, win_name)
            if it: items.append(it)
        except Exception:
            continue
    busy = False
    for acc in matches(pyatspi.StateSet(pyatspi.STATE_SHOWING), col.MATCH_ALL, [pyatspi.ROLE_DOCUMENT_WEB]):
        if _has(acc, pyatspi.STATE_BUSY):
            busy = True; continue          # 読み込み中のページは「まだ無い」扱い（見出しだけ先に出て目次が無い、を避ける）
        docs.append("doc:" + (acc.name or ""))
    if busy:
        docs.append("busy:")
        return
    # 見出しは Firefox だと SHOWING が付かないので VISIBLE + 画面内の座標で判定
    for acc in matches(pyatspi.StateSet(pyatspi.STATE_VISIBLE), col.MATCH_ALL, [pyatspi.ROLE_HEADING]):
        try:
            if acc.name and _in_view(acc, view):
                docs.append("heading:" + acc.name)
        except Exception:
            continue

USE_COLLECTION = True

def quick_state(app):
    """待ち判定用の軽い取得: ウィンドウ名、アドレスバー、見出し（画面内）、document だけ。Collection 非対応なら None"""
    windows, items, docs = [], [], []
    for j in range(app.childCount):
        w = app.getChildAtIndex(j)
        if w is None or not _showing(w):
            continue
        windows.append(f"{w.getRoleName()} '{w.name}'")
        col = w.queryCollection()
        def matches(states, roles):
            rule = col.createMatchRule(states, col.MATCH_ALL, {}, col.MATCH_NONE, roles, col.MATCH_ANY, [], col.MATCH_NONE, False)
            return col.getMatches(rule, col.SORT_ORDER_CANONICAL, 0, True)
        view = _rect(w)
        for acc in matches(pyatspi.StateSet(pyatspi.STATE_SHOWING, pyatspi.STATE_EDITABLE), [pyatspi.ROLE_COMBO_BOX]):
            if "address" in (acc.name or "").lower():
                items.append({"kind": "keytype", "name": acc.name, "win": windows[-1], "text": _text(acc)})
        n = len(matches(pyatspi.StateSet(pyatspi.STATE_SHOWING), [pyatspi.ROLE_LINK, pyatspi.ROLE_PUSH_BUTTON]))
        items.extend({"kind": "count"} for _ in range(n))   # 署名（画面が変わったか）用の頭数
        busy = False
        for acc in matches(pyatspi.StateSet(pyatspi.STATE_SHOWING), [pyatspi.ROLE_DOCUMENT_WEB]):
            if _has(acc, pyatspi.STATE_BUSY):
                busy = True; continue
            docs.append("doc:" + (acc.name or ""))
        if busy:
            docs.append("busy:"); continue
        for acc in matches(pyatspi.StateSet(pyatspi.STATE_VISIBLE), [pyatspi.ROLE_HEADING]):
            try:
                if acc.name and _in_view(acc, view):
                    docs.append("heading:" + acc.name)
            except Exception:
                continue
    return windows, items, docs

def collect(app):
    """showing なウィンドウを辿り、操作候補と状態を返す（間引き前）。Collection が使えればそれを優先"""
    items, windows, docs = [], [], []
    def walk(acc, win_name, depth):
        if acc is None or depth > 60:
            return
        try:
            role = acc.getRoleName(); name = acc.name or ""
        except Exception:
            return
        try:
            desc = acc.description or ""
        except Exception:
            desc = ""
        if not _showing(acc):
            return
        if role == "document web":
            docs.append("doc:" + name)
        elif role == "heading" and name:
            docs.append("heading:" + name)
        r = _rect(acc)
        if r and r[2] > 0 and r[3] > 0:
            if role in CLICKABLE and _sensitive(acc) and (name or desc):
                items.append({"acc": acc, "kind": "click", "role": role,
                              "name": name or desc, "win": win_name})
            elif role in TYPEABLE and _sensitive(acc):
                label = name or desc or _label_name(acc)
                items.append({"acc": acc, "kind": "type", "role": role,
                              "name": label or "text field", "win": win_name,
                              "text": _text(acc)})
            elif role in KEYTYPEABLE and _has(acc, pyatspi.STATE_EDITABLE):
                items.append({"acc": acc, "kind": "keytype", "role": role,
                              "name": name or desc or "text field", "win": win_name,
                              "text": _text(acc)})
        try:
            for i in range(acc.childCount):
                walk(acc.getChildAtIndex(i), win_name, depth + 1)
        except Exception:
            pass
    for j in range(app.childCount):
        w = app.getChildAtIndex(j)
        if w is None or not _showing(w):
            continue
        wname = f"{w.getRoleName()} '{w.name}'"
        windows.append(wname)
        if USE_COLLECTION:
            try:
                collect_collection(w, wname, items, docs); continue
            except Exception:
                pass           # 非対応（GTK アプリ等）→ 従来どおり歩く
        walk(w, wname, 0)
    return windows, items, docs

def prune(items):
    """LLM に見せる要素を絞る。入力欄は全部残し、ボタン → リンク（上限）→ その他 の順で詰める"""
    out, n_links, seen = [], 0, set()
    dropped = 0
    link_names = [it["name"].strip().lower() for it in items if it["role"] == "link" and it["name"].strip()]
    # ウィンドウ枠の Close（Minimize/Maximize と並ぶもの）は落とすが、ダイアログの Close ボタンは残す
    chrome_wins = {it["win"] for it in items if it["name"].strip().lower() in ("minimize", "maximize", "restore down")}
    for it in items:
        nm = it["name"].strip()
        if nm.lower() in DROP_NAMES or FOOTNOTE.match(nm):
            dropped += 1; continue
        if nm.lower() == "close" and it["win"] in chrome_wins:
            dropped += 1; continue
        if it["role"] in ("list item", "table cell"):
            low = nm.lower()
            if len(nm) > MAX_NAME or any(low.startswith(l) for l in link_names):   # リンクの入れ物は二重に出さない
                dropped += 1; continue
        if it["role"] == "link":
            key = nm.lower()
            if key in seen or n_links >= MAX_LINKS:
                dropped += 1; continue
            seen.add(key); n_links += 1
        out.append(it)
    if len(out) > MAX_ITEMS:
        def prio(it):
            if it["kind"] in ("type", "keytype"): return 0
            if it["role"] == "link": return 1
            if it["role"] in ("button", "push button", "page tab"): return 2
            return 3
        out.sort(key=prio)   # 安定ソートなので同じ優先度の中は元の順
        dropped += len(out) - MAX_ITEMS
        out = out[:MAX_ITEMS]
    return out, dropped

# ---------------------------------------------------------------- 達成条件
def parse_goal(g):
    if "||" in g:
        t, c = g.split("||", 1)
        return t.strip(), c.strip()
    return g.strip(), None

def condition_met(cond, windows, items, docs=()):
    if not cond:
        return False
    kind, _, val = cond.partition(":")
    val = val.lower()
    if kind == "window":
        return any(val in w.lower() for w in windows)
    if kind == "nowindow":
        return not any(val in w.lower() for w in windows)
    if kind == "title":
        return any(w.lower().startswith("frame") and val in w.lower() for w in windows)
    if kind in ("doc", "heading"):
        return any(d.lower().startswith(kind + ":") and val in d.lower()[len(kind) + 1:] for d in docs)
    if kind == "text":
        return any(it["kind"] in ("type", "keytype") and val in (it.get("text") or "").lower() for it in items)
    if kind == "url":      # url:#歴史 → アドレスバー（keytype で名前に address を含む欄）の中身で判定
        return any(it["kind"] == "keytype" and "address" in it["name"].lower()
                   and val in (it.get("text") or "").lower() for it in items)
    if kind.startswith("text@"):
        win = kind[5:].lower()
        return any(it["kind"] in ("type", "keytype") and win in it["win"].lower()
                   and val in (it.get("text") or "").lower() for it in items)
    return False

def list_windows(app):
    out = []
    for j in range(app.childCount):
        w = app.getChildAtIndex(j)
        if w is not None and _showing(w):
            out.append(f"{w.getRoleName()} '{w.name}'")
    return out

def _sig(windows, items, docs):
    """画面が変わったかを見るための粗い署名（ウィンドウ名・要素数・document と見出し）"""
    return (tuple(windows), len(items), tuple(docs))

def wait_until(app, cond, timeout=12.0, interval=0.1, before=None, settle_polls=3):
    """条件が満たされるまで待つ。before（操作前の署名）が渡されていて、条件は満たさないが画面が変わって
    落ち着いたら（署名が settle_polls 回続けて同じ）、待ち切らずに戻る。戻り値: (経過秒, 満たしたか)"""
    t0 = time.time()
    kind = cond.split(":", 1)[0]
    light = kind in ("window", "nowindow", "title")
    quick = USE_COLLECTION and kind in ("url", "heading", "doc")
    polls = []
    last_sig, same = None, 0
    while time.time() - t0 < timeout:
        p0 = time.time()
        try:
            if light:
                windows, items, docs = list_windows(app), [], []
            elif quick:
                try:
                    windows, items, docs = quick_state(app)
                except NotImplementedError:
                    quick = False; continue
            else:
                windows, items, docs = collect(app)
        except Exception as e:
            print(f"      (a11y hiccup while waiting: {str(e)[:60]} — retrying)")
            time.sleep(0.3); continue
        polls.append(time.time() - p0)
        if condition_met(cond, windows, items, docs):
            print(f"      wait: {len(polls)} polls, poll avg {1000*sum(polls)/len(polls):.0f}ms max {1000*max(polls):.0f}ms, met after {time.time()-t0:.2f}s")
            return time.time() - t0, True
        if before is not None and not light and "busy:" not in docs:
            sig = _sig(windows, items, docs)
            if sig != before:
                same = same + 1 if sig == last_sig else 1
                last_sig = sig
                if same >= settle_polls:
                    print(f"      wait: {len(polls)} polls, screen changed and settled after {time.time()-t0:.2f}s (condition not met yet)")
                    return time.time() - t0, False
        time.sleep(interval)
    if polls:
        print(f"      wait: {len(polls)} polls, poll avg {1000*sum(polls)/len(polls):.0f}ms max {1000*max(polls):.0f}ms, TIMEOUT")
    return time.time() - t0, False

# ---------------------------------------------------------------- 選択肢とプロンプト
def build_choices(windows, items):
    lines, choices = [], []
    n = 0
    for it in items:
        n += 1
        cur = (it.get("text") or "").replace("\n", "\\n")[:60]
        if it["kind"] == "click":
            lines.append(f"{n}. click {it['role']} '{it['name']}'  (in {it['win']})")
        elif it["kind"] == "type":
            lines.append(f"{n}. type into {it['role']} '{it['name']}' (current text: \"{cur}\")  (in {it['win']})")
        else:
            lines.append(f"{n}. type into {it['role']} '{it['name']}' and press Enter (current text: \"{cur}\")  (in {it['win']})")
        choices.append(it)
    n += 1; lines.append(f"{n}. DONE — the goal is already achieved"); choices.append({"kind": "done"})
    n += 1; lines.append(f"{n}. WAIT — nothing suitable yet, wait for the UI"); choices.append({"kind": "wait"})
    return "\n".join(lines), choices

SYSTEM = ("You are a GUI agent operating a Linux desktop app through its accessibility tree. "
          "You are given a goal, the visible windows, the history of actions already taken, and a numbered list of possible next actions. "
          "Pick exactly ONE action number. If the action is 'type into', also provide the text to type, exactly as the goal specifies. "
          "Never repeat an action that already succeeded. Choose DONE when the goal is fully achieved. "
          "If the page is still loading, choose WAIT. If you ended up on a wrong page, click 'Back'. "
          "Reply ONLY with JSON: {\"choice\": <int>, \"text\": <string or empty>}.")

SCHEMA = {"type": "object",
          "properties": {"choice": {"type": "integer"}, "text": {"type": "string"}},
          "required": ["choice", "text"]}

THINK = None
NUM_CTX = 8192

def build_user_prompt(goal, windows, history, choices_text, note=""):
    return (f"Goal: {goal}\n\nVisible windows: {'; '.join(windows)}{note}\n\n"
            f"History:\n" + ("\n".join(f"- {h}" for h in history) if history else "- (none)") +
            f"\n\nPossible actions:\n{choices_text}\n\nWhich action number?")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
TOKENS = {"in": 0, "out": 0}      # Anthropic バックエンドの累計（費用の見積もり用）

def ask_claude(model, goal, windows, history, choices_text, note=""):
    """実行層を Claude（Anthropic API）にする。同じ SYSTEM と選択肢を渡し、tool_use で {choice, text} を強制する。
    戻り値は ask_ollama と同じ形（r には prompt_eval_count / eval_count に相当する値を入れる）"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY が設定されていません")
    user = build_user_prompt(goal, windows, history, choices_text, note)
    body = {"model": model, "max_tokens": 200, "system": SYSTEM,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": "choose", "description": "Pick exactly one action number (and the text to type, if any).",
                       "input_schema": SCHEMA}],
            "tool_choice": {"type": "tool", "name": "choose"}}
    req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-api-key": key,
                                          "anthropic-version": "2023-06-01"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            r = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Anthropic API {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    wall = time.time() - t0
    ans = None
    for blk in r.get("content", []):
        if blk.get("type") == "tool_use" and blk.get("name") == "choose":
            ans = blk.get("input", {}); break
    if ans is None:                                   # 念のため: テキストで返ってきたら JSON を拾う
        txt = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        ans = json.loads(m.group(0)) if m else {"choice": 0, "text": ""}
    u = r.get("usage", {})
    TOKENS["in"] += u.get("input_tokens", 0); TOKENS["out"] += u.get("output_tokens", 0)
    rr = {"prompt_eval_count": u.get("input_tokens"), "eval_count": u.get("output_tokens"),
          "prompt_eval_duration": 0, "eval_duration": int(wall * 1e9)}
    return ans, wall, rr, user

def ask_ollama(model, goal, windows, history, choices_text, note=""):
    user = build_user_prompt(goal, windows, history, choices_text, note)
    body = {"model": model, "stream": False, "format": SCHEMA,
            "options": {"num_ctx": NUM_CTX, "temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    if THINK is not None:
        body["think"] = THINK
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp:
        r = json.loads(resp.read())
    wall = time.time() - t0
    ans = json.loads(r["message"]["content"])
    return ans, wall, r, user

# ---------------------------------------------------------------- 実行（AT-SPI）
def do_click(acc):
    a = acc.queryAction()
    names = [a.getName(i).lower() for i in range(a.nActions)]
    for pref in ("click", "press", "activate", "toggle"):
        if pref in names:
            return a.doAction(names.index(pref)), names[names.index(pref)]
    return a.doAction(0), names[0] if names else "?"

def do_type_atspi(acc, text):
    et = acc.queryEditableText()
    try:
        et.setTextContents(text)
        how = "setTextContents"
    except Exception:
        et.insertText(0, text, len(text))
        how = "insertText"
    time.sleep(0.15)
    return how, _text(acc, 4000) == text        # 本当に入ったかを読み返す

# ---------------------------------------------------------------- 実行（キーボード = ydotool）
KEY = {"ctrl": 29, "shift": 42, "alt": 56, "enter": 28, "esc": 1, "tab": 15, "a": 30, "l": 38, "v": 47}

def ydo(*args, text_stdin=None):
    env = dict(os.environ, YDOTOOL_SOCKET=YDOTOOL_SOCKET)
    subprocess.run(["ydotool", *args], env=env, check=True, timeout=20,
                   input=text_stdin.encode() if text_stdin is not None else None)

def kb_combo(*names):
    codes = [KEY[n] for n in names]
    ydo("key", *[f"{c}:1" for c in codes], *[f"{c}:0" for c in reversed(codes)])

# ---- キーボード配列。ydotool の `type` は US 配列の前提でキーコードを選ぶので、日本語配列の VM では
#      記号がずれる（'=' が '^'、'@' が '[' など）。記号を含む文字列は配列表を見てキーコードで打つ。
_LETTERS = {c: 30 + "asdfghjkl".index(c) for c in "asdfghjkl"}
_LETTERS.update({c: 16 + "qwertyuiop".index(c) for c in "qwertyuiop"})
_LETTERS.update({c: 44 + "zxcvbnm".index(c) for c in "zxcvbnm"})
_DIGITS = {str(d): 2 + (d - 1 if d else 9) for d in range(10)}       # 1..9 → 2..10, 0 → 11
# 記号: 文字 → (keycode, shift)
_SYM_US = {"-": (12, 0), "_": (12, 1), "=": (13, 0), "+": (13, 1), "[": (26, 0), "{": (26, 1), "]": (27, 0), "}": (27, 1),
           ";": (39, 0), ":": (39, 1), "'": (40, 0), '"': (40, 1), "`": (41, 0), "~": (41, 1), "\\": (43, 0), "|": (43, 1),
           ",": (51, 0), "<": (51, 1), ".": (52, 0), ">": (52, 1), "/": (53, 0), "?": (53, 1), " ": (57, 0),
           "!": (2, 1), "@": (3, 1), "#": (4, 1), "$": (5, 1), "%": (6, 1), "^": (7, 1), "&": (8, 1), "*": (9, 1), "(": (10, 1), ")": (11, 1)}
_SYM_JP = {"-": (12, 0), "=": (12, 1), "^": (13, 0), "~": (13, 1), "\\": (124, 0), "|": (124, 1), "@": (26, 0), "`": (26, 1),
           "[": (27, 0), "{": (27, 1), ";": (39, 0), "+": (39, 1), ":": (40, 0), "*": (40, 1), "]": (43, 0), "}": (43, 1),
           ",": (51, 0), "<": (51, 1), ".": (52, 0), ">": (52, 1), "/": (53, 0), "?": (53, 1), "_": (89, 1), " ": (57, 0),
           "!": (2, 1), '"': (3, 1), "#": (4, 1), "$": (5, 1), "%": (6, 1), "&": (7, 1), "'": (8, 1), "(": (9, 1), ")": (10, 1)}

def keyboard_layout():
    """SMALLHANDS_LAYOUT=us|jp があればそれ。無ければ GNOME の設定から推定（取れなければ us）"""
    env = os.environ.get("SMALLHANDS_LAYOUT")
    if env:
        return env.lower()
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.input-sources", "sources"],
                             capture_output=True, text=True, timeout=5).stdout
        if "'jp'" in out:
            return "jp"
    except Exception:
        pass
    return "us"

LAYOUT = None

def kb_type_keys(text, layout):
    """1 文字ずつキーコードで打つ（配列表に従う）。打てない文字があれば ValueError"""
    sym = _SYM_JP if layout == "jp" else _SYM_US
    seq = []
    for ch in text:
        if ch.lower() in _LETTERS:
            code, shift = _LETTERS[ch.lower()], ch.isupper()
        elif ch in _DIGITS:
            code, shift = _DIGITS[ch], 0
        elif ch in sym:
            code, shift = sym[ch]
        else:
            raise ValueError(f"cannot type {ch!r} with the keyboard")
        if shift: seq.append("42:1")
        seq += [f"{code}:1", f"{code}:0"]
        if shift: seq.append("42:0")
    for i in range(0, len(seq), 60):          # 引数が長くなりすぎないように分割
        ydo("key", "--key-delay", "12", *seq[i:i + 60])

def kb_type(text):
    """英数字だけなら ydotool type。記号を含めば配列表でキーコード打ち。非 ASCII は wl-copy + Ctrl+V（wl-clipboard が必要）"""
    global LAYOUT
    if LAYOUT is None:
        LAYOUT = keyboard_layout()
    if text.isascii():
        if text.isalnum() or LAYOUT == "us":
            ydo("type", "--key-delay", "12", "--file", "/dev/stdin", text_stdin=text)
            return "ydotool-type"
        kb_type_keys(text, LAYOUT)
        return f"keycodes-{LAYOUT}"
    if shutil.which("wl-copy"):
        subprocess.run(["wl-copy", "--", text], check=True, timeout=5)
        time.sleep(0.1)
        kb_combo("ctrl", "v")
        return "wl-copy+ctrl-v"
    raise RuntimeError("non-ASCII text needs wl-clipboard (sudo apt install wl-clipboard)")

def _focused_in(acc, depth=0):
    """acc 自身か、その浅い子孫（combo box の中の入力欄など）に FOCUSED が付いているか"""
    if _has(acc, pyatspi.STATE_FOCUSED):
        return True
    if depth >= 2:
        return False
    try:
        return any(_focused_in(acc.getChildAtIndex(i), depth + 1) for i in range(min(acc.childCount, 8)))
    except Exception:
        return False

def _focused(acc, tries=4, interval=0.2):
    """フォーカスが来たかを少し待ちながら確かめる"""
    for _ in range(tries):
        if _focused_in(acc):
            return True
        time.sleep(interval)
    return False

def focus_for_keys(it):
    """キー入力の前にフォーカスを当て、本当に来たかを確かめる。来なければ例外（違う場所に打つより止まる方がまし）。
    URL バーは Ctrl+L（忙しい Firefox には届かないことがあるので最大 3 回）、それ以外は grabFocus → activate の順に試す"""
    acc = it["acc"]
    if "address" in it["name"].lower():
        for attempt in range(3):
            kb_combo("ctrl", "l")
            if _focused(acc):
                return "ctrl+l" + ("" if attempt == 0 else f"x{attempt+1}")
            time.sleep(0.5)
        raise RuntimeError("address bar did not take focus after Ctrl+L x3 — not typing")
    try:
        acc.queryComponent().grabFocus()
        if _focused(acc):
            return "grabFocus"
    except Exception:
        pass
    try:
        do_click(acc)                      # 入力欄にも activate があることがある
        if _focused(acc):
            return "activate"
    except Exception:
        pass
    raise RuntimeError(f"could not focus {it['role']} '{it['name']}' — not typing")

def do_type(it, text, press_enter):
    """AT-SPI で書けたらそれで終わり。書けなければキーボードで打つ"""
    how, applied = "", False
    if it["kind"] == "type":
        try:
            how, applied = do_type_atspi(it["acc"], text)
        except Exception as e:
            how = f"atspi-failed({str(e)[:30]})"
    if applied and not press_enter:
        return how
    if applied and press_enter:
        focus_for_keys(it); kb_combo("enter")
        return how + "+enter"
    f = focus_for_keys(it)
    if it["kind"] == "type" or _text(it["acc"], 4000):
        kb_combo("ctrl", "a")            # 既存の文字を消す
    k = kb_type(text)
    if press_enter:
        time.sleep(0.1); kb_combo("enter")
    return f"{how}->keyboard({f},{k}{',enter' if press_enter else ''})"

# ---------------------------------------------------------------- コードで行うステップ
def step_launch(cmd, appname, cond, timeout=20.0):
    """アプリを起動し、AT-SPI に現れて条件を満たすまで待つ"""
    t0 = time.time()
    subprocess.Popen(cmd, shell=True, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    app = None
    while time.time() - t0 < timeout:
        # 同名のアプリが複数いることがある（別プロファイルの Firefox など）→ 条件を満たすウィンドウを持つ方を選ぶ
        for cand in find_apps(appname):
            try:
                wins = list_windows(cand)
                if wins and (not cond or condition_met(cond, wins, [], [])):
                    return cand, time.time() - t0
            except Exception:
                pass
        time.sleep(0.2)
    return find_app(appname), time.time() - t0

def docs_of(app):
    """showing な document web の名前だけを集める（本文は歩かない）"""
    out = []
    for j in range(app.childCount):
        w = app.getChildAtIndex(j)
        if w is None or not _showing(w):
            continue
        stack = [w]
        while stack:
            a = stack.pop()
            try:
                if a.getRoleName() == "document web":
                    if _showing(a): out.append("doc:" + (a.name or ""))
                    continue
                stack.extend(a.getChildAtIndex(i) for i in range(min(a.childCount, 50)))
            except Exception:
                pass
    return out

def step_read(app, max_paragraphs=2, start=None):
    """document web の最初の見出し（目次以外）と、その後の段落を読み上げる"""
    out = []
    def walk(acc, depth=0):
        if acc is None or depth > 60 or len(out) > 40:
            return
        try:
            role = acc.getRoleName()
        except Exception:
            return
        if role in ("heading", "paragraph"):
            t = _full_text(acc).strip()
            if t:
                out.append((role, t))
        try:
            for i in range(acc.childCount):
                walk(acc.getChildAtIndex(i), depth + 1)
        except Exception:
            pass
    for j in range(app.childCount):
        w = app.getChildAtIndex(j)
        if w is None or not _showing(w):
            continue
        stack = [w]
        while stack:
            a = stack.pop()
            try:
                if a.getRoleName() == "document web" and _showing(a):
                    walk(a); break
                stack.extend(a.getChildAtIndex(i) for i in range(min(a.childCount, 50)))
            except Exception:
                pass
    # 「目次」をとばして最初の見出し、その後の段落
    result, started, n_par = [], False, 0
    for role, t in out:
        if role == "heading" and not started and (start.lower() in t.lower() if start else t != "目次"):
            started = True; result.append(f"# {t}"); continue
        if started and role == "paragraph":
            result.append(t); n_par += 1
            if n_par >= max_paragraphs:
                break
        if started and role == "heading" and n_par > 0:
            break
    return result

# ---------------------------------------------------------------- メイン
def load_plan(path):
    goals = []
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if s and not s.startswith("#"):
            goals.append(s)
    return goals

def main():
    global THINK, NUM_CTX, MAX_LINKS, MAX_ITEMS, USE_COLLECTION
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", help="AT-SPI 上のアプリ名（部分一致）。--plan の launch 行から推定もする")
    ap.add_argument("--goal", action="append", default=[])
    ap.add_argument("--plan", help="計画ファイル（1 行 1 サブ目標）")
    ap.add_argument("--model", default="qwen3:4b-instruct")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--log", default="logs/agent_log.csv")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--nothink", action="store_true")
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--max-links", type=int, default=MAX_LINKS)
    ap.add_argument("--max-items", type=int, default=MAX_ITEMS)
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--no-collection", action="store_true", help="Collection を使わず木を歩く（比較用）")
    ap.add_argument("--dump", default="logs/prompts", help="各ステップのプロンプトと答えを保存するディレクトリ（'' で無効）")
    a = ap.parse_args()

    THINK = True if a.think else (False if a.nothink else None)
    NUM_CTX = a.num_ctx; MAX_LINKS = a.max_links; MAX_ITEMS = a.max_items
    USE_COLLECTION = not a.no_collection

    goals = (load_plan(a.plan) if a.plan else []) + a.goal
    if not goals:
        print("no goals (--plan or --goal)", file=sys.stderr); sys.exit(2)
    appname = a.app
    if not appname:
        for g in goals:
            t, _ = parse_goal(g)
            if t.lower().startswith("launch "):
                appname = t.split()[1]; break
    if not appname:
        print("--app is required when the plan has no launch step", file=sys.stderr); sys.exit(2)

    app = find_app(appname)
    history = []
    newlog = not os.path.exists(a.log)
    logf = open(a.log, "a", newline="")
    log = csv.writer(logf)
    if newlog:
        log.writerow(["ts", "model", "step", "goal", "t_tree", "n_items", "n_shown", "prompt_chars", "t_llm_wall",
                      "prompt_tokens", "t_prompt_eval", "eval_tokens", "t_eval", "choice", "action",
                      "t_action", "t_wait", "t_step", "ok"])

    print(f"== goals ({len(goals)}):"); [print(f"   {i+1}. {g}") for i, g in enumerate(goals)]
    print(f"== model: {a.model}  think: {THINK}  ollama: {OLLAMA}  prune: {not a.no_prune} (links<={MAX_LINKS}, items<={MAX_ITEMS})  collection: {USE_COLLECTION}")
    gi, step = 0, 0
    t_run0 = time.time()
    while gi < len(goals) and step < a.max_steps:
        goal, cond = parse_goal(goals[gi])
        signal.alarm(STEP_TIMEOUT)
        t_step0 = time.time()

        # --- コードで行うステップ
        if goal.lower().startswith("launch "):
            cmd = goal[7:].strip()
            print(f"[launch] {cmd}")
            app, t = step_launch(cmd, appname, cond)
            ok = app is not None and (not cond or condition_met(cond, list_windows(app), [], []))
            print(f"      launched in {t*1000:.0f}ms, windows: {list_windows(app) if app else '-'}  {'OK' if ok else 'NOT MET'}")
            log.writerow([time.time(), a.model, step, gi + 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, f"launch:{cmd}", 0, f"{t:.3f}", f"{time.time()-t_step0:.3f}", int(ok)]); logf.flush()
            history.append(f"sub-goal {gi+1} completed: launched {cmd}" if ok else f"sub-goal {gi+1}: launch did not reach '{cond}'")
            gi += 1; continue
        if goal.lower().startswith("read"):
            print(f"[read] {goal}")
            start = None
            if goal.lower().startswith("read@"):              # read@歴史 ... → 見出し「歴史」から読む
                start = goal[5:].split()[0]
            t0 = time.time(); lines = []; polls = 0
            while time.time() - t0 < 10.0:          # タイトルが変わってから本文がツリーに載るまで少し遅れる
                polls += 1
                try:
                    windows, _, docs = list_windows(app), [], []
                    if cond and cond.startswith("doc:") and not condition_met(cond, windows, [], docs_of(app)):
                        time.sleep(0.2); continue
                    lines = step_read(app, start=start)
                    if lines:
                        break
                except Exception as e:
                    print(f"      (a11y hiccup reading: {str(e)[:60]} — retry)")
                time.sleep(0.3)
            t = time.time() - t0
            print(f"      read: {polls} polls")
            print(f"      read {len(lines)} lines in {t*1000:.0f}ms")
            for l in lines:
                print("      | " + l[:300])
            log.writerow([time.time(), a.model, step, gi + 1, f"{t:.3f}", 0, 0, 0, 0, 0, 0, 0, 0, 0, "read", 0, 0, f"{time.time()-t_step0:.3f}", int(bool(lines))]); logf.flush()
            history.append(f"sub-goal {gi+1} completed: read the page")
            gi += 1; continue

        if app is None:
            app = find_app(appname)
            if app is None:
                print(f"app not found: {appname}", file=sys.stderr); break

        # --- ツリー
        t0 = time.time()
        for attempt in range(3):
            try:
                windows, items_all, docs = collect(app); break
            except Exception as e:
                print(f"    (a11y hiccup reading the tree: {str(e)[:60]} — retry {attempt+1}/3)")
                time.sleep(0.5)
        else:
            print("!! tree unreadable 3 times — aborting this run"); break
        t_tree = time.time() - t0
        if condition_met(cond, windows, items_all, docs):
            print(f"    ✓ sub-goal {gi+1} condition met ({cond}) — advancing without asking the LLM")
            history.append(f"sub-goal {gi+1} completed: {goal}")
            gi += 1
            if gi >= len(goals):
                print("== DONE (all goals achieved by condition)")
            continue
        items, dropped = (items_all, 0) if a.no_prune else prune(items_all)
        step += 1
        choices_text, choices = build_choices(windows, items)

        goal_text = goal if len(goals) == 1 else f"(sub-goal {gi+1}/{len(goals)}) {goal}"
        note = "\n(The page is still loading.)" if "busy:" in docs else ""
        print(f"    tree {t_tree*1000:.0f}ms ({len(items_all)} items, {len(items)} shown) — asking {a.model} ...", flush=True)
        signal.alarm(0)                       # 番犬は AT-SPI の詰まり用。LLM の待ち（モデルの初回読み込みで 60s を超えることがある）は urllib の timeout に任せる
        ask = ask_claude if a.model.startswith("claude") else ask_ollama
        ans, t_llm, r, prompt = ask(a.model, goal_text, windows, history, choices_text, note)
        signal.alarm(STEP_TIMEOUT)
        if a.dump:
            os.makedirs(a.dump, exist_ok=True)
            with open(os.path.join(a.dump, f"{time.strftime('%H%M%S')}_step{step}.txt"), "w", encoding="utf-8") as f:
                f.write(prompt + "\n\n=== answer ===\n" + json.dumps(ans, ensure_ascii=False) + "\n")
        pe, ev = r.get("prompt_eval_duration", 0) / 1e9, r.get("eval_duration", 0) / 1e9
        idx = int(ans.get("choice", 0)); text = ans.get("text", "") or ""
        row = [time.time(), a.model, step, gi + 1, f"{t_tree:.3f}", len(items_all), len(items), len(prompt), f"{t_llm:.3f}",
               r.get("prompt_eval_count"), f"{pe:.3f}", r.get("eval_count"), f"{ev:.3f}", idx]
        if not (1 <= idx <= len(choices)):
            print(f"[{step}] invalid choice {idx} (only {len(choices)} actions listed)")
            history.append(f"you answered {idx} but only {len(choices)} actions exist — pick a number from the list")
            log.writerow(row + ["invalid", 0, 0, f"{time.time()-t_step0:.3f}", 0]); logf.flush()
            time.sleep(0.3); continue
        ch = choices[idx - 1]
        label = choices_text.splitlines()[idx - 1]
        print(f"[{step}] goal {gi+1}/{len(goals)} | tree {t_tree*1000:.0f}ms ({len(items_all)} items, {len(items)} shown, {len(prompt)} chars) | "
              f"llm {t_llm*1000:.0f}ms (prompt {r.get('prompt_eval_count')} tok {pe*1000:.0f}ms, gen {r.get('eval_count')} tok {ev*1000:.0f}ms) -> {label}"
              + (f"  text={text!r}" if ch.get('kind') in ('type', 'keytype') else ""))

        ok, action, t_action, t_wait = 1, ch["kind"], 0.0, 0.0
        if ch["kind"] == "done" and cond:
            # 条件付きのサブ目標では、LLM の DONE 宣言をコードで検証する（満たしていなければ進めない）
            print(f"      LLM says DONE but condition '{cond}' is not met — rejecting")
            history.append(f"you said DONE but sub-goal {gi+1} is NOT achieved yet — pick an action that achieves it (or WAIT if the page is still loading)")
            log.writerow(row + ["done-rejected", 0, 0, f"{time.time()-t_step0:.3f}", 0]); logf.flush()
            time.sleep(0.5); continue
        if ch["kind"] == "done":
            log.writerow(row + ["done", 0, 0, f"{time.time()-t_step0:.3f}", 1]); logf.flush()
            history.append(f"sub-goal {gi+1} completed: {goal}")
            gi += 1
            if gi >= len(goals):
                print("== DONE (all goals achieved)")
            continue
        if a.dry:
            continue
        before = None            # 操作前の画面の署名（条件を満たさなくても画面が変わって落ち着いたら待ちを打ち切る）
        if cond and ch["kind"] not in ("wait", "keytype"):   # keytype（URL 入力＝ページ遷移）は読み込み途中で打ち切ると再入力を繰り返すので条件を待ち切る
            try:
                if USE_COLLECTION and cond.split(":", 1)[0] in ("url", "heading", "doc"):
                    before = _sig(*quick_state(app))
                else:
                    before = _sig(windows, items_all, docs)
            except Exception:
                before = None
        try:
            t0 = time.time()
            if ch["kind"] == "click":
                _, an = do_click(ch["acc"]); action = f"click:{an}:{ch['name']}"
            elif ch["kind"] in ("type", "keytype"):
                how = do_type(ch, text, press_enter=(ch["kind"] == "keytype"))
                action = f"type:{how}:{ch['name']}"
            else:
                time.sleep(0.5); action = "wait"
            t_action = time.time() - t0
        except Exception as e:
            ok = 0; action = f"error:{e}"; print("   !! action failed:", e)
        if cond:
            t_wait, met = wait_until(app, cond, before=before)
            if not met:
                print(f"      (condition '{cond}' not met yet)")
        else:
            time.sleep(0.5)
        history.append(label.split("  (in")[0] + (f' with text "{text}"' if ch["kind"] in ("type", "keytype") else "") + (" [OK]" if ok else " [FAILED]"))
        t_total = time.time() - t_step0
        print(f"      action {t_action*1000:.0f}ms ({action[:50]}) | wait {t_wait*1000:.0f}ms | step total {t_total*1000:.0f}ms")
        log.writerow(row + [action, f"{t_action:.3f}", f"{t_wait:.3f}", f"{t_total:.3f}", ok])
        logf.flush()
    signal.alarm(0)
    print(f"== run total {time.time()-t_run0:.1f}s, {step} LLM steps, reached goal {gi}/{len(goals)}"
          + (f", anthropic tokens in={TOKENS['in']} out={TOKENS['out']}" if TOKENS["in"] else ""))
    for _ in range(3):
        try:
            time.sleep(0.3)
            print("== final windows:", "; ".join(list_windows(app)) if app else "-"); break
        except Exception:
            continue
    logf.close()

if __name__ == "__main__":
    main()
