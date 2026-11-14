#!/usr/bin/env python3
"""頭（Ollama）の時間の内訳を条件を変えて測る。車もマイクも使わない。Pi でも PC でも動く。

知りたいこと: 指示文は毎回同じなのに、毎回 0.5 秒かけて読んでいる。前回読んだ分の使い回し（キャッシュ）が
効いていないのか、効いた上でこの時間なのか。答えの形の指定（format）が関係しているのか。

  python3 picar/head_bench.py                  # gemma3:4b
  python3 picar/head_bench.py --model gemma3:4b --n 4
"""
import argparse, json, os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import talk

QS = ["前に進んで", "バックして", "右に曲がって", "左に2回曲がって", "こんにちは", "3回バックして"]

def call(model, system, q, fmt=None, extra=None):
    body = {"model": model, "stream": False, "options": {"temperature": 0, "num_ctx": 4096},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": q}]}
    if fmt is not None:
        body["format"] = fmt
    body.update(extra or {})
    req = urllib.request.Request(f"{talk.OLLAMA}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        r = json.loads(resp.read())
    wall = time.time() - t0
    ns = lambda k: r.get(k, 0) / 1e9
    cached = r.get("prompt_eval_cached_count")
    out = r.get("message", {}).get("content", "").replace("\n", " ")[:40]
    print(f"   wall {wall:.2f}s | load {ns('load_duration'):.2f} | prompt {r.get('prompt_eval_count', 0):>4} tok"
          + (f" (cached {cached})" if cached is not None else "")
          + f" {ns('prompt_eval_duration'):.3f}s | gen {r.get('eval_count', 0):>3} tok {ns('eval_duration'):.3f}s | {q!r} -> {out}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma3:4b")
    ap.add_argument("--n", type=int, default=4)
    a = ap.parse_args()
    print(f"== {a.model} @ {talk.OLLAMA}")
    print("-- 準備（モデルの読み込み）")
    call(a.model, talk.SYSTEM_CODE, "こんにちは", talk.SCHEMA_CODE)
    print("-- A. いまの talk.py と同じ（記号の指示文＋答えの形を指定）。質問は毎回変える")
    for q in QS[:a.n]:
        call(a.model, talk.SYSTEM_CODE, q, talk.SCHEMA_CODE)
    print("-- B. 同じ質問を続けて 3 回（全部が前回と同じ。使い回しが効くなら 2 回目から prompt の時間がほぼ 0 になるはず）")
    for _ in range(3):
        call(a.model, talk.SYSTEM_CODE, "前に進んで", talk.SCHEMA_CODE)
    print("-- C. 答えの形の指定なし（format を外す）。質問は毎回変える")
    for q in QS[:a.n]:
        call(a.model, talk.SYSTEM_CODE, q, None)
    print("-- D. 答えの形を「JSON ならよい」だけにする（format: json）")
    for q in QS[:a.n]:
        call(a.model, talk.SYSTEM_CODE, q, "json")
    print("-- E. 以前の長い指示文（動作・回数・返事の文）＋答えの形を指定")
    for q in QS[:a.n]:
        call(a.model, talk.SYSTEM, q, talk.SCHEMA)
    print("-- F. A と同じ条件をもう一度（順番による違いがないかの確認）")
    for q in QS[:a.n]:
        call(a.model, talk.SYSTEM_CODE, q, talk.SCHEMA_CODE)

if __name__ == "__main__":
    main()
