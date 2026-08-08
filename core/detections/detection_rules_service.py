"""
Detection Rules Service - Manages detection rule sources (git repos, local directories).

Persists source metadata to ~/.vigil/detection_sources.json.
Provides CRUD operations, git pull updates, and builds env vars for Security-Detections-MCP.
"""

import json
import logging
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from collections import defaultdict
from core.config import vigil_path

logger = logging.getLogger(__name__)

# Default detection repos that match the existing setup
DEFAULT_SOURCES = [
    {
        "name": "Sigma Rules",
        "type": "git",
        "git_url": "https://github.com/SigmaHQ/sigma.git",
        "format": "sigma",
        "subdirectory": "rules",
        "clone_name": "sigma",
    },
    {
        "name": "Splunk ESCU",
        "type": "git",
        "git_url": "https://github.com/splunk/security_content.git",
        "format": "splunk",
        "subdirectory": "detections",
        "clone_name": "security_content",
        "story_subdirectory": "stories",
    },
    {
        "name": "Elastic Rules",
        "type": "git",
        "git_url": "https://github.com/elastic/detection-rules.git",
        "format": "elastic",
        "subdirectory": "rules",
        "clone_name": "detection-rules",
    },
    {
        "name": "KQL Queries",
        "type": "git",
        "git_url": "https://github.com/Bert-JanP/Hunting-Queries-Detection-Rules.git",
        "format": "kql",
        "subdirectory": "",
        "clone_name": "Hunting-Queries-Detection-Rules",
    },
]

# File extensions per format
FORMAT_EXTENSIONS = {
    "sigma": [".yml"],
    "splunk": [".yml"],
    "elastic": [".toml"],
    "kql": [".yaml", ".kql", ".md"],
    "auto": [".yml", ".yaml", ".toml", ".kql", ".md"],
}

# MCP env var name per format
FORMAT_ENV_VARS = {
    "sigma": "SIGMA_PATHS",
    "splunk": "SPLUNK_PATHS",
    "elastic": "ELASTIC_PATHS",
    "kql": "KQL_PATHS",
}


