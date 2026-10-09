#!/bin/bash
# 計画ファイルを N 回まわす。  使い方: bash scripts/run_batch.sh [model] [runs] [plan]
#   例: bash scripts/run_batch.sh qwen3:4b-instruct 3 plans/plan_tokyo_history.txt
#   環境変数: MAX_LINKS / MAX_ITEMS / NOPRUNE=1（間引き）、GAP=秒（run の間隔）
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL=${1:-qwen3:4b-instruct}
RUNS=${2:-3}
PLAN=${3:-plans/plan_tokyo_station.txt}
TAG=$(echo "$MODEL" | tr ':/' '__')
PLANTAG=$(basename "$PLAN" .txt | sed 's/^plan_//')
LOG="$ROOT/logs/${PLANTAG}_${TAG}.csv"
OUT="$ROOT/logs/${PLANTAG}_${TAG}.out"
cd "$ROOT"

# ydotoold（文字入力に使う。scripts/setup_ydotool.sh で一度だけサービス化する）
export YDOTOOL_SOCKET=${YDOTOOL_SOCKET:-/tmp/.ydotool_socket}
if [ ! -S "$YDOTOOL_SOCKET" ]; then
  echo "ydotoold が動いていません: bash scripts/setup_ydotool.sh を先に実行してください"; exit 1
fi

# Firefox 用の専用プロファイル（初回画面・既定ブラウザ確認・セッション復元・翻訳パネルを切る）
PROF="$HOME/ffprof"
mkdir -p "$PROF"
cat > "$PROF/user.js" <<'PREFS'
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage", "about:blank");
user_pref("browser.startup.page", 0);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("datareporting.policy.firstRunURL", "");
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.urlbar.suggest.topsites", false);
user_pref("browser.newtabpage.enabled", false);
user_pref("browser.translations.automaticallyPopup", false);
user_pref("browser.translations.enable", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
PREFS

ARGS=(--dump "$ROOT/logs/prompts")
[ -n "${MAX_LINKS:-}" ] && ARGS+=(--max-links "$MAX_LINKS")
[ -n "${MAX_ITEMS:-}" ] && ARGS+=(--max-items "$MAX_ITEMS")
[ -n "${NOPRUNE:-}" ] && ARGS+=(--no-prune)

echo "== $MODEL x $RUNS  plan=$PLAN  log=$LOG" | tee -a "$OUT"
for i in $(seq 1 "$RUNS"); do
  pkill -TERM -x firefox 2>/dev/null; pkill -TERM -f 'firefox-bin' 2>/dev/null
  for t in $(seq 1 40); do pgrep -x firefox >/dev/null || pgrep -f firefox-bin >/dev/null || break; sleep 0.25; done
  pgrep -x firefox >/dev/null && { echo "(firefox still alive, killing)"; pkill -KILL -x firefox; sleep 1; }
  sleep "${GAP:-5}"     # a11y レジストリを休ませる
  echo "---- run $i / $RUNS  $(date +%T)" | tee -a "$OUT"
  python3 -u smallhands/loop.py --plan "$PLAN" --model "$MODEL" --log "$LOG" "${ARGS[@]}" 2>&1 | tee -a "$OUT"
done
echo "== done. ログ: $LOG / $OUT  （Firefox は開いたまま。次の run の冒頭で閉じます）"
