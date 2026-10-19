#!/usr/bin/env bash
# Fetch a small slice of the detection rule repos for the security-detections
# live check: Sigma (rules/ only) and the KQL hunting queries. These are the
# paths mcp-config.json already points at under $HOME/security-detections, so
# no env needs handing to the tests. Splunk ESCU and Elastic are skipped: they
# are the large clones and add no connector coverage (same indexer).
set -euo pipefail

DIR="${SECURITY_DETECTIONS_DIR:-$HOME/security-detections}"
mkdir -p "$DIR"
cd "$DIR"

if [ ! -d sigma/rules ]; then
  rm -rf sigma
  git clone --quiet --depth 1 --filter=blob:none --sparse https://github.com/SigmaHQ/sigma.git sigma
  git -C sigma sparse-checkout set rules
fi
if [ ! -d Hunting-Queries-Detection-Rules ]; then
  git clone --quiet --depth 1 https://github.com/Bert-JanP/Hunting-Queries-Detection-Rules.git Hunting-Queries-Detection-Rules
fi

echo "sigma rules:  $(find sigma/rules -name '*.yml' | wc -l)"
echo "kql files:    $(find Hunting-Queries-Detection-Rules -type f | wc -l)"
