#!/bin/bash
# 依頼文の一覧（plans/requests.txt）を上から順に「計画（Claude）→ 実行（ローカル LLM）」して、結果を logs/requests_summary.md にまとめる
#   使い方: bash scripts/run_requests.sh [runs_per_request] [model] [requests_file]
#   例:     bash scripts/run_requests.sh 2
#   必要: ANTHROPIC_API_KEY、ホストの Ollama、ydotoold
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNS=${1:-2}
MODEL=${2:-qwen3:4b-instruct}
REQS=${3:-plans/requests.txt}
cd "$ROOT"
STAMP=$(date +%Y%m%d-%H%M%S)
SUMMARY="logs/requests_summary_${STAMP}.md"
mkdir -p logs

echo "# requests run $STAMP  executor=$MODEL  runs=$RUNS" > "$SUMMARY"
n=0
while IFS= read -r line || [ -n "$line" ]; do
  req="${line#"${line%%[![:space:]]*}"}"       # 前後の空白を落とす
  [ -z "$req" ] && continue
  case "$req" in \#*) continue;; esac
  n=$((n+1))
  PLAN="plans/generated_req${n}_${STAMP}.txt"
  echo
  echo "################ request $n: $req"
  {
    echo
    echo "## $n. $req"
    echo
  } >> "$SUMMARY"
  # 一覧で回すときは毎回まっさらな状態から始めるので、いまの画面は計画層に渡さない（渡すと「もう開いている」前提の計画になる）
  if ! python3 smallhands/planner.py "$req" --out "$PLAN" --no-context; then
    echo "計画の生成に失敗（形式チェックに落ちたか API エラー）。次の依頼へ"
    echo "計画の生成に失敗" >> "$SUMMARY"
    [ -f "$PLAN" ] && { echo '```text' >> "$SUMMARY"; cat "$PLAN" >> "$SUMMARY"; echo '```' >> "$SUMMARY"; }
    continue
  fi
  { echo '```text'; cat "$PLAN"; echo '```'; echo; } >> "$SUMMARY"
  CLEAN_CMD='rm -f "$HOME"/memo.txt "$HOME"/Documents/memo.txt "$HOME"/Untitled*' bash scripts/run_batch.sh "$MODEL" "$RUNS" "$PLAN"
  TAG=$(echo "$MODEL" | tr ':/' '__')
  OUT="logs/generated_req${n}_${STAMP}_${TAG}.out"
  python3 - "$OUT" >> "$SUMMARY" <<'PY'
import sys, re
out = open(sys.argv[1], encoding="utf-8", errors="replace").read()
runs = re.findall(r"== run total ([\d.]+)s, (\d+) LLM steps, reached goal (\d+)/(\d+)", out)
reads = out.count("      | ")
aborted = out.count("watchdog")
if not runs:
    print(f"結果: run が完走していない（watchdog {aborted} 回）"); sys.exit()
ok = sum(1 for t, s, g, n in runs if g == n)
print(f"結果: {ok}/{len(runs)} 成功、所要 " + " / ".join(f"{float(t):.0f}s({s}手)" for t, s, g, n in runs)
      + (f"、watchdog {aborted} 回" if aborted else ""))
m = re.findall(r"      \| (.+)", out)
if m:
    print(f"読み取り例: {m[0][:80]}")
PY
done < "$REQS"
echo
echo "== まとめ: $SUMMARY"
cat "$SUMMARY"
