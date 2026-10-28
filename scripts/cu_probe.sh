#!/bin/bash
# computer use（スクショ方式）の準備確認: セッション種別、画面サイズ、スクショが撮れるか、ydotool でマウスを動かせるか
#   使い方: bash scripts/cu_probe.sh
echo "== session: ${XDG_SESSION_TYPE:-?}   desktop: ${XDG_CURRENT_DESKTOP:-?}"
echo "== tools:"; for t in gnome-screenshot grim spectacle import scrot ydotool python3; do printf "   %-17s %s\n" "$t" "$(command -v $t || echo '-')"; done
python3 -c "import PIL; print('   PIL', PIL.__version__)" 2>/dev/null || echo "   PIL -  (sudo apt install python3-pil)"
OUT=/tmp/cu_probe.png; rm -f "$OUT"
echo "== screenshot:"
# gnome-screenshot は GNOME 41 以降の Wayland では撮れず、エラーダイアログを出して戻ってこないので飛ばす（SKIP_GS=0 で試す）
if [ "${SKIP_GS:-1}" = 0 ] && command -v gnome-screenshot >/dev/null; then
  timeout 5 gnome-screenshot -f "$OUT" 2>&1 | sed 's/^/   /'
fi
if [ ! -s "$OUT" ]; then
  # GNOME Shell の D-Bus（許可されたアプリ以外は拒否されることがある）
  echo "   (gnome-screenshot NG → D-Bus を試す)"
  timeout 10 gdbus call --session --dest org.gnome.Shell.Screenshot --object-path /org/gnome/Shell/Screenshot \
    --method org.gnome.Shell.Screenshot.Screenshot false false "$OUT" 2>&1 | sed 's/^/   gdbus: /'
  echo "   (D-Bus 終了)"
fi
if [ -s "$OUT" ]; then
  python3 -c "from PIL import Image; im=Image.open('$OUT'); print('   OK', im.size, im.mode)" 2>/dev/null || ls -la "$OUT" | sed 's/^/   /'
else
  echo "   NG: gnome-screenshot / D-Bus では撮れない → GNOME Shell のショートカット（Shift+Print = 全画面を保存）を ydotool で押して試す"
  export YDOTOOL_SOCKET=${YDOTOOL_SOCKET:-/tmp/.ydotool_socket}
  PICS=$(xdg-user-dir PICTURES 2>/dev/null || echo "$HOME/Pictures")
  SDIR=""; for d in "$PICS/Screenshots" "$PICS/スクリーンショット"; do [ -d "$d" ] && SDIR="$d"; done
  [ -z "$SDIR" ] && SDIR="$PICS/Screenshots"
  echo "   保存先の候補: $SDIR"
  BEFORE=$(ls -1 "$SDIR" 2>/dev/null | wc -l)
  echo "   Shift+Print を押します（シャッター音が鳴れば撮れている）"
  timeout 10 ydotool key 42:1 99:1 99:0 42:0      # shift+print
  sleep 1.5
  AFTER=$(ls -1 "$SDIR" 2>/dev/null | wc -l)
  NEW=$(ls -t "$SDIR"/*.png 2>/dev/null | head -1)
  echo "   $SDIR: before=$BEFORE after=$AFTER newest=${NEW:-なし}"
  if [ "$AFTER" -gt "$BEFORE" ] && [ -n "$NEW" ]; then
    python3 -c "from PIL import Image; im=Image.open('$NEW'); print('   OK via Shift+Print:', '$NEW', im.size)"
  else
    echo "   NG: Shift+Print でも保存されない（設定 → キーボード → ショートカット → スクリーンショットの割り当てを確認）"
    echo "   最後の手: ログイン画面で「Ubuntu on Xorg」を選び、sudo apt install scrot で撮る"
  fi
fi
echo "== mouse (ydotool):"
export YDOTOOL_SOCKET=${YDOTOOL_SOCKET:-/tmp/.ydotool_socket}
ydotool mousemove --absolute -x 200 -y 150 2>&1 | sed 's/^/   /' && echo "   moved to (200,150) — マウスカーソルが左上の方に動いていれば OK"
echo "== done"
