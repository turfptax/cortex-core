"""Local embedding client for the vector index (CP1, 2026-06-10).

Talks to the llama-embed systemd service on this host (llama-server
--embeddings serving bge-small-en-v1.5-q8_0, 384 dims, mean pooling,
port 8082, bound to 127.0.0.1). Vectors never leave the host; this is
the Slice 13 privacy posture applied to embeddings (cloud embedding
APIs leak content the same way a cloud vector DB would, see
memory/vector_index_design_seed.md).

Embedding is best-effort everywhere: every caller must tolerate a
None return. A gist that fails to embed is picked up later by the
backfill pass; nothing in the write path blocks on this service.
"""

import json
import logging
import os
import urllib.request

log = logging.getLogger("overseer.embeddings")

# Cloud migration P0 (2026-07-20): env-overridable so the cloud container
# can point at an embed sidecar. Default is the Pi's local llama-embed
# service, unchanged. The privacy posture holds either way: the URL must
# stay inside the deployment boundary (localhost or an in-app sidecar),
# never a third-party embedding API.
EMBED_URL = os.environ.get(
    "CORTEX_EMBED_URL", "http://127.0.0.1:8082/v1/embeddings")
MODEL_NAME = "bge-small-en-v1.5-q8_0"
DIM = 384

# bge-small context is 512 tokens; the service truncates, but long
# bodies waste encode time. ~1600 chars is a safe ~400-token cap and
# gist bodies are typically well under it.
_MAX_CHARS = 1600


def embed_texts(texts, timeout=60):
    """Embed a list of strings via the local llama-embed service.

    Returns a list of DIM-float lists in input order, or None on any
    failure (service down, malformed response, partial batch).
    """
    if not texts:
        return []
    payload = {"input": [(t or "")[:_MAX_CHARS] for t in texts]}
    req = urllib.request.Request(
        EMBED_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        out = [None] * len(texts)
        for item in data.get("data", []):
            idx = item.get("index")
            vec = item.get("embedding")
            if idx is not None and isinstance(vec, list) and len(vec) == DIM:
                out[idx] = vec
        if any(v is None for v in out):
            log.warning("embed_texts: incomplete batch (%d inputs)",
                        len(texts))
            return None
        return out
    except Exception as e:
        log.warning("embed_texts failed: %s", e)
        return None


def embed_one(text, timeout=30):
    """Embed a single string. Returns a DIM-float list or None."""
    result = embed_texts([text], timeout=timeout)
    return result[0] if result else None
