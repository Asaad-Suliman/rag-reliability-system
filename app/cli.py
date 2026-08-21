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

from sqlalchemy import select
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


async def _cmd_query(
    args: argparse.Namespace, settings: Settings, session: AsyncSession, vector_store: VectorStore
) -> int:
    text_query = args.text.strip()
    if not text_query:
        raise ValueError("query text cannot be empty")

    embedder = _build_embedder(settings)
    if args.arm in ("vector", "hybrid"):
        print(f"embedding query via {embedder.model}...", file=sys.stderr)

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
    )
    hits = result.hits

    if not hits:
        print("no results")
        return EXIT_OK

    for i, hit in enumerate(hits, start=1):
        print(
            f"{i}. [{hit.chunk_id}] {hit.document_id} ({hit.document_name}) "
            f"p.{hit.page} ({hit.char_start}-{hit.char_end})"
        )
        print(
            f"   rrf_score={hit.rrf_score:.4f} "
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
    report = await evaluate(
        session,
        vector_store,
        embedder,
        candidate_k=args.candidate_k,
        rrf_k=args.rrf_k,
        allow_network=args.allow_network,
    )
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

    p_reindex = sub.add_parser("reindex", help="Force a document back through the pipeline")
    p_reindex.add_argument("doc_id")
    p_reindex.add_argument("--file", type=Path, required=True)
    p_reindex.add_argument("--allow-network", action="store_true")

    sub.add_parser("stats", help="Aggregate counts across documents and vectors")

    p_evaluate = sub.add_parser("evaluate", help="Run the golden-set evaluation harness")
    p_evaluate.add_argument("--allow-network", action="store_true")
    p_evaluate.add_argument("--rrf-k", type=int, default=RRF_K)
    p_evaluate.add_argument("--candidate-k", type=int, default=DEFAULT_CANDIDATE_K)

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
        # Covers NetworkNotAllowedError (ingest_document's one raise path)
        # and evaluate()'s cache-miss RuntimeError — both are the same
        # category, a refused paid call, and both already carry a clear
        # message naming what to do next.
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
