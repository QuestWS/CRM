#!/usr/bin/env bash
#
# Quest CRM on a fresh Oracle Cloud Ubuntu 22.04/24.04 ARM instance.
#
#   sudo bash scripts/provision-oracle.sh [domain]
#
# With a domain, Caddy gets a real Let's Encrypt certificate for it. Without
# one, it serves plain HTTP on port 80 for testing - fine to look at, not fine
# to put a mailbox password into.
#
# Safe to re-run: an existing .env is never overwritten.
set -euo pipefail

DOMAIN="${1:-}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m !  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m !! %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo."

ARCH="$(uname -m)"
say "Architecture: $ARCH"
[[ "$ARCH" == "aarch64" || "$ARCH" == "x86_64" ]] \
  || die "Unexpected architecture $ARCH."

# --------------------------------------------------------------------------
say "Docker"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  usermod -aG docker ubuntu 2>/dev/null || true
else
  echo "already installed: $(docker --version)"
fi

# --------------------------------------------------------------------------
# The Oracle-specific one. Their Ubuntu images DROP everything but SSH, and
# opening the VCN security list alone does nothing at all. This is the step
# that costs people an afternoon.
say "Opening ports 80 and 443 in iptables"
apt-get install -y -qq iptables-persistent >/dev/null 2>&1 || true
for port in 80 443; do
  if iptables -C INPUT -m state --state NEW -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    echo "  $port already allowed"
  else
    # Insert above Oracle's catch-all REJECT rather than appending below it.
    iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
    echo "  $port opened"
  fi
done
netfilter-persistent save >/dev/null 2>&1 || warn "could not persist iptables rules"
warn "Also open 80 and 443 in the VCN Security List in the Oracle console."
warn "Both firewalls must allow it; neither is enough on its own."

# --------------------------------------------------------------------------
say "Configuration"
if [[ -f .env ]]; then
  warn ".env exists — leaving it alone."
else
  cp .env.example .env
  gen() { openssl rand -base64 32 | tr -d '/+=' | head -c 32; }

  # APP_SECRET signs sessions; ENCRYPTION_KEY encrypts stored mailbox
  # passwords. Losing ENCRYPTION_KEY means reconnecting every account by hand,
  # so it is worth keeping a copy of .env somewhere off this machine.
  for key in PG_DATABASE_PASSWORD APP_SECRET ENCRYPTION_KEY; do
    sed -i "s|^${key}=.*|${key}=$(gen)|" .env
  done

  IP="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
  if [[ -n "$DOMAIN" ]]; then
    sed -i "s|^SERVER_URL=.*|SERVER_URL=https://${DOMAIN}|" .env
  else
    sed -i "s|^SERVER_URL=.*|SERVER_URL=http://${IP}|" .env
  fi
  chmod 600 .env
  echo "  secrets generated, .env written (mode 600)"
fi

# --------------------------------------------------------------------------
say "Reverse proxy"
mkdir -p deploy
if [[ -n "$DOMAIN" ]]; then
  cat > deploy/Caddyfile <<CADDY
${DOMAIN} {
    encode zstd gzip
    # Twenty streams and uploads; the defaults are too tight for attachments.
    request_body {
        max_size 100MB
    }
    reverse_proxy server:3000
}
CADDY
  echo "  TLS for ${DOMAIN} via Let's Encrypt"
else
  cat > deploy/Caddyfile <<'CADDY'
:80 {
    encode zstd gzip
    request_body {
        max_size 100MB
    }
    reverse_proxy server:3000
}
CADDY
  warn "No domain given: serving plain HTTP. Do not enter a mailbox password"
  warn "until you re-run this with a domain and get a certificate."
fi

cat > docker-compose.override.yml <<'OVERRIDE'
# Written by provision-oracle.sh. Puts Caddy in front and stops Twenty's own
# port being reachable from outside.
services:
  caddy:
    image: caddy:2
    restart: always
    depends_on:
      - server
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./deploy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config

  server:
    ports: !override []

  assistant:
    ports: !override []

volumes:
  caddy-data:
  caddy-config:
OVERRIDE

# --------------------------------------------------------------------------
say "Starting"
docker compose pull -q
docker compose up -d

say "Waiting for Twenty (first boot runs migrations — a few minutes)"
ready=""
for _ in $(seq 1 90); do
  if docker compose exec -T server curl -fsS -o /dev/null http://localhost:3000/healthz 2>/dev/null; then
    ready=1; break
  fi
  sleep 10
done

IP="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
URL="$(grep '^SERVER_URL=' .env | cut -d= -f2-)"

echo
[[ -n "$ready" ]] && say "Up." || warn "Not answering yet: docker compose logs -f server"

cat <<EOM

  Twenty      ${URL}
  Public IP   ${IP}

  Next:
    1. Open it and create your workspace — the first account is the owner.
    2. Settings -> Accounts -> connect chris@questwatersports.com over IMAP.
       Host questwatersports.com, port 993, SSL. See docs/twenty-setup.md.
    3. Settings -> APIs -> new API key. Put it in .env as TWENTY_API_KEY,
       set ANTHROPIC_API_KEY too, then:  docker compose up -d assistant
    4. Check it:  docker compose exec assistant python -m crm.cli twenty-check

  If the site does not load, it is almost always the VCN Security List —
  ports 80 and 443 have to be open there as well as in iptables.

EOM
