#!/bin/bash
# 依頼文一つで最後まで: 計画層（Claude）→ 実行層（ローカル LLM）
#   使い方: bash scripts/run_request.sh "Firefox で Wikipedia の東京駅を調べて、歴史の節を読んで" [model] [runs]
#   必要: 環境変数 ANTHROPIC_API_KEY（計画層）、ホストの Ollama（実行層）、ydotoold
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REQ=${1:?"依頼文を渡してください"}
MODEL=${2:-qwen3:4b-instruct}
RUNS=${3:-1}
cd "$ROOT"
STAMP=$(date +%Y%m%d-%H%M%S)
PLAN="plans/generated_${STAMP}.txt"

echo "== planning with Claude ..."
python3 smallhands/planner.py "$REQ" --out "$PLAN" || { echo "計画の生成に失敗しました（上のメッセージを確認）"; exit 1; }
echo
echo "== executing $PLAN with $MODEL x $RUNS"
bash scripts/run_batch.sh "$MODEL" "$RUNS" "$PLAN"
