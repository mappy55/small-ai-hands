#!/usr/bin/env python3
# --shot marks の確認: いまの Firefox の選択肢に札を描いたスクショを 1 枚作って保存し、座標の対応を表示する（モデルは呼ばない）
#   使い方: python3 scripts/shot_probe.py [out.png]      （Firefox を最大化して記事を開いた状態で。端末は別の場所に）
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "smallhands"))
import pyatspi
import loop as L
out = sys.argv[1] if len(sys.argv) > 1 else "logs/shot_probe.png"
app = L.find_app("firefox")
# フレーム（窓）の矩形を DESKTOP / WINDOW / SCREEN の各座標系で表示
for j in range(app.childCount):
    w = app.getChildAtIndex(j)
    if w is None or not L._showing(w):
        continue
    c = w.queryComponent()
    for name, ct in (("DESKTOP", pyatspi.DESKTOP_COORDS), ("WINDOW", pyatspi.WINDOW_COORDS), ("SCREEN", getattr(pyatspi, "SCREEN_COORDS", pyatspi.DESKTOP_COORDS))):
        try:
            print(f"frame '{w.name[:40]}' {name}: {tuple(int(v) for v in c.getExtents(ct))}")
        except Exception as e:
            print(f"frame {name}: err {e}")
t0 = time.time()
windows, items_all, docs = L.collect(app)
items, _ = L.prune(items_all)
choices_text, choices = L.build_choices(windows, items)
t1 = time.time()
print("== choices with rects (DESKTOP coords):")
for i, ch in enumerate(choices):
    acc = ch.get("acc")
    if acc is None:
        continue
    r = L._rect(acc)
    try:
        rw = tuple(int(v) for v in acc.queryComponent().getExtents(pyatspi.WINDOW_COORDS))
    except Exception:
        rw = None
    print(f"  {i+1:>2}. {ch['kind']:<7} {ch['role']:<12} {ch['name'][:30]!r:<34} desktop={r} window={rw}")
png = L.prepare_shot("marks", choices)
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
open(out, "wb").write(png)
print(f"tree {int((t1-t0)*1000)}ms, {len(items)} shown; shot {L.SHOT['ms']}ms, {L.SHOT['n_marks']} marks, {len(png)//1024} KB -> {out}")
