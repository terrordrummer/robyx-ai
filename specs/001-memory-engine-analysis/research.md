# Research: Memory Engine Alternatives

**Date**: 2026-04-16
**Feature**: Memory Engine Evolution

## Current System Baseline

The current `memory.py` (226 lines) uses pure file I/O with a two-tier markdown system:
`active.md` (capped at 5000 words, loaded into LLM context) and `archive/YYYY-QN.md`
(quarterly, append-only). No indexing, no search beyond file globbing. Archive retrieval
requires the agent to manually glob and read files. This works for small scale but will
not survive 10,000+ entries.

## Options Evaluated

### Option 1: SQLite (stdlib sqlite3) with FTS5

- **Dependency weight**: Zero — `sqlite3` ships with Python stdlib. FTS5 is included
  in Python 3.10+ distributions.
- **Schema design**: Three tables: `entries` (id, content, topic, tags, created_at,
  archived_at), `fts_entries` (FTS5 virtual table), `active_snapshot` (single-row
  for current active memory blob).
- **Query quality**: FTS5 provides BM25 ranking, prefix queries, phrase matching,
  boolean operators. Misses semantic similarity ("shipping" vs "deployment") but
  this gap matters less for structured agent memory with consistent terminology.
- **Crash safety**: SQLite WAL mode provides ACID transactions, crash recovery,
  concurrent reads. Gold standard for embedded crash safety.
- **Performance**: FTS5 queries on 10K entries return in <10ms. Active memory load
  is sub-millisecond (single SELECT).
- **Cross-platform**: Perfect. Stdlib, no native extensions.
- **Migration complexity**: Low. Parse markdown with regex, bulk INSERT.

### Option 2: SQLite + sqlite-vec

- **Dependency weight**: sqlite-vec wheel ~165KB, pure C, no transitive deps. But
  embedding model needed: local (sentence-transformers: ~500MB+ with PyTorch) or
  API calls (zero local weight, adds latency and cost).
- **Query quality**: Semantic retrieval — "shipping" finds "deployment" entries.
  Marginal improvement over FTS5 for structured memory with consistent terminology.
  Significant for cross-topic discovery.
- **Cross-platform**: Prebuilt wheels for macOS ARM/x86, Linux x86/ARM, Windows.
  Single-maintainer project — maintenance concern (6-month gap, resolved as of
  v0.1.7, March 2026).
- **Crash safety**: Inherits SQLite ACID.
- **Performance**: Brute-force vector search on 10K entries (384-dim): ~50-100ms.

**Verdict**: Good upgrade path FROM Option 1. Do not start here.

### Option 3: ChromaDB (embedded mode)

- **Dependency weight**: ~220MB installed (hnswlib, numpy, pydantic, opentelemetry, etc.).
- **Persistence reliability**: SQLite internally for metadata, HNSW for vectors.
  Known issues: disk quota handling fragile, Docker volume corruption on crash
  reported, API churn (v0.3 → v0.4 → v1.x breaking changes).
- **Cross-platform**: Works but hnswlib native extension occasionally causes build issues.

**Verdict**: Overkill. 220MB dependencies for what sqlite-vec does with 165KB.
Designed for RAG with millions of documents, not 10K structured entries.

### Option 4: LanceDB

- **Dependency weight**: PyArrow (~150MB) + lance native extensions. Total ~200-300MB.
  Marked "3 - Alpha" on PyPI.
- **Maturity**: API has changed between versions. Production deployment story less mature.
- **Persistence model**: Apache Arrow/Lance columnar format — over-engineered for
  agent memory.

**Verdict**: Wrong tool. Designed for multimodal AI data lakes.

### Option 5: Hybrid SQLite FTS5 + Optional Vector Layer *(RECOMMENDED)*

- FTS5 as primary storage and search (zero deps, Option 1).
- sqlite-vec as optional enhancement, gated behind config flag or auto-detection.
- FTS5 handles 90% of retrieval needs; vectors add semantic search for remaining 10%.
- Code cost: ~30 extra lines for vector path. Dependency: 165KB + embedding model.

**Verdict**: Right architecture. Ship with FTS5, add vectors later.

### Option 6: Keep Markdown + Sidecar SQLite Index

- Maintain `.md` files as source of truth, build sidecar SQLite FTS5 index.
- **Critical weakness**: Dual-write consistency. Every write must update both atomically.
  Process crash between the two writes causes divergence.
- Index is rebuildable from markdown (derived data), but adds rebuild cost.
- Human readability benefit diminishes at 10K+ entries.

**Verdict**: Consistency tax not worth it at scale. If readability matters, export
from SQLite as read-only view.

## Summary Comparison

| Criterion              | SQLite+FTS5 | +sqlite-vec | ChromaDB | LanceDB | Hybrid  | MD+Index |
|------------------------|-------------|-------------|----------|---------|---------|----------|
| Dep weight             | 0           | 165KB+emb   | ~220MB   | ~250MB  | 0/165KB | 0        |
| Crash safety           | Excellent   | Excellent   | Good     | Good    | Excellent | Fair   |
| Query quality (keyword)| Excellent   | Excellent   | N/A      | N/A     | Excellent | Excellent |
| Query quality (semantic)| None       | Good        | Good     | Excellent | Optional | None  |
| Cross-platform         | Perfect     | Good        | Fair     | Fair    | Perfect/Good | Perfect |
| Migration effort       | Low         | Medium      | High     | High    | Low     | Medium   |
| Maintenance burden     | Minimal     | Low         | Medium   | Medium  | Minimal | Medium   |
| Maturity               | Stdlib      | Single-maint | Stable  | Alpha   | Stdlib  | N/A      |

## Decision

**Hybrid SQLite FTS5 + Optional Vector Layer (Option 5)**

**Rationale**: The current system's bottleneck is the complete absence of any search
capability. FTS5 is a 100x improvement over "glob files and read them." Adding semantic
search on top of that is a marginal gain that can wait until real usage data shows FTS5
misses. Zero dependencies for the core path. Clean upgrade path to vectors if needed.

**Alternatives rejected**:
- ChromaDB / LanceDB: dependency weight (200-300MB) unacceptable for an agent memory
  subsystem in a lightweight orchestrator
- Markdown + sidecar index: dual-write consistency makes it fragile at scale
- Starting with vectors: premature complexity; FTS5 solves the actual problem today
