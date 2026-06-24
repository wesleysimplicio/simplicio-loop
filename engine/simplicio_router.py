#!/usr/bin/env python3
"""simplicio_router — real headroom technique-router ONNX classifier for Simplicio.

Wraps the PUBLIC HuggingFace model ``chopratejas/technique-router-onnx``
(Apache-2.0, from the headroom project). It is a small text classifier that
picks which compression technique to apply for a given content snippet:

    transcode | crop | preserve | full_low

Mirrors headroom's loader
(https://github.com/headroomlabs-ai/headroom — headroom/image/onnx_router.py):
files ``model_quantized.onnx`` + ``tokenizer.json`` + ``config.json``, reads
``id2label`` from config, runs the ONNX session, softmaxes the logits, argmax →
technique label.

Repo overridable via env ``SIMPLICIO_ROUTER_REPO``.

CLI:
    python3 simplicio_router.py            # reads stdin -> technique + confidence
    python3 simplicio_router.py --info     # model / ONNX signature / classes

Exit codes:
    0  ok
    3  router model / deps not available
"""

from __future__ import annotations

import math
import os
import sys
from typing import Dict, List, Optional, Tuple

_DEFAULT_REPO = "chopratejas/technique-router-onnx"
_MAX_LEN = 64

# Lazy singletons.
_session = None
_tokenizer = None
_id2label: Dict[int, str] = {}
_model_path: Optional[str] = None


def _repo() -> str:
    return os.environ.get("SIMPLICIO_ROUTER_REPO", _DEFAULT_REPO).strip() or _DEFAULT_REPO


def router_available() -> bool:
    """True iff the deps (onnxruntime, huggingface_hub, tokenizers) import and
    the model can be fetched/loaded. Cheap when already loaded; otherwise it
    attempts a full lazy load (downloads on first call)."""
    try:
        _load()
        return _session is not None
    except Exception:
        return False


def _load() -> None:
    """Lazy-load ONNX session + tokenizer + id2label. Idempotent."""
    global _session, _tokenizer, _id2label, _model_path
    if _session is not None:
        return

    import json

    import onnxruntime as ort  # noqa: F401  (raises if absent)
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    repo = _repo()

    model_path = hf_hub_download(repo, "model_quantized.onnx")
    tokenizer_path = hf_hub_download(repo, "tokenizer.json")
    config_path = hf_hub_download(repo, "config.json")

    session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])

    tokenizer = Tokenizer.from_file(tokenizer_path)
    tokenizer.enable_truncation(max_length=_MAX_LEN)
    tokenizer.enable_padding(length=_MAX_LEN)

    with open(config_path) as fh:
        config = json.load(fh)
    id2label = {int(k): v for k, v in config.get("id2label", {}).items()}
    if not id2label:
        # Fall back to positional class names if a repo lacks id2label.
        n_out = session.get_outputs()[0].shape[-1]
        n = n_out if isinstance(n_out, int) else 0
        id2label = {i: f"class_{i}" for i in range(n)}

    _model_path = model_path
    _session = session
    _tokenizer = tokenizer
    _id2label = id2label


def _input_names() -> List[str]:
    return [i.name for i in _session.get_inputs()]


def route(text: str) -> Tuple[str, float, Dict[str, float]]:
    """Route a content snippet to a compression technique.

    Returns ``(technique_label, confidence, all_scores)`` where ``all_scores``
    maps every class name to its softmax probability.
    """
    import numpy as np

    _load()

    encoded = _tokenizer.encode(text or "")
    input_ids = np.array([encoded.ids], dtype=np.int64)
    attention_mask = np.array([encoded.attention_mask], dtype=np.int64)

    feed = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
    }
    # Some BERT-family exports require token_type_ids; feed zeros if present.
    if "token_type_ids" in _input_names():
        feed["token_type_ids"] = np.zeros_like(input_ids, dtype=np.int64)

    logits = _session.run(None, feed)[0][0]

    # Numerically-stable softmax.
    m = float(np.max(logits))
    exps = np.exp(logits - m)
    probs = exps / exps.sum()

    pred_id = int(np.argmax(probs))
    confidence = float(probs[pred_id])
    label = _id2label.get(pred_id, f"class_{pred_id}")

    all_scores = {
        _id2label.get(i, f"class_{i}"): float(probs[i]) for i in range(len(probs))
    }
    return label, confidence, all_scores


def _info() -> int:
    _load()
    print(f"repo:        {_repo()}")
    print(f"model_path:  {_model_path}")
    try:
        size = os.path.getsize(_model_path)
        print(f"model_size:  {size} bytes ({size / 1024 / 1024:.2f} MB)")
    except OSError:
        pass
    print("onnx_inputs:")
    for i in _session.get_inputs():
        print(f"  - {i.name}: shape={i.shape} type={i.type}")
    print("onnx_outputs:")
    for o in _session.get_outputs():
        print(f"  - {o.name}: shape={o.shape} type={o.type}")
    print("classes (id2label):")
    for idx in sorted(_id2label):
        print(f"  {idx} -> {_id2label[idx]}")
    return 0


_UNAVAILABLE_MSG = (
    "router model not available — "
    "pip install onnxruntime huggingface_hub tokenizers"
)


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if not router_available():
        print(_UNAVAILABLE_MSG, file=sys.stderr)
        return 3

    if "--info" in argv:
        return _info()

    text = sys.stdin.read()
    if not text.strip():
        print("no input on stdin", file=sys.stderr)
        return 1

    label, confidence, all_scores = route(text)
    print(f"technique: {label}")
    print(f"confidence: {confidence:.4f}")
    ranked = sorted(all_scores.items(), key=lambda kv: kv[1], reverse=True)
    print("scores: " + ", ".join(f"{k}={v:.4f}" for k, v in ranked))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
