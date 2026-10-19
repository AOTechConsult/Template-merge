#!/usr/bin/env bash
#
# Template Merge: installer for Ubuntu Server 22.04 / 24.04
# Runs alongside the Cyber Security Response Tool on the same server (its own port, default 8443).
#
#   sudo bash install.sh            Install Docker if needed, unpack the app, start it in demo mode
#   sudo bash install.sh --live     Enter sign-in, AI and Confluence details and switch to live
#   sudo bash install.sh --backup   Save a copy of the templates and merges database
#   sudo bash install.sh --status   Show whether it is running and in which mode
#
# Put this file next to template-merge.zip (or run it from inside the unpacked project folder).
# It is safe to run again: it keeps your .env and your data.
#
# Prompts can be pre-answered with environment variables for unattended runs:
#   TM_HOST, TM_PORT, TM_ORG, ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET, ENTRA_ALLOWED_GROUP_ID,
#   GLEAN_BASE_URL, GLEAN_API_TOKEN, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT,
#   CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN, CONFLUENCE_PAT
#   (answer "none" to skip an optional item)

set -euo pipefail

INSTALL_DIR="${TM_DIR:-/opt/tmerge}"
ZIP_NAME="template-merge.zip"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$INSTALL_DIR/.env"
CSRT_ENV="${CSRT_DIR:-/opt/csrt}/.env"
GUID='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m    Warning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m    Error: %s\033[0m\n' "$*" >&2; exit 1; }

# ask VAR "Prompt" "default" "regex"  : uses $VAR if already set, otherwise prompts.
ask() {
  local name=$1 prompt=$2 def=${3:-} re=${4:-} val=${!1:-}
  while :; do
    if [[ -z $val ]]; then
      if [[ -t 0 ]]; then
        read -r -p "    $prompt${def:+ [$def]}: " val
      fi
      val=${val:-$def}
    fi
    if [[ -z $re || $val =~ ^($re)$ ]]; then
      printf -v "$name" '%s' "$val"
      return
    fi
    [[ -t 0 ]] || die "$name is missing or not valid. Set it as an environment variable."
    warn "That does not look right, try again."
    val=
  done
}

# ask_secret VAR "Prompt" : hidden input.
ask_secret() {
  local name=$1 prompt=$2 val=${!1:-}
  while [[ -z $val ]]; do
    [[ -t 0 ]] || die "$name is missing. Set it as an environment variable."
    read -r -s -p "    $prompt (input hidden): " val
    echo
  done
  [[ $val != *"'"* ]] || die "$name contains a single quote, which .env cannot hold. Generate a new secret."
  printf -v "$name" '%s' "$val"
}

# set_env KEY VALUE : replaces the line in .env, or adds it. No escaping surprises.
set_env() {
  local tmp
  tmp=$(mktemp)
  KEY=$1 VAL=$2 awk '
    BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VAL"] }
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}

get_env() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -n 1 | sed "s/^'\(.*\)'$/\1/"; }

# Secrets are single-quoted so Docker Compose never expands a "$" inside them.
quote() { printf "'%s'" "$1"; }

compose() { (cd "$INSTALL_DIR" && docker compose "$@"); }

need_root() {
  [[ $EUID -eq 0 ]] || die "Run this with sudo:  sudo bash $0 $*"
}

need_install() {
  [[ -f $INSTALL_DIR/docker-compose.yml && -f $ENV_FILE ]] || die "The tool is not installed in $INSTALL_DIR yet. Run:  sudo bash $0"
}

# ---------------------------------------------------------------- install steps

check_os() {
  step "Checking the system"
  # shellcheck disable=SC1091
  . /etc/os-release 2>/dev/null || true
  if [[ ${ID:-} != ubuntu ]]; then
    warn "This script is written for Ubuntu; found '${PRETTY_NAME:-unknown}'. Continuing anyway."
  else
    info "${PRETTY_NAME}"
  fi
  local free_mb
  free_mb=$(df -Pm "$(dirname "$INSTALL_DIR")" | awk 'NR==2 {print $4}')
  (( free_mb > 4000 )) || warn "Only ${free_mb} MB free under $(dirname "$INSTALL_DIR"). The build needs about 4 GB."
}

install_packages() {
  step "Installing base packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl unzip >/dev/null
  info "ca-certificates, curl, unzip"
}

