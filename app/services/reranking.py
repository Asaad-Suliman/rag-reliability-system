"""Cross-encoder reranking behind a `Reranker` protocol.

RRF fuses by rank and never inspects content, so it cannot separate lexical
signal from the noise the OR-relaxed lexical arm introduces — recorded as a
known limitation in `09_Memory/DECISIONS.md` (2026-08-19), with this component
named as the intended fix. A cross-encoder reads (query, passage) jointly and
can produce the calibrated relevance number `rrf_score` never was: API Contract
§5.1's `citations[].score`.

`NoOpReranker` is first-class, not test scaffolding. Chunk 5 benchmarks a
no-rerank arm and it must travel the *identical* code path as the reranked one;
a `if reranker is not None` branch inside `retrieve()` would be exactly the
second path that requirement forbids.

Weights are local, pinned, and baked into the image — never downloaded at
runtime. This module deliberately imports no HTTP client and no hub library, so
there is no code path here that *could* fetch a missing file. That is enforced
by absence of capability rather than by a flag; `scripts/fetch_reranker_model.py`
is the only thing that ever reaches the network, and it runs at build time.

**One pair per `session.run()`. Do not batch — see `LocalOnnxReranker.rerank`.**
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

logger = logging.getLogger(__name__)

# Fan-out. N=40, not 30, because recall@N is a hard ceiling on any rerank arm —
# reranking reorders, it never recovers a chunk retrieval missed. Measured on
# golden set v3 (n=30 answerable): vector-only recall@30 is already 1.000, but
# hybrid's is 0.967 — question a13's gold chunk sits at rank 35 of a 52-deep
# fused pool, so a cut at 30 drops it. At 40 both arms reach 1.000, which is
# what lets chunk 5 read any miss as the reranker's fault and not the
# retriever's. Full table in the Step 03 vault note.
RERANK_N = 40

# 4 = physical cores on the development box. Threads are *not* a numerics
# variable here (verified: intra_op 1/2/4/8 give bitwise identical logits at
# batch size 1), so this is a pure latency knob and safe to tune per host.
# Because the executor below is max_workers=1, the total inference thread
# budget is 1 x this — bounded, never multiplied across concurrent requests.
DEFAULT_INTRA_OP_THREADS = 4

# Which implementation `build_reranker` returns. "local" is the default
# *implementation* of the protocol — when a reranker runs, it is this one, not a
# hosted API. Whether one runs at all is the caller's choice.
RerankerBackend = Literal["none", "local"]

DEFAULT_MODEL_DIR = Path("models/reranker")
DEFAULT_MANIFEST_PATH = Path("scripts/reranker_model.sha256")

MODEL_FILENAME = "model.onnx"
TOKENIZER_FILENAME = "tokenizer.json"
CONFIG_FILENAME = "config.json"

# [CLS] q [SEP] p [SEP] — three specials the passage has to share room with.
_SPECIAL_TOKEN_BUDGET = 3


class RerankError(RuntimeError):
    """Reranking failed for this request.

    Subclasses RuntimeError so `app/cli.py`'s existing handler reports it as
    `refused:` with exit 4 and no traceback.

    Deliberately *not* caught by `retrieve()`. One behaviour in the library —
    raise — and the policy at each boundary: the benchmark hard-fails for free
    (a row labelled "reranked" that silently isn't is the failure mode
    `GoldenSetError` exists to prevent), while the serving layer catches this
    and degrades to retrieval order, recording it in `degraded: ["rerank"]`.
    """


class RerankerModelMissing(RerankError):
    """A weight file is absent, unreadable, or does not match the manifest.

    Fatal at startup rather than per-request. Unlike Postgres or the vector store being
    unreachable — transient, external, and the reason `/health/ready` exists —
    a missing baked-in weight is a build defect that will never fix itself.
    """


class Candidate(Protocol):
    """What the reranker needs from a retrieval hit. `RetrievedChunk` satisfies
    this structurally, so no adapter is needed and this module never imports
    from `retrieval.py` (which imports *this* one).
    """

    @property
    def chunk_id(self) -> str: ...

    @property
    def text(self) -> str: ...


@dataclass(frozen=True)
class RerankTiming:
    """Per-call cost. Returned, never stored on the reranker: the reranker is a
    process-wide singleton, so last-call state on it would let concurrent
    requests overwrite each other's numbers.
    """

    n_candidates: int
    n_truncated: int
    tokenize_ms: float
    infer_ms: float
    total_ms: float
    intra_op_threads: int


@dataclass(frozen=True)
class RerankResult:
    order: list[str]
    """chunk_ids, best first, already cut to `top_k`."""

    scores: dict[str, float]
    """chunk_id -> raw cross-encoder logit. Empty for `NoOpReranker`.

    The raw logit, not a sigmoid. Order is identical either way (sigmoid is
    monotonic), but this model emits an unbounded logit — `config.json` sets
    `sbert_ce_default_activation_function: Identity` with `num_labels: 1` — and
    a sigmoid of a logit near ±10 saturates in float32, destroying the
    resolution the tie-break depends on. Convert at the presentation boundary
    with `to_probability()`, never in storage.
    """

    timing: RerankTiming


class Reranker(Protocol):
    async def rerank(
        self, query: str, candidates: Sequence[Candidate], top_k: int
    ) -> RerankResult: ...

    def close(self) -> None: ...


def to_probability(logit: float) -> float:
    """Logit -> 0-1, for API Contract §5.1's `citations[].score`."""
    return 1.0 / (1.0 + float(np.exp(-logit)))


