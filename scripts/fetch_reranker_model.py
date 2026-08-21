"""Fetch the pinned cross-encoder reranker weights. The ONLY network path to them.

    uv run python scripts/fetch_reranker_model.py                  # fetch + verify
    uv run python scripts/fetch_reranker_model.py --verify         # verify what's on disk
    uv run python scripts/fetch_reranker_model.py --write-manifest # first run only

`app/services/reranking.py` imports no HTTP client and no hub library, so at
runtime there is no code path that could fetch a missing weight — enforced by
absence of capability, not by a flag. This script is the build step that puts
the files there; the Dockerfile runs it once and bakes the result in.

Pinning is content-addressed. Files are fetched from `/resolve/<commit-sha>/`,
not from `main`, so a force-push upstream cannot change what arrives. The
manifest then checks the bytes, so a compromised CDN cannot either. Both records
are needed: the SHA says which commit, the manifest says which bytes.

Why `model_quint8_avx2.onnx` and not a generic int8 file: the repo ships no
generic one. The int8 variants are per-microarchitecture and all identical in
size (23,200,716 bytes); unsigned activations are ONNX Runtime's recommendation
on x86 without AVX-512 VNNI, where signed int8 can saturate. Verified against
this machine: /proc/cpuinfo reports avx2 and no avx512.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

# The manifest format has exactly one parser, and it lives with the code that
# verifies weights at startup — duplicating it here would let the build check
# and the runtime check drift apart silently.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.reranking import (  # noqa: E402
    read_manifest,
    sha256_file,
    verify_model_files,
)

REPO_ID = "cross-encoder/ms-marco-MiniLM-L6-v2"

# Resolved 2026-08-21 from https://huggingface.co/api/models/cross-encoder/ms-marco-MiniLM-L6-v2
# (repo lastModified 2026-08-09). This is the single place the revision appears.
RERANKER_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"

# Remote path -> local filename. config.json is fetched for `max_position_embeddings`
# and `pad_token_id`, which the loader reads rather than hardcoding.
FILES = {
    "onnx/model_quint8_avx2.onnx": "model.onnx",
    "tokenizer.json": "tokenizer.json",
    "config.json": "config.json",
}

DEFAULT_DEST = Path("models/reranker")
# Outside `--dest` on purpose: models/reranker/ is gitignored (the weights are
# baked into the image, never committed), and git cannot re-include a file whose
# parent directory is excluded — a `!models/reranker/MANIFEST.sha256` negation
# does not work. The manifest is the drift detector, so it has to stay tracked.
DEFAULT_MANIFEST = Path("scripts/reranker_model.sha256")

DOWNLOAD_TIMEOUT_SECONDS = 300.0


def write_manifest(path: Path, dest: Path) -> None:
    """Checksums go to stdout as well as into the manifest. A manifest is only
    as trustworthy as the run that produced it, so the provenance belongs
    somewhere that run cannot later edit — a terminal scrollback or a commit
    message — not only in the file the same run just wrote.
    """
    lines = [
        f"# {REPO_ID}",
        f"# revision: {RERANKER_REVISION}",
    ]
    print(f"{REPO_ID} @ {RERANKER_REVISION}")
    for remote_path, local_name in FILES.items():
        digest = sha256_file(dest / local_name)
        size = (dest / local_name).stat().st_size
        lines.append(f"{digest}  {local_name}")
        print(f"  {digest}  {local_name:<16} {size:>12,} bytes  <- {remote_path}")
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path}", file=sys.stderr)


def download(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
        for remote_path, local_name in FILES.items():
            url = f"https://huggingface.co/{REPO_ID}/resolve/{RERANKER_REVISION}/{remote_path}"
            target = dest / local_name
            print(f"fetching {remote_path} -> {target}", file=sys.stderr)
            # Written to a .part file and renamed only on success, so an
            # interrupted download can never leave a short file that then fails
            # verification in a way that looks like tampering.
            partial = target.with_suffix(target.suffix + ".part")
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with partial.open("wb") as fh:
                    for block in response.iter_bytes():
                        fh.write(block)
            partial.rename(target)
            print(f"  {target.stat().st_size:,} bytes", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--verify", action="store_true", help="verify files already on disk; download nothing"
    )
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="record checksums of the fetched files (first run only)",
    )
    args = parser.parse_args(argv)

    manifest_path = args.manifest

    if not args.verify:
        try:
            download(args.dest)
        except httpx.HTTPError as exc:
            print(f"download failed: {exc}", file=sys.stderr)
            return 1

    if args.write_manifest:
        write_manifest(manifest_path, args.dest)

    revision_ok = True
    if manifest_path.is_file():
        recorded, _ = read_manifest(manifest_path)
        revision_ok = recorded == RERANKER_REVISION

    failures = verify_model_files(args.dest, manifest_path)
    if not revision_ok:
        failures.insert(
            0,
            f"revision drift: {manifest_path} pins a different commit than this script "
            f"({RERANKER_REVISION}) — one of the two was changed without the other",
        )
    if failures:
        print(f"\nFAIL: {len(failures)} problem(s) verifying {args.dest}\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"OK: {len(FILES)} file(s) in {args.dest} match {manifest_path} "
        f"at revision {RERANKER_REVISION[:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
