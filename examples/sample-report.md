# MongoDB query advisor report

Inputs: 8 explain plans, 22 slow operations, 7 indexes, 1 pipelines.  
Findings: **4 high**, 7 medium, 2 low.

## Create these indexes

| Index | Fixes |
|---|---|
| `db.customers.createIndex({ customer_id: 1 })` | lookup-unindexed |
| `db.orders.createIndex({ amount: 1 })` | collscan, slow-query |
| `db.orders.createIndex({ status: 1, created_at: 1, amount: 1 })` | esr-order, poor-selectivity |
| `db.orders.createIndex({ status: 1, region: 1, created_at: -1 })` | poor-selectivity |

Build them in a quiet window; on a replica set the build runs on every member. Re-run explain("executionStats") afterwards and check that examined is close to returned.

## Findings

### [HIGH] $lookup scans customers once per input document

- **Query:** `shop.orders: filter {"region": "IN", "status": "paid", "amount": {"$gt": 495}}`
- **Evidence:** Join strategy NestedLoopJoin: 124 collection scans of customers, 2,480,520 documents examined to return 124.
- **Fix:** Index the foreign field so each lookup is an index seek (the plan becomes IndexedLoopJoin).
- **Index:** `db.customers.createIndex({ customer_id: 1 })`
- *Source: explain-lookup-unindexed.json*

### [HIGH] Collection scan

- **Query:** `shop.orders: filter {"amount": {"$gte": 499.5}}`
- **Evidence:** COLLSCAN: returned 213, examined 0 index keys and 200,000 documents in 25 ms.
- **Fix:** Create an index that matches the filter (equality, sort, range order).
- **Index:** `db.orders.createIndex({ amount: 1 })`
- *Source: explain-collscan.json*

### [HIGH] Slow 6x, 75 ms in total

- **Query:** `shop.orders: filter {"amount": {"$gte": 499.5}}`
- **Evidence:** Plan COLLSCAN; median 12.5 ms; 939.0 examined per returned document.
- **Fix:** Index the query shape (equality, sort, range).
- **Index:** `db.orders.createIndex({ amount: 1 })`
- *Source: slow query log / profiler*

### [HIGH] Filtering on category after the vector search

- **Query:** `products: $vectorSearch on embedding (index product_vectors)`
- **Evidence:** $match runs after $vectorSearch has already picked the top 10, so it can return fewer than 10 results, or none.
- **Fix:** Declare category as filter fields in the vector index, then move the condition into $vectorSearch.filter so it is applied during the search.
- *Source: explain-vector-search.json*

### [MEDIUM] skip(50,000) pagination

- **Query:** `shop.orders: filter {}, sort {"created_at": 1}, skip 50000`
- **Evidence:** returned 20, examined 50,020 index keys and 20 documents in 8 ms: every skipped entry is still read.
- **Fix:** Page by range instead: remember the last sort key and query {created_at: {$gt: last}} with the same sort and limit.
- *Source: explain-deep-skip.json*

### [MEDIUM] Index status_1_amount_1_created_at_1 has a range field before the sort field

- **Query:** `shop.orders: filter {"status": "paid", "amount": {"$gt": 450}}, sort {"created_at": 1}`
- **Evidence:** amount (range) comes before created_at (sort), so this index cannot return rows in sort order and the planner either sorts in memory or picks another index.
- **Fix:** Reorder to equality, sort, range: replace it with the index below (hide the old one first, then drop it).
- **Index:** `db.orders.createIndex({ status: 1, created_at: 1, amount: 1 })`
- *Source: explain-esr-violation.json*

### [MEDIUM] Reads 35x more than it returns

