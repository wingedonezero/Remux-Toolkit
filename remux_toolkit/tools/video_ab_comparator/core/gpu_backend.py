# remux_toolkit/tools/video_ab_comparator/core/gpu_backend.py
"""
GPU backend for correlation and neural matching.

Provides device management and cleanup utilities.
All GPU-accelerated modules share this to avoid recreating resources.

Works with both CUDA (NVIDIA) and ROCm (AMD) backends —
PyTorch maps the cuda API to HIP on ROCm automatically.

Usage:
    from .gpu_backend import get_device, to_torch, cleanup_gpu

    device = get_device()
    ref_gpu = to_torch(ref_chunk, device)
    # ... do GPU work ...
    cleanup_gpu()  # call after job finishes
"""

from __future__ import annotations

import gc
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Pin ROCm to the discrete GPU before torch initializes HIP: on a
# dual-GPU box the iGPU can SIGSEGV on first kernel launch. setdefault
# is respected only when the environment hasn't already chosen a device.
os.environ.setdefault("HIP_VISIBLE_DEVICES", "0")

# Module state
_device: Any = None  # torch.device, lazily initialized


def get_device() -> Any:
    """
    Get the torch device to use for GPU-accelerated operations.

    Returns CUDA/ROCm device if available, otherwise CPU.
    Caches the result for the process lifetime.
    """
    global _device
    if _device is not None:
        return _device

    import torch

    if torch.cuda.is_available():
        _device = torch.device("cuda")
        gpu_name = torch.cuda.get_device_name(0)
        logger.info("GPU backend: %s", gpu_name)
    else:
        _device = torch.device("cpu")
        logger.info("GPU backend: CPU fallback (no CUDA/ROCm)")

    return _device


def to_torch(arr: Any, device: Any | None = None) -> Any:
    """
    Convert a numpy array to a torch tensor on the target device.

    Args:
        arr: numpy float32 array.
        device: torch.device (uses get_device() if None).

    Returns:
        torch.Tensor on the target device.
    """
    import torch

    if device is None:
        device = get_device()
    return torch.from_numpy(arr).to(device)


def cleanup_gpu() -> None:
    """
    Release GPU resources after a job finishes.

    Call this after correlation/neural matching completes to prevent
    GPU memory accumulation. Works with both CUDA and ROCm (HIP).
    """
    gc.collect()

    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
    except ImportError:
        pass

    gc.collect()
