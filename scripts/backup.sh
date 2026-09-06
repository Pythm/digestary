#!/usr/bin/env bash
# Backup the Digestary CouchDB data volume to a dated tarball.
# Usage:  ./scripts/backup.sh [output-dir]
#   default output-dir: ./backups
set -euo pipefail

OUTDIR="${1:-./backups}"
VOLUME="digestary-data"
DEST="$OUTDIR/couchdb-$(date +%Y%m%d-%H%M%S).tar.gz"

mkdir -p "$OUTDIR"

echo "Backing up CouchDB volume '$VOLUME' -> $DEST"
docker run --rm \
     -v "$VOLUME:/data:ro" \
     -v "$OUTDIR:/backup:rw" \
   alpine sh -c "tar czf /backup/$(basename "$DEST") -C /data ."

echo "Done: $DEST"
