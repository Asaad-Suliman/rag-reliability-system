"""CLI: `ingest`, `query`, `reindex`, `stats`, `evaluate`, `delete`.

Invoked `uv run python -m app.cli <command> ...` — this repo has no
`[project.scripts]` (`pyproject.toml` sets `package = false`, no
`[build-system]` table), so `python -m` is the only supported invocation,
matching every other module here (`app.services.retrieval`, `.evaluation`,
`.ingestion` all run this way).

stdout is reserved for command results (so `query`'s output stays
pipeable); every status line, log, and confirmation prompt goes to stderr.

No command here has an API Contract counterpart beyond a loose, named
resemblance — see the divergence table recorded in the Step 02 vault note
and `09_Memory/DECISIONS.md`. This is a debug/ops tool, not the HTTP API.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, SettingsError, get_settings
from app.core.logging import configure_cli_logging
from app.db.models import Document, User
from app.db.session import create_engine, create_session_factory, ping
from app.documents.status import assert_status_enum_matches_db
from app.services.embeddings import Embedder, VoyageEmbedder
from app.services.evaluation import evaluate, format_report
from app.services.ingestion import (
    collection_name_for,
    delete_document,
    ingest_document,
    sha256_file,
)
from app.services.reranking import RERANK_N, Reranker, RerankerBackend, build_reranker
from app.services.retrieval import DEFAULT_CANDIDATE_K, DEFAULT_TOP_K, RRF_K, retrieve
from app.services.vector_store import ChromaVectorStore, VectorStore

logger = logging.getLogger(__name__)

# Duplicated from app/api/v1/health.py deliberately, not imported — that
# module pulls in FastAPI at module scope, which a CLI has no reason to load.
CHECK_TIMEOUT_SECONDS = 2.0

CLI_LOCAL_EMAIL = "cli@local"
# Not a real hash — app/core/security.py is a docstring-only stub; Step 04
# owns real password hashing. This row exists only to give documents.user_id
# something to point at. Nothing checks it before Step 04 exists.
CLI_PASSWORD_PLACEHOLDER = "unusable:cli-seeded-no-login"

EXIT_OK = 0
EXIT_PIPELINE_FAILED = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_REFUSED = 4

TERMINAL_FAILURE_STATUSES = ("failed", "quarantined")


class UnavailableError(RuntimeError):
    """Postgres or Chroma didn't answer within CHECK_TIMEOUT_SECONDS."""


def _build_reranker(settings: Settings, choice: RerankerBackend) -> Reranker:
    """Built per command rather than in `_dispatch`, so `stats`/`delete`/`ingest`
    never pay the load — or fail on a clone that has not fetched the weights.
    Within a command it is still built once, before the query, never per call.

    A `RerankerModelMissing` here propagates: it subclasses RuntimeError, so
    `main()` already reports it as `refused:` with exit 4 and no traceback.
    Falling back to NoOp would be the silent degradation this system forbids.
    """
    return build_reranker(
        choice,
        model_dir=settings.reranker_model_dir,
        manifest_path=settings.reranker_manifest_path,
        intra_op_threads=settings.reranker_intra_op_threads,
    )


def _build_embedder(settings: Settings) -> Embedder:
    return VoyageEmbedder(
        api_key=settings.voyage_api_key.get_secret_value(),
        model=settings.voyage_model,
        dimensions=settings.voyage_dimensions,
    )


def _check_path_allowed(path: Path, settings: Settings) -> Path:
    """Resolve `path` and verify it's inside `settings.ingest_root`.

    Must run before any file access — `ingest_document()`'s first file
    access is hashing the file's contents, so this always runs strictly
    before that. `resolve()` + `is_relative_to()`, not a string prefix
    (passes `/data/uploads-evil` against a `/data/uploads` root) and not
    `abspath()` (doesn't follow symlinks, so a symlink inside the allowed
    root pointing outside it would slip through).
    """
    candidate = path.resolve()
    if not candidate.is_relative_to(settings.ingest_root):
        raise ValueError(
            f"{path} resolves to {candidate}, which is outside the allowed "
            f"ingest root {settings.ingest_root}"
        )
    return candidate


