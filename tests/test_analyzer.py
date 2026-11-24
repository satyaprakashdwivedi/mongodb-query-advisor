"""Tests for the MongoDB query advisor (stdlib unittest): python3 -m unittest discover -s tests

The example inputs in examples/shop were captured from a real MongoDB 8.0 (Atlas Local) by
examples/capture_examples.py, so these tests run against genuine server output.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mongodb-query-advisor", "scripts"))
import analyze_mongo as A  # noqa: E402

SHOP = os.path.join(ROOT, "examples", "shop")


def report(*paths):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        A.main([*paths, "--json"])
    return json.loads(buffer.getvalue())


def ids(result):
    return [f["id"] for f in result["findings"]]


def example(name):
    return os.path.join(SHOP, name)


class TestEsrRule(unittest.TestCase):
    def test_equality_then_sort_then_range(self):
        shape = A.QueryShape("db.orders", {"status": "paid", "amount": {"$gt": 10}, "region": {"$in": ["IN"]}},
                             {"created_at": -1})
        self.assertEqual(A.esr_index(shape), [("status", 1), ("region", 1), ("created_at", -1), ("amount", 1)])

    def test_regex_only_indexed_when_anchored(self):
        anchored = A.QueryShape("db.c", {"sku": {"$regex": "^ab"}}, {})
        floating = A.QueryShape("db.c", {"sku": {"$regex": "ab"}}, {})
        self.assertEqual(A.esr_index(anchored), [("sku", 1)])
        self.assertEqual(A.esr_index(floating), [])

    def test_and_clauses_and_negations(self):
        predicates = A.analyse_filter({"$and": [{"a": 1}, {"b": {"$ne": 2}}], "$or": [{"c": 1}]})
        self.assertEqual(predicates.equality, ["a"])
        self.assertEqual(predicates.negations, ["b"])
        self.assertTrue(predicates.has_or)


class TestRealExplainPlans(unittest.TestCase):
    def test_collection_scan(self):
        result = report(example("explain-collscan.json"))
        self.assertEqual(ids(result), ["collscan"])
        self.assertEqual(result["recommended_indexes"][0]["command"], "db.orders.createIndex({ amount: 1 })")

    def test_lookup_without_foreign_index(self):
        result = report(example("explain-lookup-unindexed.json"))
        finding = next(f for f in result["findings"] if f["id"] == "lookup-unindexed")
        self.assertIn("NestedLoopJoin", finding["evidence"])
        self.assertIn("db.customers.createIndex({ customer_id: 1 })",
                      [i["command"] for i in result["recommended_indexes"]])

    def test_esr_violation_with_index_list(self):
        result = report(example("explain-esr-violation.json"), example("indexes-orders.json"))
        self.assertIn("esr-order", ids(result))
        self.assertIn("db.orders.createIndex({ status: 1, created_at: 1, amount: 1 })",
                      [i["command"] for i in result["recommended_indexes"]])

    def test_index_used_only_for_sort(self):
        result = report(example("explain-wrong-index-for-sort.json"))
        finding = next(f for f in result["findings"] if f["id"] == "poor-selectivity")
        self.assertIn("only provides the sort order", finding["evidence"])
        self.assertEqual(result["recommended_indexes"][0]["keys"], [["status", 1], ["region", 1], ["created_at", -1]])

    def test_unanchored_regex(self):
        self.assertEqual(ids(report(example("explain-unanchored-regex.json"))), ["regex-unanchored"])

    def test_deep_skip_is_not_blamed_on_the_index(self):
        result = report(example("explain-deep-skip.json"))
        self.assertEqual(ids(result), ["deep-skip"])
        self.assertEqual(result["recommended_indexes"], [])

    def test_covered_query_is_healthy(self):
        result = report(example("explain-good-covered.json"))
        self.assertEqual(result["findings"], [])
        self.assertIn("covered by the index", result["healthy"][0])


class TestVectorSearch(unittest.TestCase):
    def test_post_filter_and_low_candidates(self):
        result = report(example("pipeline-vector-search.json"), example("search-indexes-products.json"))
        self.assertEqual(sorted(ids(result)), ["vector-low-candidates", "vector-post-filter"])
        post_filter = next(f for f in result["findings"] if f["id"] == "vector-post-filter")
        self.assertIn("Declare category as filter fields", post_filter["recommendation"])

    def test_explain_output_is_understood_too(self):
        self.assertIn("vector-post-filter", ids(report(example("explain-vector-search.json"))))

    def test_undeclared_filter_field_and_embeddings_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pipeline.json")
            with open(path, "w") as handle:
                json.dump({"collection": "products", "pipeline": [
                    {"$vectorSearch": {"index": "product_vectors", "path": "embedding", "queryVector": [0.1],
                                       "numCandidates": 200, "limit": 10, "filter": {"category": "shoes"}}}]}, handle)
            result = report(path, example("search-indexes-products.json"))
        self.assertEqual(sorted(ids(result)), ["vector-filter-undeclared", "vector-returns-embeddings"])

    def test_vector_search_must_be_first(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pipeline.json")
            with open(path, "w") as handle:
                json.dump([{"$match": {"a": 1}}, {"$vectorSearch": {"index": "x", "path": "v", "queryVector": [1],
                                                                    "numCandidates": 100, "limit": 10}},
                           {"$project": {"v": 0}}], handle)
            self.assertEqual(ids(report(path)), ["vector-not-first"])


class TestIndexHygiene(unittest.TestCase):
    def test_redundant_and_unused(self):
        result = report(example("indexes-orders.json"), example("index-stats-orders.json"))
        redundant = next(f for f in result["findings"] if f["id"] == "redundant-index")
        self.assertIn("customer_id_1 is a prefix of customer_id_1_created_at_-1", redundant["title"])
        unused = next(f for f in result["findings"] if f["id"] == "unused-index")
        names = set(unused["title"].split(": ", 1)[1].split(", "))
        self.assertIn("region_1_sku_1", names)
        self.assertNotIn("sku_1", names)  # used by the workload

    def test_existing_index_is_not_recommended_again(self):
        shape_index = [("customer_id", 1)]
        advisor = A.Advisor(A.load_inputs([example("indexes-orders.json")]))
        self.assertEqual(advisor.covering_index("orders", shape_index), "customer_id_1")


class TestSlowQueryLog(unittest.TestCase):
    def test_log_groups_shapes_and_skips_explain_and_writes(self):
        result = report(example("mongod.log"))
        queries = [row["query"] for row in result["slow_queries"]]
        self.assertTrue(all("insert" not in q for q in queries))
        collscan = next(row for row in result["slow_queries"] if '"amount"' in row["query"])
        self.assertEqual(collscan["plan"], "COLLSCAN")
        self.assertIn("slow-query", ids(result))

    def test_profiler_used_when_no_log(self):
        result = report(example("profile.json"))
        self.assertTrue(result["slow_queries"])

    def test_log_preferred_over_profiler(self):
        both = report(example("mongod.log"), example("profile.json"))
        log_only = report(example("mongod.log"))
        self.assertEqual(both["slow_queries"], log_only["slow_queries"])


class TestWholeDirectory(unittest.TestCase):
    def test_markdown_report(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            A.main([SHOP])
        text = buffer.getvalue()
        self.assertIn("## Create these indexes", text)
        self.assertIn("db.customers.createIndex({ customer_id: 1 })", text)
        self.assertNotIn("Not analysed", text)

    def test_unknown_input_is_reported_not_crashed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.json")
            with open(path, "w") as handle:
                handle.write('{"hello": "world"}')
            self.assertTrue(report(path)["not_analysed"])


if __name__ == "__main__":
    unittest.main()
