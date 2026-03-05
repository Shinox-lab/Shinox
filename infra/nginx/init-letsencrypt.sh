#!/bin/bash
# ============================================================
# Bootstrap Let's Encrypt certificates for Shinox
#
# Run this ONCE on the server before deploying the stack:
#   chmod +x init-letsencrypt.sh && ./init-letsencrypt.sh
#
# Prerequisites:
#   - Docker running on the host
#   - DNS A records for all subdomains pointing to this server
# ============================================================

set -euo pipefail

DOMAINS=(dashboard.aidebate.site grafana.aidebate.site console.aidebate.site api.aidebate.site registry.aidebate.site)
EMAIL="${CERTBOT_EMAIL:-admin@aidebate.site}"
CERT_PATH="./certbot/conf"
WEBROOT_PATH="./certbot/www"
STAGING=${STAGING:-0}  # Set to 1 to use Let's Encrypt staging (for testing)

echo "==> Creating directories..."
mkdir -p "$CERT_PATH" "$WEBROOT_PATH"

# ── Step 1: Download recommended TLS parameters ──────────────
if [ ! -e "$CERT_PATH/options-ssl-nginx.conf" ]; then
  echo "==> Downloading recommended TLS parameters..."
  curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf \
    > "$CERT_PATH/options-ssl-nginx.conf"
  curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot/certbot/ssl-dhparams.pem \
    > "$CERT_PATH/ssl-dhparams.pem"
fi

# ── Step 2: Create dummy certs so Nginx can start ────────────
DUMMY_CERT_PATH="$CERT_PATH/live/aidebate.site"
echo "==> Creating dummy certificate for aidebate.site..."
mkdir -p "$DUMMY_CERT_PATH"
openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
  -keyout "$DUMMY_CERT_PATH/privkey.pem" \
  -out "$DUMMY_CERT_PATH/fullchain.pem" \
  -subj '/CN=localhost' 2>/dev/null
echo "==> Dummy certificate created."

# ── Step 3: Start Nginx with dummy certs ─────────────────────
echo "==> Starting Nginx..."
docker compose -f compose.yaml up -d nginx
echo "==> Waiting for Nginx to be ready..."
sleep 5

# ── Step 4: Remove dummy certs ───────────────────────────────
echo "==> Removing dummy certificate..."
rm -rf "$DUMMY_CERT_PATH"

# ── Step 5: Request real certificates ────────────────────────
echo "==> Requesting Let's Encrypt certificate..."

DOMAIN_ARGS=""
for domain in "${DOMAINS[@]}"; do
  DOMAIN_ARGS="$DOMAIN_ARGS -d $domain"
done

STAGING_ARG=""
if [ "$STAGING" = "1" ]; then
  STAGING_ARG="--staging"
  echo "   (Using Let's Encrypt STAGING environment)"
fi

docker compose -f compose.yaml run --rm certbot certonly \
  --webroot \
  --webroot-path=/var/www/certbot \
  --email "$EMAIL" \
  --agree-tos \
  --no-eff-email \
  --force-renewal \
  $STAGING_ARG \
  $DOMAIN_ARGS

# ── Step 6: Reload Nginx with real certs ─────────────────────
echo "==> Reloading Nginx..."
docker compose -f compose.yaml exec nginx nginx -s reload

echo ""
echo "==> Done! Certificates provisioned for:"
for domain in "${DOMAINS[@]}"; do
  echo "    https://$domain"
done
echo ""
echo "Certificates will auto-renew via the certbot service."
