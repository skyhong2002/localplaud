#!/usr/bin/env bash
#
# localplaud host bootstrap — installs Docker Engine + the compose plugin,
# and (with --gpu) the NVIDIA container toolkit. Idempotent: skips anything
# already installed. Never removes or overwrites existing configuration
# beyond `nvidia-ctk runtime configure` registering the nvidia runtime.
#
# Usage:
#   scripts/deploy/bootstrap.sh          # Docker + compose
#   scripts/deploy/bootstrap.sh --gpu    # ... plus NVIDIA container toolkit
#
# Supported: Debian/Ubuntu (apt), macOS (checks Docker Desktop, prints guidance).
set -euo pipefail

WANT_GPU=false
for arg in "$@"; do
  case "$arg" in
    --gpu) WANT_GPU=true ;;
    -h|--help)
      sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "error: unknown argument: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

log() { printf '==> %s\n' "$*"; }

# Run a command as root, using sudo only when needed.
as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "error: need root or sudo to run: $*" >&2
    exit 1
  fi
}

OS="$(uname -s)"
ARCH="$(uname -m)"
log "detected $OS on $ARCH"

# --------------------------------------------------------------------------- #
# macOS: Docker Desktop (or OrbStack etc.) must be installed manually.
# --------------------------------------------------------------------------- #
if [ "$OS" = "Darwin" ]; then
  if $WANT_GPU; then
    log "note: --gpu is a no-op on macOS — containers cannot use the Metal GPU."
    log "      For on-device ASR run 'localplaud run' natively (see README)."
  fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker with compose is already installed: $(docker --version)"
    if ! docker info >/dev/null 2>&1; then
      log "Docker is installed but the daemon isn't running — start Docker Desktop."
    fi
  else
    log "Docker Desktop not found. Install it with one of:"
    log "    brew install --cask docker"
    log "    https://www.docker.com/products/docker-desktop/"
    log "then start it once and re-run this script."
    exit 1
  fi
  log "done."
  exit 0
fi

if [ "$OS" != "Linux" ]; then
  echo "error: unsupported OS: $OS (only Linux and macOS are handled)" >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "error: this script only automates apt-based distros (Debian/Ubuntu)." >&2
  echo "       Install Docker manually: https://docs.docker.com/engine/install/" >&2
  exit 1
fi

. /etc/os-release  # sets ID (debian/ubuntu) and VERSION_CODENAME

# --------------------------------------------------------------------------- #
# Docker Engine + compose plugin (official Docker apt repository)
# --------------------------------------------------------------------------- #
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  log "Docker with compose is already installed: $(docker --version)"
else
  log "installing Docker Engine + compose plugin from download.docker.com"
  as_root apt-get update
  as_root apt-get install -y ca-certificates curl

  as_root install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \
      | as_root tee /etc/apt/keyrings/docker.asc >/dev/null
    as_root chmod a+r /etc/apt/keyrings/docker.asc
  fi

  if [ ! -f /etc/apt/sources.list.d/docker.list ]; then
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
      | as_root tee /etc/apt/sources.list.d/docker.list >/dev/null
  fi

  as_root apt-get update
  as_root apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  as_root systemctl enable --now docker
  log "installed: $(docker --version)"
fi

if [ "$(id -u)" -ne 0 ] && ! id -nG "$USER" | grep -qw docker; then
  log "adding $USER to the docker group (takes effect on next login)"
  as_root usermod -aG docker "$USER"
fi

# --------------------------------------------------------------------------- #
# NVIDIA container toolkit (--gpu)
# --------------------------------------------------------------------------- #
if $WANT_GPU; then
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    log "warning: nvidia-smi not found — install the NVIDIA driver first."
    log "         Continuing with the container toolkit anyway."
  fi

  if command -v nvidia-ctk >/dev/null 2>&1; then
    log "NVIDIA container toolkit already installed"
  else
    log "installing NVIDIA container toolkit"
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | as_root gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      | as_root tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
    as_root apt-get update
    as_root apt-get install -y nvidia-container-toolkit
  fi

  if docker info 2>/dev/null | grep -q nvidia; then
    log "nvidia runtime already configured in Docker"
  else
    log "registering the nvidia runtime with Docker"
    as_root nvidia-ctk runtime configure --runtime=docker
    as_root systemctl restart docker
  fi
fi

log "done. Next steps:"
log "  cp .env.example .env && cp config.example.toml config.toml   # then edit both"
log "  docker compose --profile <cpu|gpu|mac> up -d --build"
