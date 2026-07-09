# Retrieval pipeline lessons

The retrieval pipeline turned out to be the single highest-leverage subsystem in
the whole brain, and most of what we learned was counterintuitive at the start of
the project. This document distils the decisions that survived contact with real
workloads so that a future maintainer does not have to relearn them the hard way.
Treat every claim here as earned rather than assumed, because each one cost us at
least one bad week before it became obvious in hindsight and made it into writing.

## Hybrid search beats pure vector search

Our first implementation used dense vectors only. It was fast and it demoed well,
but it failed badly on exact-match queries: a developer searching for a specific
error code or a function name would get semantically adjacent but useless results.
Adding a lexical stage over the same chunks, then fusing the two ranked lists with
reciprocal rank fusion, recovered the exact-match cases without hurting the
semantic ones. The fusion constant barely mattered; what mattered was having both
signals available at all. We now treat lexical and dense retrieval as equal first
class citizens rather than treating the keyword index as a legacy fallback that
nobody maintains, because the day we neglected it was the day recall quietly fell.

### Reranking earns its latency

Cross-encoder reranking is expensive — it roughly doubled our tail retrieval time —
but it moved the needle on answer quality more than any embedding model swap we
tried across a full quarter of experiments. The trick was to rerank only the top
forty fused candidates rather than the full recall set, which kept the added
latency bounded and predictable under load even when the collection grew large and
the nightly ingestion backfilled tens of thousands of archived project records.

## Chunk boundaries are a retrieval decision, not a storage detail

We initially chunked everything with a fixed token window because it was simple and
required no per-format code at all. Retrieval quality improved measurably once
chunk boundaries respected the natural structure of the content: headings for
prose, whole functions for code, speaker turns for transcripts, and rows for
tables. A chunk that splits a function signature in half is nearly useless to both
the embedder and a human reader, and no amount of reranking recovers the lost
context once the boundary has already destroyed it during ingestion.

### Contextual headers keep chunks interpretable

Every chunk carries a short header naming its scope, kind, source record, and
position within that record. Isolated chunks that surface in retrieval are far
easier for the reranker and for a debugging engineer to interpret when they
announce where they came from, and the cost is only a handful of tokens per chunk.
Without the header a reranked snippet is a floating paragraph with no provenance,
and provenance is exactly what a reviewer needs to decide whether to trust it.

## Ingestion lag is a product metric, not a background chore

The gap between a Memory Event committing and its chunk becoming searchable is the
metric users actually feel, even though nobody ever asks for it by name. We export
it as a histogram and alert on the tail, because a doubling of the collection can
silently push freshly written knowledge minutes behind the queries that need it.
Making the lag visible turned a vague sense of staleness into a number we defend.