- **Query:** `shop.orders: filter {"status": "paid", "amount": {"$gt": 450}}, sort {"created_at": 1}`
- **Evidence:** returned 50, examined 1,753 index keys and 1,753 documents in 1 ms: index created_at_1_status_1 is scanned end to end (it only provides the sort order, not the filter); amount, status are checked after fetching each document instead of in the index.
- **Fix:** Use an index whose leading fields are the query's equality fields, then the sort, then ranges.
- **Index:** `db.orders.createIndex({ status: 1, created_at: 1, amount: 1 })`
- *Source: explain-esr-violation.json*

### [MEDIUM] Reads 17x more than it returns

- **Query:** `shop.orders: filter {"status": "paid", "region": "IN"}, sort {"created_at": -1}`
- **Evidence:** returned 20, examined 340 index keys and 340 documents in 0 ms: index created_at_1_status_1 is scanned end to end (it only provides the sort order, not the filter); region, status are checked after fetching each document instead of in the index.
- **Fix:** Use an index whose leading fields are the query's equality fields, then the sort, then ranges.
- **Index:** `db.orders.createIndex({ status: 1, region: 1, created_at: -1 })`
- *Source: explain-wrong-index-for-sort.json*

### [MEDIUM] Unanchored regex on sku

- **Query:** `shop.orders: filter {"sku": {"$regex": "42"}}`
- **Evidence:** returned 3,955, examined 200,000 index keys and 3,955 documents in 40 ms: the regex cannot use index bounds, so every key is read.
- **Fix:** Anchor the pattern (^prefix) and keep it case-sensitive, store a normalised copy of the field, or use Atlas Search for substring and fuzzy matching.
- *Source: explain-unanchored-regex.json*

### [MEDIUM] Slow 3x, 118 ms in total

- **Query:** `shop.orders: filter {"sku": {"$regex": "42"}}`
- **Evidence:** Plan IXSCAN { sku: 1 }; median 39 ms; 49.1 examined per returned document.
- **Fix:** Unanchored or case-insensitive regex: anchor it (^prefix) or use Atlas Search.
- *Source: slow query log / profiler*

### [MEDIUM] numCandidates is only 1x limit

- **Query:** `products: $vectorSearch on embedding (index product_vectors)`
- **Evidence:** numCandidates 10, limit 10. The approximate (HNSW) search only explores 10 candidates, so recall drops.
- **Fix:** Start at 10-20x limit (numCandidates 150) and tune against an exact-search baseline (exact: true) on a sample of queries.
- *Source: explain-vector-search.json*

### [LOW] Index customer_id_1 is a prefix of customer_id_1_created_at_-1

- **Evidence:** { customer_id: 1 } is the leading part of { customer_id: 1, created_at: -1 }, which can serve every query it serves.
- **Fix:** db.orders.hideIndex('customer_id_1'), watch for a week, then dropIndex. Every index costs RAM and slows writes.
- *Source: getIndexes*

### [LOW] 4 unused indexes: status_1_amount_1_created_at_1, customer_id_1, customer_id_1_created_at_-1, region_1_sku_1

- **Evidence:** 0 operations since 2026-11-24 19:34:39.070000. $indexStats counts from the last restart and per node, so check every replica set member and a full business cycle.
- **Fix:** Hide them (hideIndex) before dropping, so you can unhide instantly if a query regresses.
- *Source: $indexStats*

## Slowest query shapes (log + profiler)

| Query | Runs | Total ms | Median ms | Plan | Examined / returned |
|---|---|---|---|---|---|
| `shop.orders: filter {"sku": {"$regex": "42"}}` | 3 | 118 | 39 | IXSCAN { sku: 1 } | 49.1 |
| `shop.orders: filter {"amount": {"$gte": 499.5}}` | 6 | 75 | 12.5 | COLLSCAN | 939.0 |
| `shop.customers: filter {}` | 1 | 42 | 42 | ? | - |
| `shop.products: filter {}` | 1 | 5 | 5 | ? | 0.0 |

## Looks healthy

- `shop.orders: filter {"customer_id": "c42"}, sort {"created_at": -1}: returned 9, examined 9 index keys and 0 documents in 0 ms (covered by the index, no documents fetched)`