async def _preflight(engine: AsyncEngine, vector_store: VectorStore) -> None:
    """Postgres + Chroma reachability, reusing the exact calls
    `/health/ready` makes — not `/health/ready` itself, since importing
    `app.api.v1.health` would drag in FastAPI for a CLI that never runs it.
    """
    try:
        await asyncio.gather(
            ping(engine, CHECK_TIMEOUT_SECONDS),
            vector_store.check(CHECK_TIMEOUT_SECONDS),
        )
    except Exception as exc:
        raise UnavailableError(str(exc)) from exc

    # Deliberately not caught: a reachable database whose `document_status` type
    # has drifted from DocumentStatus is not an availability problem, it is a
    # correctness one, and every command here can write that column.
    await assert_status_enum_matches_db(engine)


async def _get_or_create_user(session: AsyncSession, email: str) -> User:
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None:
        return user
    user = User(email=email, password_hash=CLI_PASSWORD_PLACEHOLDER)
    session.add(user)
    await session.flush()
    await session.commit()
    return user


def _print_document(document: Document) -> None:
    print(f"id={document.id}")
    print(f"filename={document.filename}")
    print(f"status={document.status}")
    print(f"chunk_count={document.chunk_count}")
    print(f"page_count={document.page_count}")
    if document.error:
        print(f"error={document.error}")


# --- commands ---------------------------------------------------------------


async def _cmd_ingest(
    args: argparse.Namespace, settings: Settings, session: AsyncSession, vector_store: VectorStore
) -> int:
    candidate = _check_path_allowed(args.path, settings)
    if not candidate.is_file():
        raise ValueError(f"not a file: {candidate}")

    user = await _get_or_create_user(session, args.user_email)
    embedder = _build_embedder(settings)

    print(f"ingesting {candidate} as {args.user_email}...", file=sys.stderr)
    document = await ingest_document(
        candidate,
        candidate.name,
        user.id,
        session,
        vector_store,
        embedder,
        allow_network=args.allow_network,
    )
    _print_document(document)
    return EXIT_PIPELINE_FAILED if document.status in TERMINAL_FAILURE_STATUSES else EXIT_OK


async def _probe_stores(
    session: AsyncSession, vector_store: VectorStore, collection_name: str
) -> tuple[bool, bool]:
    """Presence of each store's data, not counts. Runs ONLY on the no-hits path.

    Both stores are probed even though a single arm reads only one of them,
    because the *message* needs both: a `--arm vector` run against an empty
    collection reads identically whether nothing was ever ingested or
    VOYAGE_MODEL changed, and those need opposite advice. The verdict still
    depends only on the store the arm actually read — see `_no_hits_message`.

    EXISTS rather than count(*): the decision is boolean, so there is no reason
    to make Postgres walk the table, and nothing prints a total.
    """
    chunks_present = bool(
        (await session.execute(text("SELECT EXISTS (SELECT 1 FROM chunks)"))).scalar()
    )
    vectors_present = await vector_store.count(collection_name) > 0
    return chunks_present, vectors_present


def _no_hits_message(
    arm: Literal["hybrid", "lexical", "vector"],
    chunks_present: bool,
    vectors_present: bool,
    collection_name: str,
    filtered: bool,
) -> tuple[str, bool]:
    """Pure. Returns (message, is_refusal) for a query that returned no hits.

    An empty corpus and a query nothing matched are different facts and must
    never render as the same line — that was the defect this replaces.

    Which store's emptiness counts is decided by the arm, because the arms read
    different stores: lexical reads Postgres and never touches Chroma, vector is
    the mirror. Reporting Chroma's state on a lexical run would be reporting
    something that run never depended on.

    Hybrid with exactly one store populated is a third case, not a variant of
    empty: the query silently ran on one arm. That is a degraded result rather
    than a trustworthy "no match", so it refuses — this codebase fails loudly
    instead of degrading quietly.
    """
    reads_postgres = arm in ("hybrid", "lexical")
    reads_chroma = arm in ("hybrid", "vector")
    store_missing = (reads_postgres and not chunks_present) or (
        reads_chroma and not vectors_present
    )

    if not store_missing:
        note = " (a --document-id filter was applied)" if filtered else ""
        return f"no results — the corpus is not empty; no chunk matched this query{note}", False

    if not chunks_present and not vectors_present:
        return (
            "corpus is empty: nothing has been ingested. Run:\n"
            "  uv run python -m app.cli ingest <file> --allow-network",
            True,
        )

    if chunks_present and not vectors_present:
        return (
            f"postgres has chunks but the Chroma collection {collection_name} is empty. "
            "Most likely VOYAGE_MODEL or VOYAGE_DIMENSIONS changed — collections are per "
            "model and dimensions, so the corpus needs re-embedding under the new one. "
            "Otherwise an interrupted delete or reindex; run `app.cli stats`.",
            True,
        )

    return (
        f"the Chroma collection {collection_name} has vectors but postgres has no chunks — "
        "an interrupted ingest left vectors without their rows. Run `app.cli stats`.",
        True,
    )


