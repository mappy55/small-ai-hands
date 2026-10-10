#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
planner.py — 計画層。依頼文を Claude（Anthropic API）に渡して、loop.py が読む計画ファイルを書かせる

  python3 smallhands/planner.py "Firefox で Wikipedia の東京駅を調べて、歴史の節を読んで" --out plans/generated.txt
  python3 smallhands/planner.py "..." --dry        # プロンプトを表示するだけ（API は呼ばない）

必要なもの: 環境変数 ANTHROPIC_API_KEY（リポジトリには入れない）。モデルは ANTHROPIC_MODEL か --model で変更可。
出力の計画ファイルは loop.py にそのまま渡せる（1 行 1 サブ目標、'目標文||達成条件'、launch / read 行はコードが実行）。
"""
import sys, os, json, time, argparse, urllib.request, urllib.error

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# 計画層に渡す「約束」。実行層（loop.py）の仕様をそのまま文章にしたもの。
SYSTEM = r"""You write plan files for a small GUI agent that operates Linux desktop apps (GNOME / Wayland) through the AT-SPI2 accessibility tree.

## How the executor works (read carefully — the plan must fit it)
- The plan is processed top to bottom, one sub-goal per line. Each line is:  <goal text>||<condition>
- For a normal line, a small local LLM (4B) sees ONLY: the goal text, the visible window names, the history of actions taken so far, and a numbered list of possible actions built from the accessibility tree (click <role> '<name>', type into <role> '<name>', plus DONE and WAIT). It answers one number (and the text to type). It does not know the overall task, cannot see the screen, and cannot think ahead. So each sub-goal must be ONE simple action that is possible from what is visible at that moment.
- After the action, code waits until the <condition> is met (up to ~12 s), then moves to the next line. If the small model says DONE while the condition is not met, code rejects it and asks again. Conditions are the ONLY way progress is verified, so every normal line needs one.
- Two kinds of lines are executed by code, not by the small model:
    launch <shell command>||<condition>      starts an app and waits for the condition (use this as the first line when the app is not running)
    read||doc:<page name>                     prints the first heading and paragraphs of the current web page (Firefox)
    read@<heading>||doc:<page name>           prints the section that starts at the heading containing <heading>

## Condition vocabulary (all matches are case-insensitive substrings)
  window:<text>      a visible window whose name contains <text> exists          e.g. window:mozilla firefox
  nowindow:<text>    no visible window whose name contains <text>
  title:<text>       the main window title contains <text>                         (Firefox: "<page title> — Mozilla Firefox"; becomes true BEFORE the page body is loaded — avoid for web pages)
  heading:<text>     a heading containing <text> is in the visible area of the page  (Firefox only; preferred for "page has loaded")
  url:<text>         the address bar contains <text>                                  e.g. url:#歴史  (good for in-page jumps)
  doc:<text>         the current web page's name contains <text>                      (Firefox only)
  text:<text>        some editable field contains <text>
  text@<window>:<text>   an editable field inside the window named <window> contains <text>   e.g. text@save as:qwen_test.txt

## Facts about the environment
- Firefox is started with:  launch firefox --profile $HOME/ffprof about:blank||window:mozilla firefox
  The address bar appears to the small model as:  type into combo box 'Search with Google or enter address' and press Enter
  The executor types the text with a real keyboard (ydotool), which can only type ASCII reliably. So any non-ASCII part of a URL MUST be percent-encoded (UTF-8), e.g. 東京駅 -> %E6%9D%B1%E4%BA%AC%E9%A7%85. Write the URL without "https://".
