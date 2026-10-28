#!/usr/bin/env python3
# いまの Firefox で、達成条件の判定に使う状態（ウィンドウ・アドレスバー・見出し・document）を表示する
#   使い方: python3 scripts/cu_judge_probe.py "heading:丸ノ内線"
import sys, os, traceback
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "smallhands"))
import loop as L
cond = sys.argv[1] if len(sys.argv) > 1 else ""
for i, app in enumerate(L.find_apps("firefox")):
    print(f"== app {i}: {app.name}  children={app.childCount}")
    try:
        windows, items, docs = L.quick_state(app)
        print("   windows:", windows)
        print("   address:", [it.get("text") for it in items if it.get("kind") == "keytype"])
        print("   docs:   ", docs)
        if cond:
            print("   condition", cond, "->", L.condition_met(cond, windows, items, docs))
    except Exception:
        traceback.print_exc()
