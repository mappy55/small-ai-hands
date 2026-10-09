#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atspi_dump.py — Ubuntu ゲスト内で実行。AT-SPI2 経由でアクティブウィンドウ（または
指定アプリ）の UI ツリーを、role / name / 座標 / 状態付きの JSON として出力する。

準備（ゲスト側、1回だけ）:
    sudo apt install -y python3-pyatspi accerciser
    gsettings set org.gnome.desktop.interface toolkit-accessibility true
    # ログアウト→ログイン（または対象アプリを起動し直す）

使い方:
    python3 atspi_dump.py                 # アクティブウィンドウのツリー
    python3 atspi_dump.py --app gedit     # アプリ名で指定（部分一致、大文字小文字無視）
    python3 atspi_dump.py --all           # デスクトップ上の全アプリ名を列挙
    python3 atspi_dump.py --flat          # 操作可能な要素だけを番号付き一覧で出力（LLM向け）
"""
import sys, json, argparse
import pyatspi

# LLM に見せる価値のある role（操作対象 or 状態を表すもの）
ACTIONABLE = {
    "button", "push button", "toggle button", "menu button", "check box", "radio button", "menu item",
    "check menu item", "radio menu item", "menu", "text", "entry", "password text",
    "combo box", "list item", "tree item", "page tab", "link", "slider", "spin button",
    "table cell", "tool bar", "dialog", "frame", "window", "label", "heading",
}

def rect(acc):
    try:
        comp = acc.queryComponent()
        x, y, w, h = comp.getExtents(pyatspi.DESKTOP_COORDS)
        return [int(x), int(y), int(w), int(h)]
    except Exception:
        return None

def states(acc):
    try:
        ss = acc.getState().getStates()
        names = [pyatspi.stateToString(s) for s in ss]
        keep = {"active", "checked", "enabled", "focused", "selected", "sensitive",
                "showing", "visible", "editable", "expanded", "pressed"}
        return sorted(n for n in names if n in keep)
    except Exception:
        return []

def text_of(acc):
    try:
        t = acc.queryText()
        s = t.getText(0, min(t.characterCount, 200))
        return s
    except Exception:
        return None

def node(acc, depth, max_depth):
    if acc is None or depth > max_depth:
        return None
    try:
        role = acc.getRoleName()
        name = acc.name or ""
    except Exception:
        return None
    n = {"role": role, "name": name, "rect": rect(acc), "states": states(acc)}
    try:
        d = acc.description or ""
        if d: n["desc"] = d
    except Exception:
        pass
    if role in ("text", "entry", "password text"):
        n["text"] = text_of(acc)
    kids = []
    try:
        for i in range(acc.childCount):
            c = node(acc.getChildAtIndex(i), depth + 1, max_depth)
            if c:
                kids.append(c)
    except Exception:
        pass
    if kids:
        n["children"] = kids
    return n

def flatten(n, out, path=""):
    """showing かつ操作系 role の要素だけを平坦化"""
    if n is None:
        return
    st = set(n.get("states", []))
    r = n.get("rect")
    visible = "showing" in st and r and r[2] > 0 and r[3] > 0
    unnamed_ok = ("text", "entry", "dialog", "frame", "window", "button", "toggle button", "menu button")
    if visible and n["role"] in ACTIONABLE and (n["name"] or n.get("desc") or n["role"] in unnamed_ok):
        out.append({"role": n["role"], "name": n["name"] or n.get("desc", ""), "rect": r,
                    "states": [s for s in n["states"] if s in ("checked", "focused", "selected", "editable", "expanded", "pressed")],
                    "text": n.get("text")})
    for c in n.get("children", []):
        flatten(c, out, path)

def active_window(desktop):
    for i in range(desktop.childCount):
        app = desktop.getChildAtIndex(i)
        if app is None:
            continue
        for j in range(app.childCount):
            w = app.getChildAtIndex(j)
            try:
                if w.getState().contains(pyatspi.STATE_ACTIVE):
                    return app, w
            except Exception:
                pass
    return None, None

def find_app(desktop, needle):
    needle = needle.lower()
    for i in range(desktop.childCount):
        app = desktop.getChildAtIndex(i)
        if app is not None and needle in (app.name or "").lower():
            return app
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", help="アプリ名（部分一致）")
    ap.add_argument("--all", action="store_true", help="全アプリ名を列挙")
    ap.add_argument("--flat", action="store_true", help="操作可能要素の番号付き一覧")
    ap.add_argument("--depth", type=int, default=25)
    a = ap.parse_args()

    desktop = pyatspi.Registry.getDesktop(0)

    if a.all:
        names = []
        for i in range(desktop.childCount):
            app = desktop.getChildAtIndex(i)
            if app is not None:
                names.append({"name": app.name, "windows": app.childCount})
        print(json.dumps(names, ensure_ascii=False, indent=1))
        return

    if a.app:
        app = find_app(desktop, a.app)
        if app is None:
            print(json.dumps({"error": f"app not found: {a.app}"}), file=sys.stderr)
            sys.exit(2)
        # 最初の showing なウィンドウ（なければ先頭）
        win = None
        for j in range(app.childCount):
            w = app.getChildAtIndex(j)
            if w is not None and w.getState().contains(pyatspi.STATE_SHOWING):
                win = w; break
        if win is None and app.childCount:
            win = app.getChildAtIndex(0)
    else:
        app, win = active_window(desktop)

    if win is None:
        print(json.dumps({"error": "no window"}), file=sys.stderr)
        sys.exit(3)

    tree = node(win, 0, a.depth)
    tree["app"] = app.name if app else None

    if a.flat:
        items = []
        flatten(tree, items)
        for k, it in enumerate(items, 1):
            it["id"] = k
        print(json.dumps({"app": tree["app"], "window": tree["name"], "items": items}, ensure_ascii=False, indent=1))
    else:
        print(json.dumps(tree, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