install_docker() {
  step "Installing Docker"
  if docker compose version >/dev/null 2>&1; then
    info "Already installed: $(docker --version)"
  else
    # shellcheck disable=SC1091
    . /etc/os-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
    systemctl enable --now docker >/dev/null 2>&1 || true
    info "Installed: $(docker --version)"
  fi
  if [[ -n ${SUDO_USER:-} && $SUDO_USER != root ]] && ! id -nG "$SUDO_USER" | grep -qw docker; then
    usermod -aG docker "$SUDO_USER"
    info "Added $SUDO_USER to the docker group (takes effect at next login)."
  fi
}

place_files() {
  step "Placing the tool in $INSTALL_DIR"
  local src="" zip=""
  # A zip in the current folder (or next to this script) always wins, so that
  # dropping in a newer zip and re-running updates the installed copy.
  for z in "$PWD/$ZIP_NAME" "$SCRIPT_DIR/$ZIP_NAME"; do
    [[ -f $z ]] && { zip=$z; break; }
  done
  if [[ -z $zip && -f $SCRIPT_DIR/docker-compose.yml && -f $SCRIPT_DIR/Dockerfile ]]; then
    src=$SCRIPT_DIR
  else
    if [[ -z $zip ]]; then
      [[ -f $INSTALL_DIR/docker-compose.yml ]] && { info "No zip found here; using the files already in $INSTALL_DIR."; return; }
      die "Cannot find $ZIP_NAME. Put it in the same folder as this script and run again."
    fi
    UNPACK=$(mktemp -d)
    trap 'rm -rf "${UNPACK:-}"' EXIT
    unzip -q "$zip" -d "$UNPACK"
    src=$(dirname "$(find "$UNPACK" -maxdepth 2 -name docker-compose.yml | head -n 1)")
    [[ -f $src/Dockerfile ]] || die "$zip does not look like the Template Merge project archive."
    info "Unpacked $(basename "$zip")"
  fi

  mkdir -p "$INSTALL_DIR"
  if [[ $(readlink -f "$src") != $(readlink -f "$INSTALL_DIR") ]]; then
    # Copy everything except an existing .env, build output and local data.
    (cd "$src" && tar cf - --exclude=./.env --exclude=node_modules --exclude=dist --exclude=./data .) | (cd "$INSTALL_DIR" && tar xf -)
    info "Files copied"
  else
    info "Already running from $INSTALL_DIR"
  fi
  # Keep the installer that shipped with these files; fall back to this copy.
  [[ -f $INSTALL_DIR/install.sh ]] || cp -f "${BASH_SOURCE[0]}" "$INSTALL_DIR/install.sh"
  chmod +x "$INSTALL_DIR/install.sh"
}

configure_base() {
  step "Basic settings"
  local fresh=0
  if [[ ! -f $ENV_FILE ]]; then
    cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
    fresh=1
  fi
  chmod 600 "$ENV_FILE"

  local current guess
  current=$(get_env SITE_ADDRESS)
  [[ $fresh -eq 1 || $current == csrt.corp.example.com ]] && current=""
  # Default to the security tool's address when it is installed here.
  guess=${current:-$(sed -n 's/^SITE_ADDRESS=//p' "$CSRT_ENV" 2>/dev/null | tail -n 1)}
  guess=${guess:-$(hostname -f 2>/dev/null || hostname)}
  info "The DNS name people type in the browser (not an IP address)."
  ask TM_HOST "DNS name for this server" "$guess" '[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?'
  TM_HOST=${TM_HOST,,}
  ask TM_PORT "Port for this app (80 and 443 are used by the security tool)" "$(get_env HTTPS_PORT)" '[0-9]{2,5}'
  ask TM_ORG "Organization name shown in the header" "$(get_env ORG_NAME)" '.+'

  set_env BASE_URL "https://$TM_HOST:$TM_PORT"
  set_env SITE_ADDRESS "$TM_HOST"
  set_env HTTPS_PORT "$TM_PORT"
  set_env ORG_NAME "$TM_ORG"
  [[ $fresh -eq 1 ]] && set_env DEMO_MODE true

  # Share the security tool's certificate authority so browsers trust both apps through one root.
  if docker volume inspect csrt_caddy-data >/dev/null 2>&1; then
    set_env CADDY_DATA_VOLUME csrt_caddy-data
    info "Sharing the security tool's certificate authority (one root certificate to trust)."
  elif [[ -z $(get_env CADDY_DATA_VOLUME) ]]; then
    set_env CADDY_DATA_VOLUME tmerge-caddy-data
  fi
  if ss -ltn 2>/dev/null | grep -q ":$TM_PORT " && ! compose ps -q caddy 2>/dev/null | grep -q .; then
    warn "Something is already listening on port $TM_PORT. Choose another port if the start fails."
  fi
  if ! getent hosts "$TM_HOST" >/dev/null; then
    warn "$TM_HOST does not resolve from this server yet. Add the DNS record before others try to reach it."
  fi
}

