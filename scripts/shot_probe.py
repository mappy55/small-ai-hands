#!/usr/bin/env python3
# --shot marks の確認: いまの Firefox の選択肢に札を描いたスクショを 1 枚作って保存する（モデルは呼ばない）
#   使い方: python3 scripts/shot_probe.py [out.png]      （東京駅の記事を開いた Firefox で）
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "smallhands"))
import loop as L
out = sys.argv[1] if len(sys.argv) > 1 else "logs/shot_probe.png"
app = L.find_app("firefox")
t0 = time.time()
windows, items_all, docs = L.collect(app)
items, _ = L.prune(items_all)
choices_text, choices = L.build_choices(windows, items)
t1 = time.time()
png = L.prepare_shot("marks", choices)
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
open(out, "wb").write(png)
print(f"tree {int((t1-t0)*1000)}ms, {len(items)} shown; shot {L.SHOT['ms']}ms, {L.SHOT['n_marks']} marks, {len(png)//1024} KB -> {out}")
print(choices_text[:600])
