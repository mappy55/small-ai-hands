#!/bin/bash
# スクショ方式（Claude computer use）で同じ依頼を回す。木を読む loop.py との比較用
#   使い方: bash scripts/run_cu.sh "依頼文" "達成条件" [model] [runs] [tag]
#   例:     bash scripts/run_cu.sh "Firefox で Wikipedia の東京駅を開いて、歴史の節を読んで" "url:#歴史" claude-sonnet-5-5 3 tokyo_history
#   必要: ANTHROPIC_API_KEY、ydotoold、python3-pil（スクショは GNOME の Shift+Print を ydotool で押して撮る）
#   VirtualBox のマウス統合は OFF にしておく（Host+I）。画面全体を撮るので、Firefox 以外の窓は手前に出さない
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REQ=${1:?"依頼文を渡してください"}
COND=${2:-}
MODEL=${3:-claude-sonnet-5-5}
RUNS=${4:-1}
TAG=${5:-cu}
cd "$ROOT"
[ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "ANTHROPIC_API_KEY が設定されていません"; exit 1; }
export YDOTOOL_SOCKET=${YDOTOOL_SOCKET:-/tmp/.ydotool_socket}
[ -S "$YDOTOOL_SOCKET" ] || { echo "ydotoold が動いていません: bash scripts/setup_ydotool.sh"; exit 1; }
MTAG=$(echo "$MODEL" | tr ':/' '__')
OUT="$ROOT/logs/cu_${TAG}_${MTAG}.out"
LOG="$ROOT/logs/cu_${TAG}_${MTAG}.jsonl"
mkdir -p logs
# Firefox のプロファイルは run_batch.sh と同じもの（user.js はそちらで作られている）
# GNOME の通知バナー（「Screenshot captured」）が毎回 URL バーの上に被さるので、run の間は切る
BANNERS=$(gsettings get org.gnome.desktop.notifications show-banners 2>/dev/null || echo true)
gsettings set org.gnome.desktop.notifications show-banners false 2>/dev/null
# ydotool の絶対座標移動は中身が相対移動なので、GNOME のポインタ加速が掛かるとクリック位置がずれる → run の間は加速を切る
ACCEL=$(gsettings get org.gnome.desktop.peripherals.mouse accel-profile 2>/dev/null || echo "'default'")
gsettings set org.gnome.desktop.peripherals.mouse accel-profile 'flat' 2>/dev/null
trap 'gsettings set org.gnome.desktop.notifications show-banners "$BANNERS" 2>/dev/null; gsettings set org.gnome.desktop.peripherals.mouse accel-profile "$ACCEL" 2>/dev/null' EXIT
echo "== computer use: $MODEL x $RUNS  req=$REQ  cond=$COND" | tee -a "$OUT"
for i in $(seq 1 "$RUNS"); do
  pkill -TERM -x firefox 2>/dev/null; pkill -TERM -f 'firefox-bin' 2>/dev/null
  for t in $(seq 1 40); do pgrep -x firefox >/dev/null || break; sleep 0.25; done
  pgrep -x firefox >/dev/null && { pkill -KILL -x firefox; sleep 1; }
  sleep "${GAP:-5}"
  echo "---- run $i / $RUNS  $(date +%T)" | tee -a "$OUT"
  python3 -u smallhands/cu_loop.py "$REQ" --cond "$COND" --model "$MODEL" --launch --log "$LOG" \
    --save-shots "$ROOT/logs/cu_shots/${TAG}_run$i" ${CU_ARGS:-} 2>&1 | tee -a "$OUT"
done
echo "== done. ログ: $OUT / $LOG  スクショ: logs/cu_shots/"
