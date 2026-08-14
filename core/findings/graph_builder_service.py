"""Graph Builder Service - Extracts entity relationships from findings."""

import logging
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)


class GraphBuilderService:
    """Service for building graph visualizations from security findings."""

    def __init__(self):
        self.entity_types = [
            "src_ip",
            "dst_ip",
            "hostname",
            "user",
            "query_name",
            "uri",
        ]

    def build_entity_graph(self, findings: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Build entity relationship graph from findings.

        Args:
            findings: List of finding dictionaries

        Returns:
            Graph data with nodes and links
        """
        nodes_dict: Dict[str, Dict[str, Any]] = {}
        links_dict: Dict[Tuple[str, str], Dict[str, Any]] = {}

        for finding in findings:
            entity_context = finding.get("entity_context", {})
            if not entity_context:
                continue

            finding_id = finding["finding_id"]
            severity = finding.get("severity", "unknown")

            # Extract entities from context
            entities = self._extract_entities(entity_context)

            # Add nodes
            for entity_id, entity_data in entities.items():
                if entity_id not in nodes_dict:
                    nodes_dict[entity_id] = {
                        "id": entity_id,
                        "label": entity_data["label"],
                        "type": entity_data["type"],
                        "severity": severity,
                        "findingCount": 1,
                        "metadata": {"findings": [finding_id]},
                    }
                else:
                    # Update existing node
                    nodes_dict[entity_id]["findingCount"] += 1
                    nodes_dict[entity_id]["metadata"]["findings"].append(finding_id)

                    # Update severity to highest
                    current_severity = nodes_dict[entity_id].get("severity", "low")
                    if self._severity_rank(severity) > self._severity_rank(
                        current_severity
                    ):
                        nodes_dict[entity_id]["severity"] = severity

            # Create links between entities in the same finding
            entity_list = list(entities.keys())
            for i in range(len(entity_list)):
                for j in range(i + 1, len(entity_list)):
                    source = entity_list[i]
                    target = entity_list[j]

                    # Ensure consistent ordering
                    if source > target:
                        source, target = target, source

                    link_key = (source, target)

                    if link_key not in links_dict:
                        links_dict[link_key] = {
                            "source": source,
                            "target": target,
                            "value": 1,
                            "techniques": self._extract_techniques(finding),
                        }
                    else:
                        links_dict[link_key]["value"] += 1
                        # Merge techniques
                        existing_techniques = set(
                            links_dict[link_key].get("techniques", [])
                        )
                        new_techniques = set(self._extract_techniques(finding))
                        links_dict[link_key]["techniques"] = list(
                            existing_techniques | new_techniques
                        )

        return {
            "nodes": list(nodes_dict.values()),
            "links": list(links_dict.values()),
            "metadata": {
                "total_findings": len(findings),
                "unique_entities": len(nodes_dict),
                "relationships": len(links_dict),
            },
        }

    def build_attack_path(
        self, findings: List[Dict[str, Any]], case: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Build attack path graph showing progression across systems.

        Args:
            findings: List of finding dictionaries
            case: Case dictionary

        Returns:
            Graph data with nodes and links representing attack progression
        """
        # Sort findings by timestamp
        sorted_findings = sorted(findings, key=lambda f: f.get("timestamp", ""))

        nodes_dict: Dict[str, Dict[str, Any]] = {}
        links_list: List[Dict[str, Any]] = []

        prev_entities: Set[str] = set()

        for idx, finding in enumerate(sorted_findings):
            entity_context = finding.get("entity_context", {})
            if not entity_context:
                continue

            finding_id = finding["finding_id"]
            severity = finding.get("severity", "unknown")
            techniques = self._extract_techniques(finding)

            # Extract entities
            entities = self._extract_entities(entity_context)
            current_entities = set(entities.keys())

            # Add nodes
            for entity_id, entity_data in entities.items():
                if entity_id not in nodes_dict:
                    nodes_dict[entity_id] = {
                        "id": entity_id,
                        "label": entity_data["label"],
                        "type": entity_data["type"],
                        "severity": severity,
                        "findingCount": 1,
                        "metadata": {
                            "findings": [finding_id],
                            "first_seen": finding.get("timestamp"),
                            "sequence": idx,
                        },
                    }
                else:
                    nodes_dict[entity_id]["findingCount"] += 1
                    nodes_dict[entity_id]["metadata"]["findings"].append(finding_id)

            # Create temporal links showing attack progression
            if prev_entities:
                # Link to entities from previous finding (showing progression)
                for prev_entity in prev_entities:
                    for curr_entity in current_entities:
                        if prev_entity != curr_entity:
                            links_list.append(
                                {
                                    "source": prev_entity,
                                    "target": curr_entity,
                                    "value": 1,
                                    "label": f"Step {idx}",
                                    "techniques": techniques,
                                }
                            )

            prev_entities = current_entities

        return {
            "nodes": list(nodes_dict.values()),
            "links": links_list,
            "metadata": {
                "case_id": case.get("case_id"),
                "case_title": case.get("title"),
                "total_findings": len(findings),
                "attack_steps": len(sorted_findings),
            },
        }

    def build_cluster_graph(
        self, findings: List[Dict[str, Any]], cluster_id: str
    ) -> Dict[str, Any]:
        """
        Build graph for a cluster of findings.

        Args:
            findings: List of finding dictionaries
            cluster_id: Cluster identifier

        Returns:
            Graph data for the cluster
        """
        # Add cluster node at center
        graph_data = self.build_entity_graph(findings)

        # Add cluster node
        cluster_node = {
            "id": f"cluster-{cluster_id}",
            "label": f"Cluster {cluster_id}",
            "type": "cluster",
            "findingCount": len(findings),
            "metadata": {
                "cluster_id": cluster_id,
                "findings": [f["finding_id"] for f in findings],
            },
        }

        graph_data["nodes"].append(cluster_node)

        # Link cluster to all entities
        entity_nodes = [n for n in graph_data["nodes"] if n["type"] != "cluster"]
        for node in entity_nodes[:10]:  # Limit to top 10 to avoid clutter
            graph_data["links"].append(
                {
                    "source": cluster_node["id"],
                    "target": node["id"],
                    "value": node.get("findingCount", 1),
                    "label": "member",
                }
            )

        graph_data["metadata"]["cluster_id"] = cluster_id

        return graph_data

    def build_technique_graph(
        self, findings: List[Dict[str, Any]], technique_id: str
    ) -> Dict[str, Any]:
        """
        Build graph for a specific MITRE ATT&CK technique.

        Args:
            findings: List of finding dictionaries
            technique_id: MITRE ATT&CK technique ID

        Returns:
            Graph data for the technique
        """
        graph_data = self.build_entity_graph(findings)

        # Add technique node at center
        technique_node = {
            "id": f"technique-{technique_id}",
            "label": technique_id,
            "type": "technique",
            "findingCount": len(findings),
            "metadata": {
                "technique_id": technique_id,
                "findings": [f["finding_id"] for f in findings],
            },
        }

        graph_data["nodes"].append(technique_node)

        # Link technique to entities involved
        for node in graph_data["nodes"]:
            if node["type"] != "technique":
                graph_data["links"].append(
                    {
                        "source": technique_node["id"],
                        "target": node["id"],
                        "value": node.get("findingCount", 1),
                        "label": "involves",
                        "techniques": [technique_id],
                    }
                )

        graph_data["metadata"]["technique_id"] = technique_id

        return graph_data

    def _extract_entities(
        self, entity_context: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Extract entities from entity context.

        Args:
            entity_context: Entity context dictionary

        Returns:
            Dictionary of entity_id -> entity_data
        """
        entities = {}

        # Source IP
        if "src_ip" in entity_context and entity_context["src_ip"]:
            entity_id = f"ip-{entity_context['src_ip']}"
            entities[entity_id] = {
                "label": entity_context["src_ip"],
                "type": "ip",
                "role": "source",
            }

        # Destination IP
        if "dst_ip" in entity_context and entity_context["dst_ip"]:
            entity_id = f"ip-{entity_context['dst_ip']}"
            entities[entity_id] = {
                "label": entity_context["dst_ip"],
                "type": "ip",
                "role": "destination",
            }

        # Hostname
        if "hostname" in entity_context and entity_context["hostname"]:
            entity_id = f"host-{entity_context['hostname']}"
            entities[entity_id] = {
                "label": entity_context["hostname"],
                "type": "hostname",
            }

        # User
        if "user" in entity_context and entity_context["user"]:
            entity_id = f"user-{entity_context['user']}"
            entities[entity_id] = {"label": entity_context["user"], "type": "user"}

        # Domain (from query_name or uri)
        if "query_name" in entity_context and entity_context["query_name"]:
            entity_id = f"domain-{entity_context['query_name']}"
            entities[entity_id] = {
                "label": entity_context["query_name"],
                "type": "domain",
            }

        # Port
        if "dst_port" in entity_context and entity_context["dst_port"]:
            entity_id = f"port-{entity_context['dst_port']}"
            entities[entity_id] = {
                "label": f"Port {entity_context['dst_port']}",
                "type": "port",
            }

        return entities

    def _extract_techniques(self, finding: Dict[str, Any]) -> List[str]:
        """
        Extract MITRE ATT&CK techniques from finding.

        Args:
            finding: Finding dictionary

        Returns:
            List of technique IDs
        """
        mitre_predictions = finding.get("mitre_predictions", {})
        return list(mitre_predictions.keys())

    def _severity_rank(self, severity: str) -> int:
        """
        Get numeric rank for severity.

        Args:
            severity: Severity string

        Returns:
            Numeric rank (higher = more severe)
        """
        severity_map = {"critical": 4, "high": 3, "medium": 2, "low": 1, "unknown": 0}
        return severity_map.get(severity.lower() if severity else "unknown", 0)
