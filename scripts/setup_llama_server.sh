#!/bin/bash
# Build and install llama-server from source on the Pi.
#
# Usage (run ON the Pi, or via SSH):
#   bash scripts/setup_llama_server.sh              # full setup (build + model)
#   bash scripts/setup_llama_server.sh --build-only # build llama-server only
#   bash scripts/setup_llama_server.sh --model-only # download model only
#
# IMPORTANT: Uses -j1 (single thread) to avoid OOM on 2GB Pi boards.
# Build takes ~20-30 minutes on Orange Pi Zero 2W / Pi Zero 2W.
# The binary is statically linked — no shared library issues.

set -e

PI_USER="turfptax"
MODEL_DIR="/home/${PI_USER}/models"
MODEL_FILE="qwen3.5-0.8b-q4_k_m.gguf"
MODEL_URL="https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/resolve/main/qwen3.5-0.8b-q4_k_m.gguf"
BUILD_DIR="/home/${PI_USER}/llama.cpp-build"
INSTALL_PATH="/usr/local/bin/llama-server"

build_llama_server() {
    echo ""
    echo "=== Building llama-server from source ==="
    echo "  Using -j1 (single thread) to stay within 2GB RAM."
    echo "  This takes ~20-30 minutes. Do NOT use -j4 on 2GB boards."
    echo ""

    # Install build dependencies
    sudo apt-get update -qq
    sudo apt-get install -y -qq cmake build-essential git

    # Clone llama.cpp (shallow)
    if [ -d "${BUILD_DIR}" ]; then
        echo "  Removing previous build directory..."
        rm -rf "${BUILD_DIR}"
    fi

    echo "  Cloning llama.cpp..."
    git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "${BUILD_DIR}"

    # Configure with static linking (no .so dependency issues)
    echo "  Configuring cmake (static build, no GPU)..."
    cd "${BUILD_DIR}"
    mkdir -p build && cd build
    cmake .. \
        -DBUILD_SHARED_LIBS=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_SERVER=ON \
        -DCMAKE_BUILD_TYPE=Release \
        2>&1 | tail -5

    # Build with single thread to avoid OOM
    echo ""
    echo "  Building llama-server (-j1)..."
    echo "  Started at $(date). Expect ~20-30 minutes."
    make -j1 llama-server 2>&1 | tail -10

    # Verify the binary was built
    BINARY="${BUILD_DIR}/build/bin/llama-server"
    if [ ! -f "${BINARY}" ]; then
        echo "  ERROR: Build failed — binary not found at ${BINARY}"
        exit 1
    fi

    # Verify no missing shared libraries
    MISSING=$(ldd "${BINARY}" 2>&1 | grep "not found" || true)
    if [ -n "${MISSING}" ]; then
        echo "  ERROR: Binary has missing shared libraries:"
        echo "  ${MISSING}"
        exit 1
    fi

    # Install
    echo ""
    echo "  Installing to ${INSTALL_PATH}..."
    sudo cp "${BINARY}" "${INSTALL_PATH}"
    sudo chmod +x "${INSTALL_PATH}"

    echo "  llama-server installed! ($(du -h ${INSTALL_PATH} | cut -f1))"
    echo "  Build: $(${INSTALL_PATH} --version 2>&1 | head -1 || echo 'unknown')"
}

