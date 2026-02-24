#!/bin/bash
# Deploy Cortex Core to the Raspberry Pi Zero 2 W.
#
# Usage:
#   bash scripts/deploy.sh              # deploy + restart service
#   bash scripts/deploy.sh --no-restart # deploy only (no service restart)
#   bash scripts/deploy.sh --install    # first-time install (creates dirs, installs service)
#
# Works from Git Bash on Windows (uses scp, no rsync needed).

set -e

PI_USER="turfptax"
PI_HOST="10.0.0.132"
PI_TARGET="/home/${PI_USER}/cortex-core"
PI="${PI_USER}@${PI_HOST}"

# Resolve script location so it works from any directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=== Cortex Core Deploy ==="
echo "  Source: ${REPO_DIR}"
echo "  Target: ${PI}:${PI_TARGET}"

deploy_src() {
    echo ""
    echo "--- Deploying source files ---"
    ssh "${PI}" "mkdir -p ${PI_TARGET}/src"
    scp "${REPO_DIR}"/src/*.py "${PI}:${PI_TARGET}/src/"
    echo "  Files deployed."
}

# First-time install
if [ "$1" = "--install" ]; then
    echo ""
    echo "--- First-time install ---"

    deploy_src

    # Create directories
    echo ""
    echo "--- Creating directories ---"
    ssh "${PI}" "mkdir -p /home/${PI_USER}/uploads"

    # Install systemd service
    echo ""
    echo "--- Installing systemd service ---"
    scp "${REPO_DIR}/systemd/cortex-core.service" "${PI}:/tmp/cortex-core.service"
    ssh "${PI}" "sudo mv /tmp/cortex-core.service /etc/systemd/system/cortex-core.service && \
                 sudo systemctl daemon-reload && \
                 sudo systemctl enable cortex-core && \
                 sudo systemctl start cortex-core"

    echo ""
    echo "Install complete! Service is running."
    echo "Check status: ssh ${PI} 'sudo systemctl status cortex-core'"
    exit 0
fi

# Regular deploy
deploy_src

if [ "$1" = "--no-restart" ]; then
    echo ""
    echo "Deploy complete (no restart)."
    exit 0
fi

# Restart service
echo ""
echo "--- Restarting service ---"
ssh "${PI}" "sudo systemctl restart cortex-core"

# Brief wait then check status
sleep 3
echo ""
echo "--- Service status ---"
ssh "${PI}" "sudo systemctl status cortex-core --no-pager -l" || true

# Fetch HTTP API token (generated on first run)
echo ""
echo "--- HTTP API token ---"
TOKEN=$(ssh "${PI}" "cat /home/${PI_USER}/cortex-http.secret 2>/dev/null")
if [ -n "$TOKEN" ]; then
    echo "$TOKEN" > "${HOME}/.cortex-wifi.token"
    echo "  Token saved to ~/.cortex-wifi.token"
    echo "  WiFi API: http://${PI_HOST}:8420/health"
else
    echo "  (token will be generated on first service start)"
fi

echo ""
echo "Deploy complete!"
