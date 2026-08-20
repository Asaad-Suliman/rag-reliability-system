# Golden set v3 — review table

**Status:** drafted and verified. **Not approved, not measured against.**

`tests/fixtures/golden_set_v3.json` — 36 entries (30 answerable, 6 near-miss). v2 left untouched.

`scripts/verify_golden_set.py` exits 0: every offset resolves through its containing chunk and every snippet matches byte for byte.

## Overlap distribution

Question↔snippet content-word overlap, using Postgres `to_tsvector('english', …)` — the same stemmer the lexical retrieval arm uses, so this measures what actually inflates BM25.

| n | mean | median | min | max | share = 0.00 | above 0.30 |
| --- | --- | --- | --- | --- | --- | --- |
| 36 | 0.157 | 0.167 | 0.00 | 0.43 | 6 | 1 (`u01`, justified) |

Spread of the 30 answerable entries across page-deciles of the 247-page corpus:

| decile | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| count | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 |

## Answerable — 30 entries

| id | dec | pg | question | target snippet | ratio | raw |
| --- | --- | --- | --- | --- | --- | --- |
| `a01` | 1 | 5 | Where did the person who wrote this book go to university? | He is an IIT Bombay alumnus. | 0.00 | 0/5 |
| `a02` | 1 | 11 | A single person spends five minutes asking follow-ups about one big uploaded file. If a thousand people did that daily, what does the book say the bill would be? | That single user's 5-minute session just cost $1.50. If 1,000 users do this per day, that's $1,500. | 0.10 | 2/20 |
| `a03` | 1 | 22 | Between grounding answers in documents and training the model on your own data, which does the book say comes out ahead on staying current and showing its work? | RAG wins on data freshness, external knowledge access, hallucination prevention, and transparency. | 0.07 | 1/14 |
| `a04` | 2 | 31 | What is it called when someone asks for a short overview and gets a long answer instead? | Incorrect Specificity (FP6) occurs when the level of detail does not match the request. | 0.00 | 0/9 |
| `a05` | 2 | 33 | How does the book suggest catching personal details in a user's message before the system acts on it? | PII Detection Prevent data leakage Regex + NER → placeholders | 0.00 | 0/9 |
| `a07` | 2 | 48 | Is there a way to drop weak results automatically instead of picking a fixed cutoff number? | Autocut examines similarity scores and identifies where the scores decline significantly. The system uses that point as a threshold and excludes resu… | 0.10 | 1/10 |
| `a09` | 3 | 60 | How many things does the architecture chapter's wrap-up say can go wrong, and what are they? | Enterprise RAG architecture addresses seven failure modes through deliberate design: Missing Content, Missed Top- Ranked Documents, Not in Context, N… | 0.22 | 2/9 |
| `a10` | 3 | 68 | Has anyone actually measured whether splitting along a document's own sections beats cutting it into equal pieces? | studies analyzing SEC filings found that element-based chunking (sections, tables, lists) outperformed fixed-size chunking by preserving the semantic… | 0.17 | 2/12 |
| `a11` | 3 | 71 | Why can't patient files be split at any convenient point? | You cannot split chunks in ways that inadvertently expose protected health information or separate diagnoses from their qualifying context. | 0.20 | 1/5 |
| `a13` | 4 | 90 | What do you gain by pasting a short summary in front of each piece before turning it into vectors? | The embedding now captures both the fact and its source context. | 0.00 | 0/8 |
| `a14` | 4 | 92 | Who introduced the approach that embeds a whole document first and only applies the cuts at the end, and in what year? | Introduced by Jina AI in 2024 | 0.10 | 1/10 |
| `a15` | 4 | 95 | If I had a hundred thousand documents and let a model pick the break points, roughly what would that cost? | if you have 100,000 documents and agentic chunking requires 10 LLM calls per document at $0.001 each, that's 1 million LLM calls totaling $1,000 just… | 0.09 | 1/11 |
| `a17` | 5 | 101 | There is a score for how much of a retrieved passage actually got used. What range does it sit in, and what does the top of the range mean? | The metric ranges from 0 to 1. A value of 1 indicates the entire chunk affected the response. | 0.09 | 1/11 |
| `a18` | 5 | 109 | Do shorter vectors genuinely search faster, or does that even out? | lower-dimensional embeddings tend to yield larger speedups, as fewer vector components need to be compared. | 0.17 | 1/6 |
| `a19` | 5 | 113 | What was the input ceiling on older embedding models before long-context ones arrived? | Early embedding models limited inputs to 512 tokens | 0.30 | 3/10 |
| `a20` | 6 | 128 | With OpenAI's newer embedding models, can I ask for a smaller vector than the maximum, and which sizes are on offer? | models generate up to 3,072-dimensional embeddings, but you can request 256, 512, 768, or 1,536 dimensions at embedding time. | 0.20 | 2/10 |
| `a21` | 6 | 130 | Which shipping product does the book name as searching repositories with code-aware vectors? | GitHub Copilot Chat uses code embeddings to search through codebases semantically | 0.20 | 2/10 |
| `a22` | 6 | 131 | How many test sets does the big public leaderboard for embedding models cover? | models across 58 datasets covering retrieval, classification, clustering, and semantic similarity. | 0.22 | 2/9 |
| `a23` | 7 | 155 | For text search, which way of measuring closeness is usually chosen, and what makes it the default? | In practice, cosine similarity is the most common choice for text because embedding models are designed to place similar meanings in the same directi… | 0.11 | 1/9 |
| `a24` | 7 | 157 | Why does the graph-based index get expensive once you have a lot of vectors? | HNSW's speed comes from keeping the entire graph structure in RAM. The graph connections multiply memory requirements beyond just storing the vectors… | 0.25 | 2/8 |
| `a25` | 7 | 160 | If I round my vectors down to whole numbers instead of decimals, how much quality do I give up? | significantly lowers memory usage while preserving the structure of the vector space more faithfully than binary methods. | 0.11 | 1/9 |
| `a28` | 8 | 179 | Where do the genuinely useful results tend to end up when ranking is left to similarity alone? | relevant chunks are often hidden at positions 8, 15, or 23, mixed with less useful results. | 0.22 | 2/9 |
| `a27` | 8 | 181 | How much delay does a second-pass relevance step add for fifty results, and how much worse is it when a big general-purpose model does that job? | Cross-encoder models require 200-400ms to score 50 candidates, depending on model size and hardware. LLM-based rerankers take significantly longer, w… | 0.06 | 1/17 |
| `a29` | 8 | 185 | How many candidates is the common second-pass architecture meant to handle in production? | where you can absorb 200-400ms latency and need to rerank 20- 100 candidates. | 0.10 | 1/10 |
| `a31` | 9 | 199 | When a ranking is perfect, what score does the headline reranking measure reach? | A perfect ranking (all relevant documents at the top) yields an NDCG@10 of 1.0. | 0.29 | 2/7 |
| `a32` | 9 | 217 | What is the name for a system correctly saying it has nothing useful rather than guessing? | Negative rejection assesses whether the model will decline to answer a question when none of the contexts provide useful information, rather than giv… | 0.25 | 2/8 |
| `a30` | 9 | 223 | What two ways can a system end up showing someone information they should never have seen? | PII (Personally Identifiable Information) retrieval: The RAG system may retrieve documents containing PII (such as names, addresses, or medical recor… | 0.22 | 2/9 |
| `a34` | 10 | 229 | What is the trick of putting instructions at the very start of a message to steer the model called? | Prefix injection involves inserting specific instructions or phrases at the beginning of a prompt to manipulate the model's behavior. | 0.25 | 2/8 |
| `a35` | 10 | 239 | How many sample questions should real subject-matter people review before a benchmark goes live? | Run 30 test queries through domain experts. | 0.00 | 0/12 |
| `a33` | 10 | 247 | Which diversity technique picks results that are useful but unlike what has already been chosen? | MMR Maximal Marginal Relevance; diversity mechanism balancing relevance with dissimilarity to already-selected documents. | 0.25 | 2/8 |

## Near-miss — 6 entries, each beside the passage it nearly matches

### `u01` · page 197 · overlap 0.43 (3/7)

| | |
| --- | --- |
| **Question (unanswerable)** | What does Voyage Rerank 2.5 cost per thousand queries? |
| **Adjacent passage** | Voyage Rerank 2.5 supports a 32,000 token context length, double that of Rerank 2 and 8x that of Cohere Rerank v3.5. |
| **What the corpus says vs what is asked** | The corpus specifies Rerank 2.5's context length, deployment model and benchmark margins over competitors, but never states a price for it. |
| **Absence evidence** | 1/3 probes return zero rows; non-zero probes inspected below |
| **Overlap above 0.30 — why it stands** | Overlap 3/7 = 0.43, above the 0.30 line and left as-is deliberately. All three shared lexemes are the product name ('voyag', 'rerank', '2.5'); a question about one named model's price cannot avoid naming it. Padding the question to dilute the ratio would game the metric without reducing leakage. |
| &nbsp;&nbsp;probe 0 → 1 row(s) | One match, ord 45, a hybrid-search passage in chapter 2 that contains the word 'pricing'. It states no reranker price. |
| &nbsp;&nbsp;probe 1 → 2 row(s) | Two matches. ord 125 is an embedding-provider table listing 'Per-token pricing' for OpenAI and Cohere with no figures, and concerns embedding APIs rather than rerankers. ord 213 is a reranker selection guide whose only cost language is the qualitative 'Lower cost, self-hosted'. |

### `u02` · page 5 · overlap 0.17 (1/6)

| | |
| --- | --- |
| **Question (unanswerable)** | What year did Galileo, the author's employer, raise its Series A? |
| **Adjacent passage** | Pratik Bhavsar is an AI Engineer at Galileo, specializing in AI evaluation and agent engineering. |
| **What the corpus says vs what is asked** | The corpus establishes Galileo as the author's employer and references its products throughout, but says nothing about the company's funding history. |
| **Absence evidence** | 3/3 probes return zero rows |

### `u03` · page 99 · overlap 0.25 (2/8)

| | |
| --- | --- |
| **Question (unanswerable)** | Under what licence are Galileo's Luna evaluation models released, and are the weights downloadable? |
| **Adjacent passage** | Luna consists of purpose-built 440-million parameter models fine-tuned on real-world RAG data. |
| **What the corpus says vs what is asked** | The corpus gives Luna's parameter count, training data and purpose, and elsewhere discusses licensing explicitly for other models (mxbai-rerank-large-v2 is described as being under an open license, and vector-database licensing has its own evaluation category) - but it never states a licence for Luna or says whether its weights are obtainable. |
| **Absence evidence** | 3/3 probes return zero rows |

### `u04` · page 131 · overlap 0.00 (0/4)

| | |
| --- | --- |
| **Question (unanswerable)** | How often is the MTEB leaderboard refreshed? |
| **Adjacent passage** | models across 58 datasets covering retrieval, classification, clustering, and semantic similarity. |
| **What the corpus says vs what is asked** | The corpus states MTEB's dataset count and names its current leaders, but never mentions update cadence or how often rankings change. |
| **Absence evidence** | 3/3 probes return zero rows |

### `u05` · page 199 · overlap 0.25 (1/4)

| | |
| --- | --- |
| **Question (unanswerable)** | Which research group is behind BEIR? |
| **Adjacent passage** | The BEIR (Benchmarking IR) benchmark evaluates rerankers across diverse retrieval tasks. BEIR includes 18 datasets spanning different domains: question answering (Natural Questions, HotpotQA), fact verification (FEVER), citation prediction (SCIDOCS), and more. |
| **What the corpus says vs what is asked** | The corpus describes BEIR's dataset count, its domains and its primary metric, and cites BEIR scores in two separate chapters - but never names the group that built or maintains it. |
| **Absence evidence** | 3/3 probes return zero rows |

### `u06` · page 95 · overlap 0.22 (2/9)

| | |
| --- | --- |
| **Question (unanswerable)** | What is the per-document price of the technique that prepends a generated summary before embedding? |
| **Adjacent passage** | if you have 100,000 documents and agentic chunking requires 10 LLM calls per document at $0.001 each, that's 1 million LLM calls totaling $1,000 just for chunking. |
| **What the corpus says vs what is asked** | The corpus gives a per-call price of $0.001 for agentic chunking and works the arithmetic through to $1,000 for 100,000 documents. Context-enriched chunking is described four separate times as requiring an LLM call per chunk and 'increasing preprocessing cost', but no figure is ever attached to it - every mention is qualitative. |
| **Absence evidence** | 1/3 probes return zero rows; non-zero probes inspected below |
| &nbsp;&nbsp;probe 0 → 2 row(s) | Two matches, ord 101 and ord 105. Both price *agentic* chunking - $0.001 per LLM call and 'Most expensive; $1,000+ per 100K documents'. Neither attaches any figure to context-enriched chunking. |
| &nbsp;&nbsp;probe 1 → 1 row(s) | One match, ord 101, the same agentic-chunking arithmetic; the phrase 'per document' belongs to that sentence. |

## Authoring notes — entries rewritten during review

Five were flagged above 0.30 and resolved; two of those (`a16`, `a26`) were subsequently dropped
altogether when the decile rebalance thinned the over-filled deciles, so three remain in the set.

| id | why it was rewritten |
| --- | --- |
| `a27` | Rewritten: the first draft shared 'score', 'candidates', 'take' and 'model' (0.31). 'delay', 'results' and 'worse' replace them; 'fifty' does not stem onto '50'. |
| `a33` | Retargeted from p219 to the glossary at p247 to fill decile 10, which the first draft left empty. 'useful' avoids the passage's 'relevance'. |
| `a35` | Rewritten: the first draft shared 'test', 'queries', 'domain' and 'experts' with the sentence (0.36). 'sample questions' and 'subject-matter people' carry the same intent with no borrowed vocabulary. |
