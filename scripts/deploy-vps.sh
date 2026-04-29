#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DOMAIN:-folo-radar.zaishijizhidan.dpdns.org}"
APP_DIR="${APP_DIR:-/opt/folo-telegram-mvp}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run as root."
  exit 1
fi

echo "==> Domain: ${DOMAIN}"
echo "==> App dir: ${APP_DIR}"

if [[ ! -d "${APP_DIR}" ]]; then
  echo "App directory does not exist: ${APP_DIR}"
  echo "Upload this project to ${APP_DIR} first."
  exit 1
fi

cd "${APP_DIR}"

echo "==> Installing system packages"
apt update
apt install -y ca-certificates curl gnupg ufw debian-keyring debian-archive-keyring apt-transport-https

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  install -m 0755 -d /etc/apt/keyrings
  . /etc/os-release
  DOCKER_DISTRO="ubuntu"
  if [[ "${ID:-}" == "debian" ]]; then
    DOCKER_DISTRO="debian"
  fi
  curl -fsSL "https://download.docker.com/linux/${DOCKER_DISTRO}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${DOCKER_DISTRO} ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt update
  apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  echo "==> Docker already installed"
fi

if [[ ! -f .env ]]; then
  echo "==> Creating .env from .env.example"
  cp .env.example .env
  echo "Edit ${APP_DIR}/.env before using Folo webhook."
fi

echo "==> Starting app container"
docker compose up -d --build

echo "==> Checking local app health"
for i in {1..20}; do
  if curl -fsS http://127.0.0.1:8080/health >/tmp/folo-health.json; then
    cat /tmp/folo-health.json
    echo
    break
  fi
  sleep 2
done

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Installing Caddy"
  curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" > /etc/apt/sources.list.d/caddy-stable.list
  apt update
  apt install -y caddy
else
  echo "==> Caddy already installed"
fi

echo "==> Writing Caddyfile"
cat >/etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    reverse_proxy 127.0.0.1:8080
}
EOF

caddy fmt --overwrite /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl enable caddy
systemctl reload caddy || systemctl restart caddy

echo "==> Configuring firewall"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status

echo "==> Done"
echo "Test locally on VPS:"
echo "  curl http://127.0.0.1:8080/health"
echo "Test public HTTPS after DNS/Cloudflare is ready:"
echo "  curl https://${DOMAIN}/health"