async def _cmd_query(
    args: argparse.Namespace, settings: Settings, session: AsyncSession, vector_store: VectorStore
) -> int:
    text_query = args.text.strip()
    if not text_query:
        raise ValueError("query text cannot be empty")

    embedder = _build_embedder(settings)
    if args.arm in ("vector", "hybrid"):
        print(f"embedding query via {embedder.model}...", file=sys.stderr)

    backend: RerankerBackend = args.reranker or settings.reranker
    reranker = _build_reranker(settings, backend)
    try:
        result = await retrieve(
            text_query,
            session,
            vector_store,
            embedder,
            top_k=args.top_k,
            candidate_k=args.candidate_k,
            document_ids=args.document_ids,
            arm=args.arm,
            rrf_k=args.rrf_k,
            reranker=reranker,
            rerank_n=args.rerank_n,
        )
    finally:
        reranker.close()
    hits = result.hits
    if result.rerank is not None and result.rerank.n_candidates:
        t = result.rerank
        print(
            f"rerank: backend={backend} candidates={t.n_candidates} "
            f"truncated={t.n_truncated} infer={t.infer_ms:.0f}ms "
            f"threads={t.intra_op_threads}",
            file=sys.stderr,
        )

    if not hits:
        # The probe runs here and nowhere else: on the happy path the command
        # needs no counts, and paying for them on every query to explain a
        # branch that rarely fires would be the wrong trade.
        chunks_present, vectors_present = await _probe_stores(
            session, vector_store, collection_name_for(embedder)
        )
        message, refused = _no_hits_message(
            args.arm,
            chunks_present,
            vectors_present,
            collection_name_for(embedder),
            filtered=args.document_ids is not None,
        )
        if refused:
            # RuntimeError, so main()'s existing handler reports it as `refused:`
            # with exit 4 and no traceback — the same path RerankerModelMissing
            # and the ingest network gate already take. An empty corpus is a
            # missing prerequisite, which is exactly what exit 4 documents.
            raise RuntimeError(message)
        print(message)
        return EXIT_OK

    for i, hit in enumerate(hits, start=1):
        print(
            f"{i}. [{hit.chunk_id}] {hit.document_id} ({hit.document_name}) "
            f"p.{hit.page} ({hit.char_start}-{hit.char_end})"
        )
        rerank_col = "" if hit.rerank_score is None else f"rerank_score={hit.rerank_score:.4f} "
        print(
            f"   {rerank_col}rrf_score={hit.rrf_score:.4f} "
            f"lexical_rank={hit.lexical_rank} lexical_score={hit.lexical_score} "
            f"vector_rank={hit.vector_rank} vector_distance={hit.vector_distance}"
        )
        snippet = hit.text[:200].replace("\n", " ")
        ellipsis = "..." if len(hit.text) > 200 else ""
        print(f"   {snippet}{ellipsis}")
    return EXIT_OK


