from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List


def project_workspace_graph(
    rows: Iterable[Dict[str, Any]],
    query: str = "",
    depth: int = 2,
    node_limit: int = 200,
) -> Dict[str, Any]:
    """Build a bounded entity-document graph and apply an in-memory ego traversal."""
    nodes: Dict[str, Dict[str, Any]] = {}
    links: Dict[str, Dict[str, Any]] = {}
    adjacency: Dict[str, set[str]] = defaultdict(set)
    entity_documents: Dict[str, set[str]] = defaultdict(set)

    for row in rows:
        document_id = f"document:{row['document_id']}"
        normalized_name = str(row["normalized_name"])
        entity_id = f"entity:{normalized_name}"
        frequency = max(1, int(row.get("frequency") or 1))

        nodes.setdefault(
            document_id,
            {
                "id": document_id,
                "record_id": str(row["document_id"]),
                "name": str(row.get("document_title") or "Untitled document"),
                "type": "DOCUMENT",
                "mention_count": 0,
                "document_count": 1,
                "created_at": str(row.get("document_created_at") or ""),
            },
        )
        entity = nodes.setdefault(
            entity_id,
            {
                "id": entity_id,
                "record_id": normalized_name,
                "name": str(row.get("entity_name") or normalized_name),
                "type": str(row.get("entity_type") or "CONCEPT").upper(),
                "mention_count": 0,
                "document_count": 0,
                "created_at": "",
            },
        )
        entity["mention_count"] += frequency
        nodes[document_id]["mention_count"] += frequency
        entity_documents[entity_id].add(document_id)

        link_id = f"{document_id}|MENTIONS|{entity_id}"
        links[link_id] = {
            "id": link_id,
            "source": document_id,
            "target": entity_id,
            "type": "MENTIONS",
            "weight": frequency,
        }
        adjacency[document_id].add(entity_id)
        adjacency[entity_id].add(document_id)

    for entity_id, document_ids in entity_documents.items():
        nodes[entity_id]["document_count"] = len(document_ids)

    clean_query = query.strip().casefold()
    if clean_query:
        root_ids = {
            node_id
            for node_id, node in nodes.items()
            if clean_query in node["name"].casefold()
        }
        selected_ids: set[str] = set(root_ids)
        queue = deque((node_id, 0) for node_id in sorted(root_ids))
        max_edges = max(1, min(int(depth), 4)) * 2
        while queue:
            node_id, distance = queue.popleft()
            if distance >= max_edges:
                continue
            for neighbor_id in sorted(adjacency[node_id]):
                if neighbor_id in selected_ids:
                    continue
                selected_ids.add(neighbor_id)
                queue.append((neighbor_id, distance + 1))
    else:
        root_ids = set()
        selected_ids = set(nodes)

    def node_rank(node: Dict[str, Any]) -> tuple[int, int, int, str]:
        return (
            0 if node["id"] in root_ids else 1,
            0 if node["type"] != "DOCUMENT" else 1,
            -int(node["mention_count"]),
            node["name"].casefold(),
        )

    bounded_nodes = sorted(
        (nodes[node_id] for node_id in selected_ids),
        key=node_rank,
    )[:max(1, min(int(node_limit), 250))]
    bounded_ids = {node["id"] for node in bounded_nodes}
    bounded_links = [
        link
        for link in links.values()
        if link["source"] in bounded_ids and link["target"] in bounded_ids
    ]
    bounded_links.sort(key=lambda link: (-int(link["weight"]), link["id"]))

    return {
        "nodes": bounded_nodes,
        "links": bounded_links[:500],
        "query": query.strip(),
        "depth": max(1, min(int(depth), 4)),
        "truncated": len(selected_ids) > len(bounded_nodes) or len(bounded_links) > 500,
        "stats": {
            "nodes": len(bounded_nodes),
            "links": min(len(bounded_links), 500),
            "entities": sum(node["type"] != "DOCUMENT" for node in bounded_nodes),
            "documents": sum(node["type"] == "DOCUMENT" for node in bounded_nodes),
        },
    }
