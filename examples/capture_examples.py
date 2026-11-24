#!/usr/bin/env python3
"""Capture the example inputs from a real MongoDB (Atlas Local in Docker), so the analyzer is tested against
genuine server output rather than hand-written JSON.

    docker run -d --name mqa-mongo -p 127.0.0.1:27018:27017 mongodb/mongodb-atlas-local:8.0
    pip install "pymongo>=4.10,<5"
    python3 examples/capture_examples.py mongodb://localhost:27018/?directConnection=true
    docker logs mqa-mongo 2>&1 | grep '"Slow query"' | grep '"shop.' > examples/shop/mongod.log

Writes examples/shop/: explain plans, getIndexes, $indexStats, profiler entries, a vector search pipeline with
its index definition, and (from the docker logs) slow query log lines.
"""
import datetime
import json
import random
import sys
import time
from pathlib import Path

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.operations import SearchIndexModel

OUT = Path(__file__).resolve().parent / "shop"


def save(name: str, document) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if isinstance(document, dict):
        document = {k: v for k, v in document.items() if k not in ("serverInfo", "$clusterTime", "operationTime")}
    (OUT / name).write_text(json.dumps(document, indent=1, default=str) + "\n")
    print("wrote", OUT / name)


def load_data(db) -> None:
    random.seed(1)
    db.orders.drop()
    db.customers.drop()
    db.products.drop()
    statuses = ["new", "paid", "shipped", "cancelled"]
    start = datetime.datetime(2026, 1, 1)
    db.orders.insert_many([
        {"customer_id": f"c{random.randint(1, 20000)}", "status": random.choice(statuses),
         "region": random.choice(["IN", "US", "EU", "UK"]), "amount": round(random.random() * 500, 2),
         "created_at": start + datetime.timedelta(minutes=i), "sku": f"sku-{random.randint(1, 3000)}"}
        for i in range(200_000)])
    db.customers.insert_many([{"customer_id": f"c{i}", "name": f"Customer {i}",
                               "tier": random.choice(["gold", "silver"])} for i in range(1, 20_001)])
    db.products.insert_many([{"sku": f"sku-{i}", "category": random.choice(["shoes", "bags", "watches"]),
                              "embedding": [random.uniform(-1, 1) for _ in range(8)]} for i in range(1, 3001)])

    # Indexes a team might plausibly have: one built for another query, one redundant, one never used.
    db.orders.create_index([("created_at", ASCENDING), ("status", ASCENDING)])
    db.orders.create_index([("customer_id", ASCENDING)])
    db.orders.create_index([("customer_id", ASCENDING), ("created_at", DESCENDING)])
    db.orders.create_index([("status", ASCENDING), ("amount", ASCENDING), ("created_at", ASCENDING)])
    db.orders.create_index([("sku", ASCENDING)])
    db.orders.create_index([("region", ASCENDING), ("sku", ASCENDING)])  # never used by the workload below


def capture_queries(db) -> None:
    orders = db.orders
    save("explain-wrong-index-for-sort.json",
         orders.find({"status": "paid", "region": "IN"}).sort("created_at", -1).limit(20).explain())
    save("explain-collscan.json", orders.find({"amount": {"$gte": 499.5}}).explain())
    save("explain-esr-violation.json",
         orders.find({"status": "paid", "amount": {"$gt": 450}}).sort("created_at", 1).limit(50).explain())
    save("explain-unanchored-regex.json", orders.find({"sku": {"$regex": "42"}}).explain())
    save("explain-deep-skip.json", orders.find({}).sort("created_at", 1).skip(50_000).limit(20).explain())
    save("explain-good-covered.json",
         orders.find({"customer_id": "c42"}, {"_id": 0, "customer_id": 1, "created_at": 1})
         .sort("created_at", -1).explain())
    save("explain-lookup-unindexed.json", db.command("explain", {
        "aggregate": "orders", "cursor": {},
        "pipeline": [{"$match": {"region": "IN", "status": "paid", "amount": {"$gt": 495}}},
                     {"$lookup": {"from": "customers", "localField": "customer_id",
                                  "foreignField": "customer_id", "as": "customer"}}]},
        verbosity="executionStats"))


def capture_vector_search(db) -> None:
    definition = {"fields": [{"type": "vector", "path": "embedding", "numDimensions": 8, "similarity": "cosine"}]}
    db.products.create_search_index(SearchIndexModel(definition=definition, name="product_vectors",
                                                     type="vectorSearch"))
    for _ in range(120):
        indexes = list(db.products.list_search_indexes("product_vectors"))
        if indexes and indexes[0].get("queryable"):
            break
        time.sleep(2)
    save("search-indexes-products.json", list(db.products.list_search_indexes()))
    query_vector = [0.1, -0.2, 0.3, 0.05, -0.4, 0.2, 0.0, 0.1]
    pipeline = [{"$vectorSearch": {"index": "product_vectors", "path": "embedding", "queryVector": query_vector,
                                   "numCandidates": 10, "limit": 10}},
                {"$match": {"category": "shoes"}},
                {"$project": {"sku": 1, "category": 1, "score": {"$meta": "vectorSearchScore"}}}]
    save("pipeline-vector-search.json", {"collection": "products", "pipeline": pipeline})
    save("explain-vector-search.json", db.command("explain", {"aggregate": "products", "pipeline": pipeline,
                                                              "cursor": {}}, verbosity="executionStats"))


def main(uri: str) -> None:
    client = MongoClient(uri)
    db = client.shop
    load_data(db)
    db.command("profile", 1, slowms=5)            # profiler + slow query log for anything over 5 ms
    for _ in range(3):                            # a small workload, so the log and $indexStats have numbers
        capture_queries(db)
        list(db.orders.find({"amount": {"$gte": 499.5}}))
        list(db.orders.find({"status": "paid", "region": "IN"}).sort("created_at", -1).limit(20))
        list(db.orders.find({"sku": {"$regex": "42"}}))
    capture_vector_search(db)
    save("indexes-orders.json", list(db.orders.list_indexes()))
    save("index-stats-orders.json", list(db.orders.aggregate([{"$indexStats": {}}])))
    save("profile.json", list(db.system.profile.find({"ns": {"$regex": r"^shop\."}}).sort("ts", 1).limit(200)))
    db.command("profile", 0, slowms=100)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "mongodb://localhost:27018/?directConnection=true")
