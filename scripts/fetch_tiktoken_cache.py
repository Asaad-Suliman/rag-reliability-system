"""Fetch and cache the cl100k_base BPE ranks file tiktoken loads at first use.
The ONLY network path to it.

    uv run python -m scripts.fetch_tiktoken_cache                     # verify only (default)
    uv run python -m scripts.fetch_tiktoken_cache --allow-network     # fetch + verify
    uv run python -m scripts.fetch_tiktoken_cache --allow-network \
        --write-manifest                                              # first run only
    uv run python -m scripts.fetch_tiktoken_cache --verify            # verify what's on disk

Mirrors `scripts/fetch_reranker_model.py`: content-addressed pin (a fixed blob
URL, checked against a fixed sha256 of its bytes), a checksum manifest kept
outside the gitignored cache directory, and a `.part`-then-rename write so an
interrupted download can never look like tampering.

One difference from the reranker script: the gate here is a function
parameter (`allow_network`), not a separate `--verify` CLI mode, because
`fetch()` already refuses to touch the network when the file is already
cached and verified (see "Idempotent" below) -- the same call is safe to make
by default, and only asks permission when it actually needs to leave the
machine.

`app/services/context_budget.py` and `scripts/validate_token_counter.py` will
set `TIKTOKEN_CACHE_DIR` to this script's `DEFAULT_DEST` before calling
`tiktoken.get_encoding("cl100k_base")`, so a warm cache here means that call
never reaches the network either.

BLOB_URL and EXPECTED_SHA256 are copied from tiktoken's own registry
(`tiktoken_ext/openai_public.py`'s `cl100k_base()`, which calls
`load_tiktoken_bpe(BLOB_URL, expected_hash=EXPECTED_SHA256)`). Confirmed
2026-08-24 against the installed `tiktoken==0.8.0` package's source -- the
first version of this constant, written from memory before tiktoken could be
installed, had 8 hex digits of EXPECTED_SHA256 wrong, and the download-then-
verify path below caught it immediately: refused to cache, deleted the
mismatched `.part`, exited nonzero. That is the failure mode by design if
either value ever drifts upstream again -- it cannot silently cache the
wrong bytes.

CACHE_KEY is *computed*, not copied: tiktoken's `read_file_cached`
(`tiktoken/load.py`) names cache entries `sha1(blobpath).hexdigest()`, not a
human-readable filename. Get this wrong and a pre-seeded cache is silently
ignored -- tiktoken just re-downloads under the key it actually wants -- so
it is derived from BLOB_URL here rather than hardcoded, and can never drift
from it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import httpx

# Reused, not reimplemented: `sha256_file` is a pure hashing helper the
# reranker fetch script already relies on -- duplicating it here is a second
# place it could drift from the one the app actually verifies weights with.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.reranking import sha256_file  # noqa: E402

ENCODING_NAME = "cl100k_base"

BLOB_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
# Confirmed 2026-08-24 against the installed tiktoken==0.8.0 package's own
# source (`tiktoken_ext/openai_public.py::cl100k_base`) -- the value first
# written here from memory had the URL right but the last 8 hex digits of the
# hash wrong, and the download-then-verify path below caught it (refused to
# cache, deleted the mismatched .part, exited nonzero) exactly as designed.
EXPECTED_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"

CACHE_KEY = hashlib.sha1(BLOB_URL.encode()).hexdigest()  # noqa: S324 -- tiktoken's own cache-key algorithm, not a security use

DEFAULT_DEST = Path("models/tiktoken")
# Outside `--dest` on purpose, same reasoning as fetch_reranker_model.py's
# DEFAULT_MANIFEST: models/tiktoken/ is gitignored, and git cannot re-include
# a file whose parent directory is excluded -- a `!models/tiktoken/...`
# negation does not work. This bit us before with MANIFEST.sha256 under
# models/reranker/, so the manifest stays tracked outside that tree.
DEFAULT_MANIFEST = Path("scripts/tiktoken_cache.sha256")

DOWNLOAD_TIMEOUT_SECONDS = 300.0


class NetworkFetchDisabled(RuntimeError):
    """A download was needed but `allow_network` was not set.

    Phase 7A of this chunk is write-only scaffolding: no `uv add`, no `pip`,
    no `tiktoken.get_encoding()`, no network calls. This is what makes that
    true even if `fetch()` is called directly and the file happens to be
    missing -- it is the only place in this script capable of reaching the
    network, and it refuses to unless told to.
    """


def _cache_path(dest: Path) -> Path:
    return dest / CACHE_KEY


def verify_cache(dest: Path = DEFAULT_DEST) -> bool:
    """True if the cached file is present and its bytes match EXPECTED_SHA256."""
    path = _cache_path(dest)
    return path.is_file() and sha256_file(path) == EXPECTED_SHA256


def fetch(dest: Path = DEFAULT_DEST, *, allow_network: bool = False) -> Path:
    """Materialise the cl100k_base cache file at `dest / CACHE_KEY`.

    Idempotent: if it is already there and verified, this downloads nothing
    and says so -- `allow_network` is never even consulted on that path. It
    is required only when a real fetch is about to happen, which is exactly
    the moment Phase A must not reach.
    """
    os.environ["TIKTOKEN_CACHE_DIR"] = str(dest)
    path = _cache_path(dest)

    if verify_cache(dest):
        print(f"OK: {path} already cached and verified -- no-op", file=sys.stderr)
        return path

    if not allow_network:
        raise NetworkFetchDisabled(
            f"{path} is missing or does not match EXPECTED_SHA256, and "
            "allow_network=False (the default). This script must not reach "
            "the network in Phase A. Pass allow_network=True (or run with "
            "--allow-network) once Phase B is approved."
        )

    dest.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    print(f"fetching {BLOB_URL} -> {path}", file=sys.stderr)
    # Written to a .part file and renamed only on success, so an interrupted
    # download can never leave a short file that then fails verification in a
    # way that looks like tampering -- same discipline as fetch_reranker_model.py.
    with httpx.Client(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
        with client.stream("GET", BLOB_URL) as response:
            response.raise_for_status()
            with partial.open("wb") as fh:
                for block in response.iter_bytes():
                    fh.write(block)

    digest = sha256_file(partial)
    if digest != EXPECTED_SHA256:
        partial.unlink()
        raise ValueError(
            f"downloaded {BLOB_URL} but sha256 {digest} != EXPECTED_SHA256 "
            f"{EXPECTED_SHA256} -- refusing to cache a mismatched file. "
            "EXPECTED_SHA256 or BLOB_URL may have drifted from tiktoken's "
            "current registry; confirm against the installed package's source."
        )
    partial.rename(path)
    print(f"  {path.stat().st_size:,} bytes, sha256 verified", file=sys.stderr)
    return path


def write_manifest(path: Path, dest: Path) -> None:
    """Checksum goes to stdout as well as into the manifest -- same reasoning
    as fetch_reranker_model.py's write_manifest: provenance belongs somewhere
    the run that produced it cannot later edit.
    """
    digest = sha256_file(_cache_path(dest))
    lines = [f"# {ENCODING_NAME}", f"# url: {BLOB_URL}"]
    print(f"{ENCODING_NAME} <- {BLOB_URL}")
    print(f"  {digest}  {CACHE_KEY}")
    lines.append(f"{digest}  {CACHE_KEY}")
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path}", file=sys.stderr)


def read_manifest(path: Path) -> tuple[str, str]:
    """Returns (url, sha256). Raises on anything malformed.

    An unreadable manifest must never degrade to "skip verification" -- same
    rule as `reranking.read_manifest`, which this deliberately does not reuse:
    that format requires a `# revision:` line (a git commit), which a tiktoken
    blob URL is not. A shared parser for two incompatible schemas would be
    the thing that drifts, not this one extra function.
    """
    url = ""
    digest = ""
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# url:"):
            url = line.split(":", 1)[1].strip()
            continue
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"{path}:{lineno}: expected '<sha256>  <cache_key>', got {raw!r}")
        digest = parts[0]
    if not url:
        raise ValueError(f"{path}: no '# url:' line -- cannot tell which blob this pins")
    if not digest:
        raise ValueError(f"{path}: no checksum")
    return url, digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="permit downloading if the cache is missing or stale; default refuses",
    )
    parser.add_argument(
        "--verify", action="store_true", help="verify files already on disk; download nothing"
    )
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="record the checksum of the cached file (first run only)",
    )
    args = parser.parse_args(argv)

    if args.verify:
        if not verify_cache(args.dest):
            print(f"FAIL: {_cache_path(args.dest)} missing or checksum mismatch", file=sys.stderr)
            return 1
        if args.manifest.is_file():
            _, recorded = read_manifest(args.manifest)
            actual = sha256_file(_cache_path(args.dest))
            if recorded != actual:
                print(
                    f"FAIL: {args.manifest} records {recorded} but the cached file is {actual}",
                    file=sys.stderr,
                )
                return 1
        print(f"OK: {_cache_path(args.dest)} verified against EXPECTED_SHA256")
        return 0

    try:
        path = fetch(args.dest, allow_network=args.allow_network)
    except NetworkFetchDisabled as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (httpx.HTTPError, ValueError) as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        return 1

    if args.write_manifest:
        write_manifest(args.manifest, args.dest)

    print(f"OK: {path} matches EXPECTED_SHA256 for {ENCODING_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
