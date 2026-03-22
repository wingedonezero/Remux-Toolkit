#!/bin/bash

# Remux Toolkit - Environment Setup Script
# Interactive script for managing Python environment and dependencies

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Project directory
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
PYTHON_VERSION="3.13.11"

# Function to show main menu
show_menu() {
    echo ""
    echo "========================================="
    echo "Remux Toolkit - Environment Setup"
    echo "========================================="
    echo ""
    echo -e "${BLUE}Project Directory:${NC} $PROJECT_DIR"
    echo ""
    echo "Please select an option:"
    echo ""
    echo -e "  ${CYAN}1)${NC} Full Setup - Install Python ${PYTHON_VERSION} and dependencies"
    echo -e "  ${CYAN}2)${NC} Update Libraries - Check for and install updates"
    echo -e "  ${CYAN}3)${NC} Verify Dependencies - Check all packages are installed"
    echo -e "  ${CYAN}4)${NC} Rebuild PyAV (optional, for optimized decoding)"
    echo -e "  ${CYAN}5)${NC} Install PyTorch with GPU support (ROCm/CUDA)"
    echo -e "  ${CYAN}6)${NC} Exit"
    echo ""
    echo -n "Enter your choice [1-6]: "
}

# Function to check Python version and verify it works
check_python_version() {
    local python_cmd=$1
    if command -v "$python_cmd" &> /dev/null; then
        # Check version
        local version=$("$python_cmd" --version 2>&1 | grep -oP '\d+\.\d+\.\d+')
        if [[ "$version" == 3.13.* ]]; then
            # Verify Python actually works by running multiple checks
            if "$python_cmd" -c "import sys, encodings; print('OK')" &> /dev/null; then
                # Also verify it can create a basic venv
                local test_venv="/tmp/test_venv_$$"
                if "$python_cmd" -m venv "$test_venv" 2>/dev/null; then
                    rm -rf "$test_venv"
                    echo "$python_cmd"
                    return 0
                else
                    rm -rf "$test_venv" 2>/dev/null
                    echo -e "${YELLOW}Warning: $python_cmd version $version found but cannot create venv, skipping...${NC}" >&2
                fi
            else
                echo -e "${YELLOW}Warning: $python_cmd version $version found but appears broken (missing encodings), skipping...${NC}" >&2
            fi
        fi
    fi
    return 1
}

# Function to install Python via conda
install_python_conda() {
    echo -e "${YELLOW}Attempting to install Python ${PYTHON_VERSION} via conda...${NC}"

    # Check if conda is available
    if command -v conda &> /dev/null; then
        echo -e "${BLUE}Found conda, installing Python ${PYTHON_VERSION}...${NC}"

        # Use only conda-forge to avoid TOS issues with default channels
        # Also disable default channels with --override-channels
        if conda install -y python=3.13.11 --override-channels -c conda-forge 2>/dev/null || \
           conda install -y "python>=3.13,<3.14" --override-channels -c conda-forge; then
            return 0
        else
            return 1
        fi
    elif command -v mamba &> /dev/null; then
        echo -e "${BLUE}Found mamba, installing Python ${PYTHON_VERSION}...${NC}"
        # Mamba doesn't have the same TOS restrictions, but use conda-forge anyway
        if mamba install -y python=3.13.11 -c conda-forge 2>/dev/null || \
           mamba install -y "python>=3.13,<3.14" -c conda-forge; then
            return 0
        else
            return 1
        fi
    else
        echo -e "${YELLOW}conda/mamba not found${NC}"
        return 1
    fi
}