start_stack() {
  step "Building and starting (the first build takes a few minutes)"
  compose up --build -d "$@"
}

wait_ready() {
  local host port out=""
  host=$(get_env SITE_ADDRESS)
  port=$(get_env HTTPS_PORT)
  step "Waiting for https://$host:$port to answer"
  for _ in $(seq 1 60); do
    out=$(curl -ks --max-time 5 --resolve "$host:$port:127.0.0.1" "https://$host:$port/api/me" 2>/dev/null || true)
    if [[ $out == *'"demo":'* ]]; then
      if [[ $out == *'"demo":true'* ]]; then info "Up, in DEMO mode (sample documents only)."; else info "Up, in LIVE mode."; fi
      return 0
    fi
    sleep 3
  done
  warn "No answer after 3 minutes. Recent log output:"
  compose ps || true
  compose logs --tail 40 || true
  die "The app did not start. The log lines above usually say why."
}

hand_over() {
  if [[ -n ${SUDO_USER:-} && $SUDO_USER != root ]]; then
    chown -R "$SUDO_USER":"$(id -gn "$SUDO_USER")" "$INSTALL_DIR"
  fi
  chmod 600 "$ENV_FILE"
}

# ---------------------------------------------------------------- modes

do_install() {
  check_os
  install_packages
  install_docker
  place_files
  configure_base
  start_stack
  wait_ready
  hand_over
  local base
  base=$(get_env BASE_URL)
  step "Done"
  info "Open:  $base"
  if [[ $(get_env DEMO_MODE) == true ]]; then
    cat <<MSG

    It is running in demo mode. Choose "Open the demo", then "Try the sample documents".

    To go live:
      1. Create an Entra app registration for sign-in (README, "Entra setup") with the redirect URI
           $base/auth/callback
      2. Have a Glean base URL and API token and/or an Azure OpenAI deployment ready (optional).
      3. Have a Confluence service account or token ready (optional).
      4. Run:  sudo bash $INSTALL_DIR/install.sh --live
MSG
  fi
}