async def _cmd_reindex(
    args: argparse.Namespace, settings: Settings, session: AsyncSession, vector_store: VectorStore
) -> int:
    candidate = _check_path_allowed(args.file, settings)
    if not candidate.is_file():
        raise ValueError(f"not a file: {candidate}")

    document = (
        await session.execute(select(Document).where(Document.id == args.doc_id))
    ).scalar_one_or_none()
    if document is None:
        raise ValueError(f"no document with id {args.doc_id}")

    actual_sha256 = sha256_file(candidate)
    if actual_sha256 != document.sha256:
        raise ValueError(
            f"{candidate} does not match {args.doc_id}'s stored content (sha256 mismatch) "
            "— ingest it as a new document instead"
        )

    embedder = _build_embedder(settings)
    print(f"reindexing {args.doc_id} from {candidate}...", file=sys.stderr)
    # `force=True` is the whole of what this command needs: it bypasses
    # ingest_document()'s ready -> no-op dedupe shortcut. The CLI does not write
    # `status` itself — app/documents/status.py owns every transition, and a
    # hand-set `queued` here would be an illegal move under its table anyway.
    reindexed = await ingest_document(
        candidate,
        candidate.name,
        document.user_id,
        session,
        vector_store,
        embedder,
        allow_network=args.allow_network,
        force=True,
    )
    _print_document(reindexed)
    return EXIT_PIPELINE_FAILED if reindexed.status in TERMINAL_FAILURE_STATUSES else EXIT_OK


async def _cmd_stats(
    args: argparse.Namespace, settings: Settings, session: AsyncSession, vector_store: VectorStore
) -> int:
    embedder = _build_embedder(settings)
    collection_name = collection_name_for(embedder)

    rows = (
        await session.execute(
            select(Document.id, Document.filename, Document.status, Document.chunk_count)
        )
    ).all()

    status_counts: dict[str, int] = {}
    total_chunks = 0
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
        total_chunks += row.chunk_count

    total_vectors = await vector_store.count(collection_name)

    # Consistency check: per document, Postgres chunk_count vs. actual Chroma
    # vector count. Catches the interrupted-delete/reindex half-state — a
    # document row that still says status=ready, chunk_count=N, but whose
    # vectors are gone because the process died between the two deletes in
    # delete_document()/ingest_document()'s reindex path. Silent otherwise:
    # nothing else in this CLI would ever notice.
    #
    # Restricted to terminal statuses (ready/failed/quarantined) on purpose:
    # the Postgres and Chroma reads here aren't a snapshot — they're two
    # separate queries at two separate instants — and a document actively
    # mid-pipeline (queued/parsing/indexing) can genuinely have Chroma
    # already upserted while Postgres's chunk_count commit hasn't landed
    # yet (ingestion.py writes vectors before the final commit). Comparing
    # those would flag real, harmless in-flight state as corruption. A
    # detector that cries wolf on normal operation gets ignored.
    terminal_rows = [row for row in rows if row.status in TERMINAL_FAILURE_STATUSES + ("ready",)]
    in_progress = len(rows) - len(terminal_rows)

    actual_counts = await asyncio.gather(
        *(vector_store.count_by_document(collection_name, row.id) for row in terminal_rows)
    )
    mismatches = [
        (row.id, row.filename, row.chunk_count, actual)
        for row, actual in zip(terminal_rows, actual_counts, strict=True)
        if actual != row.chunk_count
    ]

    print(f"documents: {len(rows)}")
    for status in sorted(status_counts):
        print(f"  {status}: {status_counts[status]}")
    print(f"chunks (postgres): {total_chunks}")
    print(f"vectors (chroma, collection={collection_name}): {total_vectors}")

    if in_progress:
        print(f"({in_progress} document(s) mid-pipeline — not checked for consistency)")

    if mismatches:
        print()
        print(f"WARNING: {len(mismatches)} document(s) with chunk_count/vector mismatch:")
        for doc_id, filename, pg_count, actual in mismatches:
            print(
                f"  WARNING: {doc_id} ({filename}): postgres chunk_count={pg_count}, "
                f"chroma vectors={actual} — likely an interrupted delete or reindex"
            )
    return EXIT_OK


async def _cmd_evaluate(
    args: argparse.Namespace, settings: Settings, session: AsyncSession, vector_store: VectorStore
) -> int:
    embedder = _build_embedder(settings)
    reranker = _build_reranker(settings, args.reranker)
    try:
        report = await evaluate(
            session,
            vector_store,
            embedder,
            candidate_k=args.candidate_k,
            rrf_k=args.rrf_k,
            allow_network=args.allow_network,
            reranker=reranker,
            rerank_n=args.rerank_n,
        )
    finally:
        reranker.close()
    print(format_report(report))
    return EXIT_OK


