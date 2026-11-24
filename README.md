# MongoDB Query Advisor: a Claude skill

[![test](https://github.com/satyaprakashdwivedi/mongodb-query-advisor/actions/workflows/test.yml/badge.svg)](https://github.com/satyaprakashdwivedi/mongodb-query-advisor/actions/workflows/test.yml)

A [Claude skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview) that reads what MongoDB
reports about your queries and tells you what is slow, why, and **which indexes to create, hide or drop**. It covers
classic queries, aggregations with `$lookup`, and **Atlas Vector Search** pipelines.

It is the MongoDB counterpart of my [Spark optimizer skill](https://github.com/satyaprakashdwivedi/spark-optimizer-skill):
a deterministic analyzer does the measuring, and Claude explains the result and tailors the fix.

## What it reads

| Input | How to get it |
|---|---|
| Explain plans: classic and slot-based (SBE), find and aggregate | `.explain("executionStats")` |
| Slow query log lines (`"msg": "Slow query"`) | `mongod.log`, Atlas log download |
| Profiler documents | `db.system.profile` |
| Index list and index usage | `getIndexes()`, `$indexStats` |
| Vector search pipelines and index definitions | `$vectorSearch` pipeline, `getSearchIndexes()` |

Inputs are recognised by their content, so you can point it at a whole folder. The analyzer is a single file,
[`scripts/analyze_mongo.py`](mongodb-query-advisor/scripts/analyze_mongo.py), and needs only the Python standard
library (3.9+).

## What it finds

- **Collection scans** and **in-memory sorts**, including sorts that spill to disk.
- **Indexes that only serve the sort.** The planner walks an index end to end to get the order and filters
  every document afterwards.
- **ESR order violations.** In an existing index, a range field before the sort field means that index can't
  return results in sort order.
- **`$lookup` without an index on the foreign field.** It runs as a NestedLoopJoin, scanning the foreign
  collection once per input document.
- **Deep `skip()` pagination** and **unanchored or case-insensitive regexes**.
- **Slow query shapes** from logs or the profiler. They are grouped by plan-cache shape and ranked by total time.
  Cursor batches (`getMore`) count towards the original query, and explain commands are excluded.
- **Vector search problems:**
  - a `$match` that filters after the search, which can return fewer results than `limit` or none;
  - filter fields missing from the vector index definition;
  - `numCandidates` too low for good recall, or above the 10,000 cap;
  - `$vectorSearch` not being the first stage;
  - full embeddings returned with every result.
- **Index housekeeping:**
  - redundant indexes (one is the leading part of another);
  - unused indexes, by `$indexStats`;
  - collections with too many indexes.

  It always recommends `hideIndex` before `dropIndex`.

Recommended indexes follow the **ESR rule** (equality fields, then sort fields, then range fields). They are merged
across findings into one `createIndex` per key pattern. The analyzer never recommends an index that already exists:
in that case the fix is in the query, and the finding says so. The full rule list is in
[`references/rules.md`](mongodb-query-advisor/references/rules.md).

## Proof on a real database

Every example in [`examples/shop`](examples/shop) was **captured from a real MongoDB 8.0** (Atlas Local in Docker)
by [`capture_examples.py`](examples/capture_examples.py). The dataset has 200,000 orders, 20,000 customers and 3,000
products with embeddings.

[`verify_fixes.py`](examples/verify_fixes.py) applies the advisor's recommendations and measures every query again:

| Query | Returned | Before: keys / docs examined, time | After |
|---|---|---|---|
| `$lookup` orders → customers | 124 | 520 / **2,480,520**, 262 ms | 644 / 644, **2 ms** |
| Collection scan on `amount` | 213 | 0 / **200,000**, 43 ms | 213 / 213, 0 ms |
| `status` + amount range, sorted by date | 50 | 1,753 / 1,753 | 453 / 50 |
| `status` + `region`, sorted by date | 20 | 340 / 340 | 20 / 20 |

The full report on these inputs is in [`examples/sample-report.md`](examples/sample-report.md).

CI does this on every push. A **live job** starts a fresh MongoDB, captures every example again, runs the tests
against the new output (so changes in MongoDB's explain format are caught), then applies the recommendations and
publishes the before/after table in the job summary.

## Install

**Claude Code**: a personal skill, available in every project:
```bash
git clone https://github.com/satyaprakashdwivedi/mongodb-query-advisor.git
cp -r mongodb-query-advisor/mongodb-query-advisor ~/.claude/skills/
```
To make it available in one repo only, copy it into `<repo>/.claude/skills/` instead.

**Claude.ai / Claude Desktop**: zip the `mongodb-query-advisor` folder and upload it under
*Settings → Capabilities → Skills*.

**Without Claude**:
```bash
python3 mongodb-query-advisor/scripts/analyze_mongo.py ./my-mongo-dumps          # markdown report
python3 mongodb-query-advisor/scripts/analyze_mongo.py ./my-mongo-dumps --json   # for pipelines and dashboards
```

## Use

In Claude:
> Our orders page got slow. Here are the slow query log and `getIndexes()` for orders: `~/Downloads/mongo/`.
> What should we change?

Claude runs the analyzer, leads with the indexes to create, explains each high-severity finding with the measured
numbers, and flags redundant or unused indexes to hide before dropping.

## Try it locally

```bash
docker run -d --name mqa-mongo -p 127.0.0.1:27018:27017 mongodb/mongodb-atlas-local:8.0
pip install "pymongo>=4.10,<5"
python3 examples/capture_examples.py           # loads data, runs the workload, saves the plans and logs
python3 mongodb-query-advisor/scripts/analyze_mongo.py examples/shop
python3 examples/verify_fixes.py               # apply the recommendations, measure before/after
```

## Limits

- One explain shows one execution. Confirm with the slow query log or profiler that the shape matters in production.
- Sharding (shard keys, scatter-gather) and Atlas Search (`$search`) relevance tuning are out of scope.

MIT licensed.
