#!/usr/bin/env bash
# =============================================================================
# install-presentations.sh: install or update Dikarya Presentations
#
#   https://presentations.dikarya.us/   (FastAPI app, localhost:8031 behind nginx)
#   https://presentation.dikarya.us/    (301 -> https://presentations.dikarya.us$request_uri)
#
# Run as root after reviewing:   sudo bash /tmp/install-presentations.sh
#
# Idempotent: safe to re-run for every update. What it does, in order:
#   1. Pre-flight checks (root, source tree, port, existing nginx conventions).
#   2. Installs python3-venv / rsync / certbot only if missing (SKIP_APT=1 to skip).
#   3. Creates the unprivileged system user "presentations" (no shell, no home login).
#   4. Syncs the application from $SRC_DIR into /var/www/presentations
#      (owner $CODE_OWNER, group presentations, read-only for the service).
#   5. Creates/updates the virtualenv /var/www/presentations/.venv and installs
#      requirements.txt; byte-compiles; runs an import check as the service user.
#   6. Installs /etc/systemd/system/presentations.service (hardened, 127.0.0.1:8031,
#      one Uvicorn worker, Restart=on-failure), enables and restarts it, waits for
#      /healthz.
#   7. nginx: writes ONE dedicated vhost file
#      /etc/nginx/sites-available/presentations.dikarya.us.conf and its symlink in
#      sites-enabled. Global nginx config and other sites are not touched.
#      Every change is followed by `nginx -t`; on failure the previous file is
#      restored and nginx is NOT reloaded.
#   8. TLS: reuses /etc/letsencrypt/live/presentations.dikarya.us if it already
#      covers both hostnames. Otherwise, once both hostnames are confirmed to
#      reach this server (over HTTP, or through public DNS matching dikarya.us,
#      because this host has no NAT hairpin), it requests one certificate with
#      certbot's webroot method (/var/www/letsencrypt), the same method the other
#      Dikarya subdomains (labels, images) use. SKIP_CERTBOT=1 disables this; the
#      site is then served over plain HTTP until a certificate exists.
#   9. Prints service status, listening port, nginx test result and hostnames.
#
# Tunables (environment): SRC_DIR CODE_OWNER PORT SKIP_APT SKIP_CERTBOT
# =============================================================================
set -Eeuo pipefail

APP_DIR=/var/www/presentations
SRC_DIR=${SRC_DIR:-/tmp/presentations-src}
SERVICE=presentations
SERVICE_USER=presentations
CODE_OWNER=${CODE_OWNER:-tree}
PORT=${PORT:-8031}
DOMAIN=presentations.dikarya.us
ALIAS=presentation.dikarya.us
STATE_DIR=/var/lib/presentations
UNIT_FILE=/etc/systemd/system/${SERVICE}.service
SITE_AVAILABLE=/etc/nginx/sites-available/${DOMAIN}.conf
SITE_ENABLED=/etc/nginx/sites-enabled/${DOMAIN}.conf
ACME_ROOT=/var/www/letsencrypt
CERT_NAME=${DOMAIN}
CERT_DIR=/etc/letsencrypt/live/${CERT_NAME}
BACKUP_DIR=/var/backups/presentations
SKIP_APT=${SKIP_APT:-0}
SKIP_CERTBOT=${SKIP_CERTBOT:-0}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

log()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

# install_file <src-tempfile> <dest> <mode>: replace dest only if content changed,
# keeping a timestamped backup. Returns 0 if changed, 1 if unchanged.
install_file() {
  local src=$1 dest=$2 mode=$3
  if [[ -f $dest ]] && cmp -s "$src" "$dest"; then
    rm -f "$src"; return 1
  fi
  mkdir -p "$BACKUP_DIR"
  if [[ -f $dest ]]; then cp -a "$dest" "$BACKUP_DIR/$(basename "$dest").$STAMP"; fi
  install -m "$mode" -o root -g root "$src" "$dest"
  rm -f "$src"
  return 0
}

# ---------------------------------------------------------------------------
log "1. Pre-flight checks"
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "run as root: sudo bash $0"
[[ -f $SRC_DIR/app/main.py && -f $SRC_DIR/requirements.txt ]] || die "application source not found in $SRC_DIR (set SRC_DIR=...)"
[[ $(realpath "$SRC_DIR") != "$(realpath -m "$APP_DIR")" ]] || die "SRC_DIR must not be $APP_DIR"
id "$CODE_OWNER" &>/dev/null || { warn "user $CODE_OWNER does not exist; code will be owned by root"; CODE_OWNER=root; }

