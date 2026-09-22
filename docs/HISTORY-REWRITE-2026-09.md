# History rewrite, 2026-09-22

On 2026-09-22 the text of a third-party book was removed from this repository's entire git
history, for copyright reasons. Three paths had carried it:

- `app/corpus/corpus_vectors.json`: the `document` field of every record (chunk text).
- `tests/fixtures/golden_set_v3.json`: `snippet`, `near_miss_to.snippet` and `expected_answer_substring`.
- `docs/REVIEW-golden-set-v3.md`: the quoted "Adjacent passage" for question `u05`.

That text now lives only in local files that are not committed (see the two 2026-09-22 entries in
`docs/DECISIONS.md`). No other file content, author, date or commit message changed. The
12 commits before `80b14b1` kept their hashes. The 69 commits below, from `80b14b1` on,
received new hashes.

**Hashes cited in `docs/DECISIONS.md`, in `docs/chunk5-results.json` and `docs/chunk7-scores.json`,
and inside commit messages are the OLD hashes.** Those records are pre-registered and were left
unchanged. Use the table to find the matching commit in the current history.

| old | new | date | subject |
| --- | --- | --- | --- |
| `80b14b147738fd42a38b1d842975904f69b75c05` | `424e97fb49e68d8c5d59a2eba35f377668bbac98` | 2026-08-20 | feat: golden set v3 with offset anchoring and verified near-misses |
| `7db5f3f79af0857f47ff3aac4df2146d00739496` | `4eefafc3f98213dd27a0b4197c076c94cfba5e33` | 2026-08-20 | feat: resolve gold chunk ids from offsets, not from the fixture |
| `7b3c876a8adffd1c1db0104dfb00bc9df0c7a69d` | `bcb857987c8f6e37b2aad326e3b84f133cf386e3` | 2026-08-20 | feat: re-baseline vector-only vs hybrid on golden set v3 |
| `5db0925b8519747a17ee1a405e6f19d253068e15` | `7b447c40311ccd31b65ccab95dc805653af03a6d` | 2026-08-21 | feat(rerank): local ONNX cross-encoder behind a Reranker protocol |
| `e806aade6448b43fed20980aae5228a783ed33c4` | `8f67d8c82857287b3b22df5d1b9ed2695845ddd9` | 2026-08-21 | feat(rerank): wire retrieve() to accept a Reranker; NoOp proven byte-identical |
| `0edbb39aec329b73b0db8d9c3d4287b8345655f5` | `34994ddcf48037355909583f8b426e7ee1a20b47` | 2026-08-21 | fix(eval): pin MRR to a fixed depth (MRR@10) and label the report's reranker |
| `797bcdeb5f0b3d3687af53981caada9f22aaf21a` | `cdb347ece46cf15d12a70229dc3a98522b9b8a4e` | 2026-08-21 | chore: align the ruff pre-commit pin to the venv (v0.8.6 -> v0.16.3) |
| `09598e1b96ce31777ad6b410f4737acee5acea9d` | `ba2e758ca678249f4b16829daaf338a19719908c` | 2026-08-21 | feat(rerank): construct the reranker from config; all four benchmark arms now real |
| `69f62e7f74e2e2dc268843750774cd4e5157566f` | `64774056d7d20344e3b8f7e0d2d8216221ac1f24` | 2026-08-21 | feat(eval): mark a near-miss UNSCORED when its near_miss_to span was not retrieved |
| `afa090701c655e1c2eb0842768615943f3c7868c` | `a3fd9e83395a2571935e9fd55e38b510691f06c2` | 2026-08-21 | fix(eval): report near-miss coverage per arm and suppress cross-arm comparisons |
| `476b3b1bbb3e6358357e51e71390aa5eb08a4473` | `df33b65481de889d856af9a457efdbee723fce53` | 2026-08-22 | docs: rewrite README for the current system state |
| `a8db8388f9e3e2eb3ba5d1b379272062205cb9cd` | `aff027c85bc3ec103a85186e2c2292feb3a2200d` | 2026-08-22 | fix(cli): distinguish an empty corpus from a query with no matches |
| `cd627a273db8975e133ba11895f93b8a5640f7dc` | `33ab0586d31d8f334e19e7b51f64baad1e057fda` | 2026-08-24 | bench(step03): three-arm answerable metrics + DoD #5 split verdict |
| `d7483ed3dc9428ce4aa9714af5353a47eae9d555` | `1e98d47f1eae36fd8ae1f007640a6868d16e313c` | 2026-08-24 | feat(context): add context budgeter with conservative token accounting |
| `4e7b80e581280d198f2e2d753d9b83b82d9f1249` | `927ab76907105d9b7d44332c6aad75e97aac2fed` | 2026-08-24 | refactor(eval): rename resolve_gold_chunk_ids to resolve_target_chunk_ids |
| `0e81d35e22bb9a5f8d04d0f3389b4f3da6146da2` | `82998307be84a76c2866bfc810a091186a80fe1d` | 2026-08-24 | docs: mirror the working decisions log into the repo |
| `d5744093ab4772a45cf6473a366da2351722af35` | `4ed301a7ceff55de7e406fea4feadefc6abdd5b6` | 2026-08-25 | feat(context): replace heuristic token counter with tiktoken cl100k_base |
| `16bcd06da93c13febc07c975fe82da48c54a2a8b` | `703b1588b331fb6b3ea57806352b90e03ed28535` | 2026-08-27 | chunk 7.1: reranker score does not track groundedness - 7.2 blocked |
| `7f207e827e374bf586f6a45ad74e3d5b98d44683` | `b9df1efeb915176ed7dd208caa155d1f1508f266` | 2026-08-31 | chunk 7.2: exact brute-force vector search replaces Chroma/HNSW |
| `af45ddfd01b5ec2b1f916f36748f76bfff46585b` | `f1820509f9601ce02c26bdbaceaa6072f9c21a01` | 2026-08-31 | docs: mirror the chunk 7.2 decision into the repo log |
| `5d3f974e652c7badbcd12d1ff6c98b4e8fa42b41` | `f5c259f24957ebdbb861e2f38b3abfecb6ed95cc` | 2026-09-03 | docs: record chunk 7.3 findings in DECISIONS mirror |
| `a8d0c1e262fff887911460ee8ba7acbd120eb32b` | `995491ad55483cb19a111f2e6fafd66d3a78494c` | 2026-09-03 | docs: pre-register the abstain mechanism set (chunk 7.4 phase 1) |
| `4b262b2c90fb0627d851f81c6c836cdec16d369e` | `044d6add89f04734eccb64fc371350996e7cdfbc` | 2026-09-03 | docs: repoint chunk 7.4 evidence to the persisted vault artifact |
| `ca381c79533faf22990ea251ca689be97021652d` | `a98e87cd1fcfbad7347f2e47acf69e764f1e796d` | 2026-09-03 | feat: pin the class-1 out-of-domain query vectors (chunk 7.4) |
| `d7cf00b071f9276a217f3dfc2b23bfe456d5ae68` | `d95066bdb9adea3b6fd5fef076426bd58abcd00e` | 2026-09-03 | docs: record the chunk 7.4 OOD gate result |
| `bc1e2619632ebb490daa59fef5bfd062fd490341` | `0ce3a1f11c50beaba86431f1d6bdb2cf4b592476` | 2026-09-04 | docs: close the retrieval-side measurement line; pin unfitted far-field placeholder |
| `7dffb81a30bf9b1ac9bc0d7768175dcafb59380d` | `eadb87e62e1895adf9e64a128ca254938736124f` | 2026-09-04 | feat: add the Guardrail far-field abstention with an unfitted placeholder constant |
| `68707ed78f0a482c67dc3b2826505a27d9c6f06a` | `3b490969c385c19e080446113365fbe8ba6919e6` | 2026-09-04 | feat: add POST /api/v1/query gated by the Guardrail |
| `ea7087158f92f0e7f94ed89dcbc8bbc89361c85b` | `54a0f0be356935d888146cdf5860c67fc8a88721` | 2026-09-04 | refactor: retire the bare name `guardrail`; three senses, three names |
| `7f096e33c6183f3a7fe75205ae7cbec78290c000` | `c59f0ca48d3a0abedf11828812e6c6ae4de99476` | 2026-09-04 | fix(context): retire the false MEASURED claim on the per-chunk header overhead |
| `2feacdd9055f2a1e836c10890384cd3fcce1acc4` | `67667abaa1432e405d451a4afc6346bbd7007914` | 2026-09-05 | docs(decisions): messages.guardrail records the injection_scanner sense; supersedes the not-established claim |
| `746ac15269c8ab9404d5177bfefd2fed70f242f3` | `c926cecdacf811b22fc109543814ef541e56610a` | 2026-09-08 | docs: close chunk 8.1 contract scope line; amendment stays pending |
| `d76a1bca2f47b68aeda059b8a5f31b25610ca983` | `310374aa3647d3db5023f53448b28537c10ee9b8` | 2026-09-09 | docs: record chunk 8.0's missing DECISIONS entry; rationale located, gap scoped |
| `1af392e523f2674a46b1033f6af3973f6afaf60b` | `8b4577bd85e2457ca26e782af8a217ac67d94fbf` | 2026-09-09 | feat(provenance): render context + citations from a single header format |
| `b98fe1ef57f577e8781fb874c8cfa24d5fba57a9` | `af8b5dfb1cf5b7f2343c0cc66ecdb930d36217e4` | 2026-09-09 | docs(budget): reconcile PER_CHUNK_OVERHEAD_TOKENS, retire the provenance tripwire |
| `3318ad173f1787fdc3bc92585257c42c0b107475` | `2baec16034fd6916e52dee6032b298c753b0d453` | 2026-09-10 | fix(budget): validate the budget under the counter that actually ships |
| `9330033a6e0fe6d032d71c83097aef43c60516f2` | `9105810bc5de9e5c8221537311991d5da21d4ab6` | 2026-09-11 | docs(provenance): reject wiring the renderer into the query route |
| `9c584aefc3498130aeec5b64b6f7338211482408` | `5ea0c1ffa5baaf61859136aa200c34b512fe0488` | 2026-09-11 | docs(generation): pre-register the generation design (chunk 8.8) |
| `1729ad4fdd45c637490da6f481b74ad18a73ab57` | `f97496ecbf7c9fe6e66c2b11aba960a5aa76d14d` | 2026-09-11 | feat(generation): answer ANSWER_UNVERIFIED queries via the Anthropic Messages API (chunk 8.9) |
| `2849f67fdcfe6e37b9ddf94f68d7da071397586d` | `5aa765010282d141d45898cbbb02fb05a4efe46e` | 2026-09-13 | feat(security): require a client credential, rate limit and cap the query route (chunk 8.10) |
| `4635867459cb3cc2e90cad0d2ea85b7e00e6f127` | `db559f90eea92898b1202ed9331f3927b66310c6` | 2026-09-13 | docs(verifier): pre-register Verifier v1 as a promotion gate (chunk 8.11) |
| `792b980b82099cb5e27d042490ffb1eba44b736b` | `f067a96ffe3e05f3b1f2263f421177642ff285e9` | 2026-09-13 | docs(gate): pre-register and run the gate capture recipe (chunk 8.12 Phase A) |
| `4e38dca4aea419c1ccf45f3c3662591b269fa75d` | `cc312be05c54be11fc1ffb83d4fdfb0bfcfcf0af` | 2026-09-15 | docs(inventory): record FarFieldVerdict consumer inventory (chunk 8.13 Phase A) |
| `d5756fc5b41359c1f29c697b5d3079137bd13be1` | `73670e0f585aadfb371729dcdbb9b44076d325be` | 2026-09-18 | docs(ingest): record A5 ESTABLISHED — ingest deletes points for removed notes (chunk 8.14 Phase B4) |
| `c788210609330758e6afb815611e7b7b556accfc` | `12093fb0a09310f85e9a7210265fa132aa4499bf` | 2026-09-18 | docs(decisions): record devbrain-search error handling fix (chunk 8.14 pass C1) |
| `5e1eb013b4759ec5d03498d305e490c3baf3fead` | `fc9892c440e516391c4a407b991c1aa76aacf30f` | 2026-09-18 | docs(decisions): pre-register injection_scanner v1 (RB-02 phase A) |
| `29be348dd64c0c4a14f9e91db30708efd0e628aa` | `14385572c68c433e5bd933011f540d43903de32c` | 2026-09-18 | test(injection_scanner): pre-registered fixtures and expected values |
| `fbd1fe7f022178bd50c95a779f1aea3365731b5f` | `adbd3dd768d028a3ce6ff27f9b46f8244efdf854` | 2026-09-18 | feat(agents): injection_scanner v1, report-only, unwired |
| `a7d0460bce2ec061396eb77330451735f745e37c` | `f43d5c6131f345e53a61c4cbc0385409e81d5e04` | 2026-09-18 | docs(decisions): record injection_scanner v1 chunk (a) results |
| `729ed8776c9b6213566ba44c5683e71f8043dec0` | `75d2d591824b49012c9d44f75cc51aea58f8be54` | 2026-09-18 | docs(decisions): pin injection_scanner T7 secondary baseline (RB-02 phase D) |
| `0db22e8676608f6dadcce1ee6faa88a25d93a802` | `dad6137df26b1089f9c55a94797808c6f2ef653a` | 2026-09-18 | feat(api): wire injection_scanner in report-only mode |
| `42c649929ec0939cb33f8858a6327e5e478b8e3f` | `435bb5ef33a90519635d31949d97ada873d2ba28` | 2026-09-18 | docs(decisions): record injection_scanner v1 chunk (b) results |
| `50ea4adf3bb33830e71596536ab4fbb957f29f9d` | `c8cb5b1666079dfdabcf51ef6e842dae342db652` | 2026-09-18 | docs(decisions): pre-register _neutralise tag-variant fix (RB-03 phase 2) |
| `62ed10410444ee233b5ebb9b988a05f954436901` | `65678bd5731ac4e640978036b864ee2331ca3263` | 2026-09-18 | fix(generation): neutralise retrieved_context tag variants |
| `b32aa0e9b11bbfda0841026084544d01d643429e` | `3b750859365fd3237b5fc184235d9a418e6de451` | 2026-09-18 | docs(decisions): record _neutralise tag-variant fix results (RB-03) |
| `e90688a7726f7340241bd5c7c0e490dc3f424621` | `76cf6bfd4e84f9e763e2c1eaa0870dd21d575662` | 2026-09-18 | docs: describe split query-path mechanisms; retire fused-stage prose |
| `95317fbaafc98169b62a5a66a80c02e5e02947d3` | `9f0beb968601d97a43497284fd10221619700e0d` | 2026-09-18 | docs(decisions): clarify T4b terminology |
| `14fd4710de3b5d0efdbcb64d86f7ad605c3e0e00` | `fad0b7fea972356dda76926f36690cbbd4cbb53c` | 2026-09-18 | docs(decisions): pre-register injection_scanner chunk (c) enforcement E′ (header-open defang at render) |
| `fade362b63282e2ae96e04c3c5d303f3b1dfa4df` | `5f9379c642f47f531ea4d4457eccd81b0972b49b` | 2026-09-19 | docs(decisions): amend chunk (c) pre-registration: T2 expectation under E′ |
| `af873c3d56fa447972c775e6e360c80ed32e4895` | `740ed4bc397e2083a0a4fc93b18a7b84caf9a243` | 2026-09-19 | fix(provenance): defang header-open shapes in chunk bodies at render |
| `cc39f35cd373f9f9b3291575ad4f62b1334b66b6` | `696bd072f999c537bc6617dbd795a789ee3ba0ba` | 2026-09-19 | docs(decisions): record chunk (c) E′ results |
| `d52ee8d270917f10e5798ba3dd3eecbcba7cc816` | `54375b53bb50cdb843d1c22529c3ca14536d3b88` | 2026-09-22 | docs: align README with current code; drop unrelated DECISIONS entries |
| `fdd5c1402345dfd38a9622601ced1075eb4aa749` | `3e48ed2524a523d2026e96c704cd6b328690b211` | 2026-09-22 | docs: add repo CLAUDE.md with DECISIONS scope and path rules |
| `090d2747d43b923d5fb9dfa1672a902eb9eee6c8` | `cb9c09ac15f8928f86a5f7c29514a99c58e82c44` | 2026-09-22 | docs: correct injection_scanner docstring; note vault refs are private |
| `66ccf870609953318059a855dcac0e2330dcacaa` | `c3b78d238244d6d50a630d9c29dce44c02adf9c8` | 2026-09-22 | refactor(ingestion): read self-check PDF path from INGESTION_SELFCHECK_PDF |
| `7a7a4e804bc2ddc0ff8b96438f2914fb78713089` | `5d680bfd363945e4a5764658de41c6617655014c` | 2026-09-22 | test: add pytest fixture for injection_scanner corpus chunks |
| `d304074fbf652ca802d6a1a0faf2504ee76370fa` | `cd5a056a17e3727ad0fe8299574f2a7a93dcab37` | 2026-09-22 | fix(corpus): move chunk text out of the repo into a local-only file |
| `bfaf081be4232fadbe073eff21b657a71f7a8e5d` | `54ac28732bcc017402c32a917a0e612c1c16af93` | 2026-09-22 | fix(golden-set): move snippet and answer-substring text to a local-only file |
| `f881f8af965dc4f4f3ab8ef5441c10ee38c3cf31` | `367b680543969c0eab35527ea3e1977d3956a288` | 2026-09-22 | docs(readme): state exactly what is and isn't in the repo for reproducibility |
