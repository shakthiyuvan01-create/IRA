"""
memory/learning_graph.py — Learning graph for IRA.

Tracks connections between memory entries and skills/actions to visualize
what IRA knows and how knowledge is linked.

Provides:
  - SkillNode: tracks a skill/action with metadata
  - LearningGraph: builds connectivity map between memory and skills
  - Text rendering for system prompt injection

Designed after Hermes Agent's agent/learning_graph.py by Nous Research.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SkillNode:
    """A skill or action that IRA can perform."""
    name: str
    category: str = "general"
    source: str = "builtin"
    use_count: int = 0
    state: str = "active"
    pinned: bool = False
    related: List[str] = field(default_factory=list)
    description: str = ""


def _tokenize(text: str) -> set:
    """Tokenize text into lowercase words for overlap comparison."""
    words = re.findall(r"[a-zA-Z]+", text.lower())
    # Filter common stop words
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "shall", "can",
        "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "as", "into", "through", "during", "before", "after", "above",
        "below", "between", "out", "off", "over", "under", "again",
        "further", "then", "once", "here", "there", "when", "where",
        "why", "how", "all", "each", "every", "both", "few", "more",
        "most", "other", "some", "such", "no", "nor", "not", "only",
        "own", "same", "so", "than", "too", "very", "just", "because",
        "and", "but", "or", "if", "while", "about", "up", "down",
    }
    return {w for w in words if w not in stop_words and len(w) > 2}


def _overlap(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity between two texts."""
    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


