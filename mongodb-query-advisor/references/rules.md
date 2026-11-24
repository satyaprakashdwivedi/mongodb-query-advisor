# Rules the analyzer applies

| Id | Detects | From | Fix |
|---|---|---|---|
| `collscan` | COLLSCAN reading 1,000+ documents | explain | ESR index on the filter and sort |
| `in-memory-sort` | blocking SORT stage (and spills to disk) | explain | sort fields after equality fields in the index |
| `poor-selectivity` | 10x+ more keys/documents examined than returned; names the cause: index scanned end to end only for its sort order, or fields filtered after FETCH | explain | ESR index |
| `esr-order` | an existing index puts a range field before the sort field, so it cannot serve the sort | explain + getIndexes | reorder to equality, sort, range; hide then drop the old one |
| `lookup-unindexed` | `$lookup` running as NestedLoopJoin / collection scans of the foreign collection | explain (SBE `EQ_LOOKUP` or classic `$lookup`) | index the foreignField |
| `regex-unanchored` | unanchored or case-insensitive `$regex` reading every index key | explain | anchor (`^prefix`), normalise the field, or Atlas Search |
| `deep-skip` | `skip()` of 10,000+ | explain | range (keyset) pagination |
| `slow-query` | slow query shapes (grouped by planCacheShapeHash) with COLLSCAN or 10x+ examined per returned | slow query log, profiler | ESR index for the shape |
| `vector-post-filter` | `$match` right after `$vectorSearch` (it can return fewer than `limit`) | pipeline or explain | declare filter fields in the vector index, use `$vectorSearch.filter` |
| `vector-filter-undeclared` | `$vectorSearch.filter` on a field the index does not declare as `filter` | pipeline + search index | add `{type: "filter", path}` |
| `vector-low-candidates` | numCandidates below 10x limit (recall drops) | pipeline | 10-20x limit, tuned against `exact: true` |
| `vector-high-candidates` | numCandidates above 10,000 | pipeline | stay at or under 10,000 |
| `vector-not-first` | `$vectorSearch` is not the first stage | pipeline | move it first |
| `vector-returns-embeddings` | full vectors returned with each result | pipeline | `$project: {embedding: 0}` |
| `redundant-index` | an index that is a prefix of another (not unique, partial, sparse, TTL or collated) | getIndexes | hideIndex, observe, dropIndex |
| `unused-index` | 0 operations in `$indexStats` | $indexStats | hideIndex first; check all members and a full business cycle |
| `many-indexes` | more than 12 indexes on one collection | getIndexes | consolidate into ESR compound indexes |

## The ESR rule

For a compound index, put fields in this order:

1. **Equality** fields (`{status: "paid"}`, `$in` on a small list): narrow the scan to one contiguous range.
2. **Sort** fields, in the query's sort direction: the index then returns documents already sorted, so no
   in-memory SORT stage.
3. **Range** fields (`$gt`, `$lt`, `$ne`, anchored regex): bound the scan last, because a range before the sort
   breaks the index's ability to return sorted order.

The analyzer never recommends an index whose key pattern is already the leading part of an existing index;
in that case the fix is in the query, and the finding says which index already exists.
