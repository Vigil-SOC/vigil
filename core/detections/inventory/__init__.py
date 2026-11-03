"""Rule inventory: every rule from every source, under a stable id, with versions.

Readers (``readers``) list a source's rules in one shape; ``sync`` writes what
changed into the ``rules`` and ``rule_versions`` tables. The daemon runs
``sync_all`` hourly; ``python -m core.detections.inventory`` runs it once.
"""
