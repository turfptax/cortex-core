#!/bin/bash
# Deploy Cortex Core to the Raspberry Pi Zero 2 W.
#
# Usage:
#   bash scripts/deploy.sh              # deploy + restart service
#   bash scripts/deploy.sh --no-restart # deploy only (no service restart)
#   bash scripts/deploy.sh --install    # first-time install (creates dirs, installs service)
#   bash scripts/deploy.sh --llama-setup # build llama-server from source on Pi (-j1, ~25 min)
#   bash scripts/deploy.sh --pet-setup  # deploy + compile llama-cpp + download model on Pi
#   bash scripts/deploy.sh --pet-wheel  # deploy + install pre-built wheel + download model
#
# Works from Git Bash on Windows (uses scp, no rsync needed).

set -e

PI_USER="turfptax"
PI_HOST="10.0.0.25"
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
    ssh "${PI}" "mkdir -p ${PI_TARGET}/src ${PI_TARGET}/scripts"
    scp "${REPO_DIR}"/src/*.py "${PI}:${PI_TARGET}/src/"
    scp "${REPO_DIR}"/requirements.txt "${PI}:${PI_TARGET}/"
    scp "${REPO_DIR}"/scripts/*.sh "${PI}:${PI_TARGET}/scripts/"
    echo "  Files deployed."
}

# Pet wheel setup (deploy + install pre-built wheel + download model)
if [ "$1" = "--pet-wheel" ]; then
    deploy_src

    echo ""
    echo "--- Uploading pre-built wheels ---"
    WHEELS_DIR="${REPO_DIR}/wheels"
    if [ -d "${WHEELS_DIR}" ] && ls "${WHEELS_DIR}"/*.whl >/dev/null 2>&1; then
        ssh "${PI}" "mkdir -p ${PI_TARGET}/wheels"
        scp "${WHEELS_DIR}"/*.whl "${PI}:${PI_TARGET}/wheels/"
        echo "  Wheels uploaded."
    else
        echo "  WARNING: No wheels found in ${WHEELS_DIR}"
        echo "  Download wheels first — see README."
        exit 1
    fi

    echo ""
    echo "--- Running pet setup (wheel install — fast!) ---"
    ssh "${PI}" "bash ${PI_TARGET}/scripts/setup_pet.sh"

    echo ""
    echo "--- Restarting service ---"
    ssh "${PI}" "sudo systemctl restart cortex-core"
    sleep 3
    ssh "${PI}" "sudo systemctl status cortex-core --no-pager -l" || true

    echo ""
    echo "Pet setup complete (from pre-built wheel)!"
    exit 0
fi

# llama-server setup (deploy + build llama-server from source on Pi)
# IMPORTANT: Uses -j1 to avoid OOM on 2GB boards (~20-30 min build)
if [ "$1" = "--llama-setup" ]; then
    deploy_src

    echo ""
    echo "--- Building llama-server on Pi (-j1, ~20-30 min) ---"
    echo "--- IMPORTANT: Do NOT use -j4 on 2GB boards (causes OOM crash) ---"
    ssh -o ServerAliveInterval=30 "${PI}" "bash ${PI_TARGET}/scripts/setup_llama_server.sh"

    echo ""
    echo "--- Restarting cortex-core ---"
    ssh "${PI}" "sudo systemctl restart cortex-core"
    sleep 3
    ssh "${PI}" "sudo systemctl status cortex-core --no-pager -l" || true

    echo ""
    echo "llama-server setup complete!"
    exit 0
fi

# Pet setup (deploy + compile from source on Pi)
if [ "$1" = "--pet-setup" ]; then
    deploy_src

    echo ""
    echo "--- Running pet setup on Pi (this takes 30-60 min for compilation) ---"
    ssh "${PI}" "bash ${PI_TARGET}/scripts/setup_pet.sh"

    echo ""
    echo "--- Restarting service ---"
    ssh "${PI}" "sudo systemctl restart cortex-core"
    sleep 3
    ssh "${PI}" "sudo systemctl status cortex-core --no-pager -l" || true

    echo ""
    echo "Pet setup complete!"
    exit 0
fi

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