# Function to download and install standalone Python
install_python_standalone() {
    echo -e "${YELLOW}Attempting to install Python ${PYTHON_VERSION} standalone build...${NC}" >&2

    local python_dir="$PROJECT_DIR/.python"

    # Detect architecture
    local arch
    arch=$(uname -m)
    local os
    os=$(uname -s | tr '[:upper:]' '[:lower:]')

    if [[ "$os" == "linux" ]]; then
        if [[ "$arch" == "x86_64" ]]; then
            # Use python-build-standalone (Astral) and the tag that includes CPython 3.13.11
            # (Release 20251205 includes CPython 3.13.11 for linux x86_64 gnu install_only.)
            local tag="20251205"
            local triple="x86_64-unknown-linux-gnu"
            local python_url="https://github.com/astral-sh/python-build-standalone/releases/download/${tag}/cpython-${PYTHON_VERSION}+${tag}-${triple}-install_only.tar.gz"
        else
            echo -e "${RED}Unsupported architecture: $arch${NC}" >&2
            return 1
        fi
    else
        echo -e "${RED}Unsupported OS: $os${NC}" >&2
        return 1
    fi

    echo -e "${BLUE}Downloading Python from: $python_url${NC}" >&2

    # Clean target to avoid mixing versions
    rm -rf "$python_dir"
    mkdir -p "$python_dir"

    local temp_file
    temp_file="$(mktemp -t pbs-python.XXXXXX.tar.gz)"

    # Better download: fail on HTTP errors, follow redirects, resume, retry
    if ! curl -fL -C - --retry 5 --retry-delay 1 --retry-all-errors \
        -o "$temp_file" "$python_url" >&2; then
        echo -e "${RED}Failed to download Python tarball${NC}" >&2
        rm -f "$temp_file"
        return 1
    fi

    echo -e "${BLUE}Extracting Python...${NC}" >&2
    if ! tar -xzf "$temp_file" -C "$python_dir" --strip-components=1 >&2; then
        echo -e "${RED}Failed to extract Python tarball${NC}" >&2
        rm -f "$temp_file"
        return 1
    fi
    rm -f "$temp_file"

    # Check if extraction was successful
    if [[ -x "$python_dir/bin/python3" ]]; then
        # Sanity check: ensure we truly got the version requested
        local got
        got="$("$python_dir/bin/python3" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || true)"
        if [[ "$got" != "$PYTHON_VERSION" ]]; then
            echo -e "${RED}Installed Python version mismatch: expected ${PYTHON_VERSION}, got ${got}${NC}" >&2
            echo -e "${RED}URL used: $python_url${NC}" >&2
            return 1
        fi

        echo "$python_dir/bin/python3"
        return 0
    fi

    echo -e "${RED}Failed to download/extract Python${NC}" >&2
    return 1
}

# Function to ensure venv exists and is activated
ensure_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo -e "${RED}Virtual environment not found!${NC}"
        echo -e "${YELLOW}Please run 'Full Setup' first (option 1)${NC}"
        return 1
    fi

    if [ -z "$VIRTUAL_ENV" ] || [ "$VIRTUAL_ENV" != "$VENV_DIR" ]; then
        if [ -n "$VIRTUAL_ENV" ]; then
            echo -e "${YELLOW}Warning: Another virtual environment is active (${VIRTUAL_ENV}).${NC}"
            echo -e "${YELLOW}Switching to project venv: $VENV_DIR${NC}"
        else
            echo -e "${BLUE}Activating virtual environment...${NC}"
        fi
        # shellcheck disable=SC1091
        source "$VENV_DIR/bin/activate"
    fi

    if [ ! -x "$VENV_PYTHON" ]; then
        echo -e "${RED}Virtual environment Python not found at $VENV_PYTHON${NC}"
        return 1
    fi
    echo -e "${BLUE}Active venv python: $VENV_PYTHON${NC}"
    echo -e "${BLUE}Shell python resolves to: $(command -v python)${NC}"
    return 0
}

venv_pip() {
    "$VENV_PYTHON" -m pip "$@"
}