- On Wikipedia (ja) at this window size the table of contents is collapsed behind a button named '目次の表示・非表示を切り替え'. The small model usually opens it by itself when the goal mentions the table of contents, so one sub-goal "Click the link 'X' in the table of contents" is enough; do not add a separate sub-goal for the button.
- gedit (GTK text editor) is started with:  launch gedit||window:untitled document    Its editor is 'text field'; Save opens a 'Save As' dialog; the file name field is labelled 'Name'.
- Write goal texts in English (the small model's prompt is English), but keep on-screen names such as link labels and page titles exactly as they appear (Japanese is fine inside quotes).
- Keep plans short: 2-5 lines. One action per line. Do not invent UI elements you are not sure exist; prefer navigating by URL over searching.

## Output
Output ONLY the plan lines, one per line, no numbering, no commentary, no code fences. Lines starting with # are comments and are allowed (at most one, at the top, restating the request in Japanese)."""

def ask_claude(request_text, model, context=""):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY が設定されていません（export ANTHROPIC_API_KEY=sk-ant-...）")
    user = f"Request (from the user, in Japanese): {request_text}\n"
    if context:
        user += f"\nCurrent state of the desktop (visible apps and windows):\n{context}\n"
    user += "\nWrite the plan file."
    body = {"model": model, "max_tokens": 1024, "temperature": 0,
            "system": SYSTEM, "messages": [{"role": "user", "content": user}]}
    headers = {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
    t0 = time.time()
    for attempt in range(2):
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                r = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            # 新しいモデルで受け付けないパラメータがあれば外して 1 回だけやり直す
            if attempt == 0 and e.code == 400 and "temperature" in detail and "temperature" in body:
                print(f"(API: {detail.strip()[:160]} — retrying without temperature)", file=sys.stderr)
                body.pop("temperature"); continue
            sys.exit(f"API error {e.code}: {detail.strip()[:600]}")
    text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    usage = r.get("usage", {})
    return text.strip(), time.time() - t0, usage, user

def list_models():
    """使えるモデル ID を API に聞く（/v1/models）"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY が設定されていません")
    req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=100",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read())
    for m in d.get("data", []):
        print(f"{m.get('id'):40s} {m.get('display_name','')}  ({m.get('created_at','')[:10]})")

def desktop_context():
    """いま見えているアプリとウィンドウ名（計画層に渡す。pyatspi が無い環境では空）"""
    try:
        import pyatspi
    except Exception:
        return ""
    lines = []
    try:
        desktop = pyatspi.Registry.getDesktop(0)
        for i in range(desktop.childCount):
            app = desktop.getChildAtIndex(i)
            if app is None or not app.name or app.childCount == 0:
                continue
            wins = []
            for j in range(app.childCount):
                w = app.getChildAtIndex(j)
                try:
                    if w is not None and w.getState().contains(pyatspi.STATE_SHOWING):
                        wins.append(f"{w.getRoleName()} '{w.name}'")
                except Exception:
                    pass
            if wins:
                lines.append(f"- {app.name}: " + "; ".join(wins))
    except Exception:
        pass
    return "\n".join(lines) if lines else "(no visible windows)"

def validate(plan):
    """最低限の形式チェック: 通常行には || と条件があるか、条件の種類は語彙にあるか"""
    kinds = {"window", "nowindow", "title", "heading", "url", "doc", "text"}
    problems = []
    for n, line in enumerate(plan.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "||" not in s:
            problems.append(f"line {n}: no '||' condition"); continue
        goal, cond = s.split("||", 1)
        kind = cond.split(":", 1)[0].split("@", 1)[0].strip().lower()
        if kind not in kinds:
            problems.append(f"line {n}: unknown condition kind '{kind}'")
        if not goal.strip():
            problems.append(f"line {n}: empty goal")
    return problems

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("request", nargs="?", help="依頼文（日本語でよい）")
    ap.add_argument("--list-models", action="store_true", help="使えるモデル ID を表示して終了")
    ap.add_argument("--out", default="plans/generated.txt")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-context", action="store_true", help="いまのウィンドウ一覧を渡さない")
    ap.add_argument("--dry", action="store_true", help="プロンプトを表示するだけ")
    a = ap.parse_args()
    if a.list_models:
        list_models(); return
    if not a.request:
        ap.error("依頼文を渡してください")

    context = "" if a.no_context else desktop_context()
    if a.dry:
        print("=== system ===\n" + SYSTEM + "\n\n=== user ===\nRequest: " + a.request + ("\n\n" + context if context else ""))
        return
    plan, t, usage, _ = ask_claude(a.request, a.model, context)
    header = (f"# request: {a.request}\n"
              f"# planner: {a.model}  {time.strftime('%Y-%m-%d %H:%M')}  {t:.1f}s  "
              f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} tok\n")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(header + plan + "\n")
    print(header + plan)
    probs = validate(plan)
    if probs:
        print("\n!! plan has problems:\n  " + "\n  ".join(probs), file=sys.stderr)
        sys.exit(3)
    print(f"\n== written to {a.out}")

if __name__ == "__main__":
    main()
