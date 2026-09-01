"""
Incident topology: a real dependency graph with real traversal.

SSOT §25 decision #5 makes this the project's primary technical-depth claim,
explicitly replacing a flat keyword-matched ``topology.json``. So the graph
is a genuine ``networkx`` multigraph with typed edges, and the blast-radius
query is a genuine breadth-first search that returns the *path* by which
each affected node is reached -- because "rolling back core-db breaks
payment-api" is only a credible statement if the system can say *how* it
knows.

The graph is wrapped in :class:`Topology` rather than passed around raw:

* the reverse dependency index is built **once** at construction instead of
  being rebuilt on every risk evaluation;
* edge lookups go through named, typed accessors instead of stringly-typed
  ``get_edge_data(..., key="reads_schema")`` calls scattered across checks;
* the fixture is validated on load, so a malformed edge fails at startup
  rather than silently disabling a safety check mid-incident.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping, Optional, Sequence

import networkx as nx

from backend.common.enums import TopologyEdgeType
from backend.common.errors import ConfigError

EDGE_DEPENDS_ON = TopologyEdgeType.DEPENDS_ON.value
EDGE_READS_SCHEMA = TopologyEdgeType.READS_SCHEMA.value
EDGE_COMPATIBLE_WITH = TopologyEdgeType.COMPATIBLE_WITH.value


@dataclass(frozen=True)
class DependencyPath:
    """How a dependent reaches the node under change.

    ``nodes`` runs from the dependent to the changed node, so
    ``("api-gateway", "payment-api", "core-db")`` reads as "api-gateway
    depends on payment-api, which depends on core-db".
    """

    nodes: tuple[str, ...]

    @property
    def dependent(self) -> str:
        return self.nodes[0]

    @property
    def target(self) -> str:
        return self.nodes[-1]

    @property
    def hops(self) -> int:
        return len(self.nodes) - 1

    @property
    def is_direct(self) -> bool:
        return self.hops == 1

    def render(self) -> str:
        return " -> ".join(self.nodes)


class Topology:
    """A validated, query-optimised view over the incident dependency graph."""

    __slots__ = ("_graph", "_reverse_depends_on")

    def __init__(self, graph: nx.MultiDiGraph) -> None:
        self._graph = graph
        self._validate(graph)
        # Precomputed once: "who depends on X" is the direction every
        # blast-radius query traverses.
        reverse: dict[str, list[str]] = {node: [] for node in graph.nodes}
        for dependent, depended_on, key in graph.edges(keys=True):
            if key == EDGE_DEPENDS_ON:
                reverse[depended_on].append(dependent)
        self._reverse_depends_on: Mapping[str, tuple[str, ...]] = {
            node: tuple(sorted(dependents)) for node, dependents in reverse.items()
        }

    # -- construction / validation ---------------------------------------

    @staticmethod
    def _validate(graph: nx.MultiDiGraph) -> None:
        for dependent, source, key, data in graph.edges(keys=True, data=True):
            if dependent == source:
                raise ConfigError(
                    "topology contains a self-dependency", node=dependent, edge_type=key
                )
            if key == EDGE_READS_SCHEMA and not data.get("schema_version"):
                raise ConfigError(
                    "reads_schema edge must declare schema_version",
                    dependent=dependent,
                    source=source,
                )
            if key == EDGE_COMPATIBLE_WITH and not data.get("compatible_versions"):
                raise ConfigError(
                    "compatible_with edge must declare compatible_versions",
                    dependent=dependent,
                    source=source,
                )
            if key == EDGE_READS_SCHEMA and not graph.has_edge(dependent, source, key=EDGE_DEPENDS_ON):
                # A service that reads another's schema but is not recorded
                # as depending on it would be invisible to the blast-radius
                # traversal -- a silent hole in a safety check.
                raise ConfigError(
                    "a reads_schema edge requires a matching depends_on edge",
                    dependent=dependent,
                    source=source,
                )

    # -- introspection ----------------------------------------------------

    @property
    def graph(self) -> nx.MultiDiGraph:
        return self._graph

    @property
    def node_count(self) -> int:
        return self._graph.number_of_nodes()

    @property
    def edge_count(self) -> int:
        return self._graph.number_of_edges()

    def __contains__(self, node: object) -> bool:
        return node in self._graph

    def nodes(self) -> tuple[str, ...]:
        return tuple(sorted(self._graph.nodes))

    def direct_dependents(self, node: str) -> tuple[str, ...]:
        return self._reverse_depends_on.get(node, ())

    # -- traversal --------------------------------------------------------

    def blast_radius(self, node: str, *, max_depth: Optional[int] = None) -> tuple[DependencyPath, ...]:
        """Breadth-first search over reversed ``depends_on`` edges.

        Returns one shortest path per affected node, nearest first. Explicit
        rather than delegating to ``nx.descendants`` because the *path* is
        part of the product: the intervention has to be able to say which
        chain of dependencies makes a node a casualty.

        Cycle-safe: a visited set bounds the walk, so a dependency loop in
        the fixture degrades to "each node reported once" instead of hanging
        the risk evaluation.
        """
        if node not in self._graph:
            return ()

        predecessor: dict[str, Optional[str]] = {node: None}
        depth: dict[str, int] = {node: 0}
        ordered: list[str] = []
        queue: deque[str] = deque([node])

        while queue:
            current = queue.popleft()
            if max_depth is not None and depth[current] >= max_depth:
                continue
            for dependent in self._reverse_depends_on.get(current, ()):
                if dependent in predecessor:
                    continue
                predecessor[dependent] = current
                depth[dependent] = depth[current] + 1
                ordered.append(dependent)
                queue.append(dependent)

        paths: list[DependencyPath] = []
        for affected in ordered:
            chain: list[str] = []
            cursor: Optional[str] = affected
            while cursor is not None:
                chain.append(cursor)
                cursor = predecessor[cursor]
            paths.append(DependencyPath(nodes=tuple(chain)))

        paths.sort(key=lambda path: (path.hops, path.dependent))
        return tuple(paths)

    # -- typed edge accessors --------------------------------------------

    def schema_requirement(self, dependent: str, source: str) -> Optional[str]:
        """The schema version ``dependent`` is currently wired against."""
        if not self._graph.has_edge(dependent, source, key=EDGE_READS_SCHEMA):
            return None
        data = self._graph.get_edge_data(dependent, source, key=EDGE_READS_SCHEMA) or {}
        version = data.get("schema_version")
        return str(version) if version is not None else None

    def declared_compatible_versions(self, dependent: str, source: str) -> frozenset[str]:
        """Versions ``dependent`` has explicitly declared it tolerates."""
        if not self._graph.has_edge(dependent, source, key=EDGE_COMPATIBLE_WITH):
            return frozenset()
        data = self._graph.get_edge_data(dependent, source, key=EDGE_COMPATIBLE_WITH) or {}
        return frozenset(str(v) for v in data.get("compatible_versions", ()))

    def tolerates(self, dependent: str, source: str, *, from_version: str, to_version: str) -> bool:
        """Is ``dependent`` declared compatible across this version change?"""
        declared = self.declared_compatible_versions(dependent, source)
        return from_version in declared and to_version in declared

    def current_schema_version(self, node: str) -> Optional[str]:
        if node not in self._graph:
            return None
        version = self._graph.nodes[node].get("schema_version")
        return str(version) if version is not None else None

    def rollback_target_version(self, node: str) -> Optional[str]:
        """Which schema version a rollback of ``node`` would land on.

        This is a property of the deployed system, not of the sentence that
        proposed the rollback. Nobody says "roll Core back to schema v2.3";
        they say "roll Core back to the last version", and the system is
        expected to know what that means. Reading it from the topology is
        what lets the intervention name a concrete version -- without it the
        blast-radius check could only ever report "unverifiable".
        """
        if node not in self._graph:
            return None
        version = self._graph.nodes[node].get("rollback_schema_version")
        return str(version) if version is not None else None

    def describe(self) -> dict:
        """Serialisable summary -- used by the UI's topology view and by the
        startup log, so the graph actually in memory is inspectable rather
        than assumed."""
        edges = []
        for dependent, source, key, data in self._graph.edges(keys=True, data=True):
            edge: dict = {"from": dependent, "to": source, "type": key}
            if key == EDGE_READS_SCHEMA:
                edge["schema_version"] = data.get("schema_version")
            if key == EDGE_COMPATIBLE_WITH:
                edge["compatible_versions"] = sorted(data.get("compatible_versions", ()))
            edges.append(edge)
        edges.sort(key=lambda e: (e["from"], e["to"], e["type"]))
        return {"nodes": list(self.nodes()), "edges": edges}


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------

#: The rollback target the golden demo proposes (SSOT §20 beat 4/5).
GOLDEN_DEMO_ROLLBACK_TARGET_SCHEMA = "v2.3"

#: The schema version core-db's dependents are currently wired against.
CURRENT_CORE_DB_SCHEMA = "v17"


def build_incident_topology() -> Topology:
    """The fixed 10-node incident graph used by the demo and the tests.

    Sized to SSOT §5 item 4 (~8-12 nodes) and shaped so that the golden demo
    and the held-out cases the Quality Standard asks for are both expressible:

    ``payment-api`` / ``auth-service``
        Read ``core-db``'s schema at v17. The rollback to v2.3 breaks both --
        the beat-6 catch.
    ``cache-layer``
        Depends on ``core-db`` but declares compatibility with both versions,
        so it must *not* be reported. This is the false-positive control.
    ``analytics-pipeline``
        Depends on ``core-db`` but reads no schema from it, so a schema
        rollback does not break it either.
    ``api-gateway`` / ``billing-service`` / ``user-service``
        Transitive dependents, reached at depth 2 -- they exercise the BFS
        beyond its first hop.
    ``search-index``
        A leaf nothing depends on: the "no blast radius at all" edge case.
    """
    graph = nx.MultiDiGraph()
    graph.add_nodes_from(
        [
            "payment-api",
            "auth-service",
            "billing-service",
            "notification-service",
            "api-gateway",
            "cache-layer",
            "analytics-pipeline",
            "search-index",
            "user-service",
        ]
    )
    # core-db is the only node in the fixture that owns a schema others read,
    # so it is the only one that needs a rollback target recorded.
    graph.add_node(
        "core-db",
        schema_version=CURRENT_CORE_DB_SCHEMA,
        rollback_schema_version=GOLDEN_DEMO_ROLLBACK_TARGET_SCHEMA,
    )

    depends_on: Sequence[tuple[str, str]] = (
        ("payment-api", "core-db"),
        ("auth-service", "core-db"),
        ("cache-layer", "core-db"),
        ("analytics-pipeline", "core-db"),
        ("billing-service", "payment-api"),
        ("notification-service", "billing-service"),
        ("api-gateway", "auth-service"),
        ("api-gateway", "payment-api"),
        ("search-index", "analytics-pipeline"),
        ("user-service", "auth-service"),
    )
    for dependent, source in depends_on:
        graph.add_edge(dependent, source, key=EDGE_DEPENDS_ON, edge_type=EDGE_DEPENDS_ON)

    reads_schema: Sequence[tuple[str, str, str]] = (
        ("payment-api", "core-db", CURRENT_CORE_DB_SCHEMA),
        ("auth-service", "core-db", CURRENT_CORE_DB_SCHEMA),
        ("cache-layer", "core-db", CURRENT_CORE_DB_SCHEMA),
    )
    for dependent, source, version in reads_schema:
        graph.add_edge(
            dependent,
            source,
            key=EDGE_READS_SCHEMA,
            edge_type=EDGE_READS_SCHEMA,
            schema_version=version,
        )

    # cache-layer reads the schema but is explicitly version-tolerant, so the
    # rollback does not break it. Without this edge the engine would report
    # three casualties instead of two and the demo line would be wrong.
    graph.add_edge(
        "cache-layer",
        "core-db",
        key=EDGE_COMPATIBLE_WITH,
        edge_type=EDGE_COMPATIBLE_WITH,
        compatible_versions=[CURRENT_CORE_DB_SCHEMA, GOLDEN_DEMO_ROLLBACK_TARGET_SCHEMA],
    )

    return Topology(graph)