# Root is about to install code from a staging directory (by default in /tmp,
# which anyone can write to). Refuse if it could have been tampered with.
src_owner=$(stat -c %U "$SRC_DIR")
[[ $src_owner == "$CODE_OWNER" || $src_owner == root ]] || die "$SRC_DIR is owned by $src_owner, expected $CODE_OWNER or root"
bad_owner=$(find "$SRC_DIR" -not -user "$CODE_OWNER" -not -user root -print -quit)
[[ -z $bad_owner ]] || die "$bad_owner in the source tree is owned by an unexpected user"
writable=$(find "$SRC_DIR" \( -path "$SRC_DIR/.venv" -o -path "$SRC_DIR/var*" \) -prune -o -perm /022 -not -type l -print -quit)
[[ -z $writable ]] || die "$writable is group/world-writable; fix permissions (chmod -R go-w $SRC_DIR) and re-run"
info "source:      $SRC_DIR (owner $src_owner, $(find "$SRC_DIR/app" -type f | wc -l) app files)"
info "source hash: $(cd "$SRC_DIR" && find app requirements.txt -type f -not -name '*.pyc' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)"

command -v nginx >/dev/null || die "nginx is not installed"
command -v systemctl >/dev/null || die "systemd is required"
[[ -d /etc/nginx/sites-available && -d /etc/nginx/sites-enabled ]] || die "expected Debian-style /etc/nginx/sites-{available,enabled}"
grep -Eq 'include[[:space:]]+/etc/nginx/sites-enabled/\*' /etc/nginx/nginx.conf || warn "nginx.conf does not appear to include sites-enabled/*"

# The port must be free, or already ours.
if ss -Hltn "sport = :$PORT" | grep -q .; then
  if ! systemctl is-active --quiet "$SERVICE"; then
    ss -Hltnp "sport = :$PORT" >&2 || true
    die "port $PORT is in use by something other than $SERVICE.service (set PORT=...)"
  fi
fi
# Never take over hostnames another vhost already serves.
other=$(grep -RlE "server_name[^;]*\b(${DOMAIN//./\\.}|${ALIAS//./\\.})\b" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null | grep -v "^$SITE_ENABLED$" || true)
[[ -z $other ]] || die "these nginx files already use $DOMAIN or $ALIAS: $other"

