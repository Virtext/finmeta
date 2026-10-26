#!/bin/sh
set -e

if [ ! -f "$FINMETA_CONFIG" ]; then
    echo "finmeta: no config at $FINMETA_CONFIG -- copying the default template."
    echo "finmeta: edit it in place or via the web UI (dry_run stays true until you change it)."
    cp /app/config.template.yml "$FINMETA_CONFIG"
fi

exec "$@"
