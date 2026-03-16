#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
#  Canopy Seed — Installer (macOS / Linux)
#  Run: bash install.sh
# ──────────────────────────────────────────────────────────────

set -e

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo -e "  ${GREEN}🌱 Canopy Seed — Installer${NC}"
echo "  ─────────────────────────────────────────"
echo ""

# ── 1. Python version check ───────────────────────────────────
PYTHON=$(command -v python3 || command -v python || true)
if [ -z "$PYTHON" ]; then
  echo -e "  ${RED}✗ Python not found.${NC}"
  echo "    Please install Python 3.11 or newer from https://python.org"
  exit 1
fi

PY_VERSION=$($PYTHON --version 2>&1 | awk '{print $2}')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
  echo -e "  ${RED}✗ Python $PY_VERSION found — need 3.11 or newer.${NC}"
  echo "    Please upgrade at https://python.org"
  exit 1
fi
echo -e "  ${GREEN}✓${NC} Python $PY_VERSION"

# ── 2. Create virtual environment ────────────────────────────
if [ ! -d ".venv" ]; then
  echo -e "  Creating virtual environment…"
  $PYTHON -m venv .venv
  echo -e "  ${GREEN}✓${NC} Virtual environment created"
else
  echo -e "  ${GREEN}✓${NC} Virtual environment already exists"
fi

# Activate
source .venv/bin/activate

# ── 3. Install dependencies ───────────────────────────────────
echo -e "  Installing dependencies…"
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo -e "  ${GREEN}✓${NC} Dependencies installed"

# ── 3b. Install Playwright Chromium (needed for Manager screenshots) ─────────
echo -e "  Installing Playwright Chromium browser (~150 MB, one-time)…"
if python -m playwright install chromium --with-deps >/dev/null 2>&1; then
  echo -e "  ${GREEN}✓${NC} Playwright Chromium installed"
else
  echo -e "  ${YELLOW}⚠${NC} Playwright browser install failed — Manager screenshots disabled."
  echo "    You can retry later: python -m playwright install chromium"
fi

# ── 4. Create .env (vault mode on by default — no API keys here) ─────────────
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo -e "  ${GREEN}✓${NC} Created .env (vault mode enabled — keys are entered on first launch)"
else
  echo -e "  ${GREEN}✓${NC} .env already exists"
fi

# ── 5. Create required directories ───────────────────────────
mkdir -p exports logs memory/sessions outputs
echo -e "  ${GREEN}✓${NC} Directories ready"

# ── 6. Create launch script ───────────────────────────────────
cat > start.sh << 'LAUNCHER'
#!/usr/bin/env bash
# Canopy Seed — Launch
source "$(dirname "$0")/.venv/bin/activate"
python start.py
LAUNCHER
chmod +x start.sh
echo -e "  ${GREEN}✓${NC} Launch script created: ./start.sh"

# ── Done ──────────────────────────────────────────────────────
echo ""
echo "  ─────────────────────────────────────────"
echo -e "  ${GREEN}${BOLD}Canopy Seed is ready to plant seeds.${NC}"
echo ""
echo "  Next steps:"
echo "  1. Run: ${BOLD}./start.sh${NC}"
echo "  2. The vault setup screen will appear — enter your API key(s) and"
echo "     choose a password. Keys are encrypted and stored in memory/vault.enc."
echo "  3. Open: ${BOLD}http://localhost:7822${NC}"
echo "     Hub:   ${BOLD}http://localhost:7822/hub${NC}"
echo ""
