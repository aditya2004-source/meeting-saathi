#!/usr/bin/env bash
# Daily production backup for the Docker deployment: runs the existing
# scripts/backup.py *inside* the app container (where DB_PATH/BASE_STORAGE_DIR
# actually point at the persistent /data volume -- see docker-compose.yml's
# environment: block; running backup.py on the bare host instead would
# silently back up nothing real), then encrypts the newest snapshot into a
# single .age file safe to copy off this VPS.
#
# Encryption uses the age recipient already generated in an earlier session
# step (~/.config/meeting-saathi-backup/recipient.txt on this VPS) -- the
# matching PRIVATE key lives only on the founder's own machine and is never
# present here, so even a fully compromised VPS cannot decrypt past backups.
#
# Intended to run daily via the `deploy` user's crontab on the VPS host (not
# inside Docker), since `age` is installed on the host, not in the app image.
# The plaintext backups/<timestamp>/ directories that scripts/backup.py
# writes stay on the VPS too (SSH-key-only access, same as everything else
# here) purely so a fast local restore never needs the off-machine private
# key; the .age file is what's safe to copy anywhere less trusted.
#
# Usage (from the project root on the VPS):
#   ./scripts/vps_backup_and_encrypt.sh
set -euo pipefail
cd "$(dirname "$0")/.."

RECIPIENT_FILE="$HOME/.config/meeting-saathi-backup/recipient.txt"
KEEP=7

docker compose exec -T app python3 scripts/backup.py --keep "$KEEP"

if [ ! -f "$RECIPIENT_FILE" ]; then
    echo "vps_backup_and_encrypt.sh: no age recipient at $RECIPIENT_FILE -- skipping encryption step, plaintext backup only." >&2
    exit 0
fi

LATEST=$(ls -1 backups | sort | tail -n 1)
if [ -z "$LATEST" ]; then
    echo "vps_backup_and_encrypt.sh: no backup snapshot found under backups/ -- nothing to encrypt." >&2
    exit 1
fi

TMP_TAR="$(mktemp --suffix=.tar.gz)"
tar -C backups -czf "$TMP_TAR" "$LATEST"
age -r "$(cat "$RECIPIENT_FILE")" -o "backups/${LATEST}.tar.gz.age" "$TMP_TAR"
shred -u "$TMP_TAR"

# Prune old encrypted copies beyond retention, matching backup.py's own --keep.
ls -1t backups/*.tar.gz.age 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "vps_backup_and_encrypt.sh: wrote backups/${LATEST}.tar.gz.age"
