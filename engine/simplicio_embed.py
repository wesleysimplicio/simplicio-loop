#!/usr/bin/env python3
"""simplicio_embed — REAL sentence embeddings via the exact upstream model.

This binds Simplicio's RAG/memory retrieval to the SAME embedder headroom uses:
``Qdrant/all-MiniLM-L6-v2-onnx`` on HuggingFace (see headroom/memory/adapters/
embedders.py: ``ONNX_REPO = "Qdrant/all-MiniLM-L6-v2-onnx"``). It loads the public
ONNX ``model.onnx`` + ``tokenizer.json`` and runs the standard sentence-transformer
pipeline so retrieval matches upstream byte-for-byte in semantics:

    tokenize → ONNX (input_ids, attention_mask, token_type_ids)
             → last_hidden_state
             → masked mean-pooling over tokens
             → L2-normalize
             → 384-dim sentence embedding

It is **dependency-gated**: needs ``onnxruntime``, ``huggingface_hub`` and
``tokenizers``. If they (or the model) are absent, ``embed_available()`` is False and
the CLI exits 3 with an install hint — never fake embeddings. The repo is overridable
via env ``SIMPLICIO_EMBED_ONNX_REPO`` (default the upstream Qdrant repo).

    pip install onnxruntime huggingface_hub tokenizers numpy
    python3 simplicio_embed.py search "<query>" [--top N]   # over the CCR memory store
    python3 simplicio_embed.py embed                         # stdin → vector dim + head
    python3 simplicio_embed.py --info                        # ONNX signature
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

ONNX_REPO = os.environ.get("SIMPLICIO_EMBED_ONNX_REPO", "Qdrant/all-MiniLM-L6-v2-onnx")
MODEL_FILE = "model.onnx"
TOKENIZER_FILE = "tokenizer.json"
EMBED_DIM = 384
_MAX_LEN = 256  # all-MiniLM-L6-v2 truncates at 256 tokens upstream

_INSTALL_HINT = (
    "embedder model not available — "
    "pip install onnxruntime huggingface_hub tokenizers"
)

_session = None
_tokenizer = None
_input_names = None
_np = None


def embed_available():
    """True iff deps import AND the real model+tokenizer can be located/downloaded."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        from huggingface_hub import hf_hub_download  # noqa: F401
    except Exception:
        return False
    try:
        _resolve_files()
        return True
    except Exception:
        return False


def _resolve_files():
    """Return (model_path, tokenizer_path), downloading from the hub if needed."""
    from huggingface_hub import hf_hub_download

    model_path = hf_hub_download(ONNX_REPO, MODEL_FILE)
    tokenizer_path = hf_hub_download(ONNX_REPO, TOKENIZER_FILE)
    return model_path, tokenizer_path


def _load():
    """Lazy-load the ONNX session + tokenizer (cached). Raises if deps/model absent."""
    global _session, _tokenizer, _input_names, _np
    if _session is not None:
        return _session, _tokenizer, _input_names, _np
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    model_path, tokenizer_path = _resolve_files()
    sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(tokenizer_path)
    tok.enable_truncation(max_length=_MAX_LEN)
    tok.enable_padding()  # pad to longest in batch

    _session = sess
    _tokenizer = tok
    _input_names = {i.name for i in sess.get_inputs()}
    _np = np
    return _session, _tokenizer, _input_names, _np


def signature():
    """Return the ONNX I/O signature as a dict (for --info / proof)."""
    sess, _, _, _ = _load()
    return {
        "repo": ONNX_REPO,
        "inputs": [
            {"name": i.name, "type": i.type, "shape": list(i.shape)}
            for i in sess.get_inputs()
        ],
        "outputs": [
            {"name": o.name, "type": o.type, "shape": list(o.shape)}
            for o in sess.get_outputs()
        ],
    }


