#!/bin/sh
# Backend container entrypoint.
#
# Compose already gates this on the database being healthy, so there is no wait
# loop here — if the DB is unreachable, failing loudly is more useful than
# retrying quietly.
set -e

cd /app/backend

# Migrations run inside init_db() on startup; bootstrap.py calls it first, then
# seeds the demo accounts and the sample-database connection. Idempotent, so a
# restart resets demo passwords rather than duplicating anything.
if [ "${DBBUDDY_SKIP_BOOTSTRAP:-0}" != "1" ]; then
    echo "→ bootstrapping demo instance"
    python /app/docker/bootstrap.py
fi

echo "→ starting API"
exec "$@"