# ---------------------------------------------------------------------------
log "2. OS packages"
# ---------------------------------------------------------------------------
need=()
python3 -c 'import ensurepip, venv' 2>/dev/null || need+=(python3-venv)
command -v rsync >/dev/null || need+=(rsync)
command -v curl >/dev/null || need+=(curl)
if [[ $SKIP_CERTBOT != 1 ]] && ! command -v certbot >/dev/null; then need+=(certbot); fi
if ((${#need[@]})); then
  [[ $SKIP_APT == 1 ]] && die "missing packages: ${need[*]} (SKIP_APT=1 set)"
  info "installing: ${need[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
else
  info "nothing to install ($(python3 --version))"
fi

# ---------------------------------------------------------------------------
log "3. Service user"
# ---------------------------------------------------------------------------
install -d -m 0750 "$STATE_DIR"
if ! id "$SERVICE_USER" &>/dev/null; then
  adduser --system --group --no-create-home --home "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  info "created system user $SERVICE_USER"
else
  info "user $SERVICE_USER exists"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$STATE_DIR"

# ---------------------------------------------------------------------------
log "4. Application code -> $APP_DIR"
# ---------------------------------------------------------------------------
install -d -o "$CODE_OWNER" -g "$SERVICE_USER" -m 0755 "$APP_DIR"
rsync -a --delete --no-owner --no-group \
  --exclude '/.venv/' --exclude '/var/' --exclude '/var-*/' --exclude '__pycache__/' \
  --exclude '.pytest_cache/' --exclude '*.pyc' --exclude '*.pptx' \
  "$SRC_DIR"/ "$APP_DIR"/
chown -R "$CODE_OWNER:$SERVICE_USER" "$APP_DIR"
find "$APP_DIR" -path "$APP_DIR/.venv" -prune -o -type d -exec chmod 0755 {} + -o -type f -exec chmod 0644 {} +
chmod 0755 "$APP_DIR/deploy/install-presentations.sh" 2>/dev/null || true
info "synced ($(find "$APP_DIR/app" -type f | wc -l) app files)"

# ---------------------------------------------------------------------------
log "5. Python virtualenv"
# ---------------------------------------------------------------------------
if [[ ! -x $APP_DIR/.venv/bin/python ]]; then
  python3 -m venv "$APP_DIR/.venv"
  info "created $APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/app" >/dev/null
chown -R "$CODE_OWNER:$SERVICE_USER" "$APP_DIR"
chmod -R go-w "$APP_DIR"
# Import check as the service user, before anything is restarted.
runuser -u "$SERVICE_USER" -- env PRESENTATIONS_WORK_DIR="$STATE_DIR" \
  "$APP_DIR/.venv/bin/python" -c "import sys; sys.path.insert(0, '$APP_DIR'); import app.main; app.main.create_app" \
  || die "the application failed to import; service not restarted"
info "dependencies installed, import check passed"

# ---------------------------------------------------------------------------
log "6. systemd unit"
# ---------------------------------------------------------------------------
tmp=$(mktemp)
cat >"$tmp" <<EOF
# Managed by install-presentations.sh. Re-run it instead of editing by hand.
[Unit]
Description=Dikarya Presentations (FastAPI/Uvicorn) for ${DOMAIN}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment=PRESENTATIONS_WORK_DIR=${STATE_DIR}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
# Exactly one worker: the iNaturalist rate limiter, request budgets and job
# registry are in-process and must be shared by every request.
ExecStart=${APP_DIR}/.venv/bin/uvicorn --factory app.main:create_app \\
    --host 127.0.0.1 --port ${PORT} --workers 1 \\
    --proxy-headers --forwarded-allow-ips 127.0.0.1 \\
    --timeout-keep-alive 15 --no-server-header --log-level info
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
StateDirectory=presentations
StateDirectoryMode=0750
UMask=0027
LimitNOFILE=8192
MemoryMax=3G
TasksMax=256

# Hardening: the service can write only to its own state directory.
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${STATE_DIR}
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictSUIDSGID=true
RestrictRealtime=true
RestrictNamespaces=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallArchitectures=native
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
EOF
if install_file "$tmp" "$UNIT_FILE" 0644; then info "unit updated"; else info "unit unchanged"; fi
systemctl daemon-reload
systemctl enable --quiet "$SERVICE"
systemctl restart "$SERVICE"
healthy=0
for _ in $(seq 1 40); do
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 0.5
done
if [[ $healthy != 1 ]]; then
  journalctl -u "$SERVICE" -n 40 --no-pager >&2 || true
  die "$SERVICE did not become healthy on 127.0.0.1:${PORT}"
fi
info "$SERVICE is healthy: $(curl -fsS "http://127.0.0.1:${PORT}/healthz")"

# ---------------------------------------------------------------------------
log "7/8. nginx and TLS"
# ---------------------------------------------------------------------------
BOT_GUARD=""
if grep -qs 'map \$http_user_agent \$blocked_bot' /etc/nginx/sites-enabled/* 2>/dev/null; then
  BOT_GUARD=$'    # $blocked_bot comes from sites-enabled/00-bot-blocklist (shared with dikarya.us).\n    if ($blocked_bot) { return 403; }\n'
fi
SSL_EXTRA=""
[[ -f /etc/letsencrypt/options-ssl-nginx.conf ]] && SSL_EXTRA+=$'    include /etc/letsencrypt/options-ssl-nginx.conf;\n'
[[ -f /etc/letsencrypt/ssl-dhparams.pem ]] && SSL_EXTRA+=$'    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n'

proxy_block() {  # common reverse-proxy settings
  cat <<EOF
        proxy_pass http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # The app trusts only this local proxy; overwrite any client-supplied XFF.
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection "";
EOF
}

acme_block() {
  cat <<EOF
    location ^~ /.well-known/acme-challenge/ {
        root ${ACME_ROOT};
        default_type text/plain;
        try_files \$uri =404;
    }
EOF
}

app_locations() {
  cat <<EOF
${BOT_GUARD}
    client_max_body_size 8m;

$(acme_block)

    # Finished decks can be hundreds of MB: stream them, no nginx temp file.
    location ~ ^/api/jobs/[A-Za-z0-9_-]+/download\$ {
$(proxy_block)
        proxy_buffering off;
        proxy_max_temp_file_size 0;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    location /api/ {
        limit_req zone=presentations_api burst=60 nodelay;
        limit_req_status 429;
$(proxy_block)
        proxy_read_timeout 120s;
    }

    location / {
$(proxy_block)
        proxy_read_timeout 60s;
    }
EOF
}

render_nginx() {  # $1 = http | tls
  cat <<EOF
# Managed by install-presentations.sh (mode: $1). Re-run it instead of editing.
# Dikarya Presentations: ${DOMAIN} -> 127.0.0.1:${PORT}; ${ALIAS} -> 301 ${DOMAIN}.

# http-context zone (this file is included inside http {} via sites-enabled/*).
limit_req_zone \$binary_remote_addr zone=presentations_api:10m rate=10r/s;

EOF
  if [[ $1 == http ]]; then
    cat <<EOF
# No certificate yet: serve over HTTP so the site works and ACME can validate.
server {
    listen 80;
    listen [::]:80;
    server_name ${ALIAS};
$(acme_block)
    location / {
        return 301 http://${DOMAIN}\$request_uri;
    }
}

server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
$(app_locations)
}
EOF
  else
    cat <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} ${ALIAS};
$(acme_block)
    location / {
        return 301 https://${DOMAIN}\$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name ${ALIAS};

    ssl_certificate ${CERT_DIR}/fullchain.pem;
    ssl_certificate_key ${CERT_DIR}/privkey.pem;
${SSL_EXTRA}
    return 301 https://${DOMAIN}\$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name ${DOMAIN};

    ssl_certificate ${CERT_DIR}/fullchain.pem;
    ssl_certificate_key ${CERT_DIR}/privkey.pem;
${SSL_EXTRA}
    add_header Strict-Transport-Security "max-age=31536000" always;
$(app_locations)
}
EOF
  fi
}

NGINX_TEST="not run"
# apply_nginx <mode>: write, test and reload, or restore the previous file and abort.
apply_nginx() {
  local mode=$1 tmpf prev=""
  tmpf=$(mktemp)
  render_nginx "$mode" >"$tmpf"
  if [[ -f $SITE_AVAILABLE ]]; then prev=$(mktemp); cp -a "$SITE_AVAILABLE" "$prev"; fi
  local changed=0
  install_file "$tmpf" "$SITE_AVAILABLE" 0644 && changed=1
  ln -sfn "$SITE_AVAILABLE" "$SITE_ENABLED"
  local out
  if out=$(nginx -t 2>&1); then
    NGINX_TEST="ok"
    if ((changed)) || ! systemctl is-active --quiet nginx; then
      systemctl reload nginx
      info "nginx config ($mode) installed, tested and reloaded"
    else
      info "nginx config ($mode) unchanged"
    fi
    if [[ -n $prev ]]; then rm -f "$prev"; fi
  else
    NGINX_TEST="FAILED"
    printf '%s\n' "$out" >&2
    if [[ -n $prev ]]; then cp -a "$prev" "$SITE_AVAILABLE"; rm -f "$prev"; else rm -f "$SITE_ENABLED" "$SITE_AVAILABLE"; fi
    nginx -t >/dev/null 2>&1 && info "previous nginx config restored (nginx was NOT reloaded)"
    die "nginx -t failed for the new $mode config; nothing was reloaded"
  fi
}

cert_covers_both() {
  [[ -r $CERT_DIR/fullchain.pem && -r $CERT_DIR/privkey.pem ]] || return 1
  local sans
  sans=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -ext subjectAltName 2>/dev/null) || return 1
  grep -q "DNS:${DOMAIN}" <<<"$sans" && grep -q "DNS:${ALIAS}" <<<"$sans" \
    && openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -checkend 86400 >/dev/null
}

# Public A records, bypassing /etc/hosts (which maps some Dikarya names to 127.0.0.1).
public_a() {
  local r out=""
  for r in 1.1.1.1 8.8.8.8; do
    out=$(dig +short +time=3 +tries=1 A "$1" "@$r" 2>/dev/null | grep -E '^[0-9.]+$' | sort | paste -sd, -) || true
    [[ -n $out ]] && break
  done
  printf '%s' "$out"
}

# Is it safe to ask Let's Encrypt? First try fetching a token over HTTP. That
# fails on this host because it sits behind NAT without hairpinning (hence the
# 127.0.0.1 entries in /etc/hosts), so fall back to public DNS: the name must
# resolve to the same address as dikarya.us, whose certificate already renews
# by webroot through this nginx.
name_points_here() {
  local host=$1 mine ref
  reaches_this_server "$host" && { info "$host: HTTP self-check ok"; return 0; }
  command -v dig >/dev/null || { warn "dig not available; cannot verify DNS for $host"; return 1; }
  mine=$(public_a "$host"); ref=$(public_a dikarya.us)
  if [[ -n $mine && $mine == "$ref" ]]; then
    info "$host: public DNS $mine matches dikarya.us (HTTP self-check unavailable: no NAT hairpin)"
    return 0
  fi
  warn "$host resolves to '${mine:-nothing}', dikarya.us to '${ref:-nothing}'"
  return 1
}

# Prove a hostname reaches this nginx over HTTP before asking Let's Encrypt.
reaches_this_server() {
  local host=$1 token
  token="presentations-check-$(head -c 9 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  install -d -m 0755 "$ACME_ROOT/.well-known/acme-challenge"
  echo "$token" >"$ACME_ROOT/.well-known/acme-challenge/$token"
  chmod 0644 "$ACME_ROOT/.well-known/acme-challenge/$token"
  local got
  got=$(curl -fsS --max-time 10 "http://${host}/.well-known/acme-challenge/${token}" 2>/dev/null || true)
  rm -f "$ACME_ROOT/.well-known/acme-challenge/$token"
  [[ $got == "$token" ]]
}

TLS_STATE=""
if cert_covers_both; then
  TLS_STATE="existing certificate $CERT_DIR covers both hostnames"
  apply_nginx tls
else
  apply_nginx http
  if [[ $SKIP_CERTBOT == 1 ]]; then
    TLS_STATE="NO TLS: SKIP_CERTBOT=1 and no certificate covers both hostnames; serving HTTP only"
  elif ! name_points_here "$DOMAIN" || ! name_points_here "$ALIAS"; then
    TLS_STATE="NO TLS YET: ${DOMAIN} and/or ${ALIAS} do not point at this server. Point both DNS names here and re-run."
  else
    info "requesting a certificate for ${DOMAIN} + ${ALIAS} (certbot webroot, like the other Dikarya subdomains)"
    if certbot certonly --webroot -w "$ACME_ROOT" --cert-name "$CERT_NAME" \
         -d "$DOMAIN" -d "$ALIAS" --non-interactive --agree-tos --keep-until-expiring --expand; then
      if cert_covers_both; then
        TLS_STATE="certificate issued: $CERT_DIR"
        apply_nginx tls
      else
        TLS_STATE="certbot ran but $CERT_DIR does not cover both names; still serving HTTP"
      fi
    else
      TLS_STATE="certbot FAILED (see output above); still serving HTTP"
    fi
  fi
fi
[[ $TLS_STATE == NO* || $TLS_STATE == *FAILED* || $TLS_STATE == *still* ]] && warn "$TLS_STATE"

# ---------------------------------------------------------------------------
log "9. Status"
# ---------------------------------------------------------------------------
systemctl --no-pager --lines=5 status "$SERVICE" || true
echo
info "service:        $(systemctl is-active "$SERVICE") / $(systemctl is-enabled "$SERVICE")"
info "listening:      $(ss -Hltn "sport = :$PORT" | awk '{print $4}' | paste -sd' ' -) (expected 127.0.0.1:${PORT} only)"
info "health:         $(curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/healthz" || echo FAILED)"
info "nginx -t:       ${NGINX_TEST}"
info "nginx site:     ${SITE_AVAILABLE} (enabled: $( [[ -L $SITE_ENABLED ]] && echo yes || echo no ))"
info "TLS:            ${TLS_STATE}"
info "hostnames:      ${DOMAIN} -> app;  ${ALIAS} -> 301 ${DOMAIN}\$request_uri"
info "code:           ${APP_DIR} (owner ${CODE_OWNER}:${SERVICE_USER})"
info "temp storage:   ${STATE_DIR} (workspaces, cached originals <=2h, decks <=2h)"
scheme=http; cert_covers_both && scheme=https
check() {  # check <url>: status line via the local nginx, bypassing DNS
  local url=$1 host port
  host=$(sed -E 's#^[a-z]+://([^/]+).*#\1#' <<<"$url")
  port=80; [[ $url == https* ]] && port=443
  curl -sk --max-time 5 -o /dev/null -w '%{http_code} %{redirect_url}' --resolve "${host}:${port}:127.0.0.1" "$url" || echo "unreachable"
}
info "check ${scheme}://${DOMAIN}/healthz:            $(check "${scheme}://${DOMAIN}/healthz")"
info "check ${scheme}://${ALIAS}/some/path?q=1:      $(check "${scheme}://${ALIAS}/some/path?q=1")"
[[ $scheme == https ]] && info "check http://${DOMAIN}/x?y=1:                   $(check "http://${DOMAIN}/x?y=1")"
echo
log "Done."
