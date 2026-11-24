---
name: mongodb-query-advisor
description: Analyse MongoDB query performance and recommend indexes. Use when the user shares or points to explain() output, mongod slow query logs ("Slow query", id 51803), system.profile documents, getIndexes() / $indexStats output, or an Atlas Vector Search ($vectorSearch) pipeline and index definition. Also use when they ask why a MongoDB query is slow, which index to create, whether an index is redundant or unused, how to fix COLLSCAN, in-memory sorts, slow $lookup, deep skip pagination, unanchored regex, or low vector search recall.
---

# MongoDB query advisor

Measure first, then recommend. The script reads real MongoDB output and finds the problems; you explain them and
tailor the fix to the user's workload. Never invent numbers: quote what the script measured.

## 1. Collect the inputs

Ask for whichever of these the user has (more inputs, better advice):

| Input | How to get it |
|---|---|
| Explain plan | `db.orders.find({...}).sort({...}).explain("executionStats")` or `db.orders.explain("executionStats").aggregate([...])` |
| Slow query log | `grep '"Slow query"' mongod.log` (JSON logs, MongoDB 4.4+); Atlas: Download Logs |
| Profiler | `db.setProfilingLevel(1, {slowms: 100})`, then `mongoexport -c system.profile` |
| Indexes | `db.orders.getIndexes()` saved as `indexes-orders.json` |
| Index usage | `db.orders.aggregate([{$indexStats: {}}])` saved as `index-stats-orders.json` |
| Vector search | the pipeline, plus `db.products.getSearchIndexes()` saved as `search-indexes-products.json` |

Save JSON exactly as the shell prints it (use `EJSON.stringify` or `JSON.stringify` in mongosh).

## 2. Run the analyzer

```bash
python3 scripts/analyze_mongo.py <files or a directory>            # markdown report
python3 scripts/analyze_mongo.py <files> --json                    # for further processing
```

Standard library only (Python 3.9+). Input types are detected from content, so a whole folder can be passed.

## 3. Explain the result

Lead with the recommended indexes table, then the high-severity findings. For each:

- **What is slow and the evidence** (examined vs returned, plan stage, ms). The key ratio is documents or keys
  examined per document returned; healthy queries are close to 1.
- **Why**: name the rule (see `references/rules.md`): collection scan, in-memory sort, index used only for the
  sort, range field before the sort field (ESR), unindexed `$lookup` (NestedLoopJoin), deep skip, unanchored
  regex, vector post-filter, low numCandidates.
- **The fix**, as a ready-to-run `createIndex` command, or a query change when the index already exists.

Then the housekeeping: redundant and unused indexes. Always recommend `hideIndex` before `dropIndex`, and remind
the user that `$indexStats` counts per node since the last restart.

## 4. Before they build an index

- Check write impact: every index slows inserts and updates and takes RAM. Prefer one compound index that
  serves several queries (ESR order) over several single-field ones.
- On large collections, build in a quiet window; it runs on every replica set member.
- Verify: re-run `explain("executionStats")`; examined should now be close to returned.

## Limits

- Explain shows one execution; confirm with the slow query log or profiler that the shape matters in production.
- Recommendations assume the filter values are selective. Low-cardinality fields (status with 3 values) still
  belong first for equality, but do not make a good single-field index.
- Atlas Search ($search) relevance tuning and sharding (shard keys, scatter-gather) are out of scope; say so.