download_model() {
    echo ""
    echo "=== Downloading Qwen3.5-0.8B (Q4_K_M) ==="

    mkdir -p "${MODEL_DIR}"

    if [ -f "${MODEL_DIR}/${MODEL_FILE}" ]; then
        echo "  Model already exists at ${MODEL_DIR}/${MODEL_FILE}"
        echo "  ($(du -h "${MODEL_DIR}/${MODEL_FILE}" | cut -f1))"
        echo "  Delete it first if you want to re-download."
        return 0
    fi

    echo "  Downloading to ${MODEL_DIR}/${MODEL_FILE} ..."
    echo "  (~533 MB — should take 2-5 minutes on decent WiFi)"
    echo ""

    wget -q --show-progress -O "${MODEL_DIR}/${MODEL_FILE}" "${MODEL_URL}"

    # Verify
    FILE_SIZE=$(stat -c%s "${MODEL_DIR}/${MODEL_FILE}" 2>/dev/null || echo "0")
    if [ "$FILE_SIZE" -lt 100000000 ]; then
        echo "  ERROR: Downloaded file is too small (${FILE_SIZE} bytes). Download may have failed."
        rm -f "${MODEL_DIR}/${MODEL_FILE}"
        exit 1
    fi

    echo ""
    echo "  Model downloaded! ($(du -h "${MODEL_DIR}/${MODEL_FILE}" | cut -f1))"
}

install_service() {
    echo ""
    echo "=== Installing llama-server systemd service ==="

    SERVICE_SRC="/home/${PI_USER}/cortex-core/llama-server.service"
    if [ -f "${SERVICE_SRC}" ]; then
        sudo cp "${SERVICE_SRC}" /etc/systemd/system/llama-server.service
        sudo systemctl daemon-reload
        sudo systemctl enable llama-server
        sudo systemctl restart llama-server
        sleep 3
        systemctl status llama-server --no-pager | head -8
    else
        echo "  WARNING: Service file not found at ${SERVICE_SRC}"
        echo "  Copy llama-server.service to /etc/systemd/system/ manually."
    fi
}

cleanup() {
    echo ""
    echo "=== Cleaning up build directory ==="
    rm -rf "${BUILD_DIR}"
    echo "  Removed ${BUILD_DIR} (~1.5 GB freed)"
}

verify() {
    echo ""
    echo "=== Verification ==="

    # Check binary
    if [ -f "${INSTALL_PATH}" ]; then
        echo "  ✓ llama-server binary exists ($(du -h ${INSTALL_PATH} | cut -f1))"
        MISSING=$(ldd "${INSTALL_PATH}" 2>&1 | grep "not found" || true)
        if [ -n "${MISSING}" ]; then
            echo "  ✗ Missing libraries: ${MISSING}"
        else
            echo "  ✓ All shared libraries satisfied"
        fi
    else
        echo "  ✗ llama-server NOT found at ${INSTALL_PATH}"
    fi

    # Check model
    if [ -f "${MODEL_DIR}/${MODEL_FILE}" ]; then
        echo "  ✓ Model file exists ($(du -h "${MODEL_DIR}/${MODEL_FILE}" | cut -f1))"
    else
        echo "  ✗ Model file NOT found at ${MODEL_DIR}/${MODEL_FILE}"
    fi

    # Check service
    if systemctl is-active --quiet llama-server 2>/dev/null; then
        echo "  ✓ llama-server service is running"
    else
        echo "  ✗ llama-server service is NOT running"
    fi

    # Test inference
    echo ""
    echo "  Testing inference..."
    RESPONSE=$(curl -s --connect-timeout 5 http://127.0.0.1:8081/health 2>/dev/null || echo "")
    if echo "${RESPONSE}" | grep -q "ok"; then
        echo "  ✓ llama-server is healthy and responding"
    else
        echo "  ⚠ llama-server not responding yet (may still be loading model)"
    fi

    echo ""
    echo "Done! If all checks pass, restart cortex-core:"
    echo "  sudo systemctl restart cortex-core"
}

echo "=== Cortex llama-server Setup ==="
echo "  Board: $(cat /proc/device-tree/model 2>/dev/null || uname -m)"
echo "  RAM:   $(free -m | awk '/Mem:/{print $2}') MB"
echo ""

case "${1}" in
    --build-only)
        build_llama_server
        ;;
    --model-only)
        download_model
        ;;
    --cleanup)
        cleanup
        ;;
    *)
        build_llama_server
        download_model
        install_service
        cleanup
        verify
        ;;
esac