async def _cmd_delete(
    args: argparse.Namespace, settings: Settings, session: AsyncSession, vector_store: VectorStore
) -> int:
    document = (
        await session.execute(select(Document).where(Document.id == args.doc_id))
    ).scalar_one_or_none()
    if document is None:
        raise ValueError(f"no document with id {args.doc_id}")

    print(
        f"about to delete: {document.id}  {document.filename}  "
        f"status={document.status}  chunks={document.chunk_count}",
        file=sys.stderr,
    )
    if not args.yes:
        print("Delete this document and its vectors? [y/N] ", end="", file=sys.stderr, flush=True)
        answer = input()
        if answer.strip().lower() != "y":
            print("aborted", file=sys.stderr)
            return EXIT_OK

    embedder = _build_embedder(settings)
    deleted = await delete_document(document, session, vector_store, embedder)
    print(f"deleted {document.id}: {deleted} vectors removed")
    return EXIT_OK


_HANDLERS = {
    "ingest": _cmd_ingest,
    "query": _cmd_query,
    "reindex": _cmd_reindex,
    "stats": _cmd_stats,
    "evaluate": _cmd_evaluate,
    "delete": _cmd_delete,
}


# --- argument parsing --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="RAG Reliability System CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Ingest a file into the corpus")
    p_ingest.add_argument("path", type=Path)
    p_ingest.add_argument("--user-email", default=CLI_LOCAL_EMAIL)
    p_ingest.add_argument("--allow-network", action="store_true")

    p_query = sub.add_parser("query", help="Retrieve ranked chunks for a query")
    p_query.add_argument("text")
    p_query.add_argument("--arm", choices=("hybrid", "lexical", "vector"), default="hybrid")
    p_query.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p_query.add_argument("--candidate-k", type=int, default=DEFAULT_CANDIDATE_K)
    p_query.add_argument("--rrf-k", type=int, default=RRF_K)
    p_query.add_argument("--document-id", action="append", dest="document_ids", default=None)
    # Default None, not "local": falls through to RERANKER in the environment,
    # so the flag overrides config rather than shadowing it.
    p_query.add_argument("--reranker", choices=("none", "local"), default=None)
    p_query.add_argument("--rerank-n", type=int, default=RERANK_N)

    p_reindex = sub.add_parser("reindex", help="Force a document back through the pipeline")
    p_reindex.add_argument("doc_id")
    p_reindex.add_argument("--file", type=Path, required=True)
    p_reindex.add_argument("--allow-network", action="store_true")

    sub.add_parser("stats", help="Aggregate counts across documents and vectors")

    p_evaluate = sub.add_parser("evaluate", help="Run the golden-set evaluation harness")
    p_evaluate.add_argument("--allow-network", action="store_true")
    p_evaluate.add_argument("--rrf-k", type=int, default=RRF_K)
    p_evaluate.add_argument("--candidate-k", type=int, default=DEFAULT_CANDIDATE_K)
    # "none" regardless of RERANKER, unlike `query`. The benchmark's baseline
    # arm is no-rerank by definition, and it must not move because someone
    # changed an env var: `evaluate` with no flags has to keep reproducing the
    # recorded gate figures. Chunk 5 passes --reranker local explicitly.
    p_evaluate.add_argument("--reranker", choices=("none", "local"), default="none")
    p_evaluate.add_argument("--rerank-n", type=int, default=RERANK_N)

    p_delete = sub.add_parser("delete", help="Delete a document and its vectors")
    p_delete.add_argument("doc_id")
    p_delete.add_argument("--yes", action="store_true")

    return parser