do_live() {
  need_install
  local base problems=0 code resp
  base=$(get_env BASE_URL)

  step "Microsoft Entra sign-in"
  info "Redirect URI that must be on the app registration (type Web):  $base/auth/callback"
  ask ENTRA_TENANT_ID "Directory (tenant) ID" "$(get_env ENTRA_TENANT_ID)" "$GUID"
  ask ENTRA_CLIENT_ID "Application (client) ID" "$(get_env ENTRA_CLIENT_ID)" "$GUID"
  ask_secret ENTRA_CLIENT_SECRET "Client secret value"
  info "Limit sign-in to one group (needs the groups claim, see README). Type 'none' to skip."
  ask ENTRA_ALLOWED_GROUP_ID "Allowed group object ID" "$(get_env ENTRA_ALLOWED_GROUP_ID)" "none|$GUID"
  [[ $ENTRA_ALLOWED_GROUP_ID == none ]] && ENTRA_ALLOWED_GROUP_ID=""

  step "AI fallback (optional; type 'none' to skip each)"
  local gl_default
  gl_default=$(get_env GLEAN_BASE_URL)
  ask GLEAN_BASE_URL "Glean base URL, e.g. https://company-be.glean.com" "${gl_default:-none}" 'none|https://[A-Za-z0-9.-]+/?'
  if [[ $GLEAN_BASE_URL != none ]]; then
    ask_secret GLEAN_API_TOKEN "Glean API token (chat scope)"
  fi
  local az_default
  az_default=$(get_env AZURE_OPENAI_ENDPOINT)
  ask AZURE_OPENAI_ENDPOINT "Azure OpenAI endpoint, e.g. https://name.openai.azure.com" "${az_default:-none}" 'none|https://[A-Za-z0-9.-]+/?'
  if [[ $AZURE_OPENAI_ENDPOINT != none ]]; then
    ask AZURE_OPENAI_DEPLOYMENT "Azure OpenAI deployment name" "$(get_env AZURE_OPENAI_DEPLOYMENT)" '[A-Za-z0-9._-]+'
    ask_secret AZURE_OPENAI_API_KEY "Azure OpenAI API key"
  fi

  step "Confluence (optional)"
  local cf_default
  cf_default=$(get_env CONFLUENCE_BASE_URL)
  ask CONFLUENCE_BASE_URL "Confluence URL (Cloud: https://yourorg.atlassian.net/wiki)" "${cf_default:-none}" 'none|https?://[^ ]+'
  if [[ $CONFLUENCE_BASE_URL != none ]]; then
    CONFLUENCE_BASE_URL=${CONFLUENCE_BASE_URL%/}
    if [[ $CONFLUENCE_BASE_URL == *atlassian.net* ]]; then
      ask CONFLUENCE_EMAIL "Service account email" "$(get_env CONFLUENCE_EMAIL)" '[^@ ]+@[^@ ]+'
      ask_secret CONFLUENCE_API_TOKEN "Atlassian API token"
    else
      ask_secret CONFLUENCE_PAT "Personal access token"
    fi
  fi

  step "Checking the details before changing anything"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://login.microsoftonline.com/$ENTRA_TENANT_ID/v2.0/.well-known/openid-configuration" || true)
  if [[ $code == 200 ]]; then info "Entra tenant found."; else warn "Could not confirm the tenant ID (HTTP $code)."; problems=1; fi
  resp=$(curl -s --max-time 15 "https://login.microsoftonline.com/$ENTRA_TENANT_ID/oauth2/v2.0/token" \
    --data-urlencode "client_id=$ENTRA_CLIENT_ID" --data-urlencode "client_secret=$ENTRA_CLIENT_SECRET" \
    --data-urlencode "grant_type=client_credentials" --data-urlencode "scope=https://graph.microsoft.com/.default" || true)
  if [[ $resp == *'"access_token"'* ]]; then info "Entra client ID and secret accepted."
  elif [[ $resp == *AADSTS7000215* || $resp == *AADSTS7000222* ]]; then warn "Microsoft rejected the client secret. Paste the secret's Value, not its ID."; problems=1
  elif [[ $resp == *AADSTS700016* ]]; then warn "Microsoft does not know that client ID in this tenant."; problems=1
  else warn "Could not verify the Entra client secret; sign-in will show the real result."; fi
  if [[ $GLEAN_BASE_URL != none ]]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "${GLEAN_BASE_URL%/}/rest/api/v1/chat" \
      -H "Authorization: Bearer $GLEAN_API_TOKEN" -H "content-type: application/json" \
      -d '{"messages":[{"author":"USER","messageType":"CONTENT","fragments":[{"text":"ping"}]}],"stream":false}' || true)
    if [[ $code == 200 ]]; then info "Glean answered."; else warn "Glean did not answer as expected (HTTP $code). Check the base URL and token."; problems=1; fi
  fi
  if [[ $AZURE_OPENAI_ENDPOINT != none ]]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST "${AZURE_OPENAI_ENDPOINT%/}/openai/deployments/$AZURE_OPENAI_DEPLOYMENT/chat/completions?api-version=2024-10-21" \
      -H "api-key: $AZURE_OPENAI_API_KEY" -H "content-type: application/json" -d '{"messages":[{"role":"user","content":"ping"}],"max_tokens":1}' || true)
    if [[ $code == 200 ]]; then info "Azure OpenAI deployment answered."; else warn "Azure OpenAI did not answer as expected (HTTP $code). Check the endpoint, deployment name and key."; problems=1; fi
  fi
  if [[ $CONFLUENCE_BASE_URL != none ]]; then
    if [[ -n ${CONFLUENCE_PAT:-} ]]; then
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Authorization: Bearer $CONFLUENCE_PAT" "$CONFLUENCE_BASE_URL/rest/api/space?limit=1" || true)
    else
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -u "$CONFLUENCE_EMAIL:$CONFLUENCE_API_TOKEN" "$CONFLUENCE_BASE_URL/rest/api/space?limit=1" || true)
    fi
    if [[ $code == 200 ]]; then info "Confluence credentials accepted."; else warn "Confluence did not accept the credentials (HTTP $code)."; problems=1; fi
  fi
  if [[ $problems -eq 1 ]]; then
    local go=${TM_FORCE:-}
    [[ -n $go || ! -t 0 ]] || read -r -p "    Problems were found. Save these settings and switch to live anyway? [y/N]: " go
    [[ ${go,,} == y* || $go == 1 ]] || die "Nothing was changed. Fix the values and run --live again."
  fi

  step "Saving settings and switching to live"
  cp -p "$ENV_FILE" "$ENV_FILE.bak"
  set_env ENTRA_TENANT_ID "${ENTRA_TENANT_ID,,}"
  set_env ENTRA_CLIENT_ID "${ENTRA_CLIENT_ID,,}"
  set_env ENTRA_CLIENT_SECRET "$(quote "$ENTRA_CLIENT_SECRET")"
  set_env ENTRA_ALLOWED_GROUP_ID "${ENTRA_ALLOWED_GROUP_ID,,}"
  if [[ $GLEAN_BASE_URL == none ]]; then
    set_env GLEAN_BASE_URL ""; set_env GLEAN_API_TOKEN ""
  else
    set_env GLEAN_BASE_URL "${GLEAN_BASE_URL%/}"; set_env GLEAN_API_TOKEN "$(quote "$GLEAN_API_TOKEN")"
  fi
  if [[ $AZURE_OPENAI_ENDPOINT == none ]]; then
    set_env AZURE_OPENAI_ENDPOINT ""; set_env AZURE_OPENAI_API_KEY ""; set_env AZURE_OPENAI_DEPLOYMENT ""
  else
    set_env AZURE_OPENAI_ENDPOINT "${AZURE_OPENAI_ENDPOINT%/}"; set_env AZURE_OPENAI_DEPLOYMENT "$AZURE_OPENAI_DEPLOYMENT"
    set_env AZURE_OPENAI_API_KEY "$(quote "$AZURE_OPENAI_API_KEY")"
  fi
  if [[ $CONFLUENCE_BASE_URL == none ]]; then
    set_env CONFLUENCE_BASE_URL ""; set_env CONFLUENCE_EMAIL ""; set_env CONFLUENCE_API_TOKEN ""; set_env CONFLUENCE_PAT ""
  else
    set_env CONFLUENCE_BASE_URL "$CONFLUENCE_BASE_URL"
    set_env CONFLUENCE_EMAIL "${CONFLUENCE_EMAIL:-}"
    set_env CONFLUENCE_API_TOKEN "$( [[ -n ${CONFLUENCE_API_TOKEN:-} ]] && quote "$CONFLUENCE_API_TOKEN")"
    set_env CONFLUENCE_PAT "$( [[ -n ${CONFLUENCE_PAT:-} ]] && quote "$CONFLUENCE_PAT")"
  fi
  # Prefer Glean when both are set up; otherwise whichever exists.
  if [[ -n $(get_env GLEAN_BASE_URL) ]]; then set_env DEFAULT_AI_PROVIDER glean
  elif [[ -n $(get_env AZURE_OPENAI_ENDPOINT) ]]; then set_env DEFAULT_AI_PROVIDER azure; fi
  set_env DEMO_MODE false
  info "Previous settings kept in $ENV_FILE.bak"

  start_stack --force-recreate
  wait_ready
  hand_over
  chmod 600 "$ENV_FILE.bak"
  step "Live"
  info "Open $base and sign in with Microsoft. Upload your template on the Templates page first."
}