class LearningGraph:
    """
    Builds and maintains a graph of what IRA knows and can do.

    Nodes:
      - Memory entries (from MEMORY.md and USER.md)
      - Skills/actions (from the action system)

    Edges:
      - Lexical overlap between memory entries and skills
    """

    def __init__(self, memory_dir: Optional[Path] = None):
        if memory_dir is None:
            import sys
            base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
            memory_dir = base / "memory"
        self._memory_dir = memory_dir
        self._skill_nodes: Dict[str, SkillNode] = {}
        self._edges: List[Dict[str, Any]] = []

    # -- Skill management ------------------------------------------------------

    def add_skill(self, node: SkillNode) -> None:
        """Register or update a skill node."""
        self._skill_nodes[node.name] = node

    def get_skill(self, name: str) -> Optional[SkillNode]:
        """Get a skill node by name."""
        return self._skill_nodes.get(name)

    def increment_use(self, name: str) -> None:
        """Increment the use counter for a skill."""
        node = self._skill_nodes.get(name)
        if node:
            node.use_count += 1

    def remove_skill(self, name: str) -> bool:
        """Remove a skill node."""
        return self._skill_nodes.pop(name, None) is not None

    def get_skills(self, category: Optional[str] = None) -> List[SkillNode]:
        """Get all skills, optionally filtered by category."""
        if category:
            return [n for n in self._skill_nodes.values() if n.category == category]
        return list(self._skill_nodes.values())

    # -- Graph building --------------------------------------------------------

    def build(self, memory_entries: List[str], user_entries: List[str]) -> None:
        """
        Build the full graph from memory entries and registered skills.
        Computes lexical overlap edges.
        """
        self._edges.clear()

        all_entries = [("[memory]", e) for e in memory_entries] + [("[user]", e) for e in user_entries]

        # Memory-to-memory edges
        for i in range(len(all_entries)):
            for j in range(i + 1, len(all_entries)):
                source_tag, source_text = all_entries[i]
                target_tag, target_text = all_entries[j]
                similarity = _overlap(source_text, target_text)
                if similarity >= 0.15:  # Threshold for meaningful connection
                    self._edges.append({
                        "source": f"{source_tag}: {source_text[:40]}...",
                        "target": f"{target_tag}: {target_text[:40]}...",
                        "type": "lexical_overlap",
                        "weight": round(similarity, 3),
                    })

        # Memory-to-skill edges
        for tag, entry_text in all_entries:
            for skill_name, skill_node in self._skill_nodes.items():
                similarity = _overlap(entry_text, skill_name + " " + skill_node.description)
                if similarity >= 0.15:
                    self._edges.append({
                        "source": f"{tag}: {entry_text[:40]}...",
                        "target": f"[skill] {skill_name}",
                        "type": "memory_skill",
                        "weight": round(similarity, 3),
                    })

        # Deduplicate edges (keep highest weight)
        self._edges.sort(key=lambda e: e["weight"], reverse=True)
        seen = set()
        deduped = []
        for edge in self._edges:
            key = (edge["source"], edge["target"])
            if key not in seen:
                seen.add(key)
                deduped.append(edge)
        self._edges = deduped

    # -- Rendering -------------------------------------------------------------

    def render_text(self, max_skills: int = 20, max_edges: int = 30) -> str:
        """Render the graph as a text block for system prompt injection."""
        parts = []

        if self._skill_nodes:
            parts.append("╔═══════════════════════════════════════╗")
            parts.append("║         SKILLS & CAPABILITIES         ║")
            parts.append("╚═══════════════════════════════════════╝")
            sorted_skills = sorted(
                self._skill_nodes.values(),
                key=lambda n: n.use_count,
                reverse=True,
            )
            for node in sorted_skills[:max_skills]:
                pin = " 📌" if node.pinned else ""
                parts.append(f"  • {node.name}{pin} [{node.category}] used {node.use_count}x")
            if len(sorted_skills) > max_skills:
                parts.append(f"  ... and {len(sorted_skills) - max_skills} more")

        if self._edges:
            parts.append(f"\n[Knowledge connections: {len(self._edges)} links]")
            for edge in self._edges[:max_edges]:
                arrow = "◀──" if edge["type"] == "lexical_overlap" else "──◆──"
                parts.append(f"  {edge['source']} {arrow} {edge['target']}")

        if not parts:
            return ""

        return "\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        """Export the graph as a dict for serialization."""
        return {
            "skills": [
                {
                    "name": n.name,
                    "category": n.category,
                    "source": n.source,
                    "use_count": n.use_count,
                    "state": n.state,
                    "pinned": n.pinned,
                    "related": n.related,
                    "description": n.description,
                }
                for n in self._skill_nodes.values()
            ],
            "edges": self._edges[:100],  # Cap edges for serialization
        }

    def to_json(self) -> str:
        """Export the graph as JSON."""
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    # -- Default skills -------------------------------------------------------

    def load_default_skills(self) -> None:
        """Load built-in action skills into the graph."""
        default_skills = [
            SkillNode("open_app", "system", "builtin", description="Launch applications on the computer"),
            SkillNode("web_search", "search", "builtin", description="Search the web for information"),
            SkillNode("weather_report", "information", "builtin", description="Get weather reports for any city"),
            SkillNode("send_message", "communication", "builtin", description="Send messages via messaging platforms"),
            SkillNode("reminder", "system", "builtin", description="Set timed reminders"),
            SkillNode("memory", "memory", "builtin", description="Save and retrieve persistent facts"),
            SkillNode("session_search", "memory", "builtin", description="Search past conversations"),
            SkillNode("youtube_video", "media", "builtin", description="Play and search YouTube videos"),
            SkillNode("generate_images", "creative", "builtin", description="Generate AI images from descriptions"),
            SkillNode("system_status", "system", "builtin", description="Check system metrics (CPU, RAM, GPU)"),
            SkillNode("screen_process", "vision", "builtin", description="Process screen content visually"),
            SkillNode("file_controller", "system", "builtin", description="Manage files and directories"),
            SkillNode("clipboard", "system", "builtin", description="Access clipboard history"),
            SkillNode("run_command", "system", "builtin", description="Execute terminal commands"),
            SkillNode("browser_control", "system", "builtin", description="Control web browser"),
            SkillNode("computer_settings", "system", "builtin", description="Adjust system settings"),
            SkillNode("trigger_heartbeat", "memory", "builtin", description="Trigger memory maintenance"),
            SkillNode("trigger_eval", "system", "builtin", description="Run quality evaluation"),
        ]
        for skill in default_skills:
            self._skill_nodes[skill.name] = skill


# -- Singleton helper -------------------------------------------------------

_graph_instance: Optional[LearningGraph] = None


def get_graph() -> LearningGraph:
    """Get or create the LearningGraph singleton."""
    global _graph_instance
    if _graph_instance is None:
        _graph_instance = LearningGraph()
        _graph_instance.load_default_skills()
    return _graph_instance


def rebuild(memory_entries: List[str], user_entries: List[str]) -> LearningGraph:
    """Rebuild the graph from memory entries."""
    graph = get_graph()
    graph.build(memory_entries, user_entries)
    return graph


def render_for_prompt() -> str:
    """Render the learning graph for system prompt injection."""
    return get_graph().render_text()