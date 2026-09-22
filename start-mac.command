#!/bin/bash
# ============================================================
#  Flat Studio — CHAY tren macOS
#  Double-click de chay, hoac: ./start-mac.command [port]
#  Nut Restart trong app se thoat server -> vong lap duoi bat lai (nap code moi).
#  Dong cua so Terminal de tat han.
# ============================================================
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8      # tranh crash khi in duong dan tieng Viet

PORT="${1:-8770}"
PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3                       # chua chay setup-mac thi thu python3 he thong

# Tu cap nhat code tu git remote moi 60s (bash native tren Mac) — chi khi la git repo.
if [ -f autoupdate.sh ] && command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  bash autoupdate.sh "$(pwd)" >/dev/null 2>&1 &
fi

# Mo trinh duyet sau 2s (cho server len).
( sleep 2; open "http://127.0.0.1:$PORT" ) >/dev/null 2>&1 &

# Vong lap khoi dong lai.
while true; do
  "$PY" server.py --port "$PORT"
  echo
  echo "[start] server dung — khoi dong lai sau 2s (dong cua so de tat han)..."
  sleep 2
done
