#!/usr/bin/env python3
# クリック位置の確認: 今の画面で (x, y) を押し、押す前後の AT-SPI のフォーカス要素とウィンドウ名を表示する
#   使い方: python3 scripts/cu_click_probe.py 660 427     （東京駅の記事を開いた Firefox で「丸ノ内線」の位置）
import sys, os, time, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "smallhands"))
import loop as L
x, y = int(sys.argv[1]), int(sys.argv[2])
app = L.find_app("firefox")
def state():
    try:
        wins = L.list_windows(app); f = L._focused(app, tries=1)
        return f"windows={wins} focused={(f.getRoleName()+' '+repr(f.name)) if f else None}"
    except Exception as e:
        return f"(err {e})"
print("before:", state())
env = dict(os.environ, YDOTOOL_SOCKET=L.YDOTOOL_SOCKET)
subprocess.run(["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)], env=env, check=True)
time.sleep(0.3)
subprocess.run(["ydotool", "click", "0xC0"], env=env, check=True, stdout=subprocess.DEVNULL)
time.sleep(2.0)
print("after: ", state())
