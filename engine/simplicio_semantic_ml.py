#!/usr/bin/env python3
"""Simplicio semantic ML backend — REAL embedding-based semantic compression + RAG.

This is the optional ML path that does what deterministic code can't: it uses real sentence
embeddings to (a) drop *paraphrased / semantically-redundant* lines that TF-IDF + SimHash miss,
and (b) retrieve memories by *meaning* rather than keyword overlap.

It is **opt-in and dependency-gated**: it needs `model2vec` (static embeddings, ~30 MB, no torch)
and `numpy`. If they're absent it reports so and the caller falls back to the stdlib deterministic
`simplicio_semantic` / `simplicio_rag`. Default Simplicio stays zero-dependency.

    pip install model2vec numpy        # enable the ML backend
    simplicio-cli semantic --ml            # embedding semantic-dedup compression of stdin
    simplicio-cli rag --ml "<query>"       # embedding retrieval over the CCR memory store

Compression is reversible: dropped lines are returned in a restore map (lossless round-trip).
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_MODEL_NAME = os.environ.get("SIMPLICIO_EMBED_MODEL", "minishlab/potion-base-8M")
_model = None
_np = None


def ml_available():
    try:
        import numpy  # noqa: F401
        import model2vec  # noqa: F401
        return True
    except Exception:
        return False


def _load():
    """Lazy-load the embedding model + numpy (cached)."""
    global _model, _np
    if _model is None:
        import numpy as np
        from model2vec import StaticModel
        _np = np
        _model = StaticModel.from_pretrained(_MODEL_NAME)
    return _model, _np


def _embed(texts):
    model, np = _load()
    vecs = np.asarray(model.encode(list(texts)), dtype="float32")
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms  # L2-normalized → dot product == cosine


# ── embedding semantic-dedup compression (reversible) ────────────────────────
def semantic_dedup(text, threshold=0.75, unit="line"):
    """Drop lines that are semantically near-duplicates of an earlier KEPT line.

    Returns (compressed_text, restore_blob). Lossless: restore_blob maps each marker id to the
    exact dropped lines so `semantic_restore` reproduces the byte-exact original.
    """
    _, np = _load()
    parts = text.split("\n") if unit == "line" else re.split(r"(?<=[.!?])\s+", text)
    idx = [i for i, p in enumerate(parts) if p.strip()]
    if len(idx) < 4:
        return text, {}
    vecs = _embed([parts[i] for i in idx])
    kept_vecs = []
    drop = set()
    for n, i in enumerate(idx):
        v = vecs[n]
        # always keep structurally salient lines
        if re.search(r"\b(error|fail|exception|warn|critical|traceback)\b", parts[i], re.I) or parts[i].lstrip().startswith(("#", "##", "- ", "* ")):
            kept_vecs.append(v)
            continue
        if kept_vecs and float(np.max(np.dot(np.asarray(kept_vecs), v))) >= threshold:
            drop.add(i)
        else:
            kept_vecs.append(v)
    if not drop:
        return text, {}

    out, restore, run, rid = [], {}, [], 0
    for i, p in enumerate(parts):
        if i in drop:
            run.append(p)
            continue
        if run:
            rid += 1
            key = f"#{rid}"
            restore[key] = "\n".join(run)
            out.append(f"‹simplicio:ml-elided {len(run)} {key}›")
            run = []
        out.append(p)
    if run:
        rid += 1
        restore[f"#{rid}"] = "\n".join(run)
        out.append(f"‹simplicio:ml-elided {len(run)} #{rid}›")
    compressed = "\n".join(out)
    return (compressed, restore) if len(compressed) < len(text) else (text, {})


def semantic_restore(compressed, restore):
    def sub(m):
        return restore.get(m.group(1), m.group(0))
    return re.sub(r"‹simplicio:ml-elided \d+ (#\d+)›", sub, compressed)


# ── embedding retrieval (RAG by meaning) ─────────────────────────────────────
def semantic_search(query, docs, top_k=5):
    """docs: {key: text}. Returns [(key, cosine, snippet)] ranked by embedding similarity."""
    _, np = _load()
    keys = list(docs)
    if not keys:
        return []
    qv = _embed([query])[0]
    dv = _embed([docs[k] for k in keys])
    scores = np.dot(dv, qv)
    order = np.argsort(-scores)[:top_k]
    out = []
    for j in order:
        k = keys[int(j)]
        lines = [ln for ln in docs[k].split("\n") if ln.strip()]
        snippet = (lines[0][:120] if lines else "")
        out.append((k, round(float(scores[int(j)]), 4), snippet))
    return out


def _load_memories():
    try:
        import simplicio_memory as mem
        keys = mem.list_keys() if hasattr(mem, "list_keys") else []
        return {k: (mem.recall(k) or "") for k in keys}
    except Exception:
        return {}


def main(argv=None):
    p = argparse.ArgumentParser(prog="simplicio-cli semantic --ml", description="Simplicio ML semantic backend")
    sub = p.add_subparsers(dest="cmd")
    pc = sub.add_parser("compress", help="embedding semantic-dedup of stdin")
    pc.add_argument("--threshold", type=float, default=0.75)
    pc.add_argument("--restore-test", action="store_true")
    ps = sub.add_parser("search", help="embedding retrieval over the memory store")
    ps.add_argument("query")
    ps.add_argument("--top", type=int, default=5)
    args = p.parse_args(argv)

    if not ml_available():
        print("ML backend not installed — run: pip install model2vec numpy "
              "(falling back to the deterministic simplicio_semantic / simplicio_rag)", file=sys.stderr)
        return 3

    if args.cmd == "compress":
        text = sys.stdin.read()
        comp, restore = semantic_dedup(text, threshold=args.threshold)
        saved = len(text) - len(comp)
        sys.stdout.write(comp)
        pct = round(saved / len(text) * 100, 1) if text else 0.0
        print(f"\n--- ml semantic-dedup: {len(text)} -> {len(comp)} chars ({pct}% saved, {len(restore)} elided runs)", file=sys.stderr)
        if args.restore_test:
            ok = semantic_restore(comp, restore) == text
            print(f"--- round-trip: {'OK' if ok else 'FAIL'}", file=sys.stderr)
            return 0 if ok else 1
        return 0
    if args.cmd == "search":
        docs = _load_memories()
        if not docs:
            print("no memories indexed", file=sys.stderr)
            return 0
        for n, (k, score, snip) in enumerate(semantic_search(args.query, docs, args.top), 1):
            print(f"{n}. {k}  (cosine {score})\n   {snip}")
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