def _rank(scores: dict[str, float], top_k: int) -> list[str]:
    """Stable ordering: score descending, `chunk_id` breaking exact ties.

    No rounding. At batch size 1 the scores are bitwise reproducible (see
    `LocalOnnxReranker.rerank`), so a raw float comparison is exact — rounding
    would only merge genuinely distinct scores. `chunk_id` as the tie-breaker
    matches `_LEXICAL_SQL`, which already orders `score DESC, c.id`.
    """
    return sorted(scores, key=lambda cid: (-scores[cid], cid))[:top_k]


# --- weight verification -----------------------------------------------------
# Lives here, not in the fetch script, because verifying its own weights is the
# app's concern at startup. The script imports these so there is exactly one
# manifest parser and the two can never drift.


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_manifest(path: Path) -> tuple[str, dict[str, str]]:
    """Returns (revision, {filename: sha256}).

    Raises on anything malformed. An unreadable manifest must never degrade to
    "skip verification" — that would turn the drift detector off precisely when
    something is already wrong.
    """
    revision = ""
    checksums: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# revision:"):
            revision = line.split(":", 1)[1].strip()
            continue
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"{path}:{lineno}: expected '<sha256>  <filename>', got {raw!r}")
        checksums[parts[1]] = parts[0]
    if not revision:
        raise ValueError(f"{path}: no '# revision:' line — cannot tell which commit this pins")
    if not checksums:
        raise ValueError(f"{path}: no checksums")
    return revision, checksums


def verify_model_files(model_dir: Path, manifest_path: Path) -> list[str]:
    """Every problem, not just the first — someone fixing a bad checkout wants
    the whole list. Empty return means verified.
    """
    if not manifest_path.is_file():
        return [f"{manifest_path} does not exist — cannot verify {model_dir}"]

    revision, checksums = read_manifest(manifest_path)
    failures: list[str] = []
    for filename, expected in checksums.items():
        target = model_dir / filename
        if not target.is_file():
            failures.append(f"missing: {target}")
            continue
        actual = sha256_file(target)
        if actual != expected:
            failures.append(
                f"sha256 mismatch: {target}\n      expected {expected} (revision "
                f"{revision[:12]})\n      actual   {actual}"
            )
    return failures


# --- implementations ---------------------------------------------------------


class NoOpReranker:
    """Passes retrieval order through untouched.

    First-class, and load-bearing for chunk 5: with this as `retrieve()`'s
    default, the no-rerank arm and the reranked arm run the same function, the
    same truncation, and the same provenance code. It also gives production a
    real degradation switch (`--reranker none`) with no dead branch.

    `scores` stays empty and every hit's `rerank_score` therefore ends up None,
    so "was this reranked?" is answerable from the result alone.
    """

    async def rerank(self, query: str, candidates: Sequence[Candidate], top_k: int) -> RerankResult:
        return RerankResult(
            order=[c.chunk_id for c in candidates][:top_k],
            scores={},
            timing=RerankTiming(
                n_candidates=len(candidates),
                n_truncated=0,
                tokenize_ms=0.0,
                infer_ms=0.0,
                total_ms=0.0,
                intra_op_threads=0,
            ),
        )

    def close(self) -> None:
        return None