def embed(texts):
    """Embed ``texts`` → (N, 384) float32 ndarray, masked-mean-pooled + L2-normalized."""
    if isinstance(texts, str):
        texts = [texts]
    texts = [str(t) for t in texts]
    if not texts:
        sess, _, _, np = _load()
        return np.zeros((0, EMBED_DIM), dtype="float32")

    sess, tok, input_names, np = _load()
    encs = tok.encode_batch(texts)
    input_ids = np.asarray([e.ids for e in encs], dtype="int64")
    attention_mask = np.asarray([e.attention_mask for e in encs], dtype="int64")

    feeds = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
    }
    if "token_type_ids" in input_names:
        # all-MiniLM single-sentence input → all-zeros segment ids
        feeds["token_type_ids"] = np.zeros_like(input_ids)
    feeds = {k: v for k, v in feeds.items() if k in input_names}

    last_hidden_state = sess.run(None, feeds)[0]  # (N, T, 384)

    # masked mean-pooling over tokens
    mask = attention_mask.astype("float32")[:, :, None]  # (N, T, 1)
    summed = (last_hidden_state * mask).sum(axis=1)  # (N, 384)
    counts = np.clip(mask.sum(axis=1), 1e-9, None)  # (N, 1)
    pooled = summed / counts

    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (pooled / norms).astype("float32")


def search(query, docs, top_k=5):
    """Rank ``docs`` (dict key→text) by cosine to ``query``.

    Returns a list of ``(key, cosine, snippet)`` sorted descending, length ≤ top_k.
    """
    docs = dict(docs)
    if not docs:
        return []
    sess, tok, input_names, np = _load()
    keys = list(docs.keys())
    q = embed([str(query)])[0]  # (384,)
    doc_vecs = embed([docs[k] for k in keys])  # (N, 384), L2-normalized
    sims = doc_vecs @ q  # cosine, both normalized
    order = np.argsort(-sims)[: max(0, int(top_k))]
    out = []
    for idx in order:
        key = keys[int(idx)]
        text = str(docs[key]).replace("\n", " ").strip()
        snippet = text[:160] + ("…" if len(text) > 160 else "")
        out.append((key, float(sims[int(idx)]), snippet))
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────


def _memory_docs():
    """Load the CCR memory store as {key: value}. Empty dict if unavailable."""
    try:
        import simplicio_memory
    except Exception:
        return {}
    docs = {}
    for key in simplicio_memory.list_keys():
        val = simplicio_memory.recall(key)
        if val is not None:
            docs[key] = val
    return docs


def main(argv):
    parser = argparse.ArgumentParser(
        prog="simplicio_embed.py",
        description="REAL sentence embeddings (Qdrant/all-MiniLM-L6-v2-onnx) for RAG/memory.",
    )
    parser.add_argument("--info", action="store_true", help="print ONNX signature and exit")
    sub = parser.add_subparsers(dest="cmd")

    p_search = sub.add_parser("search", help="semantic search over the CCR memory store")
    p_search.add_argument("query", help="query text")
    p_search.add_argument("--top", type=int, default=5, help="number of results (default 5)")

    sub.add_parser("embed", help="embed stdin → print dim + first few values")

    args = parser.parse_args(argv)

    if not embed_available():
        print(_INSTALL_HINT, file=sys.stderr)
        return 3

    if args.info:
        import json

        print(json.dumps(signature(), indent=2))
        return 0

    if args.cmd == "search":
        docs = _memory_docs()
        if not docs:
            print("memory store is empty (nothing to search)", file=sys.stderr)
            return 1
        results = search(args.query, docs, top_k=args.top)
        for rank, (key, cos, snippet) in enumerate(results, 1):
            print(f"{rank:>2}. {cos:.4f}  {key}\n     {snippet}")
        return 0

    if args.cmd == "embed":
        text = sys.stdin.read().strip()
        if not text:
            print("no stdin text to embed", file=sys.stderr)
            return 2
        vec = embed([text])[0]
        head = ", ".join(f"{x:.5f}" for x in vec[:8])
        norm = float((vec @ vec) ** 0.5)
        print(f"dim: {len(vec)}")
        print(f"l2_norm: {norm:.6f}")
        print(f"first 8: [{head}, ...]")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
