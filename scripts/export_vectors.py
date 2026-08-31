"""Export the frozen corpus vectors out of the Chroma index into a flat artifact.

    uv run --no-sync python -m scripts.export_vectors [--chroma-dir DIR] [--out DIR]

Read-only. Opens `chroma.sqlite3` with `mode=ro` and never touches the HNSW
segment files, so running this cannot mutate the live index.

Why this exists: the HNSW index is cross-process nondeterministic and its
`data_level0.bin` holds no vector data at all — every vector lives in the
`embeddings_queue` WAL, which has never been checkpointed. Exact brute-force
search over these 260 vectors is deterministic, 19x faster and needs 1.02 MiB
(chunk 7.1g). This script is the one-way door: after it, the index directory is
dead weight.

Output, both in `--out`:

  corpus_vectors.npy   (260, 1024) float32, C-contiguous, unit-norm
  corpus_vectors.json  row-order id manifest + the per-chunk metadata and
                       document text Chroma used to return alongside each hit

The two are paired by row index: `manifest["records"][i]` describes row `i` of
the matrix. `manifest["vectors_sha256"]` pins the matrix bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

VECTORS_FILENAME = "corpus_vectors.npy"
MANIFEST_FILENAME = "corpus_vectors.json"

# Chroma's log operations. 2 = add, 3 = delete.
_OP_ADD = 2

_METADATA_KEYS = ("document_id", "page", "char_start", "char_end")
_DOCUMENT_KEY = "chroma:document"


def _read_index(chroma_dir: Path) -> tuple[list[dict[str, Any]], np.ndarray, str, int]:
    """Returns (records, matrix, collection_name, dimensions)."""
    uri = f"file:{chroma_dir / 'chroma.sqlite3'}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        collections = conn.execute("SELECT id, name, dimension FROM collections").fetchall()
        if len(collections) != 1:
            raise SystemExit(f"expected exactly one collection, found {len(collections)}")
        _, collection_name, dimensions = collections[0]

        # The live set is the metadata segment's rows: adds minus deletes,
        # already resolved by Chroma. `embeddings.id` order is Chroma's own
        # insertion order and is what fixes the row order of the matrix.
        live_ids = [
            row[0]
            for row in conn.execute("SELECT embedding_id FROM embeddings ORDER BY id").fetchall()
        ]

        # Vectors only exist in the WAL. Last add wins, in case a chunk id was
        # ever re-added.
        blobs: dict[str, bytes] = {}
        for chunk_id, blob in conn.execute(
            "SELECT id, vector FROM embeddings_queue WHERE operation = ? ORDER BY seq_id",
            (_OP_ADD,),
        ):
            blobs[chunk_id] = blob

        meta: dict[str, dict[str, Any]] = {}
        for chunk_id, key, string_value, int_value in conn.execute(
            "SELECT e.embedding_id, m.key, m.string_value, m.int_value "
            "FROM embeddings e JOIN embedding_metadata m ON m.id = e.id"
        ):
            meta.setdefault(chunk_id, {})[key] = (
                string_value if string_value is not None else int_value
            )
    finally:
        conn.close()

    missing = [i for i in live_ids if i not in blobs]
    if missing:
        raise SystemExit(f"{len(missing)} live ids have no vector in the WAL: {missing[:5]}")

    matrix = np.stack([np.frombuffer(blobs[i], dtype=np.float32) for i in live_ids]).astype(
        np.float32
    )
    records = [
        {
            "id": chunk_id,
            **{k: meta[chunk_id][k] for k in _METADATA_KEYS},
            "document": meta[chunk_id][_DOCUMENT_KEY],
        }
        for chunk_id in live_ids
    ]
    return records, np.ascontiguousarray(matrix), collection_name, int(dimensions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chroma-dir", type=Path, default=Path("data/chroma"))
    # NOT any directory named `data/` — `.gitignore:18` is an unanchored
    # `data/`, so it swallows one at any depth and the artifact would silently
    # stop being tracked. A `!` negation cannot rescue that; see .gitignore.
    parser.add_argument("--out", type=Path, default=Path("app/corpus"))
    args = parser.parse_args()

    records, matrix, collection_name, dimensions = _read_index(args.chroma_dir)

    norms = np.linalg.norm(matrix, axis=1)
    print(f"count            : {len(records)}")
    print(f"unique ids       : {len({r['id'] for r in records})}")
    print(f"shape            : {matrix.shape}")
    print(f"dtype            : {matrix.dtype}")
    print(f"nbytes           : {matrix.nbytes}")
    print(f"norm min/max     : {norms.min():.6f} / {norms.max():.6f}")
    print(f"all-zero vectors : {int((~matrix.any(axis=1)).sum())}")
    print(f"non-finite cells : {int((~np.isfinite(matrix)).sum())}")
    print(f"duplicate vectors: {len(records) - len(np.unique(matrix, axis=0))}")

    digest = hashlib.sha256(matrix.tobytes()).hexdigest()
    print(f"corpus sha256    : {digest}")

    args.out.mkdir(parents=True, exist_ok=True)
    np.save(args.out / VECTORS_FILENAME, matrix, allow_pickle=False)
    (args.out / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "collection": collection_name,
                "dimensions": dimensions,
                "count": len(records),
                "vectors_sha256": digest,
                "records": records,
            },
            indent=2,
        )
        + "\n"
    )
    for name in (VECTORS_FILENAME, MANIFEST_FILENAME):
        print(f"wrote {args.out / name}  ({(args.out / name).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
