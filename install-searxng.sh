#!/usr/bin/env bash
# install-searxng.sh: source-based SearXNG installation for Alpine Linux (OpenRC).
#
# Derived from the Arch Linux PKGBUILD "searxng-git". Differences from the PKGBUILD:
#   - the git checkout is the live installation (editable pip install, no wheel);
#   - the user clones the repository manually when prompted;
#   - systemd, sysusers and tmpfiles units are replaced by OpenRC and adduser;
#   - the checkout is never deleted by this script.
#
# Usage (as root):  ./install-searxng.sh
#
# Environment overrides (defaults in parentheses):
#   PREFIX        (/opt/searxng)         base directory
#   SRC_DIR       ($PREFIX/src)          git checkout location
#   VENV          ($PREFIX/venv)         Python virtual environment
#   CONF_DIR      (/etc/searxng)         configuration directory
#   SVC_USER      (searxng)              unprivileged service account
#   BIND_ADDRESS  (127.0.0.1)            listen address
#   PORT          (8888)                 listen port
#   REPO_URL      (https://github.com/searxng/searxng)
#   BRANCH        (master)
#   START_SERVICE (1)                    enable and start the services when 1
set -euo pipefail

PREFIX=${PREFIX:-/opt/searxng}
SRC_DIR=${SRC_DIR:-$PREFIX/src}
VENV=${VENV:-$PREFIX/venv}
CONF_DIR=${CONF_DIR:-/etc/searxng}
SVC_USER=${SVC_USER:-searxng}
BIND_ADDRESS=${BIND_ADDRESS:-127.0.0.1}
PORT=${PORT:-8888}
REPO_URL=${REPO_URL:-https://github.com/searxng/searxng}
BRANCH=${BRANCH:-master}
START_SERVICE=${START_SERVICE:-1}

STATE_DIR=/var/lib/searxng
LOG_DIR=/var/log/searxng
INVOKER=${SUDO_USER:-${DOAS_USER:-}}

# Repositories owned by the invoking user are trusted for this process only.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'

msg()  { printf '\n==> %s\n' "$*"; }
warn() { printf '\n==> WARNING: %s\n' "$*" >&2; }
die()  { printf '\n==> ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "root privileges are required (doas or sudo)."
command -v apk >/dev/null 2>&1 || die "apk not found; Alpine Linux is required."

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
# build-base, python3-dev and the *-dev libraries cover wheels that lack a musllinux
# build; valkey replaces the PKGBUILD runtime dependency of the same name.
msg "Installing packages from the apk repositories"
apk update
apk add bash git curl openssl valkey \
  python3 python3-dev py3-pip \
  build-base libxml2-dev libxslt-dev libffi-dev openssl-dev

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  warn "Python $(python3 -V 2>&1) is older than 3.10; SearXNG may refuse to run."
fi

# ---------------------------------------------------------------------------
# 2. Source checkout (cloned manually by the user)
# ---------------------------------------------------------------------------
mkdir -p "$PREFIX"
if [ -n "$INVOKER" ]; then chown "$INVOKER" "$PREFIX"; fi

if [ -d "$SRC_DIR/.git" ]; then
  msg "Using existing checkout: $SRC_DIR"
else
  while [ ! -d "$SRC_DIR/.git" ]; do
    printf '\nThe SearXNG source repository is required at: %s\n' "$SRC_DIR"
    printf 'Clone it as the regular user (not root), for example:\n\n'
    printf '    git clone --branch %s %s %s\n\n' "$BRANCH" "$REPO_URL" "$SRC_DIR"
    read -r -p "Press Enter after cloning: " _ </dev/tty \
      || die "an interactive terminal is required."
    [ -d "$SRC_DIR/.git" ] || warn "no git repository found at $SRC_DIR"
  done
fi
[ -f "$SRC_DIR/pyproject.toml" ] && [ -d "$SRC_DIR/searx" ] \
  || die "$SRC_DIR does not look like a SearXNG checkout."

# ---------------------------------------------------------------------------
# 3. Service account and directories
# ---------------------------------------------------------------------------
msg "Creating service account and directories"
getent group "$SVC_USER" >/dev/null || addgroup -S "$SVC_USER"
getent passwd "$SVC_USER" >/dev/null \
  || adduser -S -D -H -h "$STATE_DIR" -s /sbin/nologin -g "SearXNG" \
       -G "$SVC_USER" "$SVC_USER"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$STATE_DIR" "$LOG_DIR"
install -d -m 0755 "$CONF_DIR"

# ---------------------------------------------------------------------------
# 4. Virtual environment and editable install
# ---------------------------------------------------------------------------
msg "Creating the virtual environment: $VENV"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" || {
    warn "python3 -m venv failed; falling back to py3-virtualenv."
    apk add py3-virtualenv
    rm -rf "$VENV"
    virtualenv "$VENV"
  }
fi

export PIP_DISABLE_PIP_VERSION_CHECK=1
msg "Installing build prerequisites"
"$VENV/bin/pip" install -U pip setuptools wheel pyyaml msgspec typing_extensions pybind11
msg "Installing pinned requirements"
"$VENV/bin/pip" install -r "$SRC_DIR/requirements.txt"
msg "Installing SearXNG in editable mode"
"$VENV/bin/pip" install --use-pep517 --no-build-isolation -e "$SRC_DIR"

# The editable install writes metadata into the checkout as root; ownership is
# returned to the invoking user so that the checkout remains editable.
if [ -n "$INVOKER" ]; then chown -R "$INVOKER": "$SRC_DIR"; fi
[ -x "$VENV/bin/searxng-run" ] || die "searxng-run entry point is missing from $VENV/bin."

# ---------------------------------------------------------------------------
# 5. Configuration
# ---------------------------------------------------------------------------
# The configuration overrides the defaults shipped in the checkout
# (use_default_settings), so edits to searx/settings.yml in the source tree remain
# effective.
if [ -f "$CONF_DIR/settings.yml" ]; then
  msg "Keeping existing $CONF_DIR/settings.yml"
else
  msg "Writing $CONF_DIR/settings.yml"
  umask 077
  cat >"$CONF_DIR/settings.yml" <<EOF
use_default_settings: true

server:
  bind_address: "$BIND_ADDRESS"
  port: $PORT
  secret_key: "$(openssl rand -hex 32)"
  limiter: false

valkey:
  url: valkey://127.0.0.1:6379/0
EOF
  umask 022
fi
chown "root:$SVC_USER" "$CONF_DIR/settings.yml"
chmod 0640 "$CONF_DIR/settings.yml"

if [ -f "$SRC_DIR/searx/limiter.toml" ] && [ ! -f "$CONF_DIR/limiter.toml" ]; then
  install -m 0644 "$SRC_DIR/searx/limiter.toml" "$CONF_DIR/limiter.toml"
fi

# ---------------------------------------------------------------------------
# 6. OpenRC service
# ---------------------------------------------------------------------------
msg "Writing /etc/init.d/searxng"
cat >/etc/init.d/searxng <<EOF
#!/sbin/openrc-run
# OpenRC service for SearXNG (source installation at $SRC_DIR).

name="SearXNG"
description="Privacy-respecting metasearch engine"
command="$VENV/bin/searxng-run"
command_user="$SVC_USER:$SVC_USER"
command_background=true
pidfile="/run/\${RC_SVCNAME}.pid"
directory="$STATE_DIR"
output_log="$LOG_DIR/searxng.log"
error_log="$LOG_DIR/searxng.log"

# Environment inherited by the service process. The safe.directory entry permits
# version detection through git in a checkout owned by another user.
export HOME="$STATE_DIR"
export SEARXNG_SETTINGS_PATH="$CONF_DIR/settings.yml"
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="$SRC_DIR"

depend() {
	need net
	use valkey
}
EOF
chmod 0755 /etc/init.d/searxng

# ---------------------------------------------------------------------------
# 7. Smoke test and service start
# ---------------------------------------------------------------------------
msg "Import check"
SEARXNG_SETTINGS_PATH="$CONF_DIR/settings.yml" "$VENV/bin/python" -c \
  'import searx; print("searx imported from", searx.__file__)' \
  || warn "the import check failed; inspect the output above."

if [ "$START_SERVICE" = 1 ]; then
  msg "Enabling and starting valkey and searxng"
  rc-update add valkey default >/dev/null
  rc-service valkey start || warn "valkey did not start."
  rc-update add searxng default >/dev/null
  rc-service searxng start || warn "searxng did not start; see $LOG_DIR/searxng.log."
fi

cat <<EOF

SearXNG installed from source.
  Checkout:   $SRC_DIR  (editable; changes apply after: rc-service searxng restart)
  Config:     $CONF_DIR/settings.yml
  Log:        $LOG_DIR/searxng.log
  Listening:  http://$BIND_ADDRESS:$PORT
EOF
