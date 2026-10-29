#!/bin/bash
# ゲストからホストの Ollama に届くか、モデルが答えるかを確認（curl 不要）
#   使い方: bash scripts/check_ollama.sh [model]
MODEL=${1:-qwen3:4b-instruct}
python3 - "$MODEL" <<'PY'
import sys, time, json, urllib.request, os
URL = os.environ.get("OLLAMA_URL", "http://10.0.2.2:11434")
model = sys.argv[1]
try:
    with urllib.request.urlopen(f"{URL}/api/tags", timeout=5) as r:
        d = json.loads(r.read())
    print("OK:", ", ".join(m["name"] for m in d.get("models", [])))
except Exception as e:
    print("NG:", e); print("ホストの OLLAMA_HOST=0.0.0.0 設定と Ollama の再起動、ファイアウォールを確認"); sys.exit(1)
ctx = int(os.environ.get("NUM_CTX", "8192"))
body = {"model": model, "stream": False, "options": {"num_ctx": ctx, "temperature": 0},
        "format": {"type": "object", "properties": {"choice": {"type": "integer"}, "text": {"type": "string"}}, "required": ["choice", "text"]},
        "messages": [{"role": "user", "content": "Reply with JSON {\"choice\": 1, \"text\": \"ok\"}."}]}
if os.environ.get("NOTHINK"):
    body["think"] = False
req = urllib.request.Request(f"{URL}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    print(f"chat {model}: {time.time()-t0:.1f}s  load {d.get('load_duration',0)/1e9:.1f}s  "
          f"prompt {d.get('prompt_eval_count')} tok {d.get('prompt_eval_duration',0)/1e9:.2f}s  "
          f"gen {d.get('eval_count')} tok {d.get('eval_duration',0)/1e9:.2f}s  -> {d['message']['content'][:40]!r}")
except Exception as e:
    print(f"chat {model}: FAILED after {time.time()-t0:.1f}s: {e}")
PY