do_backup() {
  need_install
  local dest="${TM_BACKUP_DIR:-$INSTALL_DIR/backups}" id file
  mkdir -p "$dest"
  chmod 700 "$dest"
  id=$(compose ps -aq app)
  [[ -n $id ]] || die "The app container does not exist yet. Run the installer first."
  file="tmerge-data-$(date +%Y%m%d-%H%M%S).tgz"
  step "Backing up templates and merges"
  compose stop app >/dev/null
  trap 'compose start app >/dev/null' EXIT
  docker run --rm --volumes-from "$id" -v "$dest":/backup alpine tar czf "/backup/$file" -C /data .
  compose start app >/dev/null
  trap - EXIT
  chmod 600 "$dest/$file"
  info "Saved $dest/$file ($(du -h "$dest/$file" | cut -f1))"
}

do_status() {
  need_install
  step "Containers"
  compose ps
  wait_ready
  info "Address:  $(get_env BASE_URL)"
  info "Logs:     cd $INSTALL_DIR && docker compose logs -f app"
}

case "${1:-}" in
  "")        need_root; do_install ;;
  --live)    need_root "$@"; do_live ;;
  --backup)  need_root "$@"; do_backup ;;
  --status)  need_root "$@"; do_status ;;
  -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//' ;;
  *)         die "Unknown option '$1'. Try:  bash $0 --help" ;;
esac
