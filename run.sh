#!/bin/bash

# Remux Toolkit - Application Launcher
# Runs the application in a terminal window so you can see any errors

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Project directory
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

# Check if virtual environment exists
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${RED}Error: Virtual environment not found!${NC}"
    echo ""
    echo "Please run the setup GUI first:"
    echo -e "  ${BLUE}python3 setup_gui.py${NC}"
    echo ""
    exit 1
fi

# Setup GPU environment variables for ROCm support
setup_rocm_environment() {
    # Only set if not already defined (user's custom settings take priority)

    # Detect AMD GPU using rocm-smi or lspci
    AMD_GPU_DETECTED=false
    GFX_VERSION=""
    HSA_VERSION=""

    if command -v rocm-smi &> /dev/null; then
        GPU_NAME=$(rocm-smi --showproductname 2>/dev/null | head -1)
        if [[ ! -z "$GPU_NAME" ]]; then
            AMD_GPU_DETECTED=true

            # Map common GPUs to gfx architecture
            case "$GPU_NAME" in
                *"7900"*|*"7800"*|*"7700"*|*"7600"*)
                    GFX_VERSION="gfx1100"
                    HSA_VERSION="11.0.0"
                    ;;
                *"6900"*|*"6800"*|*"6700"*|*"6600"*)
                    GFX_VERSION="gfx1030"
                    HSA_VERSION="10.3.0"
                    ;;
                *"890M"*)
                    GFX_VERSION="gfx1151"
                    HSA_VERSION="11.5.1"
                    ;;
                *"780M"*)
                    GFX_VERSION="gfx1103"
                    HSA_VERSION="11.0.3"
                    ;;
                *)
                    # Generic modern Radeon default
                    GFX_VERSION="gfx1100"
                    HSA_VERSION="11.0.0"
                    ;;
            esac
        fi
    elif command -v lspci &> /dev/null; then
        if lspci -nn 2>/dev/null | grep -iE 'VGA|Display' | grep -iE 'AMD|Radeon' &> /dev/null; then
            AMD_GPU_DETECTED=true
            GFX_VERSION="gfx1100"
            HSA_VERSION="11.0.0"
        fi
    fi

    AMDGPU_IDS_FILE=""
    if [ -n "$AMDGPU_IDS_PATH" ] && [ -e "$AMDGPU_IDS_PATH" ]; then
        AMDGPU_IDS_FILE="$AMDGPU_IDS_PATH"
    elif [ -n "$LIBDRM_AMDGPU_IDS_PATH" ] && [ -e "$LIBDRM_AMDGPU_IDS_PATH" ]; then
        AMDGPU_IDS_FILE="$LIBDRM_AMDGPU_IDS_PATH"
    else
        for candidate in "/opt/amdgpu/share/libdrm/amdgpu.ids" "/usr/share/libdrm/amdgpu.ids"; do
            if [ -e "$candidate" ]; then
                AMDGPU_IDS_FILE="$candidate"
                break
            fi
        done
    fi

    if [ -z "$AMDGPU_IDS_FILE" ]; then
        AMDGPU_IDS_FILE="/dev/null"
    fi

    export AMDGPU_IDS_PATH="$AMDGPU_IDS_FILE"
    export LIBDRM_AMDGPU_IDS_PATH="$AMDGPU_IDS_FILE"

    # Set ROCm environment variables if AMD GPU detected
    if [ "$AMD_GPU_DETECTED" = true ]; then
        # Pin all GPU work to the discrete GPU (device 0) — on dual-GPU
        # systems (discrete Radeon + Ryzen iGPU) the iGPU otherwise gets
        # initialized by ROCm and crashes on the first kernel launch.
        [ -z "$ROCR_VISIBLE_DEVICES" ] && export ROCR_VISIBLE_DEVICES=0
        [ -z "$HIP_VISIBLE_DEVICES" ] && export HIP_VISIBLE_DEVICES=0
        [ -z "$HSA_OVERRIDE_GFX_VERSION" ] && export HSA_OVERRIDE_GFX_VERSION="$HSA_VERSION"
        [ -z "$AMD_TEE_LOG_PATH" ] && export AMD_TEE_LOG_PATH="/dev/null"  # Suppress amdgpu.ids errors
    fi
}

# Activate virtual environment (ensures PATH/VIRTUAL_ENV/conda libs are set)
activate_venv() {
    if [ -f "$VENV_DIR/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$VENV_DIR/bin/activate"
        return 0
    fi
    echo -e "${RED}Error: Failed to activate virtual environment.${NC}"
    return 1
}

# Function to run in current terminal
run_in_current_terminal() {
    echo "========================================="
    echo "Remux Toolkit"
    echo "========================================="
    echo ""
    echo -e "${BLUE}Starting application...${NC}"
    echo -e "${YELLOW}Press Ctrl+C to exit${NC}"
    echo ""

    cd "$PROJECT_DIR"

    if ! activate_venv; then
        exit 1
    fi

    setup_rocm_environment

    # Use venv Python from PATH (activation ensures correct env/paths)
    python Remux-Toolkit.py 2>&1
    EXIT_CODE=$?

    # Always show exit status and wait
    echo ""
    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${GREEN}Application exited normally${NC}"
    else
        echo -e "${RED}Application exited with error code: $EXIT_CODE${NC}"
    fi
    echo -e "${YELLOW}Press Enter to close...${NC}"
    read
}

main() {
    # Wrapper script for terminal emulators - ensures errors are shown
    WRAPPER_CMD="cd '$PROJECT_DIR' && source '$PROJECT_DIR/run.sh' && activate_venv && setup_rocm_environment && echo '=========================================' && echo 'Remux Toolkit' && echo '=========================================' && echo '' && echo 'Starting application...' && echo '' && python Remux-Toolkit.py 2>&1; EXIT_CODE=\$?; echo ''; if [ \$EXIT_CODE -eq 0 ]; then echo -e '${GREEN}Application exited normally${NC}'; else echo -e '${RED}Application exited with error code:' \$EXIT_CODE'${NC}'; fi; echo -e '${YELLOW}Press Enter to close...${NC}'; read"

    # If running from a terminal, just run it
    if [ -t 0 ]; then
        run_in_current_terminal
    else
        # Try to open in a new terminal window
        # Detect available terminal emulator
        if command -v konsole &> /dev/null; then
            # KDE Konsole
            konsole -e bash -c "$WRAPPER_CMD"
        elif command -v gnome-terminal &> /dev/null; then
            # GNOME Terminal
            gnome-terminal -- bash -c "$WRAPPER_CMD"
        elif command -v xfce4-terminal &> /dev/null; then
            # XFCE Terminal
            xfce4-terminal -e "bash -c \"$WRAPPER_CMD\""
        elif command -v alacritty &> /dev/null; then
            # Alacritty
            alacritty -e bash -c "$WRAPPER_CMD"
        elif command -v kitty &> /dev/null; then
            # Kitty
            kitty bash -c "$WRAPPER_CMD"
        elif command -v xterm &> /dev/null; then
            # xterm (fallback)
            xterm -e bash -c "$WRAPPER_CMD"
        else
            # No terminal found, run in current shell
            echo -e "${YELLOW}No suitable terminal emulator found. Running in current shell...${NC}"
            run_in_current_terminal
        fi
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
