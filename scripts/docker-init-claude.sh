#!/bin/bash
# Auto-bootstrap Claude Code CLI + Python SDK inside the container.
# Idempotent: skips work already done. Invoked by docker-run.sh as the
# container's startup command.
#
# Disable by exporting AUTO_INSTALL_CLAUDE=0 before ./docker-run.sh

set -u

if [[ "${AUTO_INSTALL_CLAUDE:-1}" == "0" ]]; then
    echo "[init-claude] AUTO_INSTALL_CLAUDE=0 -> skipping"
    exit 0
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
log() { echo -e "${GREEN}[init-claude]${NC} $*"; }
warn() { echo -e "${YELLOW}[init-claude]${NC} $*"; }
err() { echo -e "${RED}[init-claude]${NC} $*" >&2; }

# 1. PATH: ensure ~/.local/bin is first (standalone installer target)
export PATH="$HOME/.local/bin:$PATH"
if [[ -f "$HOME/.bashrc" ]] && ! grep -q 'HOME/.local/bin' "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    log "added ~/.local/bin to ~/.bashrc"
fi

# 2. claude CLI: install if missing
if command -v claude >/dev/null 2>&1; then
    log "claude CLI found: $(command -v claude) ($(claude --version 2>&1 | head -1))"
else
    log "claude CLI not found -> running standalone installer"
    if ! command -v curl >/dev/null 2>&1; then
        err "curl missing; cannot install. apt-get install curl, then re-run."
    else
        if curl -fsSL https://claude.ai/install.sh | bash; then
            export PATH="$HOME/.local/bin:$PATH"
            if command -v claude >/dev/null 2>&1; then
                log "installed: $(claude --version 2>&1 | head -1)"
            else
                warn "installer finished but claude still not on PATH"
            fi
        else
            err "installer failed"
        fi
    fi
fi

# 3. claude-agent-sdk Python package: install if missing
if python -c "import claude_agent_sdk" 2>/dev/null; then
    log "claude_agent_sdk Python pkg present"
else
    log "claude_agent_sdk missing -> pip install -e .[claude-code]"
    if [[ -f "pyproject.toml" ]]; then
        if pip install --quiet -e ".[claude-code]"; then
            log "installed claude-agent-sdk"
        else
            warn "pip install failed; run manually: pip install -e \".[claude-code]\""
        fi
    else
        warn "pyproject.toml not in cwd ($(pwd)); skipping pip install"
    fi
fi

# 4. Auth sanity check
if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
    log "auth env var present"
else
    warn "no CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY in env"
    warn "  host에서: claude setup-token -> export CLAUDE_CODE_OAUTH_TOKEN=... -> re-run docker-run.sh"
fi