class DetectionRulesService:
    """Service for managing detection rule sources."""

    def __init__(self):
        self.config_path = vigil_path("detection_sources.json")
        self.write_path = vigil_path("detection_sources.json", write=True)
        self.base_dir = Path.home() / "security-detections"
        self.sources: List[Dict[str, Any]] = []
        self._load_config()

    def _load_config(self):
        """Load sources from config file, or seed defaults on first run."""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r") as f:
                    data = json.load(f)
                    self.sources = data.get("sources", [])
                    logger.info(f"Loaded {len(self.sources)} detection rule sources from config")
                    return
            except Exception as e:
                logger.error(f"Error loading detection sources config: {e}")

        # First run or corrupt config -- seed defaults
        logger.info("No detection sources config found, seeding defaults")
        self._seed_defaults()

    def _seed_defaults(self):
        """Seed default sources based on existing repos on disk."""
        self.sources = []
        for default in DEFAULT_SOURCES:
            clone_dir = self.base_dir / default["clone_name"]
            source = {
                "id": str(uuid.uuid4())[:8],
                "name": default["name"],
                "type": default["type"],
                "git_url": default["git_url"],
                "format": default["format"],
                "subdirectory": default.get("subdirectory", ""),
                "story_subdirectory": default.get("story_subdirectory", ""),
                "clone_name": default["clone_name"],
                "local_path": str(clone_dir),
                "rule_count": 0,
                "last_updated": None,
                "status": "unknown",
            }

            # Check if repo already exists on disk
            if clone_dir.exists():
                source["status"] = "ready"
                source["rule_count"] = self._count_rules(clone_dir, default["format"], default.get("subdirectory", ""))
                source["last_updated"] = datetime.now().isoformat()
                logger.info(f"Found existing repo: {default['name']} ({source['rule_count']} rules)")
            else:
                source["status"] = "not_cloned"
                logger.info(f"Default source not yet cloned: {default['name']}")

            self.sources.append(source)

        self._save_config()

    def _save_config(self):
        try:
            with open(self.write_path, "w") as f:
                json.dump({"sources": self.sources, "version": 1}, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving detection sources config: {e}")

    def _count_rules(self, base_path: Path, fmt: str, subdirectory: str = "") -> int:
        """Count rule files in a directory by format."""
        target = base_path / subdirectory if subdirectory else base_path
        if not target.exists():
            return 0

        extensions = FORMAT_EXTENSIONS.get(fmt, FORMAT_EXTENSIONS["auto"])
        count = 0
        for ext in extensions:
            count += len(list(target.rglob(f"*{ext}")))
        return count

    def _get_rules_path(self, source: Dict) -> str:
        """Get the actual rules path for a source (base + subdirectory)."""
        base = Path(source["local_path"])
        subdir = source.get("subdirectory", "")
        if subdir:
            return str(base / subdir)
        return str(base)

    def _get_story_path(self, source: Dict) -> Optional[str]:
        """Get the story path for a source (Splunk only)."""
        story_sub = source.get("story_subdirectory", "")
        if story_sub:
            return str(Path(source["local_path"]) / story_sub)
        return None

    # --- Public API ---

    def list_sources(self) -> List[Dict[str, Any]]:
        """Return all registered rule sources with metadata."""
        return self.sources

    def get_source(self, source_id: str) -> Optional[Dict[str, Any]]:
        """Get a single source by ID."""
        for source in self.sources:
            if source["id"] == source_id:
                return source
        return None

    def add_source(
        self,
        name: str,
        source_type: str,
        format: str,
        url: Optional[str] = None,
        path: Optional[str] = None,
        subdirectory: str = "",
        story_subdirectory: str = "",
    ) -> Dict[str, Any]:
        """
        Add a new detection rule source.

        Args:
            name: Display name for the source
            source_type: 'git' or 'local'
            format: 'sigma', 'splunk', 'elastic', 'kql', or 'auto'
            url: Git repository URL (required for type='git')
            path: Local directory path (required for type='local')
            subdirectory: Subdirectory within the repo/path containing rules
            story_subdirectory: Subdirectory for Splunk stories (optional)
        """
        source_id = str(uuid.uuid4())[:8]

        if source_type == "git":
            if not url:
                raise ValueError("Git URL is required for type 'git'")
            # Derive clone name from URL
            clone_name = url.rstrip("/").split("/")[-1].replace(".git", "")
            local_path = str(self.base_dir / clone_name)
        elif source_type == "local":
            if not path:
                raise ValueError("Path is required for type 'local'")
            local_path = path
            clone_name = Path(path).name
        else:
            raise ValueError(f"Invalid source type: {source_type}")

        source = {
            "id": source_id,
            "name": name,
            "type": source_type,
            "git_url": url or "",
            "format": format,
            "subdirectory": subdirectory,
            "story_subdirectory": story_subdirectory,
            "clone_name": clone_name,
            "local_path": local_path,
            "rule_count": 0,
            "last_updated": None,
            "status": "not_cloned" if source_type == "git" else "unknown",
        }

        # If git, clone the repo
        if source_type == "git":
            clone_dir = Path(local_path)
            if clone_dir.exists():
                # Already cloned, just scan
                source["status"] = "ready"
                source["rule_count"] = self._count_rules(clone_dir, format, subdirectory)
                source["last_updated"] = datetime.now().isoformat()
            else:
                try:
                    self._git_clone(url, local_path)
                    source["status"] = "ready"
                    source["rule_count"] = self._count_rules(clone_dir, format, subdirectory)
                    source["last_updated"] = datetime.now().isoformat()
                except Exception as e:
                    source["status"] = "error"
                    logger.error(f"Failed to clone {url}: {e}")
                    raise
        elif source_type == "local":
            local_dir = Path(local_path)
            if local_dir.exists():
                source["status"] = "ready"
                source["rule_count"] = self._count_rules(local_dir, format, subdirectory)
                source["last_updated"] = datetime.now().isoformat()
            else:
                source["status"] = "error"
                raise ValueError(f"Local path does not exist: {local_path}")

        self.sources.append(source)
        self._save_config()
        logger.info(f"Added source: {name} ({source['rule_count']} rules)")
        return source

    def remove_source(self, source_id: str, delete_files: bool = False) -> bool:
        """Remove a rule source by ID."""
        source = self.get_source(source_id)
        if not source:
            return False

        if delete_files and source["type"] == "git":
            import shutil
            clone_dir = Path(source["local_path"])
            if clone_dir.exists():
                try:
                    shutil.rmtree(clone_dir)
                    logger.info(f"Deleted cloned directory: {clone_dir}")
                except Exception as e:
                    logger.error(f"Failed to delete {clone_dir}: {e}")

        self.sources = [s for s in self.sources if s["id"] != source_id]
        self._save_config()
        logger.info(f"Removed source: {source['name']}")
        return True

    def update_source(self, source_id: str) -> Dict[str, Any]:
        """Update a single source (git pull or rescan)."""
        source = self.get_source(source_id)
        if not source:
            raise ValueError(f"Source not found: {source_id}")

        if source["type"] == "git":
            clone_dir = Path(source["local_path"])
            if not clone_dir.exists():
                # Not cloned yet, clone it
                self._git_clone(source["git_url"], source["local_path"])
            else:
                # Git pull
                self._git_pull(source["local_path"])

        # Rescan rule count
        source["rule_count"] = self._count_rules(
            Path(source["local_path"]), source["format"], source.get("subdirectory", "")
        )
        source["last_updated"] = datetime.now().isoformat()
        source["status"] = "ready"
        self._save_config()
        logger.info(f"Updated source: {source['name']} ({source['rule_count']} rules)")
        return source

    def update_all(self) -> List[Dict[str, Any]]:
        """Update all sources."""
        results = []
        for source in self.sources:
            try:
                updated = self.update_source(source["id"])
                results.append({"id": source["id"], "name": source["name"], "success": True, "rule_count": updated["rule_count"]})
            except Exception as e:
                results.append({"id": source["id"], "name": source["name"], "success": False, "error": str(e)})
                logger.error(f"Failed to update {source['name']}: {e}")
        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get aggregate detection rule statistics."""
        total = 0
        by_format = defaultdict(int)
        by_source = []

        for source in self.sources:
            count = source.get("rule_count", 0)
            total += count
            by_format[source["format"]] += count
            by_source.append({
                "name": source["name"],
                "format": source["format"],
                "count": count,
                "status": source.get("status", "unknown"),
            })

        return {
            "total_rules": total,
            "sources_count": len(self.sources),
            "by_format": dict(by_format),
            "sources": by_source,
        }

    def get_mcp_env_vars(self) -> Dict[str, str]:
        """
        Build environment variable dict for Security-Detections-MCP.

        Combines multiple paths per format with commas, matching the MCP server's
        expected SIGMA_PATHS, SPLUNK_PATHS, ELASTIC_PATHS, KQL_PATHS, STORY_PATHS format.
        """
        paths_by_format: Dict[str, List[str]] = defaultdict(list)
        story_paths: List[str] = []

        for source in self.sources:
            if source.get("status") != "ready":
                continue

            rules_path = self._get_rules_path(source)
            if Path(rules_path).exists():
                fmt = source["format"]
                if fmt in FORMAT_ENV_VARS:
                    paths_by_format[fmt].append(rules_path)

            # Collect story paths (Splunk)
            story_path = self._get_story_path(source)
            if story_path and Path(story_path).exists():
                story_paths.append(story_path)

        env_vars = {}
        for fmt, env_var in FORMAT_ENV_VARS.items():
            if paths_by_format[fmt]:
                env_vars[env_var] = ",".join(paths_by_format[fmt])

        if story_paths:
            env_vars["STORY_PATHS"] = ",".join(story_paths)

        return env_vars

    # --- Git operations ---

    def _git_clone(self, url: str, target_path: str):
        """Clone a git repository with shallow depth."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Cloning {url} to {target_path}...")
        result = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", url, target_path],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Git clone failed: {result.stderr.strip()}")
        logger.info(f"Successfully cloned {url}")

    def _git_pull(self, repo_path: str):
        """Pull latest changes in a git repository."""
        logger.info(f"Pulling updates in {repo_path}...")
        result = subprocess.run(
            ["git", "pull", "--quiet"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Git pull failed: {result.stderr.strip()}")
        logger.info(f"Successfully pulled updates in {repo_path}")


# Global singleton
_detection_rules_service: Optional[DetectionRulesService] = None


def get_detection_rules_service() -> DetectionRulesService:
    """Get or create the global DetectionRulesService instance."""
    global _detection_rules_service
    if _detection_rules_service is None:
        _detection_rules_service = DetectionRulesService()
    return _detection_rules_service