# Function to check for updates
check_updates() {
    echo ""
    echo "========================================="
    echo "Checking for Updates"
    echo "========================================="
    echo ""

    if ! ensure_venv; then
        return 1
    fi

    echo -e "${YELLOW}Checking for package updates...${NC}"
    echo ""

    # Get list of outdated packages
    outdated=$(venv_pip list --outdated --format=json 2>/dev/null)

    if [ "$outdated" == "[]" ] || [ -z "$outdated" ]; then
        echo -e "${GREEN}✓ All packages are up to date!${NC}"

        # Check Python version
        echo ""
        echo -e "${YELLOW}Checking Python version...${NC}"
        current_python=$(python --version 2>&1 | grep -oP '\d+\.\d+\.\d+')
        echo -e "${BLUE}Current Python version: $current_python${NC}"
        echo -e "${YELLOW}Latest Python ${PYTHON_VERSION} will be checked during full setup${NC}"
        return 0
    fi

    # Parse and display outdated packages
    echo -e "${YELLOW}The following packages have updates available:${NC}"
    echo ""
    echo "$outdated" | "$VENV_PYTHON" -c "
import sys, json

data = json.load(sys.stdin)
for pkg in data:
    print(f\"  {pkg['name']:30s} {pkg['version']:15s} -> {pkg['latest_version']}\")
"

    echo ""
    echo -n "Do you want to update these packages? [y/N]: "
    read -r response

    if [[ "$response" =~ ^[Yy]$ ]]; then
        echo ""
        echo -e "${BLUE}Updating packages...${NC}"
        venv_pip install --upgrade $(echo "$outdated" | "$VENV_PYTHON" -c "
import sys, json

data = json.load(sys.stdin)
print(' '.join([pkg['name'] for pkg in data]))
")
        echo ""
        echo -e "${GREEN}✓ Packages updated successfully!${NC}"
    else
        echo -e "${YELLOW}Update cancelled${NC}"
    fi
}

# Function to verify dependencies
verify_dependencies() {
    echo ""
    echo "========================================="
    echo "Verify Dependencies"
    echo "========================================="
    echo ""

    if ! ensure_venv; then
        return 1
    fi

    echo -e "${YELLOW}Checking required dependencies...${NC}"
    echo ""

    # Read requirements.txt and check each package
    missing=()
    installed=()

    while IFS= read -r line; do
        # Skip comments and empty lines
        [[ "$line" =~ ^#.*$ ]] && continue
        [[ -z "$line" ]] && continue

        # Extract package name (before any version specifier)
        pkg=$(echo "$line" | sed 's/[>=<\[].*$//' | xargs)

        if venv_pip show "$pkg" &> /dev/null; then
            version=$(venv_pip show "$pkg" 2>/dev/null | grep "^Version:" | cut -d' ' -f2)
            installed+=("$pkg ($version)")
        else
            missing+=("$pkg")
        fi
    done < "$PROJECT_DIR/requirements.txt"

    # Display results
    echo -e "${GREEN}✓ Installed packages: ${#installed[@]}${NC}"
    for pkg in "${installed[@]}"; do
        echo "  $pkg"
    done

    echo ""

    if [ ${#missing[@]} -eq 0 ]; then
        echo -e "${GREEN}✓ All required dependencies are installed!${NC}"
    else
        echo -e "${RED}✗ Missing packages: ${#missing[@]}${NC}"
        for pkg in "${missing[@]}"; do
            echo "  $pkg"
        done
        echo ""
        echo -n "Do you want to install missing packages? [y/N]: "
        read -r response

        if [[ "$response" =~ ^[Yy]$ ]]; then
            echo ""
            echo -e "${BLUE}Installing missing packages...${NC}"
            venv_pip install -r "$PROJECT_DIR/requirements.txt"
            echo ""
            echo -e "${GREEN}✓ Missing packages installed${NC}"
        fi
    fi
}

# Function to rebuild PyAV against system FFmpeg
rebuild_pyav_from_source() {
    echo ""
    echo "========================================="
    echo "Rebuild PyAV from Source"
    echo "========================================="
    echo ""

    if ! ensure_venv; then
        return 1
    fi

    if command -v ffmpeg &> /dev/null; then
        echo -e "${GREEN}✓ FFmpeg detected${NC}"
    else
        echo -e "${YELLOW}Warning: FFmpeg not found in PATH.${NC}"
        echo -e "${YELLOW}PyAV will still build, but codec support may be limited.${NC}"
    fi

    echo ""
    echo -e "${BLUE}Rebuilding PyAV against system FFmpeg...${NC}"
    echo -e "${YELLOW}This can take a few minutes and may require build tools.${NC}"
    echo ""

    venv_pip uninstall -y av 2>/dev/null
    if venv_pip install --no-binary av av; then
        echo -e "${GREEN}✓ PyAV rebuilt from source${NC}"
    else
        echo -e "${RED}Failed to build PyAV from source.${NC}"
        echo -e "${YELLOW}Make sure build tools and FFmpeg dev libraries are installed.${NC}"
        return 1
    fi
}

# Function to install PyTorch with GPU support
install_pytorch_gpu() {
    echo ""
    echo "========================================="
    echo "Install PyTorch with GPU Support"
    echo "========================================="
    echo ""

    if ! ensure_venv; then
        return 1
    fi

    echo "Select your GPU type:"
    echo ""
    echo -e "  ${CYAN}1)${NC} AMD (ROCm 6.2.4)"
    echo -e "  ${CYAN}2)${NC} NVIDIA (CUDA 12.4)"
    echo -e "  ${CYAN}3)${NC} Cancel"
    echo ""
    echo -n "Enter your choice [1-3]: "
    read -r gpu_choice

    case $gpu_choice in
        1)
            echo ""
            echo -e "${BLUE}Installing PyTorch with ROCm support...${NC}"
            echo -e "${YELLOW}This may take a few minutes (large download).${NC}"
            echo ""
            venv_pip install torch torchaudio --index-url https://download.pytorch.org/whl/rocm6.2.4
            ;;
        2)
            echo ""
            echo -e "${BLUE}Installing PyTorch with CUDA support...${NC}"
            echo -e "${YELLOW}This may take a few minutes (large download).${NC}"
            echo ""
            venv_pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
            ;;
        3)
            echo -e "${YELLOW}Cancelled${NC}"
            return 0
            ;;
        *)
            echo -e "${RED}Invalid choice${NC}"
            return 1
            ;;
    esac

    if [ $? -eq 0 ]; then
        echo ""
        echo -e "${GREEN}✓ PyTorch with GPU support installed${NC}"

        # Verify GPU detection
        echo ""
        echo -e "${YELLOW}Verifying GPU detection...${NC}"
        python -c "
import torch
if torch.cuda.is_available():
    print(f'  GPU detected: {torch.cuda.get_device_name(0)}')
    print(f'  CUDA/ROCm version: {torch.version.cuda or \"ROCm\"}')
else:
    print('  WARNING: No GPU detected by PyTorch')
    print('  CPU fallback will be used (slower)')
"
    else
        echo -e "${RED}Failed to install PyTorch with GPU support${NC}"
        return 1
    fi
}

# Function for full setup
full_setup() {
    echo ""
    echo "========================================="
    echo "Full Setup"
    echo "========================================="
    echo ""

    # Step 1: Find or install Python
    echo -e "${YELLOW}[1/3] Checking for Python ${PYTHON_VERSION}...${NC}"

    PYTHON_CMD=""

    # Initialize conda if it exists but isn't in PATH
    # Try common conda installation paths
    if [ -z "$(command -v conda)" ]; then
        for conda_path in \
            "$HOME/miniconda3/etc/profile.d/conda.sh" \
            "$HOME/anaconda3/etc/profile.d/conda.sh" \
            "/opt/conda/etc/profile.d/conda.sh" \
            "/opt/miniconda3/etc/profile.d/conda.sh" \
            "$HOME/.conda/etc/profile.d/conda.sh"; do
            if [ -f "$conda_path" ]; then
                echo -e "${BLUE}Found conda at: $conda_path${NC}"
                # shellcheck disable=SC1090
                source "$conda_path"
                break
            fi
        done
    fi

    # First, try to install via conda if available (preferred method)
    if command -v conda &> /dev/null || command -v mamba &> /dev/null; then
        echo -e "${BLUE}Conda/Mamba detected, installing Python ${PYTHON_VERSION}...${NC}"
        if install_python_conda; then
            for py in python3.13 python3 python; do
                if PYTHON_CMD=$(check_python_version "$py"); then
                    echo -e "${GREEN}✓ Installed Python ${PYTHON_VERSION} via conda: $PYTHON_CMD${NC}"
                    break
                fi
            done
        fi
    else
        echo -e "${YELLOW}Conda/Mamba not detected in PATH${NC}"
    fi

    # If conda install failed or not available, try to find existing Python
    if [ -z "$PYTHON_CMD" ]; then
        echo -e "${YELLOW}Checking for existing Python ${PYTHON_VERSION}...${NC}"
        for py in python3.13 python3 python; do
            if PYTHON_CMD=$(check_python_version "$py"); then
                echo -e "${GREEN}✓ Found Python ${PYTHON_VERSION}: $PYTHON_CMD${NC}"
                break
            fi
        done
    fi

    # If still not found, try standalone as last resort
    if [ -z "$PYTHON_CMD" ]; then
        echo -e "${YELLOW}Python ${PYTHON_VERSION} not found. Downloading standalone build...${NC}"
        if PYTHON_CMD=$(install_python_standalone); then
            echo -e "${GREEN}✓ Installed Python ${PYTHON_VERSION} standalone: $PYTHON_CMD${NC}"
        else
            echo -e "${RED}Failed to install Python ${PYTHON_VERSION}${NC}"
            echo ""
            echo "Please install Python ${PYTHON_VERSION} manually:"
            echo "  - Via conda: conda install python=${PYTHON_VERSION}"
            echo "  - Or download from: https://www.python.org/downloads/"
            exit 1
        fi
    fi

    # Verify Python version
    PYTHON_VERSION_USED=$("$PYTHON_CMD" --version)
    echo -e "${BLUE}Using: $PYTHON_VERSION_USED${NC}"
    echo -e "${BLUE}Base interpreter: $PYTHON_CMD${NC}"
    echo ""

    # Step 2: Create virtual environment
    echo -e "${YELLOW}[2/3] Setting up virtual environment...${NC}"

    if [ -d "$VENV_DIR" ]; then
        echo -e "${YELLOW}Removing existing virtual environment...${NC}"
        rm -rf "$VENV_DIR"
    fi

    echo -e "${BLUE}Creating virtual environment at: $VENV_DIR${NC}"

    # Try creating venv with pip first
    if "$PYTHON_CMD" -m venv "$VENV_DIR" 2>/dev/null; then
        echo -e "${GREEN}✓ Virtual environment created${NC}"
    else
        # If that fails (missing ensurepip), create without pip and install manually
        echo -e "${YELLOW}ensurepip not available, creating venv without pip...${NC}"
        if ! "$PYTHON_CMD" -m venv --without-pip "$VENV_DIR" 2>/dev/null; then
            echo -e "${RED}Failed to create virtual environment${NC}"
            echo -e "${YELLOW}The Python installation appears to be broken or incomplete${NC}"
            echo ""
            echo "Attempting to download and install a working Python ${PYTHON_VERSION}..."

            # Remove the broken venv if it exists
            [ -d "$VENV_DIR" ] && rm -rf "$VENV_DIR"

            # Try standalone Python installation
            if PYTHON_CMD=$(install_python_standalone); then
                echo -e "${GREEN}✓ Installed working Python: $PYTHON_CMD${NC}"
                # Try creating venv again with the new Python
                if ! "$PYTHON_CMD" -m venv "$VENV_DIR" 2>/dev/null; then
                    echo -e "${RED}Still unable to create virtual environment${NC}"
                    exit 1
                fi
            else
                echo -e "${RED}Failed to install a working Python${NC}"
                exit 1
            fi
        fi

        # Activate and install pip manually
        # shellcheck disable=SC1091
        source "$VENV_DIR/bin/activate"

        # Verify venv actually works
        if ! python -c "import sys; sys.exit(0)" 2>/dev/null; then
            echo -e "${RED}Virtual environment is broken${NC}"
            exit 1
        fi

        echo -e "${BLUE}Installing pip manually...${NC}"
        if ! curl -sS https://bootstrap.pypa.io/get-pip.py | python; then
            echo -e "${RED}Failed to install pip${NC}"
            exit 1
        fi

        if ! command -v pip &> /dev/null; then
            echo -e "${RED}pip is not available after installation${NC}"
            exit 1
        fi
        echo -e "${GREEN}✓ pip installed successfully${NC}"
    fi

    # Activate virtual environment
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"

    # Verify the venv is actually working and isolated
    if python -c "import sys; sys.exit(0 if 'site-packages' not in sys.prefix or sys.prefix.startswith('$VENV_DIR') else 1)" 2>/dev/null; then
        echo -e "${GREEN}✓ Virtual environment activated${NC}"
    else
        echo -e "${RED}Warning: Virtual environment may not be properly isolated${NC}"
    fi

    # Upgrade pip
    echo -e "${BLUE}Upgrading pip...${NC}"
    venv_pip install --upgrade pip

    echo -e "${GREEN}✓ Virtual environment ready${NC}"
    echo ""

    # Step 3: Install dependencies
    echo -e "${YELLOW}[3/3] Installing dependencies...${NC}"
    echo "This may take a few minutes..."

    cd "$PROJECT_DIR"
    venv_pip install -r requirements.txt

    echo -e "${GREEN}✓ Dependencies installed${NC}"
    echo ""

    # Verify installation
    echo -e "${YELLOW}Verifying installation...${NC}"
    python --version
    echo -e "${GREEN}✓ Setup complete!${NC}"
    echo ""

    echo "========================================="
    echo -e "${GREEN}Environment setup successful!${NC}"
    echo "========================================="
    echo ""
    echo "To run the application, use:"
    echo -e "  ${BLUE}./run.sh${NC}"
    echo ""
    echo "Or manually activate the environment and run:"
    echo -e "  ${BLUE}source .venv/bin/activate${NC}"
    echo -e "  ${BLUE}python Remux-Toolkit.py${NC}"
    echo ""
}

# Main script execution
main() {
    # If script is run with arguments, execute directly
    case "$1" in
        --full-setup)
            full_setup
            exit 0
            ;;
        --update)
            check_updates
            exit 0
            ;;
        --verify)
            verify_dependencies
            exit 0
            ;;
        --rebuild-pyav)
            rebuild_pyav_from_source
            exit 0
            ;;
        --install-gpu)
            install_pytorch_gpu
            exit 0
            ;;
    esac

    # Interactive menu mode
    while true; do
        show_menu
        read -r choice

        case $choice in
            1)
                full_setup
                ;;
            2)
                check_updates
                ;;
            3)
                verify_dependencies
                ;;
            4)
                rebuild_pyav_from_source
                ;;
            5)
                install_pytorch_gpu
                ;;
            6)
                echo ""
                echo -e "${GREEN}Goodbye!${NC}"
                echo ""
                exit 0
                ;;
            *)
                echo ""
                echo -e "${RED}Invalid choice. Please enter 1-6.${NC}"
                ;;
        esac

        echo ""
        echo -n "Press Enter to return to menu..."
        read -r
    done
}

# Run main function
main "$@"
