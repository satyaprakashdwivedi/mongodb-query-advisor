#!/usr/bin/env python3
"""MongoDB query advisor: read explain plans, slow query logs, profiler output, index lists, $indexStats and
Atlas Vector Search pipelines, then report what is slow, why, and which indexes to create, hide or drop.

    python3 analyze_mongo.py <files or directories> [--json] [--out report.md] [--top 10]

Accepted inputs (detected by content, not file name):
  explain output        db.coll.find(...).explain("executionStats"), aggregate explain (classic or SBE)
  slow query log        mongod JSON log lines with "msg":"Slow query" (id 51803), one per line
  profiler documents    db.system.profile.find() exported as a JSON array or JSON lines
  index list            db.coll.getIndexes()
  index usage           db.coll.aggregate([{$indexStats: {}}])
  search indexes        db.coll.getSearchIndexes() / listSearchIndexes
  vector pipeline       {"collection": "...", "pipeline": [...]} or a bare pipeline array starting with $vectorSearch

Standard library only (Python 3.9+), so it runs anywhere a Claude skill or a CI job runs.
"""
import argparse
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field

RANGE_OPERATORS = {"$gt", "$gte", "$lt", "$lte", "$ne", "$nin", "$exists", "$type", "$not", "$mod", "$size"}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# A query is worth flagging when it reads this many documents or index keys per document returned.
POOR_SELECTIVITY_RATIO = 10
MIN_EXAMINED_TO_CARE = 1000          # a collection scan smaller than this is not worth an index
MIN_EXAMINED_FOR_RATIO = 200         # below this, a high examined/returned ratio is noise
DEEP_SKIP = 10_000


# ------------------------------------------------------------------------------------------ input loading

