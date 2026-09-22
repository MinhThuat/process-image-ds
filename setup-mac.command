#!/bin/bash
# ============================================================
#  Flat Studio — SETUP cho macOS (chay 1 lan)
#  Cai Python/Node/Git (qua Homebrew) + thu vien Python (trong .venv)
#  + codex (npm) + OpenArt CLI ban Mac (fallback).
#  Double-click de chay, hoac: ./setup-mac.command
# ============================================================
cd "$(dirname "$0")" || exit 1
echo "== Flat Studio — SETUP macOS =="

# ---- 1. Homebrew ----
if ! command -v brew >/dev/null 2>&1; then
  echo "!! Chua co Homebrew. Cai xong roi chay lai file nay:"
  echo '   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  read -r -p "Nhan Enter de thoat..."; exit 1
fi

# ---- 2. Python + Node + Git ----
echo "== 1. Python / Node / Git (brew) =="
for pkg in python@3.12 node git; do
  if brew list "$pkg" >/dev/null 2>&1; then echo "  da co $pkg"; else echo "  cai $pkg..."; brew install "$pkg"; fi
done

# ---- 3. venv + thu vien Python ----
# Dung venv de tranh loi "externally-managed-environment" (PEP 668) cua Python brew.
echo "== 2. Moi truong Python (.venv) + thu vien =="
python3 -m venv .venv || { echo "!! Tao .venv that bai"; read -r -p "Enter..."; exit 1; }
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install "aiohttp>=3.9" pillow numpy opencv-python-headless scipy "openai>=1.0" python-dotenv psd-tools \
  || { echo "!! pip install that bai"; read -r -p "Enter..."; exit 1; }

# ---- 4. codex (npm global) ----
echo "== 3. codex (gen chinh) =="
if command -v codex >/dev/null 2>&1; then echo "  da co codex"; else npm install -g @openai/codex; fi

# ---- 5. .env ----
echo "== 4. .env (ARK_API_KEY cho dola-seed) =="
if [ -f .env ] && grep -q '^ARK_API_KEY=' .env; then echo "  OK: co ARK_API_KEY"
else echo "  !! Tao file .env canh day, them dong:  ARK_API_KEY=ark-..."; fi

# ---- 6. OpenArt CLI ban Mac (fallback) ----
# Ban Mac (arm64 + amd64) da BUNDLE san trong bin/openart-darwin-*. Chi copy dung kien truc.
echo "== 5. OpenArt CLI (fallback) — ban Mac =="
mkdir -p bin
[ "$(uname -m)" = "x86_64" ] && OA=amd64 || OA=arm64          # Intel=amd64, Apple Silicon=arm64
if [ -x "bin/openart-darwin-$OA" ]; then
  cp "bin/openart-darwin-$OA" bin/openart; chmod +x bin/openart
  echo "  OK: bin/openart (Mac $OA — dung ban bundle san)"
else
  # phong ho: neu thieu bundle thi tai ve
  url="https://github.com/OpenArt-AI/cli/releases/download/v0.1.1/openart_0.1.1_darwin_${OA}.tar.gz"
  echo "  bundle thieu, thu tai: $url"
  if curl -fsSL "$url" -o /tmp/dsds_oa.tar.gz && ( cd /tmp && rm -f openart && tar -xzf dsds_oa.tar.gz openart ); then
    mv /tmp/openart bin/openart; chmod +x bin/openart; echo "  OK: bin/openart (Mac $OA — tai ve)"
  else echo "  !! Khong co OpenArt Mac. Bo qua fallback — gen chinh qua codex van chay."; fi
fi
if [ -x bin/openart ]; then echo "  Mo dang nhap OpenArt trong trinh duyet, xong quay lai..."; ./bin/openart login || true; fi

# ---- 7. Dang nhap codex ----
echo "== 6. Dang nhap ChatGPT/codex =="
if [ -f "$HOME/.codex/auth.json" ]; then echo "  OK: da dang nhap codex"
elif command -v codex >/dev/null 2>&1; then codex login || true; fi

echo
echo "XONG. Chay tool:  double-click start-mac.command  (hoac ./start-mac.command)"
read -r -p "Nhan Enter de dong..."
