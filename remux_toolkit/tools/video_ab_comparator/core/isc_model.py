# remux_toolkit/tools/video_ab_comparator/core/isc_model.py
"""
ISC (Image Similarity Challenge) model management.

Handles downloading, caching, and loading the ISC feature extractor model
for neural frame matching in video alignment.

The model weights are stored in .config/isc_models/ under the project root.
On first use the weights (~85MB) are downloaded from GitHub releases.

Model: ISC ft_v107 — 256-dim descriptors from EfficientNetV2-M backbone.
Designed for near-duplicate image detection (Meta ISC21 competition winner).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def get_model_dir() -> Path:
    """Get the directory where ISC model weights are stored.

    Returns .config/isc_models/ under the project root directory.
    Creates the directory if it doesn't exist.
    """
    # Use the project root's .config directory
    project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    model_dir = project_root / ".config" / "isc_models"
    model_dir.mkdir(parents=True, exist_ok=True)
    return model_dir


def is_model_downloaded() -> bool:
    """Check if the ISC model weights have been downloaded."""
    model_dir = get_model_dir()
    return any(model_dir.glob("*.pth.tar"))


def create_isc_model(
    device: str = "cuda",
    model_dir: str | None = None,
    log=None,
):
    """Create and return the ISC model and preprocessor.

    Downloads model weights on first use to .config/isc_models/.

    Args:
        device: torch device string ("cuda" or "cpu")
        model_dir: Override model directory (defaults to .config/isc_models/)
        log: Optional log callback function

    Returns:
        Tuple of (model, preprocessor)
    """
    import torch

    if model_dir is None:
        model_dir = str(get_model_dir())

    if log:
        if not is_model_downloaded():
            log("[NeuralMatch] Downloading ISC model weights (first run, ~85MB)...")
        else:
            log("[NeuralMatch] Loading ISC model...")

    try:
        from isc_feature_extractor import create_model

        model, preprocessor = create_model(
            weight_name="isc_ft_v107",
            model_dir=model_dir,
            device=device,
        )
        model.eval()

        if log:
            n_params = sum(p.numel() for p in model.parameters())
            log(f"[NeuralMatch] ISC model loaded ({n_params/1e6:.1f}M params, {device})")

        return model, preprocessor

    except ImportError:
        if log:
            log("[NeuralMatch] ERROR: isc-feature-extractor not installed")
            log("[NeuralMatch] Install with: pip install isc-feature-extractor")
        raise
    except Exception as e:
        if log:
            log(f"[NeuralMatch] ERROR loading ISC model: {e}")
        raise
