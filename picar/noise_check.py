#!/usr/bin/env python3
# 録音した wav の雑音の性質を見る: 20ms ごとの音量の分布と、突発的な雑音（スパイク）の間隔
#   arecord -D plughw:CARD=Device,DEV=0 -f S16_LE -r 16000 -c 1 -d 10 quiet.wav   （黙ったまま 10 秒）
#   python3 picar/noise_check.py quiet.wav
import sys, wave, array
w = wave.open(sys.argv[1]); rate = w.getframerate()
a = array.array("h", w.readframes(w.getnframes()))
n = rate // 50                                              # 20ms
frames = [a[i:i + n] for i in range(0, len(a) - n + 1, n)]
lv = [(sum(x * x for x in f) / n) ** 0.5 for f in frames]
s = sorted(lv)
pct = lambda p: s[min(len(s) - 1, int(len(s) * p / 100))]
med = pct(50)
print(f"{sys.argv[1]}: {len(a)/rate:.1f}s, {len(lv)} frames of 20ms")
print(f"  rms per frame: p10 {pct(10):.0f}  p50 {med:.0f}  p90 {pct(90):.0f}  p99 {pct(99):.0f}  max {s[-1]:.0f}   whole-file rms {(sum(x*x for x in a)/len(a))**0.5:.0f}")
spikes = [i for i, v in enumerate(lv) if v > max(med * 4, 200)]
# 連続したフレームは一つのスパイクにまとめる
starts = [i for k, i in enumerate(spikes) if k == 0 or i - spikes[k - 1] > 1]
print(f"  spikes (> 4x median): {len(spikes)} frames in {len(starts)} bursts")
if len(starts) > 2:
    gaps = sorted((starts[k] - starts[k - 1]) * 0.02 for k in range(1, len(starts)))
    print(f"  gap between bursts: min {gaps[0]:.2f}s  median {gaps[len(gaps)//2]:.2f}s  max {gaps[-1]:.2f}s")
    print("  burst times (s):", " ".join(f"{i*0.02:.2f}" for i in starts[:40]))
# 1 秒ごとの帯: 静かなフレームは '.', スパイクは '#'
for sec in range(0, len(lv), 50):
    print(f"  {sec//50:2d}s " + "".join("#" if v > max(med * 4, 200) else ("+" if v > med * 2 else ".") for v in lv[sec:sec + 50]))