NOOP_RERANKER = NoOpReranker()
"""Module singleton. Stateless, so sharing it is free and safe."""


def build_reranker(
    backend: RerankerBackend,
    model_dir: Path = DEFAULT_MODEL_DIR,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    intra_op_threads: int = DEFAULT_INTRA_OP_THREADS,
) -> Reranker:
    """The one place a reranker is constructed.

    Takes primitives rather than `Settings` so this module stays independent of
    config, matching `retrieval.py` and `vector_store.py`. Both the HTTP app and
    the CLI call this, so they cannot drift on how loading or failure works.

    Raises `RerankerModelMissing` for `"local"` when the weights are absent or do
    not match the manifest. It is deliberately not caught here: callers make it
    fatal at startup rather than degrading to `"none"`, because a reranker that
    silently is not there produces answers that look reranked and are not.
    """
    if backend == "none":
        return NOOP_RERANKER
    return LocalOnnxReranker(
        model_dir=model_dir,
        manifest_path=manifest_path,
        intra_op_threads=intra_op_threads,
    )


class LocalOnnxReranker:
    """int8 ONNX cross-encoder, loaded once per process.

    Constructed at startup (`main.py`'s lifespan / `cli.py`'s `_dispatch`),
    beside the vector store, and injected as a parameter — never imported as
    a module global and never built inside `retrieve()`.
    """

    def __init__(
        self,
        model_dir: Path = DEFAULT_MODEL_DIR,
        manifest_path: Path = DEFAULT_MANIFEST_PATH,
        intra_op_threads: int = DEFAULT_INTRA_OP_THREADS,
    ) -> None:
        failures = verify_model_files(model_dir, manifest_path)
        if failures:
            raise RerankerModelMissing(
                f"reranker weights in {model_dir} did not verify against {manifest_path}:\n  "
                + "\n  ".join(failures)
                + "\n\nRun: uv run python scripts/fetch_reranker_model.py"
            )

        config = json.loads((model_dir / CONFIG_FILENAME).read_text())
        self._max_seq_len: int = int(config["max_position_embeddings"])
        self._intra_op_threads = intra_op_threads

        options = ort.SessionOptions()
        options.intra_op_num_threads = intra_op_threads
        options.inter_op_num_threads = 1
        # A 6-layer BERT is a linear graph; inter-op parallelism buys nothing.
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self._session = ort.InferenceSession(
            str(model_dir / MODEL_FILENAME), options, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}

        self._tokenizer = Tokenizer.from_file(str(model_dir / TOKENIZER_FILENAME))
        # `only_second`, never the library default `longest_first`: the query is
        # 10-25 tokens and truncating it would change the question being scored,
        # which is a correctness bug. Clipping a passage tail loses evidence but
        # still answers the question actually asked.
        self._tokenizer.enable_truncation(
            max_length=self._max_seq_len, strategy="only_second", direction="right"
        )
        # No enable_padding(): batch size is 1, there is nothing to pad to.

        # A second, truncation-free tokenizer, used only to measure the query in
        # rerank()'s length guard. `only_second` truncation raises "Second
        # sequence not provided" on a single-sequence encode, but *only* once
        # truncation actually has to engage — i.e. in exactly the oversized case
        # the guard exists to catch. Toggling truncation off and on around the
        # measurement would be shorter and wrong: the guard runs on the event
        # loop, so concurrent requests would race on that shared state.
        self._length_probe = Tokenizer.from_file(str(model_dir / TOKENIZER_FILENAME))

        # Dedicated and bounded. `vector_store.py` routes every blocking store call
        # through asyncio's shared default pool; a rerank is seconds of solid
        # CPU and would stall those reads if it queued there too. max_workers=1
        # also makes the total thread budget knowable: 1 x intra_op.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerank")

    async def rerank(self, query: str, candidates: Sequence[Candidate], top_k: int) -> RerankResult:
        if not candidates:
            return RerankResult([], {}, self._timing(0, 0, 0.0, 0.0, 0.0))

        query_len = len(self._length_probe.encode(query, add_special_tokens=False).ids)
        if query_len > self._max_seq_len - _SPECIAL_TOKEN_BUDGET:
            # Guarded rather than silently truncated: with `only_second` an
            # oversized query leaves zero room for the passage, so the model
            # would score an empty document and report a confident number.
            raise RerankError(
                f"query is {query_len} tokens, over the {self._max_seq_len} limit minus "
                f"{_SPECIAL_TOKEN_BUDGET} special tokens — refusing to truncate the query"
            )

        # Sorted so the result is a pure function of the candidate *set*, never
        # of the order retrieval happened to hand them over.
        ordered = sorted(candidates, key=lambda c: c.chunk_id)

        loop = asyncio.get_running_loop()
        # NOTE: run_in_executor is not cancellable — on client disconnect or
        # timeout the worker runs to completion (09_Memory/LEARNINGS.md).
        # Bounded here by N (40) rather than by cancellation.
        try:
            scores, n_truncated, tokenize_ms, infer_ms = await loop.run_in_executor(
                self._executor, self._score_all, query, ordered
            )
        except RerankError:
            raise
        except Exception as exc:
            raise RerankError(f"reranker inference failed: {exc}") from exc

        timing = self._timing(
            len(ordered), n_truncated, tokenize_ms, infer_ms, tokenize_ms + infer_ms
        )
        logger.debug(
            "rerank",
            extra={
                "n_candidates": timing.n_candidates,
                "n_truncated": timing.n_truncated,
                "tokenize_ms": round(timing.tokenize_ms, 2),
                "infer_ms": round(timing.infer_ms, 2),
                "intra_op_threads": timing.intra_op_threads,
            },
        )
        return RerankResult(order=_rank(scores, top_k), scores=scores, timing=timing)

    def _score_all(
        self, query: str, candidates: Sequence[Candidate]
    ) -> tuple[dict[str, float], int, float, float]:
        """One `session.run()` per pair. Runs in the executor thread.

        ponytail: one pair per call looks wasteful and is measured *faster*
        (0.71x of a single batch of 40 — a batch pads every row to the longest
        one, this pads nothing). It is also the only batch-invariant option.
        This model is dynamically quantized — 50x DynamicQuantizeLinear + 50x
        MatMulInteger — so activation scales are computed at runtime from each
        tensor's min/max *across the whole batch*. A neighbour in the batch
        moves your logit by ~0.1, which reorders results: measured 17 of 435
        pairs discordant between batched and unbatched scoring, with the top-10
        order differing. Fixed-length padding does NOT fix this; only batch
        size 1 does. Do not "optimize" this into a batch without re-running
        `_check_grouping_invariance()` in _demo() below.
        """
        scores: dict[str, float] = {}
        n_truncated = 0
        tokenize_ms = 0.0
        infer_ms = 0.0

        for candidate in candidates:
            t0 = time.perf_counter()
            encoding = self._tokenizer.encode(query, candidate.text)
            ids = encoding.ids
            # `only_second` clips the passage at exactly max_seq_len, so hitting
            # the ceiling is the truncation signal. Counted, not assumed away:
            # chunk 5 has to be able to see whether truncation confounds a metric.
            if len(ids) >= self._max_seq_len:
                n_truncated += 1
            feed = {
                "input_ids": np.array([ids], dtype=np.int64),
                "attention_mask": np.array([encoding.attention_mask], dtype=np.int64),
                "token_type_ids": np.array([encoding.type_ids], dtype=np.int64),
            }
            t1 = time.perf_counter()
            outputs = self._session.run(
                None, {k: v for k, v in feed.items() if k in self._input_names}
            )
            t2 = time.perf_counter()

            scores[candidate.chunk_id] = float(outputs[0].reshape(-1)[0])
            tokenize_ms += (t1 - t0) * 1000.0
            infer_ms += (t2 - t1) * 1000.0

        return scores, n_truncated, tokenize_ms, infer_ms

    def _timing(
        self,
        n_candidates: int,
        n_truncated: int,
        tokenize_ms: float,
        infer_ms: float,
        total_ms: float,
    ) -> RerankTiming:
        return RerankTiming(
            n_candidates=n_candidates,
            n_truncated=n_truncated,
            tokenize_ms=tokenize_ms,
            infer_ms=infer_ms,
            total_ms=total_ms,
            intra_op_threads=self._intra_op_threads,
        )

    def close(self) -> None:
        self._executor.shutdown(wait=True)


