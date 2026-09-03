#!/usr/bin/env bash
#
# Set up Quest CRM on a fresh Ubuntu 22.04/24.04 server.
#
#   bash scripts/provision-vps.sh
#
# Installs Docker, generates strong passwords, writes .env, and starts
# EspoCRM plus the assistant. Safe to re-run: it will not overwrite an
# existing .env.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m !  %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m !! %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run this as root (or with sudo)."

say "Installing Docker"
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
else
  echo "Docker already present: $(docker --version)"
fi

say "Writing configuration"
if [[ -f .env ]]; then
  warn ".env already exists — leaving it alone."
else
  cp .env.example .env
  # openssl rather than $RANDOM: these guard a database holding customer mail.
  for key in ESPO_ADMIN_PASSWORD ESPO_DB_PASSWORD ESPO_DB_ROOT_PASSWORD; do
    secret="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
    sed -i "s|^${key}=.*|${key}=${secret}|" .env
  done
  chmod 600 .env
  echo "Generated passwords into .env (mode 600)."
fi

say "Firewall"
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null 2>&1 || true
  ufw allow 8080/tcp >/dev/null 2>&1 || true
  ufw --force enable >/dev/null 2>&1 || true
  echo "ufw: SSH and 8080 open."
  warn "8080 is HTTP. Put a reverse proxy with TLS in front before this holds"
  warn "real customer data — a mailbox password crosses that connection."
else
  warn "ufw not installed; no firewall rules applied."
fi

say "Starting EspoCRM"
docker compose pull -q
docker compose up -d

echo
say "Waiting for EspoCRM to answer"
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://localhost:${ESPO_PORT:-8080}/" 2>/dev/null; then
    ready=1; break
  fi
  sleep 5
done

IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"

echo
if [[ -n "${ready:-}" ]]; then
  say "Up."
else
  warn "EspoCRM has not answered yet. First boot builds the database and can"
  warn "take a few minutes. Watch it with: docker compose logs -f espocrm"
fi

cat <<EOM

  EspoCRM      http://${IP}:${ESPO_PORT:-8080}
  Username     admin
  Password     $(grep '^ESPO_ADMIN_PASSWORD=' .env | cut -d= -f2-)

  Next:
    1. Log in and change that password.
    2. Put TLS in front before entering your mailbox password.
    3. Set ANTHROPIC_API_KEY in .env, then: docker compose up -d assistant
    4. Follow docs/espocrm-setup.md to connect the mailbox.

  Useful:
    docker compose ps
    docker compose logs -f espocrm
    docker compose exec assistant python -m crm.cli espo-check

EOM
