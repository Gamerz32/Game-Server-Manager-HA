#!/usr/bin/env bash
set -Eeuo pipefail

readonly OPTIONS_FILE="/data/options.json"
readonly MIGRATION_MARKER="/data/.ha-minecraft-initialized"

log_info() { printf '[INFO] %s\n' "$*"; }
log_warn() { printf '[WARN] %s\n' "$*"; }
log_error() { printf '[ERROR] %s\n' "$*" >&2; }

if [[ ! -r "$OPTIONS_FILE" ]]; then
  log_error "Home Assistant options file is missing: ${OPTIONS_FILE}"
  exit 1
fi

option() {
  jq -r --arg key "$1" 'if has($key) then .[$key] else "" end' "$OPTIONS_FILE"
}

EULA_ACCEPTED="$(option eula)"
if [[ "$EULA_ACCEPTED" != "true" ]]; then
  log_error "Set 'eula' to true in the add-on configuration after reviewing https://aka.ms/MinecraftEULA."
  exit 1
fi
export EULA=TRUE

SERVER_TYPE="$(option server_type)"
SERVER_TYPE="${SERVER_TYPE^^}"
MC_VERSION="$(option mc_version)"
JAVA_VERSION="$(option java_version)"
MODPACK="$(option modpack)"
CF_API_KEY="$(option curseforge_api_key)"
LEGACY_DATA_DIR="$(option data_dir)"
CUSTOM_SERVER_URL="$(option server_jar_url)"
XMS_MB="$(option xms_mb)"
XMX_MB="$(option xmx_mb)"

MOTD="$(option motd)"
MAX_PLAYERS="$(option max_players)"
DIFFICULTY="$(option difficulty)"
GAME_MODE="$(option game_mode)"
ONLINE_MODE="$(option online_mode)"
PVP="$(option pvp)"
VIEW_DISTANCE="$(option view_distance)"
SIMULATION_DISTANCE="$(option simulation_distance)"
ALLOW_FLIGHT="$(option allow_flight)"

[[ -n "$SERVER_TYPE" ]] || SERVER_TYPE="VANILLA"
[[ -n "$MC_VERSION" ]] || MC_VERSION="LATEST"
[[ -n "$JAVA_VERSION" ]] || JAVA_VERSION="auto"
[[ -n "$XMS_MB" ]] || XMS_MB=1024
[[ -n "$XMX_MB" ]] || XMX_MB=4096
[[ -n "$LEGACY_DATA_DIR" ]] || LEGACY_DATA_DIR="/share/minecraft-vanilla"

if [[ ! "$XMS_MB" =~ ^[0-9]+$ || ! "$XMX_MB" =~ ^[0-9]+$ ]]; then
  log_error "Memory values must be whole numbers in MB."
  exit 1
fi
if (( XMS_MB < 512 || XMX_MB < XMS_MB )); then
  log_error "xms_mb must be at least 512 and xmx_mb must be greater than or equal to xms_mb."
  exit 1
fi

export INIT_MEMORY="${XMS_MB}M"
export MAX_MEMORY="${XMX_MB}M"
export VERSION="$MC_VERSION"
export MOTD MAX_PLAYERS DIFFICULTY
export MODE="$GAME_MODE"
export ONLINE_MODE PVP VIEW_DISTANCE SIMULATION_DISTANCE ALLOW_FLIGHT

case "$SERVER_TYPE" in
  VANILLA|PAPER|PURPUR|FABRIC|FORGE|NEOFORGE|QUILT)
    export TYPE="$SERVER_TYPE"
    ;;
  MODRINTH)
    if [[ -z "$MODPACK" ]]; then
      log_error "Enter a Modrinth modpack slug, ID, project URL, version URL, or .mrpack URL in 'modpack'."
      exit 1
    fi
    export TYPE=MODRINTH
    export MODRINTH_MODPACK="$MODPACK"
    ;;
  AUTO_CURSEFORGE)
    if [[ -z "$MODPACK" ]]; then
      log_error "Enter a CurseForge modpack URL or slug in 'modpack'."
      exit 1
    fi
    export TYPE=AUTO_CURSEFORGE
    export CF_PAGE_URL="$MODPACK"
    if [[ -n "$CF_API_KEY" ]]; then
      export CF_API_KEY
    fi

    ;;
  CUSTOM)
    if [[ -z "$CUSTOM_SERVER_URL" ]]; then
      log_error "Set 'server_jar_url' to a custom server JAR URL when server_type is CUSTOM."
      exit 1
    fi
    export TYPE=CUSTOM
    export CUSTOM_SERVER="$CUSTOM_SERVER_URL"
    ;;
  *)
    log_error "Unsupported server_type '${SERVER_TYPE}'. Choose a value from the add-on configuration list."
    exit 1
    ;;
esac

# Preserve compatibility with the previous vanilla add-on's custom JAR option.
if [[ -n "$CUSTOM_SERVER_URL" && "$SERVER_TYPE" == "VANILLA" ]]; then
  log_warn "A saved server_jar_url was found; using it as a custom server JAR."
  export TYPE=CUSTOM
  export CUSTOM_SERVER="$CUSTOM_SERVER_URL"
elif [[ -n "$CUSTOM_SERVER_URL" && "$SERVER_TYPE" != "CUSTOM" ]]; then
  log_error "server_jar_url can only be used with server_type CUSTOM."
  exit 1
fi