async def _dispatch(args: argparse.Namespace, settings: Settings) -> int:
    # A malformed-but-present DATABASE_URL (e.g. "") passes SettingsError's
    # pydantic-level check (the field is a non-empty SecretStr) and only
    # fails here, when SQLAlchemy actually parses it — that's still an
    # environment problem, not a bug, so it gets the same exit code as
    # SettingsError rather than an uncaught traceback.
    try:
        engine = create_engine(settings)
        vector_store = ChromaVectorStore(settings.chroma_persist_dir)
    except Exception as exc:
        raise UnavailableError(str(exc)) from exc

    session_factory = create_session_factory(engine)
    try:
        await _preflight(engine, vector_store)
        async with session_factory() as session:
            handler = _HANDLERS[args.command]
            return await handler(args, settings, session, vector_store)
    finally:
        vector_store.close()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except SettingsError as exc:
        print(f"environment error: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    configure_cli_logging(settings.log_level)

    try:
        return asyncio.run(_dispatch(args, settings))
    except UnavailableError as exc:
        print(f"unavailable: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except RuntimeError as exc:
        # Covers NetworkNotAllowedError (ingest_document's one raise path),
        # evaluate()'s cache-miss RuntimeError, RerankerModelMissing, and
        # _cmd_query's empty-corpus refusal — all the same category, a missing
        # prerequisite or a refused paid call, and all already carry a clear
        # message naming what to do next.
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


def _demo() -> None:
    """Offline truth table for the no-hits branch. No Postgres, no Chroma.

    Not reachable via `python -m app.cli` — that entry point is the CLI itself
    and requires a subcommand. Run it as:

        uv run python -c "from app.cli import _demo; _demo()"
    """
    collection = "chunks_voyage4lite_1024"
    arms: tuple[Literal["hybrid", "lexical", "vector"], ...] = ("hybrid", "lexical", "vector")

    def check(
        arm: Literal["hybrid", "lexical", "vector"], chunks: bool, vectors: bool
    ) -> tuple[str, bool]:
        return _no_hits_message(arm, chunks, vectors, collection, filtered=False)

    # --- both stores populated: a real no-match on every arm, never a refusal ---
    for arm in arms:
        message, refused = check(arm, True, True)
        assert not refused, (arm, message)
        assert message.startswith("no results — the corpus is not empty"), message

    # --- both stores empty: an empty corpus on every arm, always a refusal ---
    for arm in arms:
        message, refused = check(arm, False, False)
        assert refused, (arm, message)
        assert message.startswith("corpus is empty"), message
        # It must name the way out, not merely the problem.
        assert "app.cli ingest" in message, message

    # --- half-states: the arm decides whether the missing store matters ---
    # Postgres populated, Chroma empty.
    message, refused = check("lexical", True, False)
    assert not refused, message  # lexical never reads Chroma
    message, refused = check("vector", True, False)
    assert refused and collection in message, message
    assert "VOYAGE_MODEL" in message, message  # the likely cause, not "corpus is empty"
    message, refused = check("hybrid", True, False)
    assert refused and "VOYAGE_MODEL" in message, message

    # Chroma populated, Postgres empty — the mirror, and a different message.
    message, refused = check("vector", False, True)
    assert not refused, message  # vector never reads Postgres
    message, refused = check("lexical", False, True)
    assert refused and "interrupted ingest" in message, message
    message, refused = check("hybrid", False, True)
    assert refused and "interrupted ingest" in message, message

    # A half-state must never be described as an empty corpus: that would send
    # someone to re-ingest a corpus that is already there.
    for chunks, vectors in ((True, False), (False, True)):
        message, _ = check("hybrid", chunks, vectors)
        assert not message.startswith("corpus is empty"), message

    # --- the --document-id clause rides on the no-match line only ---
    plain, _ = _no_hits_message("hybrid", True, True, collection, filtered=False)
    filtered, _ = _no_hits_message("hybrid", True, True, collection, filtered=True)
    assert "--document-id" not in plain, plain
    assert "--document-id" in filtered, filtered
    # It must not leak into a refusal, where it would not be the cause.
    empty, refused = _no_hits_message("hybrid", False, False, collection, filtered=True)
    assert refused and "--document-id" not in empty, empty

    print(
        "ok: no-hits truth table — 3 arms x {populated, empty, both half-states}, "
        "the --document-id clause, and no half-state reported as an empty corpus"
    )


if __name__ == "__main__":
    sys.exit(main())
