#!/usr/bin/env bash
# Generate an SPDX + CycloneDX SBOM for the OzzGraph competition image (T31).
#
# Usage:
#   scripts/gen-sbom.sh [IMAGE] [OUT_DIR]
#
#   IMAGE    image to scan (default: ozzgraph:latest)
#   OUT_DIR  output directory (default: sbom/)
#
# Requirements:
#   - syft (https://github.com/anchore/syft) on PATH (or set SYFT_BIN)
#   - a locally built image (see docs/IMAGE_HARDENING.md for the build recipe)
#
# Outputs (under OUT_DIR):
#   ozzgraph.spdx.json  — SPDX 2.3 JSON
#   ozzgraph.cdx.json   — CycloneDX 1.5 JSON
#
# The SBOM is a supply-chain audit artifact: it lists every OS and Python
# package baked into the image. Optionally attach it to the image as an OCI
# artifact (requires cosign):
#   cosign attach sbom --sbom sbom/ozzgraph.spdx.json --type spdx ozzgraph:latest
#
# Fallback without syft: pip-audit against the locked runtime requirements
# (uv export --frozen --no-dev | pip-audit -r /dev/stdin).
set -euo pipefail

IMAGE="${1:-ozzgraph:latest}"
OUT_DIR="${2:-sbom}"
SYFT_BIN="${SYFT_BIN:-syft}"

if ! command -v "$SYFT_BIN" >/dev/null 2>&1; then
    echo "error: syft not found on PATH" >&2
    echo "  install: curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

# syft >= 1.x uses `syft scan`; older releases used `syft packages`.
if "$SYFT_BIN" scan --help >/dev/null 2>&1; then
    SCAN_CMD=("$SYFT_BIN" scan)
else
    SCAN_CMD=("$SYFT_BIN" packages)
fi

echo "scanning ${IMAGE} with ${SCAN_CMD[*]} ..."

"${SCAN_CMD[@]}" "$IMAGE" -o spdx-json --file "${OUT_DIR}/ozzgraph.spdx.json"
"${SCAN_CMD[@]}" "$IMAGE" -o cyclonedx-json --file "${OUT_DIR}/ozzgraph.cdx.json"

echo "SBOM written:"
ls -lh "${OUT_DIR}/ozzgraph.spdx.json" "${OUT_DIR}/ozzgraph.cdx.json"

# Quick sanity summary from the SPDX document (best effort).
if command -v python3 >/dev/null 2>&1; then
    python3 - "${OUT_DIR}/ozzgraph.spdx.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
print(
    "packages: "
    f"{len(doc.get('packages', []))}  "
    f"(spdxVersion {doc.get('spdxVersion')}, "
    f"created {doc.get('creationInfo', {}).get('created')})"
)
PY
fi