# Older releases kept their world under /share. Copy into persistent /data on
# first start, preserve the source, and resume safely if storage fills mid-copy.
if [[ ! -e "$MIGRATION_MARKER" ]]; then
  if [[ "$LEGACY_DATA_DIR" != /share/* || "$LEGACY_DATA_DIR" =~ (^|/)\.\.?(/|$) ]]; then
    log_error "data_dir must be a directory below /share (for example /share/minecraft-vanilla)."
    exit 1
  fi

  migration_in_progress="/data/.ha-minecraft-migration-in-progress"
  if [[ -f "$migration_in_progress" ]]; then
    if [[ ! -d "$LEGACY_DATA_DIR" ]]; then
      log_error "A previous migration stopped partway through, but ${LEGACY_DATA_DIR} is no longer available. Restore it from backup before restarting."
      exit 1
    fi
    log_info "Resuming the previous world-data copy from ${LEGACY_DATA_DIR}."
    shopt -s dotglob nullglob
    for item in "$LEGACY_DATA_DIR"/*; do
      item_name="${item##*/}"
      case "$item_name" in
        options.json|.ha-minecraft-initialized|.ha-minecraft-migration-in-progress) continue ;;
      esac
      cp -a -- "$item" /data/
    done
    touch "$MIGRATION_MARKER"
    rm -f "$migration_in_progress"
    log_info "World-data migration completed; the original folder remains as a backup."
  elif [[ -d "$LEGACY_DATA_DIR" ]] && [[ -n "$(find "$LEGACY_DATA_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    target_has_server_data="$(find /data -mindepth 1 -maxdepth 1 ! -name options.json ! -name lost+found ! -name .java -print -quit)"
    if [[ -n "$target_has_server_data" ]]; then
      if [[ -f /data/server.properties || -f /data/eula.txt || -d /data/world ]]; then
        log_warn "Minecraft data already exists in /data; leaving ${LEGACY_DATA_DIR} untouched."
        touch "$MIGRATION_MARKER"
      else
        log_error "Legacy files exist at ${LEGACY_DATA_DIR}, but /data also contains unrelated files. Refusing to merge or overwrite anything."
        exit 1
      fi
    else
      log_info "Copying existing world and server files from ${LEGACY_DATA_DIR} into persistent app storage."
      touch "$migration_in_progress"
      shopt -s dotglob nullglob
      for item in "$LEGACY_DATA_DIR"/*; do
        item_name="${item##*/}"
        case "$item_name" in
          options.json|.ha-minecraft-initialized|.ha-minecraft-migration-in-progress) continue ;;
        esac
        cp -a -- "$item" /data/
      done
      touch "$MIGRATION_MARKER"
      rm -f "$migration_in_progress"
      log_info "World-data migration completed; the original folder remains as a backup."
    fi
  else
    touch "$MIGRATION_MARKER"
  fi
fi

if [[ "$JAVA_VERSION" != "auto" ]]; then
  case "$JAVA_VERSION" in
    17|21|25) ;;
    *)
      log_error "java_version must be auto, 17, 21, or 25."
      exit 1
      ;;
  esac

  bundled_java="$(java -XshowSettings:properties -version 2>&1 | awk -F= '/java.specification.version/ { gsub(/[[:space:]]/, "", $2); print $2; exit }')"
  [[ "$bundled_java" == "1.8" ]] && bundled_java=8
  if [[ "$bundled_java" != "$JAVA_VERSION" ]]; then
    case "$(uname -m)" in
      x86_64|amd64) adoptium_arch=x64 ;;
      aarch64|arm64) adoptium_arch=aarch64 ;;
      *)
        log_error "No Temurin runtime download is configured for architecture $(uname -m)."
        exit 1
        ;;
    esac

    java_dir="/data/.java/${JAVA_VERSION}"
    java_bin="${java_dir}/bin/java"
    if [[ ! -x "$java_bin" ]]; then
      log_info "Downloading Temurin Java ${JAVA_VERSION} (${adoptium_arch}) for this server."
      java_parent="/data/.java"
      mkdir -p "$java_parent"
      temp_dir="$(mktemp -d "${java_parent}/.install.XXXXXX")"
      trap 'rm -rf "$temp_dir"' EXIT
      metadata_url="https://api.adoptium.net/v3/assets/latest/${JAVA_VERSION}/hotspot?architecture=${adoptium_arch}&image_type=jre&os=linux&vendor=eclipse"
      metadata="$(curl -fsSL --retry 3 --retry-delay 2 "$metadata_url")"
      java_url="$(jq -er '.[0].binary.package.link' <<< "$metadata")"
      java_checksum="$(jq -er '.[0].binary.package.checksum' <<< "$metadata")"
      curl -fsSL --retry 3 --retry-delay 2 "$java_url" -o "${temp_dir}/runtime.tar.gz"
      if ! printf '%s  %s\n' "$java_checksum" "${temp_dir}/runtime.tar.gz" | sha256sum --check --status; then
        log_error "Temurin Java ${JAVA_VERSION} download checksum did not match Adoptium metadata."
        exit 1
      fi
      mkdir -p "${temp_dir}/runtime"
      tar -xzf "${temp_dir}/runtime.tar.gz" -C "${temp_dir}/runtime" --strip-components=1
      if [[ ! -x "${temp_dir}/runtime/bin/java" ]]; then
        log_error "Temurin Java ${JAVA_VERSION} archive did not contain bin/java."
        exit 1
      fi
      rm -rf "$java_dir"
      mv "${temp_dir}/runtime" "$java_dir"
      rm -rf "$temp_dir"
      trap - EXIT
    fi
    export JAVA_HOME="$java_dir"
    export PATH="${JAVA_HOME}/bin:${PATH}"
  fi
fi

log_info "Minecraft server type: ${TYPE}"
log_info "Minecraft version: ${VERSION}"
log_info "Java runtime: $(java -version 2>&1 | head -n 1)"
log_info "Memory: ${INIT_MEMORY} initial / ${MAX_MEMORY} maximum"
log_info "Server files and worlds are stored in this add-on's persistent /data folder."

exec /image/scripts/start "$@"