def read_json_documents(path):
    """A file can hold one JSON document, a JSON array, or JSON lines (logs, mongoexport)."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return [json.loads(stripped)]
    except json.JSONDecodeError:
        documents = []
        for line in stripped.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    documents.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return documents


def expand_paths(paths):
    files = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, names in os.walk(path):
                files.extend(os.path.join(root, name) for name in sorted(names)
                             if name.endswith((".json", ".log", ".jsonl", ".txt")))
        else:
            files.append(path)
    return files


@dataclass
class Inputs:
    explains: list = field(default_factory=list)          # (source, explain document)
    slow_ops: list = field(default_factory=list)          # normalised slow operations from logs and profiler
    indexes: dict = field(default_factory=lambda: defaultdict(list))        # namespace -> getIndexes()
    index_stats: dict = field(default_factory=lambda: defaultdict(list))    # namespace -> $indexStats
    search_indexes: dict = field(default_factory=lambda: defaultdict(list))  # collection -> search index defs
    pipelines: list = field(default_factory=list)          # (source, collection, pipeline)
    unread: list = field(default_factory=list)


def load_inputs(paths):
    inputs = Inputs()
    for path in expand_paths(paths):
        source = os.path.basename(path)
        try:
            documents = read_json_documents(path)
        except OSError as error:
            inputs.unread.append(f"{source}: {error}")
            continue
        if len(documents) == 1 and isinstance(documents[0], list):
            documents = documents[0]
        if not documents:
            inputs.unread.append(f"{source}: no JSON found")
            continue
        classify(source, documents, inputs)
    return inputs


def namespace_from_file(source):
    """indexes-orders.json -> orders; used when an index list does not say which collection it is for."""
    match = re.search(r"(?:indexes|index-stats|search-indexes)-([\w.-]+?)\.json$", source)
    return match.group(1) if match else source


def classify(source, documents, inputs):
    first = documents[0] if isinstance(documents[0], dict) else {}
    if "queryPlanner" in first or ("stages" in first and "explainVersion" in first):
        inputs.explains.extend((source, doc) for doc in documents)
    elif first.get("msg") == "Slow query" or "attr" in first:
        for doc in documents:
            if doc.get("msg") == "Slow query":
                op = slow_op_from_log(doc)
                if op:
                    inputs.slow_ops.append(op)
    elif "millis" in first and "op" in first:
        for doc in documents:
            op = slow_op_from_profile(doc)
            if op:
                inputs.slow_ops.append(op)
    elif "accesses" in first and "key" in first:
        inputs.index_stats[namespace_from_file(source)].extend(documents)
    elif "key" in first and "name" in first and "v" in first:
        namespace = first.get("ns") or namespace_from_file(source)
        inputs.indexes[namespace.split(".")[-1]].extend(documents)
    elif first.get("type") in ("vectorSearch", "search") and ("latestDefinition" in first or "definition" in first):
        inputs.search_indexes[namespace_from_file(source)].extend(documents)
    elif "pipeline" in first:
        inputs.pipelines.append((source, first.get("collection"), first["pipeline"]))
    elif all(isinstance(doc, dict) and len(doc) == 1 and next(iter(doc)).startswith("$") for doc in documents):
        inputs.pipelines.append((source, None, documents))
    else:
        inputs.unread.append(f"{source}: not a recognised MongoDB input")


# ------------------------------------------------------------------------------------------ query shapes

@dataclass
class QueryShape:
    namespace: str
    filter: dict
    sort: dict
    projection: dict = field(default_factory=dict)
    skip: int = 0

    @property
    def collection(self):
        return self.namespace.split(".", 1)[-1]


def shape_from_command(namespace, command):
    """Turn a find/aggregate/count/distinct command into the filter and sort the planner had to satisfy."""
    if not isinstance(command, dict):
        return None
    if "explain" in command and isinstance(command["explain"], dict):
        command = command["explain"]
    if "find" in command:
        return QueryShape(namespace, command.get("filter") or {}, command.get("sort") or {},
                          command.get("projection") or {}, command.get("skip") or 0)
    if "aggregate" in command:
        match, sort = {}, {}
        for stage in command.get("pipeline") or []:
            if "$match" in stage and not sort:
                match.update(stage["$match"])
            elif "$sort" in stage and not sort:
                sort = stage["$sort"]
            else:
                break
        return QueryShape(namespace, match, sort)
    if "count" in command or "distinct" in command:
        return QueryShape(namespace, command.get("query") or {}, {})
    return None


@dataclass
class Predicates:
    equality: list = field(default_factory=list)
    ranges: list = field(default_factory=list)
    regex: list = field(default_factory=list)       # (field, anchored, case_insensitive)
    has_or: bool = False
    negations: list = field(default_factory=list)


def analyse_filter(query_filter, predicates=None):
    predicates = predicates or Predicates()
    for key, value in (query_filter or {}).items():
        if key == "$and":
            for clause in value:
                analyse_filter(clause, predicates)
        elif key in ("$or", "$nor"):
            predicates.has_or = True
        elif key.startswith("$"):
            continue
        elif isinstance(value, dict) and value and all(k.startswith("$") for k in value):
            operators = set(value)
            if "$regex" in operators:
                pattern = str(value["$regex"])
                predicates.regex.append((key, pattern.startswith("^"), "i" in str(value.get("$options", ""))))
            elif operators <= {"$eq"} or operators == {"$in"}:
                predicates.equality.append(key)
            elif operators & {"$ne", "$nin", "$not"}:
                predicates.negations.append(key)
                predicates.ranges.append(key)
            elif operators & RANGE_OPERATORS or operators & {"$elemMatch"}:
                predicates.ranges.append(key)
            else:
                predicates.equality.append(key)
        else:
            predicates.equality.append(key)
    return predicates


def esr_index(shape):
    """Equality, then Sort, then Range: the compound index order that lets MongoDB filter and sort from the index."""
    predicates = analyse_filter(shape.filter)
    keys = []
    for name in predicates.equality:
        if name not in dict(keys):
            keys.append((name, 1))
    for name, direction in (shape.sort or {}).items():
        if name not in dict(keys) and isinstance(direction, int):
            keys.append((name, direction))
    for name in predicates.ranges + [r[0] for r in predicates.regex if r[1]]:
        if name not in dict(keys):
            keys.append((name, 1))
    return keys


def format_keys(keys):
    return "{ " + ", ".join(f"{name}: {direction}" for name, direction in keys) + " }"


def create_index_command(collection, keys):
    return f"db.{collection}.createIndex({format_keys(keys)})"


def shape_label(shape):
    parts = [f"filter {json.dumps(shape.filter, default=str)}"]
    if shape.sort:
        parts.append(f"sort {json.dumps(shape.sort)}")
    if shape.skip:
        parts.append(f"skip {shape.skip}")
    return f"{shape.namespace}: " + ", ".join(parts)


# ------------------------------------------------------------------------------------------ plan walking

def plan_nodes(node):
    """Every stage in a winning plan, classic (stage/inputStage/inputStages) or SBE (queryPlan)."""
    if isinstance(node, dict):
        if "stage" in node:
            yield node
        for key, value in node.items():
            if key in ("rejectedPlans", "allPlansExecution", "slotBasedPlan"):
                continue
            yield from plan_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from plan_nodes(item)


def winning_plan(explain):
    planner = explain.get("queryPlanner") or {}
    if not planner and "stages" in explain:  # aggregate explain: the cursor stage holds the query planner
        for stage in explain["stages"]:
            if "$cursor" in stage:
                planner = stage["$cursor"].get("queryPlanner") or {}
                explain = stage["$cursor"]
                break
    plan = planner.get("winningPlan") or {}
    return planner, plan.get("queryPlan", plan), explain.get("executionStats") or {}


# ------------------------------------------------------------------------------------------ findings

@dataclass
class Finding:
    id: str
    severity: str
    title: str
    evidence: str
    recommendation: str
    query: str = ""
    index: tuple = ()           # recommended index keys, if any
    collection: str = ""
    source: str = ""


class Advisor:
    def __init__(self, inputs, top=10):
        self.inputs = inputs
        self.top = top
        self.findings = []
        self.healthy = []
        self.slow_query_table = []

    def add(self, **kwargs):
        self.findings.append(Finding(**kwargs))

    def covering_index(self, collection, keys):
        """An existing index whose leading keys are exactly `keys`: then there is nothing to create."""
        for index in self.inputs.indexes.get(collection, []):
            existing = list(index.get("key", {}).items())
            if keys and existing[:len(keys)] == list(keys):
                return index["name"]
        return None

    def run(self):
        for source, explain in self.inputs.explains:
            if "stages" in explain and any("$vectorSearch" in s for s in explain["stages"]):
                pipeline = [{k: v for k, v in s.items() if k.startswith("$")} for s in explain["stages"]]
                self.check_vector_pipeline(source, explain.get("command", {}).get("aggregate"), pipeline)
                continue
            self.check_explain(source, explain)
        for source, collection, pipeline in self.inputs.pipelines:
            self.check_vector_pipeline(source, collection, pipeline)
        self.check_slow_ops()
        self.check_indexes()
        for finding in self.findings:
            existing = finding.index and self.covering_index(finding.collection, finding.index)
            if existing:
                finding.recommendation += f" The index already exists as {existing}; the fix is in the query."
                finding.index = ()
        self.findings = dedupe(self.findings)
        self.findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.collection, f.id))
        return self

    # ---- explain plans

    def check_explain(self, source, explain):
        planner, plan, stats = winning_plan(explain)
        namespace = planner.get("namespace", "?")
        shape = shape_from_command(namespace, explain.get("command")) or \
            QueryShape(namespace, planner.get("parsedQuery") or {}, {})
        nodes = list(plan_nodes(plan))
        exec_nodes = list(plan_nodes(stats.get("executionStages") or {}))
        stages = {n["stage"] for n in nodes}
        returned = stats.get("nReturned", 0)
        docs = stats.get("totalDocsExamined", 0)
        keys = stats.get("totalKeysExamined", 0)
        millis = stats.get("executionTimeMillis")
        numbers = f"returned {returned:,}, examined {keys:,} index keys and {docs:,} documents" + \
            (f" in {millis} ms" if millis is not None else "")
        label = shape_label(shape)
        recommended = esr_index(shape)
        collection = shape.collection
        found_before = len(self.findings)

        lookups = [n for n in nodes if n["stage"] in ("EQ_LOOKUP", "$lookup")]
        for lookup in lookups:
            strategy = lookup.get("strategy", "")
            scans = max([n.get("collectionScans", 0) for n in exec_nodes] + [0])
            if strategy == "NestedLoopJoin" or scans:
                foreign = lookup.get("foreignCollection", "?").split(".", 1)[-1]
                self.add(id="lookup-unindexed", severity="high", collection=foreign, source=source, query=label,
                         title=f"$lookup scans {foreign} once per input document",
                         evidence=f"Join strategy {strategy or 'collection scan'}: {scans:,} collection scans of "
                                  f"{foreign}, {docs:,} documents examined to return {returned:,}.",
                         recommendation=f"Index the foreign field so each lookup is an index seek "
                                        f"(the plan becomes IndexedLoopJoin).",
                         index=((lookup.get("foreignField", "?"), 1),))

        if "COLLSCAN" in stages and docs >= MIN_EXAMINED_TO_CARE:
            self.add(id="collscan", severity="high", collection=collection, source=source, query=label,
                     title="Collection scan", evidence=f"COLLSCAN: {numbers}.",
                     recommendation="Create an index that matches the filter (equality, sort, range order)."
                     if recommended else "No filter or sort to index: this reads the whole collection by design; "
                                         "page it or project fewer fields.",
                     index=tuple(recommended))

        blocking_sorts = [n for n in exec_nodes or nodes if n["stage"] in ("SORT", "sort")]
        for sort in blocking_sorts:
            spilled = sort.get("usedDisk") or sort.get("spills")
            self.add(id="in-memory-sort", severity="high", collection=collection, source=source, query=label,
                     title="Sort done in memory" + (" and spilled to disk" if spilled else ""),
                     evidence=f"A blocking SORT stage ran ({numbers}); no index returned documents in "
                              f"{json.dumps(shape.sort)} order." + (" It spilled to disk." if spilled else ""),
                     recommendation="Put the sort fields after the equality fields and before any range field, "
                                    "so the index delivers rows already sorted.",
                     index=tuple(recommended))

        # With a $lookup, totalDocsExamined includes the foreign side; judge the base query by its index keys.
        examined = keys if lookups else max(docs, keys)
        skipped = sum(n.get("skipAmount", 0) for n in nodes if n["stage"] == "SKIP")
        if returned and examined - skipped >= MIN_EXAMINED_FOR_RATIO and \
                (examined - skipped) / returned >= POOR_SELECTIVITY_RATIO and "COLLSCAN" not in stages:
            scans = [n for n in nodes if n["stage"] in ("IXSCAN", "ixseek")]
            unbounded = [n for n in scans if n.get("indexBounds") and
                         next(iter(n["indexBounds"].values()), [""])[0] in ("[MinKey, MaxKey]", "[MaxKey, MinKey]")]
            residual = [n for n in nodes if n["stage"] == "FETCH" and n.get("filter")]
            reasons = []
            if unbounded:
                reasons.append(f"index {unbounded[0].get('indexName')} is scanned end to end (it only provides "
                               f"the sort order, not the filter)")
            if residual:
                fields = sorted(_filter_fields(residual[0]["filter"]))
                reasons.append(f"{', '.join(fields)} {'is' if len(fields) == 1 else 'are'} checked after "
                               f"fetching each document instead of in the index")
            regex = [r for r in analyse_filter(shape.filter).regex if not r[1] or r[2]]
            if regex:
                name, _anchored, insensitive = regex[0]
                self.add(id="regex-unanchored", severity="medium", collection=collection, source=source, query=label,
                         title=f"{'Case-insensitive' if insensitive else 'Unanchored'} regex on {name}",
                         evidence=f"{numbers}: the regex cannot use index bounds, so every key is read.",
                         recommendation="Anchor the pattern (^prefix) and keep it case-sensitive, store a normalised "
                                        "copy of the field, or use Atlas Search for substring and fuzzy matching.")
            else:
                self.add(id="poor-selectivity", severity="high" if examined / returned >= 100 else "medium",
                         collection=collection, source=source, query=label,
                         title=f"Reads {(examined - skipped) / returned:,.0f}x more than it returns",
                         evidence=f"{numbers}" + (": " + "; ".join(reasons) if reasons else "") + ".",
                         recommendation="Use an index whose leading fields are the query's equality fields, then the "
                                        "sort, then ranges.", index=tuple(recommended))

        for node in nodes:
            if node["stage"] == "SKIP" and node.get("skipAmount", 0) >= DEEP_SKIP:
                self.add(id="deep-skip", severity="medium", collection=collection, source=source, query=label,
                         title=f"skip({node['skipAmount']:,}) pagination",
                         evidence=f"{numbers}: every skipped entry is still read.",
                         recommendation="Page by range instead: remember the last sort key and query "
                                        "{created_at: {$gt: last}} with the same sort and limit.")

        if self.inputs.indexes.get(collection):
            self.check_esr_of_existing(collection, shape, source, label)

        if len(self.findings) == found_before:
            covered = "PROJECTION_COVERED" in stages or (docs == 0 and keys > 0)
            self.healthy.append(f"{label}: {numbers}" + (" (covered by the index, no documents fetched)"
                                                          if covered else ""))

    def check_esr_of_existing(self, collection, shape, source, label):
        """An existing index that has the right fields in the wrong order: a range field before the sort field."""
        predicates = analyse_filter(shape.filter)
        if not shape.sort:
            return
        for index in self.inputs.indexes[collection]:
            fields = list(index.get("key", {}))
            if not fields or fields[0] not in predicates.equality:
                continue
            positions = {name: i for i, name in enumerate(fields)}
            sort_positions = [positions[s] for s in shape.sort if s in positions]
            range_positions = [positions[r] for r in predicates.ranges if r in positions]
            if sort_positions and range_positions and min(range_positions) < min(sort_positions):
                range_field = fields[min(range_positions)]
                sort_field = fields[min(sort_positions)]
                self.add(id="esr-order", severity="medium", collection=collection, source=source, query=label,
                         title=f"Index {index['name']} has a range field before the sort field",
                         evidence=f"{range_field} (range) comes before {sort_field} (sort), so this index cannot return "
                                  f"rows in sort order and the planner either sorts in memory or picks another index.",
                         recommendation=f"Reorder to equality, sort, range: replace it with the index below "
                                        f"(hide the old one first, then drop it).", index=tuple(esr_index(shape)))

    # ---- vector search

    def check_vector_pipeline(self, source, collection, pipeline):
        stages = [s for s in pipeline if isinstance(s, dict)]
        positions = [i for i, s in enumerate(stages) if "$vectorSearch" in s]
        if not positions:
            return
        if positions[0] != 0:
            self.add(id="vector-not-first", severity="high", collection=collection or "?", source=source,
                     title="$vectorSearch is not the first stage",
                     evidence=f"It is stage {positions[0] + 1} of {len(stages)}.",
                     recommendation="$vectorSearch must be the first stage of the pipeline; move other stages after it.")
        search = stages[positions[0]]["$vectorSearch"]
        limit, candidates = search.get("limit", 0), search.get("numCandidates")
        index_name, path = search.get("index", "default"), search.get("path")
        definition = self.vector_index_definition(collection, index_name)
        filter_fields = {f["path"] for f in definition.get("fields", []) if f.get("type") == "filter"}
        label = f"{collection or '?'}: $vectorSearch on {path} (index {index_name})"

        if not search.get("exact") and candidates is not None and limit:
            ratio = candidates / limit
            if ratio < 10:
                self.add(id="vector-low-candidates", severity="medium", collection=collection or "?", source=source,
                         query=label, title=f"numCandidates is only {ratio:g}x limit",
                         evidence=f"numCandidates {candidates}, limit {limit}. The approximate (HNSW) search only "
                                  f"explores {candidates} candidates, so recall drops.",
                         recommendation=f"Start at 10-20x limit (numCandidates {limit * 15}) and tune against an "
                                        f"exact-search baseline (exact: true) on a sample of queries.")
            elif candidates > 10_000:
                self.add(id="vector-high-candidates", severity="high", collection=collection or "?", source=source,
                         query=label, title="numCandidates above 10,000",
                         evidence=f"numCandidates {candidates} exceeds the server maximum.",
                         recommendation="Keep numCandidates at or below 10,000; raise recall with better embeddings or "
                                        "filters instead.")

        following = [s for s in stages[positions[0] + 1:] if not next(iter(s), "").startswith("$_internal")]
        next_stage = following[0] if following else {}
        if "$match" in next_stage:
            fields = sorted(_filter_fields(next_stage["$match"]))
            missing = [f for f in fields if f not in filter_fields]
            self.add(id="vector-post-filter", severity="high", collection=collection or "?", source=source,
                     query=label, title=f"Filtering on {', '.join(fields)} after the vector search",
                     evidence=f"$match runs after $vectorSearch has already picked the top {limit}, so it can return "
                              f"fewer than {limit} results, or none.",
                     recommendation=("Declare " + ", ".join(missing) + " as filter fields in the vector index, then "
                                     if missing else "") +
                                    f"move the condition into $vectorSearch.filter so it is applied during the search.")

        for name in sorted(_filter_fields(search.get("filter") or {})):
            if definition and name not in filter_fields:
                self.add(id="vector-filter-undeclared", severity="high", collection=collection or "?", source=source,
                         query=label, title=f"Filter field {name} is not declared in the vector index",
                         evidence=f"Index {index_name} declares filter fields {sorted(filter_fields) or 'none'}.",
                         recommendation=f"Add {{type: 'filter', path: '{name}'}} to the index definition.")

        projects = [s["$project"] for s in stages if "$project" in s]
        if path and not any(_projection_drops(p, path) for p in projects):
            self.add(id="vector-returns-embeddings", severity="low", collection=collection or "?", source=source,
                     query=label, title="Results include the embedding vectors",
                     evidence=f"No $project removes {path}; every result carries its full vector.",
                     recommendation=f"Add {{$project: {{{path}: 0}}}} (or project only the fields you need).")

    def vector_index_definition(self, collection, index_name):
        for key in ([collection] if collection else []) + list(self.inputs.search_indexes):
            for index in self.inputs.search_indexes.get(key, []):
                if index.get("name") == index_name:
                    return index.get("latestDefinition") or index.get("definition") or {}
        return {}

    # ---- slow query log and profiler

    def check_slow_ops(self):
        # The slow query log and the profiler record the same operations; use the log when both are given.
        ops_in_scope = [op for op in self.inputs.slow_ops if op["source"] == "log"] or self.inputs.slow_ops
        groups = defaultdict(list)
        for op in ops_in_scope:
            groups[op["shape_key"]].append(op)
        rows = []
        for ops in groups.values():
            first = ops[0]
            durations = [op["millis"] for op in ops]
            examined = sum(max(op["docs_examined"], op["keys_examined"]) for op in ops)
            returned = sum(op["returned"] for op in ops)
            rows.append({"query": shape_label(first["shape"]), "collection": first["shape"].collection,
                         "count": len(ops), "total_ms": sum(durations), "max_ms": max(durations),
                         "median_ms": statistics.median(durations), "plan": first["plan"],
                         "examined_per_returned": round(examined / returned, 1) if returned else None,
                         "examined": examined, "shape": first["shape"]})
        rows.sort(key=lambda r: r["total_ms"], reverse=True)
        self.slow_query_table = rows[:self.top]
        for row in self.slow_query_table:
            ratio = row["examined_per_returned"]
            if "COLLSCAN" in row["plan"] or (ratio and ratio >= POOR_SELECTIVITY_RATIO and
                                             row["examined"] >= MIN_EXAMINED_TO_CARE):
                keys = esr_index(row["shape"])
                bad_regex = [r for r in analyse_filter(row["shape"].filter).regex if not r[1] or r[2]]
                fix = ("Unanchored or case-insensitive regex: anchor it (^prefix) or use Atlas Search." if bad_regex
                       else "Index the query shape (equality, sort, range)." if keys
                       else "No filter to index; reduce how often it runs or what it reads.")
                self.add(id="slow-query", severity="high" if row["total_ms"] >= 1000 or "COLLSCAN" in row["plan"]
                         else "medium", collection=row["collection"], source="slow query log / profiler",
                         query=row["query"],
                         title=f"Slow {row['count']}x, {row['total_ms']:,} ms in total",
                         evidence=f"Plan {row['plan']}; median {row['median_ms']:g} ms; "
                                  f"{ratio if ratio is not None else '-'} examined per returned document.",
                         recommendation=fix, index=tuple(keys))

    # ---- index hygiene

    def check_indexes(self):
        for collection, indexes in self.inputs.indexes.items():
            for index in indexes:
                if index["name"] == "_id_" or index.get("unique") or index.get("partialFilterExpression") \
                        or index.get("sparse") or "expireAfterSeconds" in index or index.get("collation"):
                    continue
                keys = list(index["key"].items())
                for other in indexes:
                    other_keys = list(other["key"].items())
                    if other is not index and len(other_keys) > len(keys) and other_keys[:len(keys)] == keys:
                        self.add(id="redundant-index", severity="low", collection=collection, source="getIndexes",
                                 title=f"Index {index['name']} is a prefix of {other['name']}",
                                 evidence=f"{format_keys(keys)} is the leading part of {format_keys(other_keys)}, which "
                                          f"can serve every query it serves.",
                                 recommendation=f"db.{collection}.hideIndex('{index['name']}'), watch for a week, then "
                                                f"dropIndex. Every index costs RAM and slows writes.")
                        break
            if len(indexes) > 12:
                self.add(id="many-indexes", severity="low", collection=collection, source="getIndexes",
                         title=f"{len(indexes)} indexes on {collection}",
                         evidence="Each index is updated on every write and competes for the WiredTiger cache.",
                         recommendation="Consolidate with compound indexes that follow the ESR rule.")
        for collection, stats in self.inputs.index_stats.items():
            unused = [s for s in stats if s["name"] != "_id_" and int(_number(s["accesses"].get("ops"))) == 0
                      and not s.get("spec", {}).get("unique")]
            if unused:
                since = min(str(s["accesses"].get("since", "?")) for s in unused)
                self.add(id="unused-index", severity="low", collection=collection, source="$indexStats",
                         title=f"{len(unused)} unused index{'es' if len(unused) > 1 else ''}: "
                               f"{', '.join(s['name'] for s in unused)}",
                         evidence=f"0 operations since {since}. $indexStats counts from the last restart and per "
                                  f"node, so check every replica set member and a full business cycle.",
                         recommendation="Hide them (hideIndex) before dropping, so you can unhide instantly if a "
                                        "query regresses.")


# ------------------------------------------------------------------------------------------ helpers

def _number(value):
    if isinstance(value, dict):  # extended JSON {"$numberLong": "3"}
        value = next(iter(value.values()), 0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _projection_drops(projection, path):
    """True when a $project stage removes `path`: excluded explicitly, or left out of an inclusion projection."""
    if projection.get(path) in (0, False):
        return True
    included = [k for k, v in projection.items() if k != "_id" and v not in (0, False)]
    return bool(included) and path not in projection


def _filter_fields(query_filter):
    fields = set()
    for key, value in (query_filter or {}).items():
        if key in ("$and", "$or", "$nor"):
            for clause in value:
                fields |= _filter_fields(clause)
        elif not key.startswith("$"):
            fields.add(key)
    return fields


def _query_command(command, originating):
    """The command whose shape matters: getMore batches belong to the find/aggregate that opened the cursor;
    explain commands (someone investigating) are not workload."""
    if not isinstance(command, dict) or "explain" in command:
        return None
    if "getMore" in command:
        return originating if isinstance(originating, dict) else None
    return command


def slow_op_from_log(entry):
    attr = entry.get("attr") or {}
    command = _query_command(attr.get("command"), attr.get("originatingCommand"))
    shape = shape_from_command(attr.get("ns", "?"), command)
    if not shape:
        return None
    return {"source": "log", "shape": shape, "shape_key": attr.get("planCacheShapeHash") or attr.get("queryHash") or shape_label(shape),
            "millis": int(_number(attr.get("durationMillis"))), "plan": attr.get("planSummary", "?"),
            "docs_examined": int(_number(attr.get("docsExamined"))),
            "keys_examined": int(_number(attr.get("keysExamined"))), "returned": int(_number(attr.get("nreturned")))}


def slow_op_from_profile(entry):
    command = _query_command(entry.get("command"), entry.get("originatingCommand"))
    shape = shape_from_command(entry.get("ns", "?"), command)
    if not shape:
        return None
    return {"source": "profiler", "shape": shape, "shape_key": entry.get("planCacheShapeHash") or entry.get("queryHash") or shape_label(shape),
            "millis": int(_number(entry.get("millis"))), "plan": entry.get("planSummary", "?"),
            "docs_examined": int(_number(entry.get("docsExamined"))),
            "keys_examined": int(_number(entry.get("keysExamined"))), "returned": int(_number(entry.get("nreturned")))}


def dedupe(findings):
    seen, unique = set(), []
    for finding in findings:
        key = (finding.id, finding.collection, finding.query, finding.title)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def recommended_indexes(findings):
    """Merge index recommendations: one createIndex per distinct key pattern, listing what it fixes.
    A pattern that is a prefix of another recommended pattern on the same collection is folded into it."""
    by_collection = defaultdict(dict)
    for finding in findings:
        if finding.index:
            by_collection[finding.collection].setdefault(finding.index, []).append(finding.id)
    merged = []
    for collection, patterns in by_collection.items():
        for keys, reasons in patterns.items():
            longer = [other for other in patterns if len(other) > len(keys) and other[:len(keys)] == keys]
            if longer:
                patterns[longer[0]].extend(reasons)
                continue
            merged.append({"collection": collection, "keys": [list(k) for k in keys],
                           "command": create_index_command(collection, keys), "fixes": sorted(set(reasons))})
    return merged


# ------------------------------------------------------------------------------------------ report

def report_markdown(advisor, inputs):
    findings = advisor.findings
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITY_ORDER}
    out = ["# MongoDB query advisor report", "",
           f"Inputs: {len(inputs.explains)} explain plans, {len(inputs.slow_ops)} slow operations, "
           f"{sum(len(v) for v in inputs.indexes.values())} indexes, {len(inputs.pipelines)} pipelines.  ",
           f"Findings: **{counts['high']} high**, {counts['medium']} medium, {counts['low']} low.", ""]
    indexes = recommended_indexes(findings)
    if indexes:
        out += ["## Create these indexes", "", "| Index | Fixes |", "|---|---|"]
        out += [f"| `{i['command']}` | {', '.join(i['fixes'])} |" for i in indexes]
        out += ["", "Build them in a quiet window; on a replica set the build runs on every member. Re-run "
                    "explain(\"executionStats\") afterwards and check that examined is close to returned.", ""]
    if findings:
        out += ["## Findings", ""]
        for finding in findings:
            out += [f"### [{finding.severity.upper()}] {finding.title}", ""]
            if finding.query:
                out.append(f"- **Query:** `{finding.query}`")
            out += [f"- **Evidence:** {finding.evidence}", f"- **Fix:** {finding.recommendation}"]
            if finding.index:
                out.append(f"- **Index:** `{create_index_command(finding.collection, finding.index)}`")
            out += [f"- *Source: {finding.source}*", ""]
    if advisor.slow_query_table:
        out += ["## Slowest query shapes (log + profiler)", "",
                "| Query | Runs | Total ms | Median ms | Plan | Examined / returned |", "|---|---|---|---|---|---|"]
        out += [f"| `{r['query'][:110]}` | {r['count']} | {r['total_ms']:,} | {r['median_ms']:g} | {r['plan']} | "
                f"{r['examined_per_returned'] if r['examined_per_returned'] is not None else '-'} |"
                for r in advisor.slow_query_table]
        out.append("")
    if advisor.healthy:
        out += ["## Looks healthy", ""] + [f"- `{h}`" for h in advisor.healthy] + [""]
    if inputs.unread:
        out += ["## Not analysed", ""] + [f"- {u}" for u in inputs.unread] + [""]
    return "\n".join(out)


def report_json(advisor, inputs):
    return {"findings": [f.__dict__ | {"index": [list(k) for k in f.index]} for f in advisor.findings],
            "recommended_indexes": recommended_indexes(advisor.findings),
            "slow_queries": [{k: v for k, v in r.items() if k != "shape"} for r in advisor.slow_query_table],
            "healthy": advisor.healthy, "not_analysed": inputs.unread}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument("--top", type=int, default=10, help="slow query shapes to list (default 10)")
    args = parser.parse_args(argv)

    inputs = load_inputs(args.paths)
    advisor = Advisor(inputs, top=args.top).run()
    output = json.dumps(report_json(advisor, inputs), indent=2, default=str) if args.json \
        else report_markdown(advisor, inputs)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
