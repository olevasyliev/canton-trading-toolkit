#!/usr/bin/env bash
# Deploy Canton Venues from a git ref, never from the working tree.
#   examples/canton_venues/deploy/deploy.sh [ref]     (default: HEAD)
# No secrets: every source the collector reads is public.
set -euo pipefail

SERVER="${VENUES_SERVER:-root@46.225.216.13}"
HOST="cantonvenues.com"
OLD_HOST="canton.46-225-216-13.nip.io"
REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
SHA="$(git -C "$REPO" rev-parse --short "${1:-HEAD}")"

echo "▸ release $SHA"
git -C "$REPO" archive --format=tar "$SHA" | ssh "$SERVER" "
  set -e
  mkdir -p /opt/canton-venues/releases/$SHA /var/www/canton-venues
  tar xf - -C /opt/canton-venues/releases/$SHA"

ssh "$SERVER" "
  set -e
  cd /opt/canton-venues
  test -d .venv || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade ./releases/$SHA
  ln -sfn releases/$SHA current
  install -m 644 current/examples/canton_venues/site/index.html current/examples/canton_venues/site/*.png /var/www/canton-venues/
  install -m 644 current/examples/canton_venues/deploy/canton-venues.service /etc/systemd/system/canton-venues.service
  # (re)install the vhost only when the repo's conf-version changes; certbot then adds TLS
  if ! grep -q \"\$(head -1 current/examples/canton_venues/deploy/nginx.conf)\" /etc/nginx/sites-available/canton-venues 2>/dev/null; then
    install -m 644 current/examples/canton_venues/deploy/nginx.conf /etc/nginx/sites-available/canton-venues
    ln -sfn /etc/nginx/sites-available/canton-venues /etc/nginx/sites-enabled/canton-venues
    nginx -t && systemctl reload nginx
    certbot --nginx -d $HOST -d www.$HOST --non-interactive --agree-tos --register-unsafely-without-email --redirect
    certbot --nginx -d $OLD_HOST --non-interactive --agree-tos --register-unsafely-without-email --reinstall --redirect
  fi
  systemctl daemon-reload
  systemctl enable canton-venues >/dev/null 2>&1
  systemctl restart canton-venues
  sleep 3
  systemctl is-active canton-venues
  ls -1t releases | tail -n +6 | sed 's|^|releases/|' | xargs -r rm -r --"

echo "✓ https://$HOST"
