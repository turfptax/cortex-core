#!/bin/bash
# Set up the Pet / LLM inference engine on the Raspberry Pi Zero 2 W.
#
# Usage:
#   bash scripts/setup_pet.sh              # full setup (deps + model)
#   bash scripts/setup_pet.sh --deps-only  # install llama-cpp-python only
#   bash scripts/setup_pet.sh --model-only # download model only
#
# Run this ONCE after initial deploy, before starting the service.
# Compiling llama-cpp-python takes ~30-60 minutes on Pi Zero 2W.

set -e

PI_USER="turfptax"
MODEL_DIR="/home/${PI_USER}/models"
MODEL_FILE="smollm2-135m-instruct-q4_k_m.gguf"
MODEL_URL="https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/resolve/main/SmolLM2-135M-Instruct-Q4_K_M.gguf"

install_deps() {
    echo ""
    echo "=== Installing llama-cpp-python ==="

    # Check for pre-built wheel first (transferred by deploy.sh --pet-wheel)
    WHEEL_DIR="/home/${PI_USER}/cortex-core/wheels"
    PY_VER=$(python3 -c "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')")
    ARCH=$(uname -m)

    WHEEL_FILE=""
    if [ -d "${WHEEL_DIR}" ]; then
        # Try exact match: cpXYZ + architecture
        WHEEL_FILE=$(ls "${WHEEL_DIR}"/llama_cpp_python-*-${PY_VER}-${PY_VER}-linux_${ARCH}.whl 2>/dev/null | head -1)
    fi

    if [ -n "${WHEEL_FILE}" ]; then
        echo "  Found pre-built wheel: $(basename ${WHEEL_FILE})"
        echo "  Installing from wheel (fast — no compilation needed)..."
        pip3 install "${WHEEL_FILE}" --break-system-packages --no-cache-dir
    else
        echo "  No pre-built wheel found for Python ${PY_VER} / ${ARCH}"
        echo "  Compiling from source — expect 30-60 minutes on Pi Zero 2W."
        echo ""

        # Build deps
        sudo apt-get update -qq
        sudo apt-get install -y -qq cmake build-essential python3-dev

        # Compile llama-cpp-python without GPU acceleration
        CMAKE_ARGS="-DGGML_NO_METAL=ON -DGGML_NO_CUDA=ON" \
            pip3 install llama-cpp-python>=0.3.0 --break-system-packages --no-cache-dir
    fi

    echo ""
    echo "  llama-cpp-python installed successfully!"
}

download_model() {
    echo ""
    echo "=== Downloading SmolLM2-135M-Instruct (Q4_K_M) ==="

    mkdir -p "${MODEL_DIR}"

    if [ -f "${MODEL_DIR}/${MODEL_FILE}" ]; then
        echo "  Model already exists at ${MODEL_DIR}/${MODEL_FILE}"
        echo "  Delete it first if you want to re-download."
        return 0
    fi

    echo "  Downloading to ${MODEL_DIR}/${MODEL_FILE} ..."
    echo "  (~85 MB — should take 1-3 minutes on decent WiFi)"
    echo ""

    wget -q --show-progress -O "${MODEL_DIR}/${MODEL_FILE}" "${MODEL_URL}"

    # Verify file is not empty/corrupt
    FILE_SIZE=$(stat -c%s "${MODEL_DIR}/${MODEL_FILE}" 2>/dev/null || echo "0")
    if [ "$FILE_SIZE" -lt 1000000 ]; then
        echo "  ERROR: Downloaded file is too small (${FILE_SIZE} bytes). Download may have failed."
        rm -f "${MODEL_DIR}/${MODEL_FILE}"
        exit 1
    fi

    echo ""
    echo "  Model downloaded! ($(du -h "${MODEL_DIR}/${MODEL_FILE}" | cut -f1))"
}

verify() {
    echo ""
    echo "=== Verification ==="

    # Check llama-cpp-python
    if python3 -c "import llama_cpp; print(f'  llama-cpp-python v{llama_cpp.__version__}')" 2>/dev/null; then
        echo "  ✓ llama-cpp-python OK"
    else
        echo "  ✗ llama-cpp-python NOT installed"
    fi

    # Check model file
    if [ -f "${MODEL_DIR}/${MODEL_FILE}" ]; then
        echo "  ✓ Model file exists ($(du -h "${MODEL_DIR}/${MODEL_FILE}" | cut -f1))"
    else
        echo "  ✗ Model file NOT found at ${MODEL_DIR}/${MODEL_FILE}"
    fi

    echo ""
    echo "If both checks pass, restart cortex-core:"
    echo "  sudo systemctl restart cortex-core"
    echo ""
    echo "Then test with:"
    echo "  curl -s http://localhost:8420/cmd -d 'pet_status:{}' -H 'Authorization: Bearer \$(cat ~/cortex-http.secret)'"
}

echo "=== Cortex Pet Setup ==="

case "${1}" in
    --deps-only)
        install_deps
        ;;
    --model-only)
        download_model
        ;;
    *)
        install_deps
        download_model
        ;;
esac

verify
