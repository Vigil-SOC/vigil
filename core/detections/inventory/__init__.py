"""Detection rules from every configured repo, under a stable id, with versions.

``readers`` parses a source's files into ``RuleRecord``s; ``sync`` writes what
changed into ``detection_rules`` and ``detection_rule_versions``; ``catalog``
is what the agent detection tools read. The daemon syncs hourly, and the
detection-rules API syncs after a source changes.
"""
