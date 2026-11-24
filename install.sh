#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${HOME}/.gemini/config/plugins/task-message-bus"

echo "Installing task-message-bus plugin to: ${TARGET_DIR}"
mkdir -p "${TARGET_DIR}/scripts" "${TARGET_DIR}/rules" "${TARGET_DIR}/skills/task-message-bus"

cp "${SRC_DIR}/plugin.json" "${TARGET_DIR}/plugin.json"
cp "${SRC_DIR}/hooks.json" "${TARGET_DIR}/hooks.json"
cp "${SRC_DIR}/README.md" "${TARGET_DIR}/README.md"
cp "${SRC_DIR}/rules/AGENTS.md" "${TARGET_DIR}/rules/AGENTS.md"
cp "${SRC_DIR}/skills/task-message-bus/SKILL.md" "${TARGET_DIR}/skills/task-message-bus/SKILL.md"
cp "${SRC_DIR}/scripts/"*.py "${TARGET_DIR}/scripts/"

PYTHON_BIN="python3"
if ! command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

cat > "${TARGET_DIR}/mcp_config.json" <<EOF
{
  "mcpServers": {
    "task-message-bus": {
      "command": "${PYTHON_BIN}",
      "args": [
        "${TARGET_DIR}/scripts/mcp_server.py"
      ]
    }
  }
}
EOF

echo "Installed successfully! Antigravity will auto-discover the plugin on the next turn."
