#!/bin/bash
# session-start.sh — install all project dependencies for Claude Code on the web.
# Runs synchronously on SessionStart so every tool (pytest, eslint, etc.) is
# available before the agent loop begins.

set -euo pipefail

# Only run in remote (web) sessions
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# ── Python dependencies ──────────────────────────────────────────────────────
echo "Installing Python dependencies..."
pip install -r requirements.txt -q

# ── Playwright browser ───────────────────────────────────────────────────────
# Install Chromium + system deps if not already present.
# The --with-deps flag is idempotent; it skips already-installed packages.
echo "Installing Playwright Chromium..."
playwright install chromium --with-deps -q 2>/dev/null || true

# ── Frontend (Node) dependencies ────────────────────────────────────────────
echo "Installing frontend Node dependencies..."
cd "$CLAUDE_PROJECT_DIR/frontend"
npm install --prefer-offline

echo "Session-start hook complete."
