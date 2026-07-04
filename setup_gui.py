#!/usr/bin/env python3
"""
Remux Toolkit - Environment Setup (GUI)

Standalone PyQt6 app that manages the project's Python virtual environment,
dependencies and optional GPU packages.

Replaces the bash setup_env.sh with a proper GUI featuring live log output,
progress bars, and clickable actions.

Run with system Python:  python3 setup_gui.py
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# =============================================================================
# Constants
# =============================================================================

PROJECT_DIR = Path(__file__).parent.resolve()
VENV_DIR = PROJECT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
PYPROJECT_PATH = PROJECT_DIR / "pyproject.toml"

# ROCm PyTorch index URLs
ROCM_OPTIONS: list[tuple[str, str, bool]] = [
    # (label, index_url, is_nightly)
    (
        "ROCm 6.4 (Stable)",
        "https://download.pytorch.org/whl/rocm6.4",
        False,
    ),
    (
        "ROCm 7.2 (Stable - recommended)",
        "https://download.pytorch.org/whl/rocm7.2",
        False,
    ),
]

# GPU options for PyTorch installation
GPU_OPTIONS: list[tuple[str, str]] = [
    ("AMD GPU (ROCm 7.2 - Stable, recommended)", "rocm72"),
    ("AMD GPU (ROCm 6.4 - Stable)", "rocm64"),
    ("NVIDIA GPU (CUDA 12.4)", "cuda"),
    ("CPU only (keep PyPI build)", "cpu"),
]

# =============================================================================
# Update routing — packages that must NOT be blindly upgraded from PyPI
# =============================================================================
# The GPU stack has its own channels: the default PyPI torch wheels are CUDA
# builds that would silently replace the ROCm build. Version constraints are
# read from pyproject at runtime — each pin's comment there documents the OS
# constraint forcing it (e.g. av capped by Debian's FFmpeg, PyQt6 capped to
# the system Qt, VapourSynth matched to the system core).

# Upgraded via the PyTorch ROCm wheel index (detect_rocm_index()).
ROCM_ROUTED_PACKAGES = {"torch", "torchaudio", "torchvision"}
# Managed by the torch upgrade's dependency resolution — never explicitly.
ROCM_MANAGED_PACKAGES = {"triton", "triton-rocm", "pytorch-triton-rocm"}
# Ride along with a constrained parent package: upgraded (or held) via the
# parent's requirement spec so pip keeps the set internally consistent.
PIN_COMPANIONS: dict[str, set[str]] = {
    "pyqt6": {"pyqt6-qt6", "pyqt6-sip"},
}


def _norm_pkg(name: str) -> str:
    return name.lower().replace("_", "-")


def get_pyproject_constraints() -> dict[str, str]:
    """Read version-constrained deps from pyproject [project.dependencies].

    Returns {normalized-name: full requirement spec} for every dependency
    that carries any version specifier (==, <, <=, >=, ~=, !=).
    """
    import tomllib

    try:
        with open(PYPROJECT_PATH, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    constraints: dict[str, str] = {}
    for req in data.get("project", {}).get("dependencies", []):
        spec = req.split("#", 1)[0].strip()
        for op in ("==", "<=", ">=", "~=", "!=", "<", ">"):
            if op in spec:
                name = spec.split(op, 1)[0].strip().split("[")[0]
                constraints[_norm_pkg(name)] = spec
                break
    return constraints


def get_pyproject_pins() -> dict[str, str]:
    """Read exact (==) pins from pyproject [project.dependencies]."""
    pins: dict[str, str] = {}
    for name, spec in get_pyproject_constraints().items():
        if "==" in spec and not any(op in spec for op in ("<", ">", "~", "!")):
            pins[name] = spec.partition("==")[2].strip()
    return pins


def detect_rocm_index() -> str:
    """Pick the ROCm wheel index matching the installed torch build."""
    try:
        result = subprocess.run(
            [
                str(VENV_PYTHON),
                "-c",
                "from importlib.metadata import version; print(version('torch'))",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        installed = result.stdout.strip()
        if "+rocm" in installed:
            tag = installed.split("+", 1)[1]  # e.g. "rocm7.2"
            return f"https://download.pytorch.org/whl/{tag}"
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ROCM_OPTIONS[-1][1]  # newest stable channel in the list


# Log colors
COLOR_INFO = "#888"
COLOR_SUCCESS = "#00AA00"
COLOR_WARNING = "#E0A800"
COLOR_ERROR = "#dc3545"
COLOR_CMD = "#5599DD"


# =============================================================================
# Helpers
# =============================================================================


def pip_cmd(*args: str) -> list[str]:
    """Build a pip command list using the venv Python."""
    return [str(VENV_PYTHON), "-m", "pip", *args]


def venv_exists() -> bool:
    """Check if the venv exists and has a working Python."""
    return VENV_PYTHON.is_file()


def get_venv_python_version() -> str | None:
    """Get the Python version string from the venv, or None."""
    if not venv_exists():
        return None
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# =============================================================================
# Signals
# =============================================================================


class SetupSignals(QObject):
    """Signals emitted by worker threads."""

    log = pyqtSignal(str)  # Log line (plain text, colored by receiver)
    progress = pyqtSignal(float)  # 0.0 to 1.0
    status = pyqtSignal(str)  # Short status text
    finished = pyqtSignal(bool, str)  # (success, message)


# =============================================================================
# Workers
# =============================================================================


class SubprocessWorker(QRunnable):
    """Runs a subprocess command with live stdout/stderr streaming."""

    def __init__(
        self,
        cmd: list[str],
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        label: str = "",
    ):
        super().__init__()
        self.cmd = cmd
        self.cwd = str(cwd) if cwd else str(PROJECT_DIR)
        self.env = env
        self.label = label
        self.signals = SetupSignals()
        self.cancelled = False
        self.process: subprocess.Popen[str] | None = None

    def _emit_log(self, msg: str) -> None:
        try:
            self.signals.log.emit(msg)
        except RuntimeError:
            pass

    def _emit_status(self, msg: str) -> None:
        try:
            self.signals.status.emit(msg)
        except RuntimeError:
            pass

    @pyqtSlot()
    def run(self) -> None:
        if self.label:
            self._emit_status(self.label)
            self._emit_log(f"[CMD] {' '.join(self.cmd)}")

        run_env = os.environ.copy()
        if self.env:
            run_env.update(self.env)

        try:
            self.process = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.cwd,
                env=run_env,
                text=True,
                bufsize=1,
            )
            assert self.process.stdout is not None
            for line in iter(self.process.stdout.readline, ""):
                if self.cancelled:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                    self._emit_log("[CANCELLED]")
                    self.signals.finished.emit(False, "Cancelled")
                    return
                self._emit_log(line.rstrip())

            self.process.wait()
            rc = self.process.returncode
            if rc == 0:
                self.signals.finished.emit(True, f"{self.label or 'Command'} completed")
            else:
                self._emit_log(f"[ERROR] Process exited with code {rc}")
                self.signals.finished.emit(False, f"Failed (exit code {rc})")

        except FileNotFoundError:
            self._emit_log(f"[ERROR] Command not found: {self.cmd[0]}")
            self.signals.finished.emit(False, f"Command not found: {self.cmd[0]}")
        except OSError as e:
            self._emit_log(f"[ERROR] {e}")
            self.signals.finished.emit(False, str(e))


# =============================================================================
# Dialogs
# =============================================================================


class RadioChoiceDialog(QDialog):
    """Simple dialog with radio button choices."""

    def __init__(
        self,
        title: str,
        message: str,
        options: list[str],
        default_index: int = 0,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(message))

        self.button_group = QButtonGroup(self)
        self.radios: list[QRadioButton] = []
        for i, option in enumerate(options):
            radio = QRadioButton(option)
            if i == default_index:
                radio.setChecked(True)
            self.button_group.addButton(radio, i)
            self.radios.append(radio)
            layout.addWidget(radio)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_index(self) -> int:
        return self.button_group.checkedId()


# =============================================================================
# Controller
# =============================================================================


class SetupController:
    """Business logic: maps UI actions to worker creation and step chaining."""

    def __init__(self, window: SetupWindow):
        self.window = window
        self._current_worker: QRunnable | None = None
        self._step_chain: list[tuple[str, list[str], dict[str, str] | None]] = []
        self._step_index: int = 0

    # -- State checks --

    def refresh_venv_status(self) -> None:
        """Update UI based on current venv state."""
        version = get_venv_python_version()
        if version:
            self.window.set_venv_status(f".venv ({version})")
            self.window.set_venv_buttons_enabled(True)
        else:
            self.window.set_venv_status("No venv found")
            self.window.set_venv_buttons_enabled(False)

    # -- Worker management --

    def _start_worker(self, worker: QRunnable) -> None:
        self._current_worker = worker
        self.window.set_running(True)
        QThreadPool.globalInstance().start(worker)

    def _on_step_finished(self, success: bool, message: str) -> None:
        """Handle completion of one step in a chain."""
        if not success:
            self.window.log_error(f"Step failed: {message}")
            self.window.set_running(False)
            self.refresh_venv_status()
            return

        self._step_index += 1
        if self._step_index < len(self._step_chain):
            label, cmd, env = self._step_chain[self._step_index]
            self.window.log_success(
                f"Step {self._step_index}/{len(self._step_chain)} done"
            )
            progress = self._step_index / len(self._step_chain)
            self.window.set_progress(progress)
            self._run_subprocess(
                cmd, label=label, env=env, on_finished=self._on_step_finished
            )
        else:
            self.window.log_success("All steps completed!")
            self.window.set_progress(1.0)
            self.window.set_running(False)
            self.refresh_venv_status()

    def _run_subprocess(
        self,
        cmd: list[str],
        label: str = "",
        env: dict[str, str] | None = None,
        on_finished: Any = None,
    ) -> None:
        worker = SubprocessWorker(cmd, label=label, env=env)
        worker.signals.log.connect(self.window.append_log)
        worker.signals.status.connect(self.window.set_status)
        if on_finished:
            worker.signals.finished.connect(on_finished)
        else:
            worker.signals.finished.connect(self._default_finished)
        self._start_worker(worker)

    def _run_chain(
        self,
        steps: list[tuple[str, list[str], dict[str, str] | None]],
    ) -> None:
        """Run a sequence of subprocess steps."""
        if not steps:
            return
        self._step_chain = steps
        self._step_index = 0
        self.window.set_progress(0.0)
        label, cmd, env = steps[0]
        self.window.log_info(f"Running {len(steps)} steps...")
        self._run_subprocess(
            cmd, label=label, env=env, on_finished=self._on_step_finished
        )

    def _default_finished(self, success: bool, message: str) -> None:
        if success:
            self.window.log_success(message)
        else:
            self.window.log_error(message)
        self.window.set_running(False)
        self.refresh_venv_status()

    def cancel(self) -> None:
        """Cancel the current operation."""
        if self._current_worker is not None:
            if hasattr(self._current_worker, "cancelled"):
                self._current_worker.cancelled = True  # type: ignore[union-attr]
            if hasattr(self._current_worker, "process"):
                proc = self._current_worker.process  # type: ignore[union-attr]
                if proc is not None:
                    proc.terminate()
        self._step_chain = []
        self._step_index = 0

    # -- Helpers --

    @staticmethod
    def _patch_venv_qt_theme() -> bool:
        """Patch the venv activate script to use system Qt plugins for KDE theming.

        This lets venv-installed PyQt6 pick up the Breeze/KDE platform theme
        from the system Qt plugin dir instead of falling back to Fusion.
        """
        activate = VENV_DIR / "bin" / "activate"
        if not activate.is_file():
            return False

        # Debian/Ubuntu use the multiarch path
        qt_plugin_dir = Path("/usr/lib/x86_64-linux-gnu/qt6/plugins")
        if not qt_plugin_dir.is_dir():
            # Fallback for non-multiarch distros (Arch, Fedora, etc.)
            qt_plugin_dir = Path("/usr/lib/qt6/plugins")
        if not qt_plugin_dir.is_dir():
            return False

        marker = "# --- Qt/KDE theme fix (added by setup_gui) ---"
        content = activate.read_text(encoding="utf-8")
        if marker in content:
            return True  # Already patched

        patch = (
            f"\n{marker}\n"
            f'export QT_PLUGIN_PATH="{qt_plugin_dir}"\n'
            'export QT_QPA_PLATFORMTHEME="kde"\n'
            "unset QT_STYLE_OVERRIDE\n"
            "# --- end Qt/KDE theme fix ---\n"
        )
        activate.write_text(content + patch, encoding="utf-8")
        return True

    # -- Actions --

    def full_setup(self) -> None:
        """Create venv, upgrade pip, install core + dev deps."""
        self.window.clear_log()
        self.window.log_info("Starting full setup...")
        self.window.log_info(f"Base interpreter: {sys.executable}")

        steps: list[tuple[str, list[str], dict[str, str] | None]] = [
            (
                "Creating virtual environment...",
                [sys.executable, "-m", "venv", str(VENV_DIR)],
                None,
            ),
            ("Upgrading pip...", pip_cmd("install", "--upgrade", "pip"), None),
            ("Installing core dependencies...", pip_cmd("install", "-e", "."), None),
            (
                "Installing dev dependencies...",
                pip_cmd("install", "-e", ".[dev]"),
                None,
            ),
        ]

        self._run_chain(steps)

    def install_dev_deps(self) -> None:
        """Install dev dependencies only."""
        self.window.clear_log()
        self._run_subprocess(
            pip_cmd("install", "-e", ".[dev]"),
            label="Installing dev dependencies...",
        )

    def update_packages(self) -> None:
        """Check for outdated packages and offer to update."""
        self.window.clear_log()
        self.window.log_info("Checking for outdated packages...")
        self.window.set_running(True)

        worker = SubprocessWorker(
            pip_cmd("list", "--outdated", "--format=json"),
            label="Checking for updates...",
        )
        self._output_lines: list[str] = []

        def capture_log(line: str) -> None:
            self.window.append_log(line)
            self._output_lines.append(line)

        def on_check_done(success: bool, message: str) -> None:
            self.window.set_running(False)
            if not success:
                self.window.log_error("Failed to check for updates")
                return

            # Parse JSON from captured output (skip non-JSON lines)
            json_text = ""
            for line in self._output_lines:
                if line.startswith("[CMD]"):
                    continue
                stripped = line.strip()
                if stripped.startswith("["):
                    json_text = stripped
                    break

            if not json_text or json_text == "[]":
                self.window.log_success("All packages are up to date!")
                return

            try:
                outdated = json.loads(json_text)
            except json.JSONDecodeError:
                self.window.log_error("Could not parse pip output")
                return

            if not outdated:
                self.window.log_success("All packages are up to date!")
                return

            # Show outdated packages
            self.window.log_info(f"Found {len(outdated)} outdated packages:")
            for pkg in outdated:
                self.window.append_log(
                    f"  {pkg['name']:30s} {pkg['version']:15s} "
                    f"-> {pkg['latest_version']}"
                )

            # Partition: pinned holds / capped ranges / ROCm-routed / plain.
            # A blind `pip install --upgrade` would replace ROCm torch with
            # the CUDA build and pinned packages with OS-incompatible ones.
            constraints = get_pyproject_constraints()
            pins = get_pyproject_pins()
            held: list[tuple[str, str]] = []  # (name, reason)
            capped: list[str] = []  # requirement specs, upgraded within range
            rocm: list[str] = []
            plain: list[str] = []
            for pkg in outdated:
                name = pkg["name"]
                n = _norm_pkg(name)
                companion_of = next(
                    (
                        p
                        for p, comps in PIN_COMPANIONS.items()
                        if p in constraints and n in comps
                    ),
                    None,
                )
                if n in pins:
                    held.append((name, f"pinned =={pins[n]} in pyproject"))
                elif companion_of:
                    held.append(
                        (name, f"managed via constrained {companion_of} upgrade")
                    )
                elif n in constraints:
                    capped.append(constraints[n])
                elif n in ROCM_MANAGED_PACKAGES:
                    held.append((name, "managed by the torch ROCm upgrade"))
                elif n.startswith("nvidia-") or n.startswith("cuda-"):
                    held.append((name, "NVIDIA package on a ROCm system"))
                elif n in ROCM_ROUTED_PACKAGES:
                    rocm.append(name)
                else:
                    plain.append(name)

            if held:
                self.window.log_warning("Held back (see pyproject comments):")
                for name, reason in held:
                    self.window.append_log(f"  {name}: {reason}")

            steps: list[tuple[str, list[str], dict[str, str] | None]] = []
            if plain:
                steps.append(
                    (
                        f"Updating {len(plain)} packages...",
                        pip_cmd("install", "--upgrade", *plain),
                        None,
                    )
                )
            if capped:
                self.window.log_info(
                    "Upgrading within pyproject caps: " + ", ".join(capped)
                )
                steps.append(
                    (
                        f"Updating {len(capped)} capped packages within range...",
                        pip_cmd("install", "--upgrade", *capped),
                        None,
                    )
                )
            if rocm:
                index_url = detect_rocm_index()
                self.window.log_info(f"GPU stack routed via ROCm index: {index_url}")
                steps.append(
                    (
                        f"Updating GPU stack ({', '.join(rocm)}) via ROCm...",
                        pip_cmd(
                            "install",
                            "--upgrade",
                            *rocm,
                            "--index-url",
                            index_url,
                        ),
                        None,
                    )
                )

            if not steps:
                self.window.log_success(
                    "Nothing to update (all outdated packages are held back)."
                )
                return

            self.window.set_running(True)
            self._run_chain(steps)

        worker.signals.log.connect(capture_log)
        worker.signals.status.connect(self.window.set_status)
        worker.signals.finished.connect(on_check_done)
        self._start_worker(worker)

    def show_installed(self) -> None:
        """Show all installed packages."""
        self.window.clear_log()
        self.window.log_info("Fetching installed packages...")
        self.window.set_running(True)

        worker = SubprocessWorker(
            pip_cmd("list", "--format=json"),
            label="Listing packages...",
        )
        self._installed_lines: list[str] = []

        def capture_log(line: str) -> None:
            self._installed_lines.append(line)

        def on_done(success: bool, message: str) -> None:
            self.window.set_running(False)
            if not success:
                self.window.log_error("Failed to list packages")
                return

            json_text = ""
            for line in self._installed_lines:
                if line.startswith("[CMD]"):
                    continue
                stripped = line.strip()
                if stripped.startswith("["):
                    json_text = stripped
                    break

            try:
                packages = json.loads(json_text)
            except (json.JSONDecodeError, ValueError):
                self.window.log_error("Could not parse pip output")
                return

            packages.sort(key=lambda p: p["name"].lower())

            ai_kw = {"torch", "isc", "numba", "llvmlite", "onnx"}
            media_kw = {
                "av",
                "ffmpeg",
                "opencv",
                "pillow",
                "vapour",
                "scene",
                "librosa",
                "soundfile",
                "soxr",
                "videohash",
                "imagehash",
                "videotimestamps",
            }
            gui_kw = {"pyqt", "qt", "matplotlib"}

            def categorize(name: str) -> str:
                nl = name.lower()
                for kw in ai_kw:
                    if kw in nl:
                        return "AI/ML"
                for kw in media_kw:
                    if kw in nl:
                        return "Media"
                for kw in gui_kw:
                    if kw in nl:
                        return "GUI"
                return ""

            self.window.append_log(f"{'Package':<40} {'Version':<20} {'Category'}")
            self.window.append_log("-" * 70)
            for pkg in packages:
                name = pkg["name"]
                version = pkg["version"]
                cat = categorize(name)
                self.window.append_log(f"{name:<40} {version:<20} {cat}")

            self.window.log_success(f"Total: {len(packages)} packages installed")

        worker.signals.log.connect(capture_log)
        worker.signals.status.connect(self.window.set_status)
        worker.signals.finished.connect(on_done)
        self._start_worker(worker)

    def verify_deps(self) -> None:
        """Verify all pyproject.toml dependencies are installed."""
        self.window.clear_log()
        self.window.log_info("Verifying dependencies from pyproject.toml...")
        self.window.set_running(True)

        # Run verification in a subprocess using venv python
        pyproject_str = str(PYPROJECT_PATH)
        script = (
            "import sys, tomllib, subprocess, re\n"
            "from pathlib import Path\n"
            "\n"
            "pyproject = Path(" + repr(pyproject_str) + ")\n"
            "with open(pyproject, 'rb') as f:\n"
            "    config = tomllib.load(f)\n"
            "\n"
            "deps = config.get('project', {}).get('dependencies', [])\n"
            "optional = config.get('project', {}).get('optional-dependencies', {})\n"
            "\n"
            "def get_version(pkg_name):\n"
            "    normalized = pkg_name.lower().replace('_', '-').split('[')[0]\n"
            "    r = subprocess.run([sys.executable, '-m', 'pip', 'show', normalized],\n"
            "                       capture_output=True, text=True)\n"
            "    if r.returncode == 0:\n"
            "        for line in r.stdout.split('\\n'):\n"
            "            if line.startswith('Version:'):\n"
            "                return line.split(':', 1)[1].strip()\n"
            "    return None\n"
            "\n"
            "def parse_name(dep):\n"
            "    m = re.match(r'^([a-zA-Z0-9_-]+)', dep.replace('_', '-'))\n"
            "    return m.group(1).lower() if m else dep.lower()\n"
            "\n"
            "missing = []\n"
            "print('Core dependencies:')\n"
            "for dep in deps:\n"
            "    name = parse_name(dep)\n"
            "    ver = get_version(name)\n"
            "    if ver:\n"
            "        print(f'  OK {name} ({ver})')\n"
            "    else:\n"
            "        missing.append(name)\n"
            "        print(f'  MISSING {name}')\n"
            "\n"
            "print()\n"
            "print('Optional dependencies:')\n"
            "for group, gdeps in optional.items():\n"
            "    print(f'  [{group}]:')\n"
            "    for dep in gdeps:\n"
            "        name = parse_name(dep)\n"
            "        ver = get_version(name)\n"
            "        if ver:\n"
            "            print(f'    OK {name} ({ver})')\n"
            "        else:\n"
            "            print(f'    NOTINSTALLED {name}')\n"
            "\n"
            "print()\n"
            "if missing:\n"
            "    print(f'RESULT_MISSING {len(missing)}: ' + ', '.join(missing))\n"
            "else:\n"
            "    print('RESULT_OK All core dependencies installed!')\n"
        )
        worker = SubprocessWorker(
            [str(VENV_PYTHON), "-c", script],
            label="Verifying dependencies...",
        )
        self._verify_lines: list[str] = []

        def capture_log(line: str) -> None:
            self.window.append_log(line)
            self._verify_lines.append(line)

        def on_verify_done(success: bool, message: str) -> None:
            # Check if there were missing packages
            for line in self._verify_lines:
                if line.startswith("RESULT_MISSING"):
                    self.window.log_info("Installing missing dependencies...")
                    self._run_subprocess(
                        pip_cmd("install", "-e", "."),
                        label="Installing missing dependencies...",
                    )
                    return
            self.window.log_success(message)
            self.window.set_running(False)
            self.refresh_venv_status()

        worker.signals.log.connect(capture_log)
        worker.signals.status.connect(self.window.set_status)
        worker.signals.finished.connect(on_verify_done)
        self._start_worker(worker)

    def install_pytorch_gpu(self) -> None:
        """Install PyTorch with GPU support (ROCm/CUDA)."""
        labels = [label for label, _ in GPU_OPTIONS]
        dialog = RadioChoiceDialog(
            "GPU / PyTorch Setup",
            "Select your hardware for GPU-accelerated correlation\n"
            "and neural frame matching.",
            labels,
            default_index=0,
            parent=self.window,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        idx = dialog.selected_index()
        _, gpu_type = GPU_OPTIONS[idx]

        self.window.clear_log()

        steps: list[tuple[str, list[str], dict[str, str] | None]] = []

        if gpu_type != "cpu":
            # pip treats an installed torch of any build (e.g. the default
            # PyPI CUDA wheel) as "requirement already satisfied" and would
            # skip the index install entirely — remove it first, plus stray
            # companions from earlier installs.
            steps.append(
                (
                    "Removing existing PyTorch build...",
                    pip_cmd(
                        "uninstall",
                        "-y",
                        "torch",
                        "torchvision",
                        "torchaudio",
                        "triton",
                        "pytorch-triton-rocm",
                    ),
                    None,
                )
            )

        if gpu_type == "cuda":
            steps.append(
                (
                    "Installing CUDA-enabled PyTorch...",
                    pip_cmd(
                        "install",
                        "torch",
                        "--index-url",
                        "https://download.pytorch.org/whl/cu124",
                    ),
                    None,
                )
            )
        elif gpu_type == "rocm64":
            steps.append(
                (
                    "Installing PyTorch with ROCm 6.4...",
                    pip_cmd(
                        "install",
                        "torch",
                        "--index-url",
                        "https://download.pytorch.org/whl/rocm6.4",
                    ),
                    None,
                )
            )
        elif gpu_type == "rocm72":
            steps.append(
                (
                    "Installing PyTorch with ROCm 7.2...",
                    pip_cmd(
                        "install",
                        "torch",
                        "--index-url",
                        "https://download.pytorch.org/whl/rocm7.2",
                    ),
                    None,
                )
            )
        elif gpu_type == "cpu":
            self.window.log_info(
                "Keeping the default PyPI PyTorch build (CPU fallback)."
            )
            return

        # Verification step
        verify_script = """
