# remux_toolkit/tools/video_ab_comparator/core/alignment_subprocess.py
"""
Subprocess worker for advanced_align (audio SCC + sliding pHash).

Isolates torch / GPU allocation in a separate process so the main app
doesn't leak several GB of host RAM per comparison run. ``torch.cuda.
empty_cache()`` does NOT reclaim the CUDA/ROCm context state; only
``os.exit()`` does. Both the audio GPU cross-correlation
(``find_delay_scc``) and the sliding pHash matcher import torch, so
they're both wrapped by this single worker.

Protocol:
- Parent writes ``AlignmentConfig`` fields as JSON to a temp file.
- Parent launches ``python -m remux_toolkit.tools.video_ab_comparator.core.alignment_subprocess``
  with --config-json + --output-json + --source-a + --source-b + fps.
- Child runs ``advanced_align()``, writes ``AlignResult`` as JSON.
- Child emits log lines to stdout; parent forwards them.
- Child prints ``__RTK_ALIGN_JSON__ {...}`` on stdout as a completion
  marker. Parent reads the JSON payload from that line.

The parent (``alignment.py::_run_advanced_align_subprocess``) parses
this marker and then loads the result JSON file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


JSON_PREFIX = "__RTK_ALIGN_JSON__ "


def _result_to_dict(r) -> dict:
    """Convert an AlignResult to a JSON-serializable dict."""
    return {
        "offset_sec": float(r.offset_sec),
        "drift_ratio": float(r.drift_ratio),
        "confidence": float(r.confidence),
        "chunk_results": _make_serializable(r.chunk_results),
        "accepted_count": int(r.accepted_count),
        "method": str(r.method),
        "offset_frames": r.offset_frames if r.offset_frames is None else int(r.offset_frames),
        "details": _make_serializable(r.details) if r.details else None,
    }


def _make_serializable(obj):
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_serializable(v) for v in obj]
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    try:
        import numpy as np
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except ImportError:
        pass
    return str(obj)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run advanced_align (audio SCC + sliding pHash) in a subprocess."
    )
    parser.add_argument("--source-a", required=True)
    parser.add_argument("--source-b", required=True)
    parser.add_argument("--config-json", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--fps-a", required=True, type=float)
    parser.add_argument("--fps-b", required=True, type=float)
    args = parser.parse_args()

    config_path = Path(args.config_json)
    output_path = Path(args.output_json)

    try:
        with open(config_path, encoding="utf-8") as f:
            config_dict = json.load(f)
    except Exception as exc:
        payload = {"success": False, "error": f"Failed to load config: {exc}"}
        print(f"{JSON_PREFIX}{json.dumps(payload)}", flush=True)
        return 1

    try:
        from remux_toolkit.tools.video_ab_comparator.core.alignment_advanced import (
            advanced_align,
            AlignmentConfig,
        )
    except Exception as exc:
        payload = {"success": False, "error": f"Failed to import advanced_align: {exc}"}
        print(f"{JSON_PREFIX}{json.dumps(payload)}", flush=True)
        return 1

    # Build config from dict. AlignmentConfig is a plain dataclass so
    # we only accept fields it actually defines.
    allowed_fields = set(AlignmentConfig.__dataclass_fields__.keys())
    filtered = {k: v for k, v in config_dict.items() if k in allowed_fields}
    try:
        config = AlignmentConfig(**filtered)
    except Exception as exc:
        payload = {"success": False, "error": f"Invalid config: {exc}"}
        print(f"{JSON_PREFIX}{json.dumps(payload)}", flush=True)
        return 1

    try:
        result = advanced_align(
            source_a_path=args.source_a,
            source_b_path=args.source_b,
            config=config,
            fps_a=args.fps_a,
            fps_b=args.fps_b,
            progress_callback=None,
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        payload = {"success": False, "error": f"advanced_align failed: {exc}"}
        print(f"{JSON_PREFIX}{json.dumps(payload)}", flush=True)
        return 1

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(_result_to_dict(result), f, indent=2, ensure_ascii=False)
    except Exception as exc:
        payload = {"success": False, "error": f"Failed to write result: {exc}"}
        print(f"{JSON_PREFIX}{json.dumps(payload)}", flush=True)
        return 1

    payload = {"success": True, "json_path": str(output_path)}
    print(f"{JSON_PREFIX}{json.dumps(payload)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