def _demo() -> None:
    """Offline checks always; the live ones only when the weights are present.

    The live checks pin `onnxruntime==1.29.0` (pyproject.toml). The negative
    control below asserts a property of ORT's *dynamic quantization* — that
    batch composition perturbs activation scales — and an unpinned runtime
    could change that behaviour, failing the assertion for reasons that have
    nothing to do with this module.
    """
    import random
    import tempfile

    # --- pure ranking math, no weights ---
    assert _rank({"c": 1.0, "a": 3.0, "b": 2.0}, 3) == ["a", "b", "c"]
    assert _rank({"c": 1.0, "a": 3.0, "b": 2.0}, 2) == ["a", "b"]
    # Exact tie -> chunk_id decides, so the order can't depend on dict insertion.
    assert _rank({"z": 5.0, "a": 5.0}, 2) == ["a", "z"]
    assert _rank({"a": 5.0, "z": 5.0}, 2) == ["a", "z"]
    assert _rank({}, 5) == []

    @dataclass(frozen=True)
    class _Cand:
        chunk_id: str
        text: str

    # Structural conformance, checked by mypy rather than asserted at runtime.
    _proto_check: Reranker = NOOP_RERANKER
    assert _proto_check is NOOP_RERANKER

    # --- NoOp is the identity, and preserves retrieval order exactly ---
    cands = [_Cand(f"chk_{i:02d}", f"passage {i}") for i in (3, 1, 2, 0)]
    noop = asyncio.run(NOOP_RERANKER.rerank("q", cands, top_k=10))
    assert noop.order == ["chk_03", "chk_01", "chk_02", "chk_00"], noop.order
    assert noop.scores == {}, "NoOp must not invent scores"
    assert noop.timing.n_candidates == 4
    truncated = asyncio.run(NOOP_RERANKER.rerank("q", cands, top_k=2))
    assert truncated.order == ["chk_03", "chk_01"]
    assert asyncio.run(NOOP_RERANKER.rerank("q", [], top_k=5)).order == []

    # --- manifest parsing refuses to degrade to "skip verification" ---
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "m.sha256"
        for content, why in (
            ("abc  model.onnx\n", "no revision line"),
            ("# revision: deadbeef\n", "no checksums"),
            ("# revision: deadbeef\nonly-one-field\n", "malformed row"),
        ):
            bad.write_text(content)
            try:
                read_manifest(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"read_manifest accepted a bad manifest: {why}")

        # A missing weight must name the absent path, not fail vaguely.
        empty = Path(tmp) / "weights"
        empty.mkdir()
        good = Path(tmp) / "good.sha256"
        good.write_text("# revision: deadbeef\n" + f"{'0' * 64}  {MODEL_FILENAME}\n")
        problems = verify_model_files(empty, good)
        assert problems and MODEL_FILENAME in problems[0], problems
        assert verify_model_files(empty, Path(tmp) / "nope.sha256"), "absent manifest must fail"

        try:
            LocalOnnxReranker(model_dir=empty, manifest_path=good)
        except RerankerModelMissing as exc:
            assert MODEL_FILENAME in str(exc) and "fetch_reranker_model" in str(exc), exc
        else:
            raise AssertionError("LocalOnnxReranker loaded with no weights present")

    print("ok (offline): ranking math, tie-break, NoOp identity, manifest + missing-weight guards")

    if not (DEFAULT_MODEL_DIR / MODEL_FILENAME).is_file():
        print(
            f"skipped (live): no weights at {DEFAULT_MODEL_DIR} — "
            "run scripts/fetch_reranker_model.py"
        )
        return

    # --- live: the grouping-invariance guarantee, and proof the test has teeth ---
    reranker = LocalOnnxReranker()
    try:
        query = "What does the glossary say about Cross-Encoder rerankers?"
        rng = random.Random(0)
        vocab = (
            "retrieval augmented generation embedding vector reranker cross encoder relevance "
            "passage query chunk index corpus latency precision recall groundedness citation"
        ).split()
        pool = [
            _Cand(f"chk_{i:02d}", " ".join(rng.choice(vocab) for _ in range(rng.randint(120, 260))))
            for i in range(RERANK_N)
        ]

        def scores_of(cands: list[_Cand]) -> dict[str, float]:
            return asyncio.run(reranker.rerank(query, cands, top_k=RERANK_N)).scores

        def as_bytes(s: dict[str, float]) -> bytes:
            return np.array([s[k] for k in sorted(s)], dtype=np.float32).tobytes()

        baseline = scores_of(pool)
        reversed_order = scores_of(list(reversed(pool)))
        shuffled = list(pool)
        rng.shuffle(shuffled)
        shuffled_scores = scores_of(shuffled)

        assert as_bytes(baseline) == as_bytes(reversed_order), "reversed input changed the scores"
        assert as_bytes(baseline) == as_bytes(shuffled_scores), "shuffled input changed the scores"
        assert _rank(baseline, 5) == _rank(shuffled_scores, 5)

        # Same again on a second session at a different thread count. Locks in
        # that intra_op is a pure latency knob, so benchmark figures stay
        # comparable across hosts.
        single_threaded = LocalOnnxReranker(intra_op_threads=1)
        try:
            assert as_bytes(baseline) == as_bytes(
                asyncio.run(single_threaded.rerank(query, pool, top_k=RERANK_N)).scores
            ), "intra_op changed the scores"
        finally:
            single_threaded.close()

        # NEGATIVE CONTROL. A test that can only pass proves nothing, so prove
        # the property it guards is real: score the same pairs as one batch of
        # N and as two batches of N/2, and require those to DIFFER. If this
        # ever stops differing, the invariance assertions above have gone
        # vacuous and the batch-1 rule can be revisited on evidence.
        def batched(cands: list[_Cand], size: int) -> dict[str, float]:
            tok = Tokenizer.from_file(str(DEFAULT_MODEL_DIR / TOKENIZER_FILENAME))
            tok.enable_truncation(max_length=512, strategy="only_second", direction="right")
            tok.enable_padding(pad_id=0, pad_token="[PAD]")
            session = reranker._session
            names = reranker._input_names
            out: dict[str, float] = {}
            for i in range(0, len(cands), size):
                group = cands[i : i + size]
                encs = tok.encode_batch([(query, c.text) for c in group])
                feed = {
                    "input_ids": np.array([e.ids for e in encs], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
                    "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64),
                }
                logits = session.run(None, {k: v for k, v in feed.items() if k in names})[0]
                for c, v in zip(group, logits.reshape(-1), strict=True):
                    out[c.chunk_id] = float(v)
            return out

        one_batch = batched(pool, RERANK_N)
        two_batches = batched(pool, RERANK_N // 2)
        assert as_bytes(one_batch) != as_bytes(two_batches), (
            "negative control failed: batching no longer perturbs scores, so the "
            "grouping-invariance assertions above no longer prove anything"
        )
        max_delta = max(abs(one_batch[k] - two_batches[k]) for k in one_batch)

        # --- truncation is counted, not silently absorbed ---
        long_passage = " ".join(["token"] * 4000)
        result = asyncio.run(reranker.rerank(query, [_Cand("chk_long", long_passage)], top_k=1))
        assert result.timing.n_truncated == 1, result.timing

        # --- an oversized query is refused, never truncated ---
        try:
            asyncio.run(reranker.rerank(" ".join(["word"] * 2000), pool[:1], top_k=1))
        except RerankError as exc:
            assert "refusing to truncate the query" in str(exc), exc
        else:
            raise AssertionError("an oversized query was accepted")

        assert 0.0 < to_probability(baseline[_rank(baseline, 1)[0]]) < 1.0
    finally:
        reranker.close()

    print(
        f"ok (live): scores bitwise-identical across input order and thread count; "
        f"negative control confirms batching still perturbs them (max delta "
        f"{max_delta:.3f} logits); truncation counted; oversized query refused"
    )


if __name__ == "__main__":
    _demo()