import sys
try:
    import torch
    print(f"PyTorch version: {torch.__version__}")
    hip = getattr(torch.version, 'hip', None)
    if hip:
        print(f"ROCm/HIP version: {hip}")
    print(f"GPU available: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            print(f"  GPU {i}: {torch.cuda.get_device_name(i)}")
        print("OK GPU is working!")
    else:
        print("WARNING GPU not detected. CPU fallback will be used.")
except Exception as e:
    print(f"ERROR {e}")
    sys.exit(1)
"""
        # Pin verification to the discrete GPU — on dual-GPU systems the
        # iGPU otherwise gets initialized and can crash the check. Respect
        # an explicit override from the user's environment.
        verify_env = (
            None
            if "HIP_VISIBLE_DEVICES" in os.environ
            else {"HIP_VISIBLE_DEVICES": "0"}
        )
        steps.append(
            (
                "Verifying PyTorch installation...",
                [str(VENV_PYTHON), "-c", verify_script],
                verify_env,
            )
        )

        self._run_chain(steps)

    def rebuild_pyav(self) -> None:
        """Rebuild PyAV from source against system FFmpeg (optional, for
        optimized decoding). The version is read from the pyproject pin so
        the source build matches what Debian's FFmpeg supports."""
        pinned = get_pyproject_pins().get("av")
        av_spec = f"av=={pinned}" if pinned else "av"
        self.window.clear_log()
        self.window.log_info(
            f"Rebuilding PyAV ({av_spec}) from source against system FFmpeg..."
        )
        self._run_subprocess(
            pip_cmd(
                "install",
                "--no-binary",
                "av",
                "--no-cache-dir",
                "--force-reinstall",
                av_spec,
            ),
            label="Rebuilding PyAV from source...",
        )

    def fix_qt_kde_theme(self) -> None:
        """Patch venv activate script for KDE/Breeze Qt theming."""
        self.window.clear_log()
        self.window.log_info("Applying Qt/KDE theme fix to venv...")
        if self._patch_venv_qt_theme():
            self.window.log_success(
                "Qt/KDE theme fix applied. "
                "Venv activate script now exports QT_PLUGIN_PATH "
                "and QT_QPA_PLATFORMTHEME=kde."
            )
        else:
            self.window.log_error(
                "Could not apply fix. Check that the venv exists "
                "and the system Qt plugin dir is available."
            )

    def fix_rocm_pytorch(self) -> None:
        """Remove NVIDIA packages and reinstall PyTorch with ROCm."""
        labels = [label for label, _, _ in ROCM_OPTIONS]
        dialog = RadioChoiceDialog(
            "Fix ROCm / PyTorch",
            "This will:\n"
            "  1. Remove NVIDIA CUDA packages\n"
            "  2. Uninstall torch (and stray torchaudio/torchvision)\n"
            "  3. Reinstall PyTorch with ROCm support\n\n"
            "Select ROCm version:",
            labels,
            default_index=1,
            parent=self.window,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        idx = dialog.selected_index()
        _, rocm_url, is_nightly = ROCM_OPTIONS[idx]

        self.window.clear_log()
        self.window.log_info(f"Fixing ROCm/PyTorch with {labels[idx]}...")

        # Detect NVIDIA packages to remove
        nvidia_pkgs: list[str] = []
        try:
            result = subprocess.run(
                pip_cmd("list", "--format=json"),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                pkgs = json.loads(result.stdout)
                for pkg in pkgs:
                    name = pkg["name"].lower()
                    if name.startswith("nvidia") or name.startswith("cuda-"):
                        nvidia_pkgs.append(pkg["name"])
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass

        steps: list[tuple[str, list[str], dict[str, str] | None]] = []

        if nvidia_pkgs:
            self.window.log_info(
                f"Found {len(nvidia_pkgs)} NVIDIA/CUDA packages to remove"
            )
            steps.append(
                (
                    "Removing NVIDIA/CUDA packages...",
                    pip_cmd("uninstall", "-y", *nvidia_pkgs),
                    None,
                )
            )

        steps.append(
            (
                "Uninstalling torch, torchaudio...",
                pip_cmd(
                    "uninstall",
                    "-y",
                    "torch",
                    "torchvision",
                    "torchaudio",
                    "triton",
                    "pytorch-triton-rocm",
                ),
                None,
            )
        )

        torch_cmd = pip_cmd(
            "install", "torch", "--index-url", rocm_url
        )
        if is_nightly:
            torch_cmd = pip_cmd(
                "install",
                "--pre",
                "torch",
                "--index-url",
                rocm_url,
            )

        steps.append(
            (
                f"Installing PyTorch with {labels[idx]}...",
                torch_cmd,
                None,
            )
        )

        # Verification step
        verify_script = """
import sys
try:
    import torch
    print(f"PyTorch version: {torch.__version__}")
    hip = getattr(torch.version, 'hip', None)
    if hip:
        print(f"ROCm/HIP version: {hip}")
    else:
        print("ROCm/HIP: Not detected")
    print(f"GPU available: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            print(f"  GPU {i}: {torch.cuda.get_device_name(i)}")
        print("OK GPU is working!")
    else:
        print("WARNING GPU not detected. Check ROCm installation.")
except Exception as e:
    print(f"ERROR {e}")
    sys.exit(1)
"""
        # Pin verification to the discrete GPU — on dual-GPU systems the
        # iGPU otherwise gets initialized and can crash the check. Respect
        # an explicit override from the user's environment.
        verify_env = (
            None
            if "HIP_VISIBLE_DEVICES" in os.environ
            else {"HIP_VISIBLE_DEVICES": "0"}
        )
        steps.append(
            (
                "Verifying PyTorch installation...",
                [str(VENV_PYTHON), "-c", verify_script],
                verify_env,
            )
        )

        self._run_chain(steps)


# =============================================================================
# Main Window
# =============================================================================


class SetupWindow(QMainWindow):
    """Main setup GUI window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Remux Toolkit - Environment Setup")
        self.setMinimumSize(900, 600)
        self.resize(1000, 700)

        self.controller = SetupController(self)
        self._action_buttons: list[QPushButton] = []

        self._build_ui()
        self.controller.refresh_venv_status()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # -- Left sidebar --
        sidebar = QWidget()
        sidebar.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(4)

        # Wrap sidebar in scroll area for small screens
        scroll = QScrollArea()
        scroll.setWidget(sidebar)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(240)

        # Setup group
        self._add_group(
            sidebar_layout,
            "Setup",
            [
                ("Full Setup", self.controller.full_setup),
                ("Install Dev Tools", self.controller.install_dev_deps),
            ],
        )

        # Packages group
        self._add_group(
            sidebar_layout,
            "Packages",
            [
                ("Update Packages", self.controller.update_packages),
                ("Show Installed", self.controller.show_installed),
                ("Verify Dependencies", self.controller.verify_deps),
            ],
        )

        # Optional group
        self._add_group(
            sidebar_layout,
            "Optional",
            [
                ("GPU / PyTorch", self.controller.install_pytorch_gpu),
                ("Rebuild PyAV", self.controller.rebuild_pyav),
            ],
        )

        # Fixes group
        self._add_group(
            sidebar_layout,
            "Fixes",
            [
                ("Fix Qt/KDE Theme", self.controller.fix_qt_kde_theme),
                ("Fix ROCm / PyTorch", self.controller.fix_rocm_pytorch),
            ],
        )

        # Cancel button (hidden by default)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setStyleSheet(
            "QPushButton { background-color: #dc3545; color: white; "
            "padding: 6px; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #c82333; }"
        )
        self.cancel_btn.clicked.connect(self.controller.cancel)
        self.cancel_btn.setVisible(False)
        sidebar_layout.addWidget(self.cancel_btn)

        sidebar_layout.addStretch()

        # -- Right panel --
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Log output
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("monospace", 9))
        self.log_output.setStyleSheet(
            "QTextEdit { background-color: #1e1e1e; color: #d4d4d4; "
            "border: 1px solid #333; }"
        )
        right_layout.addWidget(self.log_output)

        # Progress area
        progress_widget = QWidget()
        progress_layout = QHBoxLayout(progress_widget)
        progress_layout.setContentsMargins(0, 4, 0, 0)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(16)
        progress_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(f"color: {COLOR_INFO};")
        self.status_label.setMinimumWidth(200)
        progress_layout.addWidget(self.status_label)

        right_layout.addWidget(progress_widget)

        # -- Assemble --
        main_layout.addWidget(scroll)
        main_layout.addWidget(right, 1)

        # -- Status bar --
        self.venv_label = QLabel("Venv: checking...")
        self.statusBar().addPermanentWidget(self.venv_label)

    def _add_group(
        self,
        parent_layout: QVBoxLayout,
        title: str,
        buttons: list[tuple[str, Any]],
    ) -> None:
        group = QGroupBox(title)
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(3)
        group_layout.setContentsMargins(6, 10, 6, 6)

        for label, callback in buttons:
            btn = QPushButton(label)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(callback)
            group_layout.addWidget(btn)
            self._action_buttons.append(btn)

        parent_layout.addWidget(group)

    # -- Public interface for controller --

    def append_log(self, line: str) -> None:
        """Append a log line with auto-coloring."""
        escaped = html.escape(line)

        if line.startswith("[CMD]"):
            colored = f'<span style="color: {COLOR_CMD};">{escaped}</span>'
        elif line.startswith("[ERROR]") or line.startswith("ERROR"):
            colored = f'<span style="color: {COLOR_ERROR};">{escaped}</span>'
        elif line.startswith("[WARNING]") or line.startswith("WARNING"):
            colored = f'<span style="color: {COLOR_WARNING};">{escaped}</span>'
        elif line.startswith("[OK]") or line.startswith("OK "):
            colored = f'<span style="color: {COLOR_SUCCESS};">{escaped}</span>'
        elif line.startswith("[SKIP]"):
            colored = f'<span style="color: {COLOR_INFO};">{escaped}</span>'
        elif line.startswith("[CANCELLED]"):
            colored = f'<span style="color: {COLOR_WARNING};">{escaped}</span>'
        elif line.startswith("RESULT_OK"):
            colored = f'<span style="color: {COLOR_SUCCESS};">{escaped}</span>'
        elif line.startswith("RESULT_MISSING") or (
            "MISSING" in line and not line.startswith(" ")
        ):
            colored = f'<span style="color: {COLOR_ERROR};">{escaped}</span>'
        else:
            colored = escaped

        self.log_output.append(colored)
        # Auto-scroll to bottom
        cursor = self.log_output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_output.setTextCursor(cursor)

    def log_info(self, msg: str) -> None:
        self.append_log(f"[INFO] {msg}")

    def log_success(self, msg: str) -> None:
        self.append_log(f"[OK] {msg}")

    def log_warning(self, msg: str) -> None:
        self.append_log(f"[WARNING] {msg}")

    def log_error(self, msg: str) -> None:
        self.append_log(f"[ERROR] {msg}")

    def clear_log(self) -> None:
        self.log_output.clear()

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def set_progress(self, value: float) -> None:
        self.progress_bar.setValue(int(value * 1000))

    def set_venv_status(self, text: str) -> None:
        self.venv_label.setText(f"Venv: {text}")

    def set_venv_buttons_enabled(self, enabled: bool) -> None:
        """Enable/disable buttons that require a venv to exist.

        Full Setup is always enabled (it creates the venv).
        """
        for btn in self._action_buttons:
            if btn.text() == "Full Setup":
                continue
            btn.setEnabled(enabled)

    def set_running(self, running: bool) -> None:
        """Toggle UI state when an operation is in progress."""
        for btn in self._action_buttons:
            btn.setEnabled(not running)
        self.cancel_btn.setVisible(running)
        if not running:
            self.set_progress(0.0)
            self.set_status("Ready")
            # Re-check what buttons should be enabled
            self.controller.refresh_venv_status()


# =============================================================================
# Entry point
# =============================================================================


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Remux Toolkit Setup")

    window = SetupWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
