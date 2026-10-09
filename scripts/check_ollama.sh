#!/bin/bash
# ゲストからホストの Ollama に届くか確認（curl 不要）
python3 - <<'PY'
import urllib.request, json
try:
    with urllib.request.urlopen("http://10.0.2.2:11434/api/tags", timeout=5) as r:
        d = json.loads(r.read())
    print("OK:", ", ".join(m["name"] for m in d.get("models", [])))
except Exception as e:
    print("NG:", e)
    print("ホストの OLLAMA_HOST=0.0.0.0 設定と Ollama の再起動、ファイアウォールを確認")
PY
