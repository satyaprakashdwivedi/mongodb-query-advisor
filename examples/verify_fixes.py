#!/usr/bin/env python3
"""Apply the advisor's recommendations to the example database and measure each query before and after.

    python3 examples/verify_fixes.py mongodb://localhost:27018/?directConnection=true

Prints a table (documents and keys examined per returned, time) that the README quotes.
"""
import json
import subprocess
import sys
from pathlib import Path

from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
QUERIES = {
    "collection scan on amount": lambda db: db.orders.find({"amount": {"$gte": 499.5}}),
    "status + region, sorted by date": lambda db: db.orders.find({"status": "paid", "region": "IN"})
    .sort("created_at", -1).limit(20),
    "status + amount range, sorted by date": lambda db: db.orders.find({"status": "paid", "amount": {"$gt": 450}})
    .sort("created_at", 1).limit(50),
}


def measure(db):
    rows = {}
    for name, query in QUERIES.items():
        stats = query(db).explain()["executionStats"]
        rows[name] = (stats["nReturned"], stats["totalKeysExamined"], stats["totalDocsExamined"],
                      stats["executionTimeMillis"])
    pipeline = [{"$match": {"region": "IN", "status": "paid", "amount": {"$gt": 495}}},
                {"$lookup": {"from": "customers", "localField": "customer_id", "foreignField": "customer_id",
                             "as": "customer"}}]
    stats = db.command("explain", {"aggregate": "orders", "pipeline": pipeline, "cursor": {}},
                       verbosity="executionStats")["executionStats"]
    rows["$lookup orders -> customers"] = (stats["nReturned"], stats["totalKeysExamined"],
                                           stats["totalDocsExamined"], stats["executionTimeMillis"])
    return rows


def main(uri):
    db = MongoClient(uri).shop
    before = measure(db)
    report = json.loads(subprocess.run(
        [sys.executable, str(ROOT / "mongodb-query-advisor/scripts/analyze_mongo.py"), str(ROOT / "examples/shop"),
         "--json"], capture_output=True, text=True, check=True).stdout)
    created = []
    for index in report["recommended_indexes"]:
        db[index["collection"]].create_index([tuple(k) for k in index["keys"]])
        created.append(index["command"])
    after = measure(db)
    print("Applied:\n  " + "\n  ".join(created) + "\n")
    print("| Query | Returned | Examined before (keys / docs, ms) | Examined after (keys / docs, ms) |")
    print("|---|---|---|---|")
    for name in before:
        b, a = before[name], after[name]
        print(f"| {name} | {a[0]:,} | {b[1]:,} / {b[2]:,}, {b[3]} ms | {a[1]:,} / {a[2]:,}, {a[3]} ms |")
    for index in report["recommended_indexes"]:  # leave the example database as it was
        db[index["collection"]].drop_index([tuple(k) for k in index["keys"]])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "mongodb://localhost:27018/?directConnection=true")
