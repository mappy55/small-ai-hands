#!/bin/bash
# computer use（スクショ方式）の準備確認: セッション種別、画面サイズ、スクショが撮れるか、ydotool でマウスを動かせるか
#   使い方: bash scripts/cu_probe.sh
echo "== session: ${XDG_SESSION_TYPE:-?}   desktop: ${XDG_CURRENT_DESKTOP:-?}"
echo "== tools:"; for t in gnome-screenshot grim spectacle import scrot ydotool python3; do printf "   %-17s %s\n" "$t" "$(command -v $t || echo '-')"; done
python3 -c "import PIL; print('   PIL', PIL.__version__)" 2>/dev/null || echo "   PIL -  (sudo apt install python3-pil)"
OUT=/tmp/cu_probe.png; rm -f "$OUT"
echo "== screenshot:"
if command -v gnome-screenshot >/dev/null; then
  gnome-screenshot -f "$OUT" 2>&1 | sed 's/^/   /'
fi
if [ ! -s "$OUT" ]; then
  # GNOME Shell の D-Bus（許可されたアプリ以外は拒否されることがある）
  gdbus call --session --dest org.gnome.Shell.Screenshot --object-path /org/gnome/Shell/Screenshot \
    --method org.gnome.Shell.Screenshot.Screenshot false false "$OUT" 2>&1 | sed 's/^/   gdbus: /'
fi
if [ -s "$OUT" ]; then
  python3 -c "from PIL import Image; im=Image.open('$OUT'); print('   OK', im.size, im.mode)" 2>/dev/null || ls -la "$OUT" | sed 's/^/   /'
else
  echo "   NG: スクショが撮れない（Xorg セッションに切り替えて scrot/import を使うか、方法を変える）"
fi
echo "== mouse (ydotool):"
export YDOTOOL_SOCKET=${YDOTOOL_SOCKET:-/tmp/.ydotool_socket}
ydotool mousemove --absolute -x 200 -y 150 2>&1 | sed 's/^/   /' && echo "   moved to (200,150) — マウスカーソルが左上の方に動いていれば OK"
echo "== done"
