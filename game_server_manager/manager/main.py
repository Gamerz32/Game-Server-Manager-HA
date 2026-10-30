from __future__ import annotations

import asyncio
import base64
import binascii
import html
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import docker
import httpx
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from manager.games import get_game_driver, registered_games
from manager.games.minecraft import MINECRAFT_DRIVER

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
LOG = logging.getLogger("game-server-manager")
SHARE_DIR = Path("/share")
SHARE_DATA_DIR = SHARE_DIR / "game_server_manager"
LEGACY_DATA_DIR = Path("/data")
DATA_DIR = SHARE_DATA_DIR
SERVERS_DIR = DATA_DIR / "servers"
SETTINGS_FILE = DATA_DIR / "manager-settings.json"
OPTIONS_FILE = LEGACY_DATA_DIR / "options.json"
STATIC_DIR = Path(__file__).parent / "static"
DOCKER_SOCKET = "unix:///run/docker.sock"
MANAGER_LABEL = "io.gamerz32.game-server-manager"
LEGACY_MINECRAFT_MANAGER_LABEL = "io.gamerz32.minecraft-manager"
INGRESS_PEER = "172.30.32.2"
MC_GAME_ID = 432
JAVA_TAGS = MINECRAFT_DRIVER.java_tags
SERVER_TYPES = set(MINECRAFT_DRIVER.server_types)
CF_CLASS_IDS = {"modpack": 4471, "mod": 6, "plugin": 5}
CATALOG_CONTENT_KINDS = {"resourcepack", "datapack", "world"}
MODRINTH_BASE = "https://api.modrinth.com/v2"
CURSEFORGE_BASE = "https://api.curseforge.com/v1"
HANGAR_BASE = "https://hangar.papermc.io/api/v1"
JAVA_TAGS = {"auto": "latest", "8": "java8", "17": "java17", "21": "java21", "25": "java25"}
UPDATE_CHECK_INTERVAL = 4 * 60 * 60
MAINTENANCE_INTERVAL = 60
UPDATE_DISCONNECT_REASON = "The Server is Updating, Please Wait"
MINECRAFT_NETWORK_NAME = "ha-minecraft-manager"
MINECRAFT_NETWORK_LABEL = f"{LEGACY_MINECRAFT_MANAGER_LABEL}.network"
GATEWAY_LABEL = f"{LEGACY_MINECRAFT_MANAGER_LABEL}.gateway"
GATEWAY_CONTAINER_PREFIX = "ha-minecraft-gateway-"
GATEWAY_INTERNAL_PORT = 18080
GATEWAY_CONTAINER_PORT = 25565
GATEWAY_RELAY_MAGIC = b"MCG1"
GATEWAY_RELAY_HEADER_SIZE = 4 + 36 + 64
GATEWAYS: dict[str, dict[str, Any]] = {}
HOST_NETWORKING_ENABLED = False
MINECRAFT_NETWORK_IP = ""
MANAGER_IMAGE = ""
GATEWAY_SECRET = ""
GATEWAY_RELAY_SERVER: asyncio.AbstractServer | None = None
MAINTENANCE_TASK: asyncio.Task | None = None
SERVER_TYPES = {"VANILLA", "BUKKIT", "PAPER", "PURPUR", "FABRIC", "FORGE", "NEOFORGE", "QUILT", "MODRINTH", "AUTO_CURSEFORGE"}
MOD_LOADERS = {"FABRIC", "FORGE", "NEOFORGE", "QUILT"}
PLUGIN_SERVER_TYPES = {"BUKKIT", "PAPER", "PURPUR"}
CF_LOADER_IDS = {"FORGE": 1, "FABRIC": 4, "QUILT": 5, "NEOFORGE": 6}
CF_LOADER_NAMES = {value: key.lower() for key, value in CF_LOADER_IDS.items()}
MINECRAFT_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
MINECRAFT_VERSION_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
BEDROCK_ADDON_MAX_BYTES = 32 * 1024 * 1024
BEDROCK_ADDON_MAX_EXPANDED_BYTES = 256 * 1024 * 1024
BEDROCK_ADDON_MAX_FILES = 10000
CONTENT_ARCHIVE_MAX_BYTES = 128 * 1024 * 1024
CONTENT_ARCHIVE_MAX_EXPANDED_BYTES = 512 * 1024 * 1024
CONTENT_ARCHIVE_MAX_FILES = 20000
MANAGED_JAR_MAX_BYTES = 32 * 1024 * 1024

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="game-server-job")
HOST_DATA_SOURCE: Path | None = None
DOCKER_ERROR: str | None = None
DOCKER_CLIENT: docker.DockerClient | None = None
DOCKER_CLIENT_LOCK = threading.Lock()
SERVER_LOCKS: dict[str, threading.Lock] = {}
SERVER_LOCKS_LOCK = threading.Lock()
SERVER_CONTAINER_LOCKS: dict[str, threading.Lock] = {}
SERVER_CONTAINER_LOCKS_LOCK = threading.Lock()
SHUTTING_DOWN = False
CURSEFORGE_API_KEY = ""
DOCKER_UNAVAILABLE_MESSAGE = "Docker Engine is unavailable. Check the add-on logs for details."


def docker_client() -> docker.DockerClient:
    global DOCKER_CLIENT
    with DOCKER_CLIENT_LOCK:
        if DOCKER_CLIENT is None:
            DOCKER_CLIENT = docker.DockerClient(base_url=DOCKER_SOCKET, version="auto", timeout=60)
        return DOCKER_CLIENT


def server_lock(server_id: str) -> threading.Lock:
    with SERVER_LOCKS_LOCK:
        if server_id not in SERVER_LOCKS:
            SERVER_LOCKS[server_id] = threading.Lock()
        return SERVER_LOCKS[server_id]


def server_container_lock(server_id: str) -> threading.Lock:
    with SERVER_CONTAINER_LOCKS_LOCK:
        if server_id not in SERVER_CONTAINER_LOCKS:
            SERVER_CONTAINER_LOCKS[server_id] = threading.Lock()
        return SERVER_CONTAINER_LOCKS[server_id]


def server_dir(server_id: str) -> Path:
    try:
        safe_id = str(uuid.UUID(server_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="Server not found.")
    path = SERVERS_DIR / safe_id
    if path.parent.resolve() != SERVERS_DIR.resolve():
        raise HTTPException(status_code=404, detail="Server not found.")
    return path


def config_path(server_id: str) -> Path:
    return server_dir(server_id) / "server.json"


def server_data_root(server_id: str, create: bool = False) -> Path:
    server_path = server_dir(server_id)
    server_root = server_path.resolve()
    servers_root = SERVERS_DIR.resolve()
    if server_root.parent != servers_root or server_root.name != server_path.name:
        raise RuntimeError("Invalid server data path.")
    data_root = server_root / "data"
    if create:
        data_root.mkdir(parents=True, exist_ok=True)
    resolved_data_root = data_root.resolve()
    if resolved_data_root.parent != server_root or resolved_data_root.name != "data":
        raise RuntimeError("Invalid server data path.")
    return resolved_data_root


def read_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return fallback or {}
        # Older server records predate game_id; their first registered driver is Minecraft.
        if path.name == "server.json" and value.get("id"):
            value.setdefault("game_id", "minecraft")
            if value.get("game_id") == "minecraft":
                value.setdefault("crossplay_enabled", False)
                value.setdefault("crossplay_port", None)
            value.setdefault("world_name", "Bedrock level" if value.get("game_id") == "minecraft_bedrock" else "world")
            value.setdefault("world_seed", "")
            value.setdefault("game_mode", "survival")
            value.setdefault("difficulty", "easy")
            value.setdefault("max_players", 10)
            value.setdefault("pvp_enabled", True)
            value.setdefault("allow_cheats", False)
            value.setdefault("level_type", "DEFAULT")
            value.setdefault("op_permission_level", 4)
            value.setdefault("default_player_permission_level", "member")
            value.setdefault("whitelist_enabled", False)
            value.setdefault("whitelist_players", [])
            value.setdefault("blacklist_enabled", False)
            value.setdefault("blacklist_players", [])
            value.setdefault("operators", [])
            value.setdefault("members", [])
            value.setdefault("visitors", [])
            value.setdefault("bedrock_addons", [])
            value.setdefault("control_generation", 0)
            value.setdefault("force_stopped", False)
            value.setdefault("access_lists_applied", {"whitelist_enabled": False, "whitelist_players": [], "blacklist_players": [], "operators": [], "members": [], "visitors": []})
        return value
    except (OSError, json.JSONDecodeError):
        return fallback or {}


def write_json(path: Path, value: dict[str, Any], secret: bool = False) -> None:
    # A force-exit or explicit start advances this generation. A background job
    # that read the old config before that request must not overwrite its newer
    # desired state when it eventually finishes.
    if path.name == "server.json" and path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(current, dict):
                current_generation = int(current.get("control_generation", 0))
                next_generation = int(value.get("control_generation", 0))
                if current_generation > next_generation:
                    value["control_generation"] = current_generation
                    for key in (
                        "force_stopped", "desired_state", "job_state", "updating",
                        "world_resetting", "deleting", "auto_stopped", "auto_paused",
                    ):
                        if key in current:
                            value[key] = current[key]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp_path.replace(path)
    if secret:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def settings() -> dict[str, Any]:
    value = read_json(SETTINGS_FILE, {"eula_accepted": False})
    if "curseforge_api_key" in value:
        # Remove keys persisted by older versions. CurseForge credentials are now memory-only.
        value.pop("curseforge_api_key", None)
        save_settings(value)
    return value


def save_settings(value: dict[str, Any]) -> None:
    persisted = dict(value)
    persisted.pop("curseforge_api_key", None)
    write_json(SETTINGS_FILE, persisted, secret=True)


def current_curseforge_api_key() -> str:
    if CURSEFORGE_API_KEY:
        return CURSEFORGE_API_KEY
    configured = read_json(OPTIONS_FILE, {}).get("curseforge_api_key", "")
    return str(configured).strip() if isinstance(configured, str) else ""


def scrub_legacy_settings() -> None:
    # An interrupted legacy write could leave the plaintext key in this sibling temp file.
    temporary_settings = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".tmp")
    temporary_settings.unlink(missing_ok=True)
    settings()


def game_driver_for(item: dict[str, Any]):
    game_id = str(item.get("game_id") or "minecraft").strip().lower()
    try:
        return get_game_driver(game_id)
    except KeyError as err:
        raise RuntimeError(str(err)) from err


def host_ports_for(item: dict[str, Any], protocol: str) -> set[int]:
    """Return host ports used by one managed server for TCP or UDP."""
    driver = game_driver_for(item)
    ports: set[int] = set()
    if protocol == "tcp" and driver.networking_adapter == "minecraft_gateway":
        ports.add(int(item.get("port", 0)))
    for container_port, binding in (driver.container_port_bindings(item) or {}).items():
        if not str(container_port).lower().endswith(f"/{protocol}"):
            continue
        values = binding if isinstance(binding, (list, tuple, set)) else [binding]
        for value in values:
            if isinstance(value, (list, tuple)):
                value = value[-1]
            try:
                ports.add(int(value))
            except (TypeError, ValueError):
                continue
    return {port for port in ports if 1 <= port <= 65535}


def require_game_capability(item: dict[str, Any], capability: str, feature: str):
    driver = game_driver_for(item)
    if not getattr(driver.capabilities, capability, False):
        raise HTTPException(status_code=409, detail=f"{feature} is not supported for {driver.display_name} servers.")
    return driver


def game_eula_accepted(game_id: str) -> bool:
    current = settings()
    accepted = current.get("game_eula_accepted")
    if isinstance(accepted, dict) and accepted.get(game_id) is True:
        return True
    # Preserve existing installations that stored a single Minecraft EULA flag.
    if game_id == "minecraft":
        return current.get("eula_accepted") is True
    try:
        return not get_game_driver(game_id).requires_eula
    except KeyError:
        return False


def all_configs() -> list[dict[str, Any]]:
    results = []
    if not SERVERS_DIR.exists():
        return results
    for child in SERVERS_DIR.iterdir():
        if not child.is_dir():
            continue
        item = read_json(child / "server.json")
        if item.get("id"):
            results.append(item)
    return sorted(results, key=lambda item: item.get("created_at", ""))


def docker_self_container(client: docker.DockerClient):
    hostname = os.environ.get("HOSTNAME", "")
    own = None
    if hostname:
        try:
            own = client.containers.get(hostname)
        except DockerException:
            pass
    if own is None:
        for candidate in client.containers.list(all=True):
            try:
                candidate.reload()
            except DockerException:
                continue
            if hostname and candidate.attrs.get("Config", {}).get("Hostname") == hostname:
                own = candidate
                break
    if own is None:
        raise RuntimeError("Could not identify this Home Assistant app container through the Docker API.")
    return own


def set_data_root(path: Path) -> None:
    global DATA_DIR, SERVERS_DIR, SETTINGS_FILE
    DATA_DIR = path
    SERVERS_DIR = DATA_DIR / "servers"
    SETTINGS_FILE = DATA_DIR / "manager-settings.json"


def _storage_entries(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    # Home Assistant owns options.json and rewrites it on every app start.
    return sorted((child for child in path.iterdir() if child.name != "options.json"), key=lambda child: child.name)


def _entry_signature(path: Path) -> tuple[tuple[Any, ...], ...]:
    """Describe an entry without following links so interrupted copies can be resumed safely."""
    result: list[tuple[Any, ...]] = []

    def visit(current: Path, relative: str) -> None:
        info = current.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            result.append((relative, "link", os.readlink(current), info.st_uid, info.st_gid, mode))
        elif stat.S_ISDIR(info.st_mode):
            result.append((relative, "dir", info.st_uid, info.st_gid, mode))
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                child_relative = f"{relative}/{child.name}" if relative else child.name
                visit(child, child_relative)
        elif stat.S_ISREG(info.st_mode):
            result.append((relative, "file", info.st_size, info.st_uid, info.st_gid, mode))
        else:
            raise RuntimeError(f"Unsupported special file in app data: {current}")

    visit(path, "")
    return tuple(result)


def _chown_like(source: Path, destination: Path) -> None:
    info = source.lstat()
    os.chown(destination, info.st_uid, info.st_gid, follow_symlinks=False)


def _copy_file_preserving_owner(source: str, destination: str) -> str:
    source_path = Path(source)
    destination_path = Path(destination)
    shutil.copy2(source_path, destination_path)
    _chown_like(source_path, destination_path)
    return destination


def _copy_storage_entry(source: Path, destination: Path) -> None:
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode):
        os.symlink(os.readlink(source), destination)
        _chown_like(source, destination)
    elif stat.S_ISDIR(info.st_mode):
        shutil.copytree(source, destination, symlinks=True, copy_function=_copy_file_preserving_owner)
        for current, directories, files in os.walk(destination, topdown=False, followlinks=False):
            for name in directories + files:
                copied = Path(current) / name
                original = source / copied.relative_to(destination)
                _chown_like(original, copied)
        _chown_like(source, destination)
    elif stat.S_ISREG(info.st_mode):
        shutil.copy2(source, destination)
        _chown_like(source, destination)
    else:
        raise RuntimeError(f"Unsupported special file in app data: {source}")


def _remove_storage_entry(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def _stop_managed_containers_for_storage_move(client: docker.DockerClient) -> None:
    for container in client.containers.list(all=True):
        container.reload()
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        generic_server = labels.get(MANAGER_LABEL) == "true" and labels.get(f"{MANAGER_LABEL}.server_id")
        legacy_server = (
            labels.get(LEGACY_MINECRAFT_MANAGER_LABEL) == "true"
            and labels.get(f"{LEGACY_MINECRAFT_MANAGER_LABEL}.server_id")
            and labels.get(GATEWAY_LABEL) != "true"
        )
        if not (generic_server or legacy_server):
            continue
        if container.status == "paused":
            container.unpause()
            container.reload()
        if container.status in {"running", "restarting"}:
            container.stop(timeout=120)
            container.reload()
        if container.status in {"running", "restarting", "paused"}:
            raise RuntimeError(f"Could not stop managed game container {container.name} before moving its data.")


def migrate_private_data_to_share(client: docker.DockerClient) -> bool:
    """Move app-owned files from private /data into the shared Home Assistant folder."""
    if SHARE_DATA_DIR.is_symlink():
        raise RuntimeError("The /share/game_server_manager storage folder must not be a symbolic link.")
    SHARE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SHARE_DIR.resolve() not in SHARE_DATA_DIR.resolve().parents:
        raise RuntimeError("The game server storage folder must remain inside /share.")
    source_entries = _storage_entries(LEGACY_DATA_DIR)
    if not source_entries:
        set_data_root(SHARE_DATA_DIR)
        return True

    # Reconcile a prior interrupted copy only when the source and public copy match.
    pending: list[tuple[Path, Path]] = []
    for source in source_entries:
        destination = SHARE_DATA_DIR / source.name
        if destination.exists() or destination.is_symlink():
            if _entry_signature(source) != _entry_signature(destination):
                LOG.error("Cannot migrate app data: %s already exists in /share and differs from /data.", destination)
                set_data_root(LEGACY_DATA_DIR)
                return False
        else:
            pending.append((source, destination))

    if source_entries:
        _stop_managed_containers_for_storage_move(client)

    try:
        same_filesystem = os.stat(LEGACY_DATA_DIR).st_dev == os.stat(SHARE_DATA_DIR).st_dev
    except OSError:
        same_filesystem = False

    if same_filesystem:
        moved: list[tuple[Path, Path]] = []
        try:
            for source, destination in pending:
                os.replace(source, destination)
                moved.append((source, destination))
        except OSError:
            # A mount nested under /data can still cross devices. Undo earlier renames,
            # then use the staged copy path for the whole migration.
            for source, destination in reversed(moved):
                os.replace(destination, source)
            same_filesystem = False

    if not same_filesystem:
        staging = SHARE_DATA_DIR / f".gsm-storage-migration-{uuid.uuid4().hex}"
        staging.mkdir()
        published: list[Path] = []
        try:
            staged: list[tuple[Path, Path]] = []
            for source, destination in pending:
                temporary = staging / source.name
                _copy_storage_entry(source, temporary)
                if _entry_signature(source) != _entry_signature(temporary):
                    raise RuntimeError(f"App data copy verification failed for {source}.")
                staged.append((temporary, destination))
            for temporary, destination in staged:
                os.replace(temporary, destination)
                published.append(destination)
        except Exception:
            for destination in reversed(published):
                _remove_storage_entry(destination)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(staging, ignore_errors=True)

    # All destination entries now exist and have been moved or verified. Remove any
    # matching source copies left by an interrupted earlier attempt or cross-device copy.
    for source in source_entries:
        if source.exists() or source.is_symlink():
            try:
                _remove_storage_entry(source)
            except OSError:
                LOG.warning("Copied %s into /share but could not remove the old private copy.", source)

    set_data_root(SHARE_DATA_DIR)
    LOG.info("Moved existing server and manager files into /share/game_server_manager.")
    return True

def docker_self_data_source(client: docker.DockerClient, destination: str = "/share") -> Path:
    own = docker_self_container(client)
    for mount in own.attrs.get("Mounts", []):
        if mount.get("Destination") == destination and mount.get("Source"):
            return Path(mount["Source"])
    raise RuntimeError(f"Home Assistant's persistent {destination} mount was not found in the Docker container details.")


def ensure_minecraft_network(client: docker.DockerClient):
    global MINECRAFT_NETWORK_IP, MANAGER_IMAGE
    try:
        network = client.networks.get(MINECRAFT_NETWORK_NAME)
    except NotFound:
        network = client.networks.create(
            MINECRAFT_NETWORK_NAME,
            driver="bridge",
            attachable=True,
            labels={MINECRAFT_NETWORK_LABEL: "true"},
        )
    network.reload()
    labels = network.attrs.get("Labels") or {}
    if labels.get(MINECRAFT_NETWORK_LABEL) != "true":
        raise RuntimeError(f"Docker network {MINECRAFT_NETWORK_NAME} exists but is not owned by this app.")
    own = docker_self_container(client)
    own.reload()
    attached = own.attrs.get("NetworkSettings", {}).get("Networks", {})
    if MINECRAFT_NETWORK_NAME not in attached:
        network.connect(own, aliases=["ha-minecraft-manager"])
        own.reload()
    details = own.attrs.get("NetworkSettings", {}).get("Networks", {}).get(MINECRAFT_NETWORK_NAME, {})
    MINECRAFT_NETWORK_IP = str(details.get("IPAddress") or "")
    MANAGER_IMAGE = str(own.attrs.get("Config", {}).get("Image") or "")
    if not MINECRAFT_NETWORK_IP or not MANAGER_IMAGE:
        raise RuntimeError("Could not determine the app's address and image for the Minecraft gateway network.")
    return network


def ensure_server_network(container) -> None:
    if not MINECRAFT_NETWORK_IP:
        raise RuntimeError("The private Minecraft Docker network is not ready.")
    container.reload()
    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    if MINECRAFT_NETWORK_NAME not in networks:
        docker_client().networks.get(MINECRAFT_NETWORK_NAME).connect(container)
        container.reload()
    if MINECRAFT_NETWORK_NAME not in container.attrs.get("NetworkSettings", {}).get("Networks", {}):
        raise RuntimeError("Could not attach the Minecraft server to the private app network.")

def host_server_data_dir(server_id: str) -> Path:
    global HOST_DATA_SOURCE
    try:
        safe_server_id = str(uuid.UUID(server_id))
    except (ValueError, TypeError, AttributeError) as err:
        raise RuntimeError("Invalid server data path.") from err
    if HOST_DATA_SOURCE is None:
        cli = docker_client()
        if DATA_DIR == LEGACY_DATA_DIR:
            HOST_DATA_SOURCE = docker_self_data_source(cli, "/data")
        else:
            HOST_DATA_SOURCE = docker_self_data_source(cli, "/share") / "game_server_manager"
    root = (HOST_DATA_SOURCE / "servers").resolve()
    server_root = (root / safe_server_id).resolve()
    if not str(server_root).startswith(f"{root}{os.sep}") or server_root.parent != root or server_root.name != safe_server_id:
        raise RuntimeError("Invalid server data path.")
    path = (server_root / "data").resolve()
    if not str(path).startswith(f"{server_root}{os.sep}") or path.parent != server_root:
        raise RuntimeError("Invalid server data path.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def import_legacy_server() -> None:
    """Copy the previous vanilla add-on's /share data into managed storage once."""
    if all_configs():
        return
    legacy = Path("/share/minecraft-vanilla")
    if not legacy.is_dir():
        return
    server_id = str(uuid.uuid4())
    destination = server_dir(server_id) / "data"
    shutil.copytree(legacy, destination, dirs_exist_ok=True, symlinks=False)
    old_options = read_json(OPTIONS_FILE)
    try:
        min_ram = int(old_options.get("xms_mb", 1024))
        max_ram = int(old_options.get("xmx_mb", 4096))
    except (TypeError, ValueError):
        min_ram, max_ram = 1024, 4096
    version = str(old_options.get("mc_version", "LATEST")).strip() or "LATEST"
    if version.lower() == "latest":
        version = "LATEST"
    item = {
        "id": server_id,
        "name": "Imported Vanilla Server",
        "port": 25565,
        "min_ram_mb": max(512, min(min_ram, 131072)),
        "max_ram_mb": max(512, min(max(max_ram, min_ram), 131072)),
        "game_id": "minecraft",
        "java_version": "auto",
        "minecraft_version": version[:40],
        "created_at": __import__("datetime").datetime.now().isoformat(),
        "server_type": "VANILLA",
        "mod_loader": "",
        "modpack": None,
        "mods": [],
        "desired_state": "stopped",
        "job_state": "stopped",
        "imported_from": "/share/minecraft-vanilla",
    }
    write_json(config_path(server_id), item)
    LOG.info("Imported the previous vanilla server into /share/game_server_manager; its original copy was kept intact.")


def get_container(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise NotFound(f"Managed server {server_id} does not exist.")
    driver = game_driver_for(item)
    container = docker_client().containers.get(driver.container_name(server_id))
    container.reload()
    labels = container.attrs.get("Config", {}).get("Labels") or {}
    generic_managed = labels.get(MANAGER_LABEL) == "true" and labels.get(f"{MANAGER_LABEL}.server_id") == server_id
    legacy_label = driver.legacy_manager_label
    legacy_managed = bool(legacy_label) and labels.get(legacy_label) == "true" and labels.get(f"{legacy_label}.server_id") == server_id
    if not (generic_managed or legacy_managed):
        raise RuntimeError("The Docker container name is occupied by a container this app does not manage.")
    if driver.networking_adapter == "minecraft_gateway":
        ensure_server_network(container)
    return container


def current_status(server_id: str) -> tuple[str, Any | None]:
    try:
        container = get_container(server_id)
        return container.status, container
    except NotFound:
        return "stopped", None
    except DockerException as err:
        return "unavailable", None


def public_server(item: dict[str, Any]) -> dict[str, Any]:
    status, container = current_status(item["id"])
    job = None
    with JOBS_LOCK:
        recent = [j for j in JOBS.values() if j.get("server_id") == item["id"]]
        if recent:
            job = max(recent, key=lambda j: j.get("created_at", ""))
    driver = game_driver_for(item)
    icon_path = server_dir(item["id"]) / "data" / "gsm-server-icon.png"
    return {**item, "game_id": driver.game_id, "game_name": driver.display_name, "server_icon": driver.game_id == "minecraft" and icon_path.is_file(), "status": status, "job": job}


def normalized_player_list(data: dict[str, Any], key: str, game_id: str) -> list[str]:
    raw = data.get(key, [])
    if isinstance(raw, str):
        values = raw.splitlines()
    elif isinstance(raw, list) and all(isinstance(value, str) for value in raw):
        values = raw
    else:
        raise HTTPException(status_code=422, detail=f"{key.replace('_', ' ').title()} must be a list of player names, one per line.")
    if len(values) > 100:
        raise HTTPException(status_code=422, detail=f"{key.replace('_', ' ').title()} can contain at most 100 players.")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        name = raw_value.strip()
        if not name:
            continue
        if game_id == "minecraft":
            valid = re.fullmatch(r"[A-Za-z0-9_]{1,16}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", name)
        else:
            valid = re.fullmatch(r"[A-Za-z0-9_ .-]{1,32}(?::[0-9]{16,20})?", name)
        if not valid:
            edition = "Java player name or UUID" if game_id == "minecraft" else "Bedrock gamertag, XUID, or gamertag:XUID"
            raise HTTPException(status_code=422, detail=f"Use a valid {edition} for {key.replace('_', ' ')}.")
        folded = name.casefold()
        if folded not in seen:
            normalized.append(name)
            seen.add(folded)
    return normalized


def validate_world_and_access_options(data: dict[str, Any], driver) -> dict[str, Any]:
    game_id = driver.game_id
    world_name = str(data.get("world_name", "Bedrock level" if game_id == "minecraft_bedrock" else "world")).strip()
    if not world_name or len(world_name) > 64 or not re.fullmatch(r"[A-Za-z0-9 _.-]+", world_name) or world_name in {".", ".."} or world_name.endswith((".", " ")):
        raise HTTPException(status_code=422, detail="Use a world name with up to 64 letters, numbers, spaces, dots, dashes, or underscores.")
    world_seed = str(data.get("world_seed", "")).strip()
    if len(world_seed) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in world_seed):
        raise HTTPException(status_code=422, detail="The world seed must be 128 characters or fewer and cannot contain control characters.")
    game_mode = str(data.get("game_mode", "survival")).strip().lower()
    valid_modes = {"survival", "creative", "adventure"} if game_id == "minecraft_bedrock" else {"survival", "creative", "adventure", "spectator"}
    if game_mode not in valid_modes:
        raise HTTPException(status_code=422, detail="Choose a game mode supported by this Minecraft edition.")
    difficulty = str(data.get("difficulty", "easy")).strip().lower()
    if difficulty not in {"peaceful", "easy", "normal", "hard"}:
        raise HTTPException(status_code=422, detail="Choose peaceful, easy, normal, or hard difficulty.")
    try:
        max_players = int(data.get("max_players", 10))
        op_permission_level = int(data.get("op_permission_level", 4))
    except (TypeError, ValueError) as err:
        raise HTTPException(status_code=422, detail="Player limit and operator permission level must be whole numbers.") from err
    if max_players < 1 or max_players > 1000:
        raise HTTPException(status_code=422, detail="Choose a player limit between 1 and 1000.")
    if op_permission_level < 1 or op_permission_level > 4:
        raise HTTPException(status_code=422, detail="Java operator permission level must be between 1 and 4.")
    pvp_enabled = data.get("pvp_enabled", True)
    allow_cheats = data.get("allow_cheats", False)
    whitelist_enabled = data.get("whitelist_enabled", False)
    blacklist_enabled = data.get("blacklist_enabled", False)
    if any(not isinstance(value, bool) for value in (pvp_enabled, allow_cheats, whitelist_enabled, blacklist_enabled)):
        raise HTTPException(status_code=422, detail="World and access-control toggles must be true or false.")
    permission_level = str(data.get("default_player_permission_level", "member")).strip().lower()
    if permission_level not in {"visitor", "member", "operator"}:
        raise HTTPException(status_code=422, detail="Choose visitor, member, or operator as the default Bedrock permission level.")
    level_type = str(data.get("level_type", "DEFAULT")).strip().upper()
    if level_type not in {"DEFAULT", "FLAT", "LEGACY"}:
        raise HTTPException(status_code=422, detail="Choose a Bedrock world type of Default, Flat, or Legacy.")
    operators = normalized_player_list(data, "operators", game_id)
    members = normalized_player_list(data, "members", game_id) if game_id == "minecraft_bedrock" else []
    visitors = normalized_player_list(data, "visitors", game_id) if game_id == "minecraft_bedrock" else []
    whitelist_players = normalized_player_list(data, "whitelist_players", game_id)
    blacklist_players = normalized_player_list(data, "blacklist_players", game_id)
    if game_id == "minecraft_bedrock" and (blacklist_enabled or blacklist_players):
        raise HTTPException(status_code=422, detail="Vanilla Bedrock Dedicated Server has no native blacklist. Use the allow-list to control who can join.")
    return {
        "world_name": world_name,
        "world_seed": world_seed,
        "game_mode": game_mode,
        "difficulty": difficulty,
        "max_players": max_players,
        "pvp_enabled": pvp_enabled,
        "allow_cheats": allow_cheats if game_id == "minecraft_bedrock" else False,
        "level_type": level_type if game_id == "minecraft_bedrock" else "DEFAULT",
        "op_permission_level": op_permission_level,
        "default_player_permission_level": permission_level if game_id == "minecraft_bedrock" else "member",
        "whitelist_enabled": whitelist_enabled,
        "whitelist_players": whitelist_players,
        "blacklist_enabled": blacklist_enabled,
        "blacklist_players": blacklist_players,
        "operators": operators,
        "members": members,
        "visitors": visitors,
    }


def server_icon_path(server_id: str) -> Path:
    return server_dir(server_id) / "data" / "gsm-server-icon.png"


def decode_server_icon(encoded: Any) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > 350_000:
        raise HTTPException(status_code=422, detail="Choose a PNG image smaller than 256 KB after resizing.")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as err:
        raise HTTPException(status_code=422, detail="The server icon image data is invalid.") from err
    if len(image) > 262_144 or len(image) < 24 or image[:8] != b"\x89PNG\r\n\x1a\n" or image[12:16] != b"IHDR":
        raise HTTPException(status_code=422, detail="The server icon must be a valid 64×64 PNG image under 256 KB.")
    width = int.from_bytes(image[16:20], "big")
    height = int.from_bytes(image[20:24], "big")
    if width != 64 or height != 64:
        raise HTTPException(status_code=422, detail="The server icon must be resized to exactly 64×64 pixels.")
    return image


def parse_bedrock_addon_archive(filename: Any, encoded: Any) -> dict[str, Any]:
    if not isinstance(filename, str) or not isinstance(encoded, str):
        raise HTTPException(status_code=422, detail="Choose a .mcaddon or .mcpack file to upload.")
    safe_filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    extension = Path(safe_filename).suffix.lower()
    if extension not in {".mcaddon", ".mcpack"}:
        raise HTTPException(status_code=422, detail="Bedrock add-ons must be uploaded as .mcaddon or .mcpack files.")
    if not safe_filename or len(safe_filename) > 180 or any(ord(char) < 32 or ord(char) == 127 for char in safe_filename):
        raise HTTPException(status_code=422, detail="The add-on filename is invalid.")
    if len(encoded) > (BEDROCK_ADDON_MAX_BYTES * 4 // 3) + 8:
        raise HTTPException(status_code=413, detail="Bedrock add-ons must be 32 MB or smaller.")
    try:
        archive = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as err:
        raise HTTPException(status_code=422, detail="The add-on upload data is invalid.") from err
    if not archive or len(archive) > BEDROCK_ADDON_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Bedrock add-ons must be 32 MB or smaller.")

    try:
        normalized_buffer = io.BytesIO()
        total_size = 0
        total_files = 0
        normalized_names: set[str] = set()
        embedded_index = 0
        with zipfile.ZipFile(normalized_buffer, "w", compression=zipfile.ZIP_STORED) as normalized_bundle:
            def expand_bundle(bundle_bytes: bytes, prefix: PurePosixPath, expand_embedded: bool) -> None:
                nonlocal total_size, total_files, embedded_index
                with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as bundle:
                    for info in bundle.infolist():
                        name = info.filename
                        if not name or "\\" in name or "\x00" in name or info.flag_bits & 0x1:
                            raise HTTPException(status_code=422, detail="The add-on archive contains an unsafe or encrypted file entry.")
                        path = PurePosixPath(name)
                        if not path.parts or path.is_absolute() or any(part in {"..", ""} for part in path.parts) or (path.parts and ":" in path.parts[0]):
                            raise HTTPException(status_code=422, detail="The add-on archive contains a path outside its package folder.")
                        mode = (info.external_attr >> 16) & 0xFFFF
                        file_type = stat.S_IFMT(mode)
                        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                            raise HTTPException(status_code=422, detail="The add-on archive contains a link or special file.")
                        if info.is_dir():
                            continue
                        total_files += 1
                        total_size += info.file_size
                        if total_files > BEDROCK_ADDON_MAX_FILES:
                            raise HTTPException(status_code=422, detail="The add-on archive has an unsupported number of files.")
                        if total_size > BEDROCK_ADDON_MAX_EXPANDED_BYTES:
                            raise HTTPException(status_code=413, detail="The expanded add-on archive must be 256 MB or smaller.")
                        if expand_embedded and path.suffix.lower() == ".mcpack":
                            embedded = bundle.read(info)
                            nested_prefix = prefix / f"__gsm_embedded_pack_{embedded_index:04d}"
                            embedded_index += 1
                            expand_bundle(embedded, nested_prefix, False)
                            continue
                        output_path = prefix.joinpath(*path.parts)
                        normalized = "/".join(output_path.parts)
                        if normalized in normalized_names:
                            raise HTTPException(status_code=422, detail="The add-on archive contains duplicate file paths.")
                        normalized_names.add(normalized)
                        normalized_bundle.writestr(normalized, bundle.read(info))

            expand_bundle(archive, PurePosixPath(), extension == ".mcaddon")

        normalized_buffer.seek(0)
        with zipfile.ZipFile(normalized_buffer) as bundle:
            manifests: list[dict[str, Any]] = []
            for info in bundle.infolist():
                path = PurePosixPath(info.filename)
                if path.name != "manifest.json":
                    continue
                if info.file_size > 1024 * 1024:
                    raise HTTPException(status_code=422, detail="An add-on manifest is too large to process.")
                try:
                    manifest = json.loads(bundle.read(info))
                except (UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as err:
                    raise HTTPException(status_code=422, detail="An add-on manifest is invalid JSON.") from err
                header = manifest.get("header") if isinstance(manifest, dict) else None
                modules = manifest.get("modules") if isinstance(manifest, dict) else None
                if not isinstance(header, dict) or not isinstance(modules, list):
                    continue
                try:
                    pack_id = str(uuid.UUID(str(header.get("uuid", ""))))
                except (ValueError, AttributeError) as err:
                    raise HTTPException(status_code=422, detail="An add-on manifest has an invalid pack UUID.") from err
                raw_version = header.get("version")
                if isinstance(raw_version, list) and len(raw_version) == 3 and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in raw_version):
                    version = raw_version
                elif isinstance(raw_version, str) and re.fullmatch(r"\d+\.\d+\.\d+", raw_version):
                    version = [int(part) for part in raw_version.split(".")]
                else:
                    raise HTTPException(status_code=422, detail="An add-on manifest must have a three-part version number.")
                module_types = {str(module.get("type", "")).lower() for module in modules if isinstance(module, dict)}
                if module_types & {"data", "script"}:
                    kind = "behavior"
                elif "resources" in module_types:
                    kind = "resource"
                else:
                    continue
                root = path.parent
                root_name = "" if str(root) == "." else "/".join(root.parts)
                display_name = str(header.get("name", "")).strip()[:120] or Path(safe_filename).stem[:120]
                manifests.append({"uuid": pack_id, "kind": kind, "name": display_name, "version": version, "root": root_name})

            if not manifests:
                raise HTTPException(status_code=422, detail="The archive has no supported behavior-pack or resource-pack manifest.")
            ids = [entry["uuid"] for entry in manifests]
            roots = [entry["root"] for entry in manifests]
            if len(ids) != len(set(ids)) or len(roots) != len(set(roots)):
                raise HTTPException(status_code=422, detail="The archive contains duplicate add-on pack IDs or manifests.")
            root_parts = [PurePosixPath(root).parts if root else () for root in roots]
            for index, parts in enumerate(root_parts):
                for other_index, other in enumerate(root_parts):
                    if index != other_index and len(parts) < len(other) and other[:len(parts)] == parts:
                        raise HTTPException(status_code=422, detail="The archive contains overlapping add-on package folders.")
    except HTTPException:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError) as err:
        raise HTTPException(status_code=422, detail="The add-on file is not a readable ZIP-based .mcaddon or .mcpack archive.") from err
    return {"filename": safe_filename, "extension": extension, "archive": normalized_buffer.getvalue(), "packs": manifests}


def bedrock_addon_pack_folder(data_root: Path, pack: dict[str, Any]) -> Path:
    kind = str(pack.get("kind", ""))
    if kind not in {"behavior", "resource"}:
        raise RuntimeError("The saved add-on has an unsupported pack type.")
    try:
        pack_id = str(uuid.UUID(str(pack.get("uuid", ""))))
    except (ValueError, AttributeError) as err:
        raise RuntimeError("The saved add-on has an invalid pack UUID.") from err
    parent = data_root / ("behavior_packs" if kind == "behavior" else "resource_packs")
    if data_root not in parent.resolve().parents:
        raise RuntimeError("The add-on pack folder is outside this server's data directory.")
    return parent / pack_id


def bedrock_world_pack_changes(data_root: Path, world_name: str, packs: list[dict[str, Any]], removing: bool = False) -> dict[Path, tuple[bytes | None, bytes]]:
    world_dir = data_root / "worlds" / world_name
    if data_root not in world_dir.resolve().parents:
        raise RuntimeError("The configured world path is outside this server's data folder.")
    changes: dict[Path, tuple[bytes | None, bytes]] = {}
    for kind in sorted({str(pack.get("kind", "")) for pack in packs}):
        if kind not in {"behavior", "resource"}:
            raise RuntimeError("The saved add-on has an unsupported pack type.")
        filename = "world_behavior_packs.json" if kind == "behavior" else "world_resource_packs.json"
        path = world_dir / filename
        if path.is_symlink():
            raise RuntimeError(f"Refusing to modify a linked add-on manifest: {filename}.")
        old = path.read_bytes() if path.is_file() else None
        try:
            entries = json.loads(old.decode("utf-8")) if old else []
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise RuntimeError(f"The existing {filename} is invalid JSON; repair it before changing add-ons.") from err
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise RuntimeError(f"The existing {filename} must contain a JSON list of pack entries.")
        ids = {str(pack.get("uuid", "")).lower() for pack in packs if pack.get("kind") == kind}
        if removing:
            updated = [entry for entry in entries if str(entry.get("pack_id", "")).lower() not in ids]
        else:
            if any(str(entry.get("pack_id", "")).lower() in ids for entry in entries):
                raise RuntimeError("An add-on pack UUID is already enabled in this world.")
            updated = [*entries, *({"pack_id": pack["uuid"], "version": pack["version"]} for pack in packs if pack.get("kind") == kind)]
        content = (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        changes[path] = (old, content)
    return changes


def write_bedrock_world_pack_changes(changes: dict[Path, tuple[bytes | None, bytes]]) -> None:
    written: list[Path] = []
    try:
        for path, (_, content) in changes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_name(path.name + ".gsm-tmp")
            temp_path.write_bytes(content)
            temp_path.replace(path)
            written.append(path)
    except Exception:
        for path in written:
            original = changes[path][0]
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    temp_path = path.with_name(path.name + ".gsm-restore")
                    temp_path.write_bytes(original)
                    temp_path.replace(path)
            except OSError:
                LOG.exception("Could not restore Bedrock add-on manifest %s", path)
        raise


def restore_bedrock_world_pack_changes(changes: dict[Path, tuple[bytes | None, bytes]]) -> None:
    for path, (original, _) in changes.items():
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                temp_path = path.with_name(path.name + ".gsm-restore")
                temp_path.write_bytes(original)
                temp_path.replace(path)
        except OSError:
            LOG.exception("Could not restore Bedrock add-on manifest %s", path)


def install_bedrock_addon_files(server_id: str, addon_id: str, addon: dict[str, Any]) -> None:
    data_root = (server_dir(server_id) / "data").resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(addon["archive"])) as bundle:
        infos = bundle.infolist()
        stage_root = data_root / ".gsm-addon-staging" / addon_id
        staged: list[tuple[dict[str, Any], Path, Path]] = []
        installed: list[Path] = []
        changes: dict[Path, tuple[bytes | None, bytes]] = {}
        manifests_written = False
        try:
            for pack in addon["packs"]:
                destination = bedrock_addon_pack_folder(data_root, pack)
                if destination.exists() or destination.is_symlink():
                    raise RuntimeError(f"A {pack['kind']} pack with UUID {pack['uuid']} already exists on this server.")
                stage = stage_root / f"{pack['kind']}-{pack['uuid']}"
                stage.mkdir(parents=True, exist_ok=False)
                root_parts = PurePosixPath(pack["root"]).parts if pack["root"] else ()
                for info in infos:
                    path = PurePosixPath(info.filename)
                    if info.is_dir() or path.parts[:len(root_parts)] != root_parts:
                        continue
                    relative_parts = path.parts[len(root_parts):]
                    if not relative_parts:
                        continue
                    output = stage.joinpath(*relative_parts)
                    if stage.resolve() not in output.resolve().parents:
                        raise RuntimeError("The add-on archive contains a path outside its package folder.")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(info) as source, output.open("xb") as target:
                        shutil.copyfileobj(source, target)
                staged.append((pack, stage, destination))
            current = read_json(config_path(server_id))
            changes = bedrock_world_pack_changes(data_root, str(current.get("world_name", "Bedrock level")), addon["packs"])
            for pack, stage, destination in staged:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if data_root not in destination.parent.resolve().parents:
                    raise RuntimeError("The add-on pack folder is outside this server's data directory.")
                stage.replace(destination)
                installed.append(destination)
            write_bedrock_world_pack_changes(changes)
            manifests_written = True
            current["bedrock_addons"] = [*current.get("bedrock_addons", []), {
                "id": addon_id,
                "name": str(addon.get("display_name") or Path(addon["filename"]).stem)[:120],
                "filename": addon["filename"],
                "extension": addon["extension"],
                "packs": [{"uuid": pack["uuid"], "kind": pack["kind"], "name": pack["name"], "version": pack["version"]} for pack in addon["packs"]],
            }]
            write_json(config_path(server_id), current)
        except Exception:
            if manifests_written:
                restore_bedrock_world_pack_changes(changes)
            for destination in installed:
                shutil.rmtree(destination, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)


def remove_bedrock_addon_files(server_id: str, addon_id: str) -> None:
    item = read_json(config_path(server_id))
    if not item:
        raise RuntimeError("Server configuration was not found.")
    addon = next((value for value in item.get("bedrock_addons", []) if value.get("id") == addon_id), None)
    if not addon:
        raise RuntimeError("The Bedrock add-on is no longer installed.")
    packs = addon.get("packs", [])
    data_root = (server_dir(server_id) / "data").resolve()
    world_name = str(item.get("world_name", "Bedrock level"))
    changes = bedrock_world_pack_changes(data_root, world_name, packs, removing=True)
    trash_root = data_root / ".gsm-addon-removal" / addon_id
    moved: list[tuple[Path, Path]] = []
    manifests_written = False
    removal_complete = False
    try:
        for pack in packs:
            target = bedrock_addon_pack_folder(data_root, pack)
            if target.is_symlink():
                raise RuntimeError("Refusing to remove a linked Bedrock add-on pack folder.")
            if data_root not in target.resolve().parents:
                raise RuntimeError("The add-on pack folder is outside this server's data directory.")
            if target.exists():
                trash_root.mkdir(parents=True, exist_ok=True)
                trash_target = trash_root / f"{pack['kind']}-{pack['uuid']}"
                target.replace(trash_target)
                moved.append((trash_target, target))
        write_bedrock_world_pack_changes(changes)
        manifests_written = True
        item["bedrock_addons"] = [value for value in item.get("bedrock_addons", []) if value.get("id") != addon_id]
        write_json(config_path(server_id), item)
        removal_complete = True
    except Exception:
        if manifests_written:
            restore_bedrock_world_pack_changes(changes)
        for source, target in reversed(moved):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.exists():
                    source.replace(target)
            except OSError:
                LOG.exception("Could not restore Bedrock add-on pack directory %s", target)
        raise
    finally:
        if removal_complete:
            shutil.rmtree(trash_root, ignore_errors=True)


def queue_bedrock_addon_change(server_id: str, operation: str, change) -> tuple[str, dict[str, Any]]:
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    require_game_capability(item, "supports_addons", "Bedrock add-ons")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    keep_running = item.get("desired_state") == "running"
    item["updating"] = True
    item["job_state"] = "addon-queued"
    write_json(config_path(server_id), item)

    def apply_addon_change():
        restart_attempted = False
        try:
            stop_container(server_id, remove=True, timeout=120)
            change()
            if keep_running:
                restart_attempted = True
                run_container(server_id)
            latest = read_json(config_path(server_id))
            if latest:
                latest["updating"] = False
                latest["job_state"] = "running" if keep_running else "stopped"
                write_json(config_path(server_id), latest)
        except Exception:
            latest = read_json(config_path(server_id))
            if keep_running and not restart_attempted:
                try:
                    run_container(server_id)
                except Exception:
                    LOG.exception("Could not restore Bedrock server %s after add-on change failed", server_id)
            latest = read_json(config_path(server_id))
            if latest:
                latest["updating"] = False
                latest["job_state"] = "addon-failed"
                write_json(config_path(server_id), latest)
            raise

    try:
        job_id = submit_job(server_id, operation, apply_addon_change)
    except Exception:
        item = read_json(config_path(server_id))
        if item:
            item["updating"] = False
            item["job_state"] = "stopped" if item.get("desired_state") == "stopped" else "running"
            write_json(config_path(server_id), item)
        raise
    return job_id, item


def safe_icon_url(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 2048:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme == "https" and parsed.netloc else ""


def validate_config(data: dict[str, Any], existing_id: str | None = None) -> dict[str, Any]:
    game_id = str(data.get("game_id") or "minecraft").strip().lower()
    try:
        driver = get_game_driver(game_id)
    except KeyError as err:
        raise HTTPException(status_code=422, detail=f"Unsupported game: {game_id or 'unspecified'}.") from err
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=422, detail="Enter a server name with 1 to 60 characters.")
    try:
        port = int(data.get("port", driver.default_port))
        min_ram = int(data.get("min_ram_mb", 1024))
        max_ram = int(data.get("max_ram_mb", 4096))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Port and RAM values must be whole numbers.")
    if port < 1024 or port > 65535 or port == 8099:
        raise HTTPException(status_code=422, detail="Choose a TCP port between 1024 and 65535, excluding 8099 (the manager UI port).")
    if min_ram < 512 or max_ram < min_ram or max_ram > 131072:
        raise HTTPException(status_code=422, detail="RAM must be at least 512 MB, with maximum RAM greater than or equal to minimum RAM.")
    def bounded_int(key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(data.get(key, default))
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"{key.replace('_', ' ').title()} must be a whole number.")
        if value < low or value > high:
            raise HTTPException(status_code=422, detail=f"{key.replace('_', ' ').title()} must be between {low} and {high}.")
        return value

    try:
        game_options = driver.validate_options(data)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    world_and_access = validate_world_and_access_options(data, driver)
    candidate = {
        **data,
        **game_options,
        **world_and_access,
        "game_id": driver.game_id,
        "port": port,
    }
    candidate_tcp = host_ports_for(candidate, "tcp")
    candidate_udp = host_ports_for(candidate, "udp")
    for other in all_configs():
        if other.get("id") == existing_id:
            continue
        tcp_conflicts = candidate_tcp & host_ports_for(other, "tcp")
        if tcp_conflicts:
            assigned = min(tcp_conflicts)
            raise HTTPException(status_code=409, detail=f"TCP port {assigned} is already assigned to {other.get('name', 'another server')}.")
        udp_conflicts = candidate_udp & host_ports_for(other, "udp")
        if udp_conflicts:
            assigned = min(udp_conflicts)
            raise HTTPException(status_code=409, detail=f"UDP port {assigned} is already assigned to {other.get('name', 'another server')}.")
    capabilities = driver.capabilities
    crossplay_enabled = game_options.get("crossplay_enabled") is True
    feature_flags = {
        "auto_pause_enabled": "supports_auto_pause",
        "auto_stop_enabled": "supports_auto_stop",
        "auto_update_enabled": "supports_auto_update",
        "wake_on_join": "supports_wake_on_join",
    }
    for flag, capability in feature_flags.items():
        if data.get(flag) is True and not getattr(capabilities, capability):
            raise HTTPException(status_code=422, detail=f"{driver.display_name} does not support {flag.replace('_enabled', '').replace('_', ' ')}.")
    resource_pack_options: dict[str, Any] = {}
    if driver.game_id == "minecraft":
        resource_pack_url = str(data.get("resource_pack_url", "")).strip()
        if len(resource_pack_url) > 2048:
            raise HTTPException(status_code=422, detail="The resource-pack URL must be 2048 characters or shorter.")
        if resource_pack_url:
            parsed_resource_url = urlsplit(resource_pack_url)
            if parsed_resource_url.scheme != "https" or not parsed_resource_url.hostname or parsed_resource_url.username or parsed_resource_url.password:
                raise HTTPException(status_code=422, detail="Use an HTTPS resource-pack URL that players can reach without signing in.")
        resource_pack_sha1 = str(data.get("resource_pack_sha1", "")).strip().lower()
        if resource_pack_sha1 and not re.fullmatch(r"[0-9a-f]{40}", resource_pack_sha1):
            raise HTTPException(status_code=422, detail="Resource-pack SHA-1 must contain exactly 40 hexadecimal characters.")
        resource_pack_options = {
            "resource_pack_url": resource_pack_url,
            "resource_pack_sha1": resource_pack_sha1,
            "resource_pack_enforce": data.get("resource_pack_enforce") is True,
        }
    return {
        "game_id": driver.game_id,
        "name": name,
        "port": port,
        "min_ram_mb": min_ram,
        "max_ram_mb": max_ram,
        **game_options,
        **world_and_access,
        **resource_pack_options,
        "auto_pause_enabled": data.get("auto_pause_enabled", False) is True and capabilities.supports_auto_pause and not crossplay_enabled,
        "auto_pause_minutes": bounded_int("auto_pause_minutes", 20, 5, 1440),
        "auto_stop_enabled": data.get("auto_stop_enabled", False) is True and capabilities.supports_auto_stop and not crossplay_enabled,
        "auto_stop_days": bounded_int("auto_stop_days", 3, 1, 365),
        "auto_update_enabled": data.get("auto_update_enabled", False) is True and capabilities.supports_auto_update,
        "auto_update_idle_hours": bounded_int("auto_update_idle_hours", 3, 1, 168),
        "wake_on_join": data.get("wake_on_join", True) is not False and capabilities.supports_wake_on_join and not crossplay_enabled,
    }

def run_container(server_id: str) -> None:
    if SHUTTING_DOWN:
        return
    item = read_json(config_path(server_id))
    if not item:
        raise RuntimeError("Server configuration was not found.")
    cli = docker_client()
    driver = game_driver_for(item)
    image = driver.image_for(item)

    with server_container_lock(server_id):
        item = read_json(config_path(server_id))
        if not item:
            raise RuntimeError("Server configuration was not found.")
        if item.get("force_stopped") or item.get("desired_state") != "running":
            item["desired_state"] = "stopped"
            item["job_state"] = "force-stopped" if item.get("force_stopped") else "stopped"
            write_json(config_path(server_id), item)
            return
        try:
            existing = get_container(server_id)
            if existing.status == "running":
                return
            existing.remove(force=True)
        except NotFound:
            pass
        item["job_state"] = "pulling"
        write_json(config_path(server_id), item)

    # Image pulls can take a long time. Leave the container action lock free so
    # Force exit can return immediately; the second check below prevents this
    # older job from creating a container after the force-exit request.
    try:
        cli.images.get(image)
    except ImageNotFound:
        cli.images.pull(image)
    if SHUTTING_DOWN:
        return

    with server_container_lock(server_id):
        item = read_json(config_path(server_id))
        if not item:
            raise RuntimeError("Server configuration was not found.")
        if item.get("force_stopped") or item.get("desired_state") != "running":
            item["desired_state"] = "stopped"
            item["job_state"] = "force-stopped" if item.get("force_stopped") else "stopped"
            write_json(config_path(server_id), item)
            return
        try:
            existing = get_container(server_id)
            if existing.status == "running":
                return
            existing.remove(force=True)
        except NotFound:
            pass
        item["job_state"] = "starting"
        write_json(config_path(server_id), item)
        data_source = host_server_data_dir(server_id)
        driver = game_driver_for(item)
        labels = {
            MANAGER_LABEL: "true",
            f"{MANAGER_LABEL}.server_id": server_id,
            f"{MANAGER_LABEL}.game_id": driver.game_id,
        }
        if driver.legacy_manager_label:
            labels[driver.legacy_manager_label] = "true"
            labels[f"{driver.legacy_manager_label}.server_id"] = server_id
        create_options: dict[str, Any] = {
            "image": image,
            "name": driver.container_name(server_id),
            "labels": labels,
            "environment": driver.environment_for(item, {**settings(), "curseforge_api_key": current_curseforge_api_key()}),
            "volumes": {str(data_source): {"bind": "/data", "mode": "rw"}},
            "restart_policy": {"Name": "unless-stopped"},
            "tty": True,
            "stdin_open": True,
        }
        if driver.game_id == "minecraft_bedrock":
            create_options["mem_limit"] = int(item.get("max_ram_mb", 4096)) * 1024 * 1024
        if driver.container_network:
            create_options["network"] = driver.container_network
        port_bindings = driver.container_port_bindings(item)
        if port_bindings:
            create_options["ports"] = port_bindings
        container = cli.containers.create(**create_options)
        try:
            container.start()
        except Exception:
            try:
                container.remove(force=True)
            except DockerException:
                pass
            raise
        item["job_state"] = "running"
        item["desired_state"] = "running"
        write_json(config_path(server_id), item)
    threading.Thread(target=sync_server_access, args=(server_id,), name=f"access-sync-{server_id[:8]}", daemon=True).start()


def stop_container(server_id: str, remove: bool = False, timeout: int = 120, force: bool = False) -> None:
    if force:
        # Serialize a force exit with container creation/start, but let it cut
        # through a normal stop that may be waiting on Docker's restart backoff.
        with server_container_lock(server_id):
            _stop_container(server_id, remove=remove, timeout=timeout, force=True)
        return
    else:
        # Ordinary stops can wait through Docker's restart delay. Do not hold
        # the start lock here: Force exit must be able to interrupt that wait.
        _stop_container(server_id, remove=remove, timeout=timeout, force=force)


def _stop_container(server_id: str, remove: bool = False, timeout: int = 120, force: bool = False) -> None:
    try:
        container = get_container(server_id)
    except NotFound:
        return

    if force:
        # A forced exit removes only the container. Server files live in a bind
        # mount, so they remain intact for a later Start, reset, or delete.
        # Removing the container also avoids waiting for Docker's restart backoff.
        try:
            container.update(restart_policy={"Name": "no"})
        except APIError as err:
            if err.status_code == 404:
                return
            if err.status_code not in {304, 409}:
                LOG.warning("Could not disable restart policy before force-removing server %s: %s", server_id, err)
        try:
            container.remove(force=True, v=False)
        except NotFound:
            return
        except APIError as err:
            if err.status_code == 404:
                return
            raise RuntimeError(f"Docker could not force-remove server container {server_id}: {err}") from err
        return

    deadline = time.monotonic() + max(timeout, 10) + 65
    last_conflict = ""

    while True:
        try:
            container.reload()
        except NotFound:
            return

        status = container.status
        if status == "paused":
            try:
                container.unpause()
            except APIError as err:
                if err.status_code == 404:
                    return
                if err.status_code not in {304, 409}:
                    raise
                last_conflict = str(err)
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Timed out unpausing server container {server_id}: {last_conflict}") from err
                time.sleep(0.25)
            continue

        if status == "running":
            try:
                container.stop(timeout=timeout)
            except APIError as err:
                if err.status_code == 404:
                    return
                if err.status_code not in {304, 409}:
                    raise
                last_conflict = str(err)
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Timed out stopping server container {server_id}: {last_conflict}") from err
                time.sleep(0.25)
            continue

        if status == "restarting":
            if time.monotonic() >= deadline:
                detail = f": {last_conflict}" if last_conflict else ""
                raise RuntimeError(f"Timed out waiting for server container {server_id} to leave Docker's restarting state{detail}")
            time.sleep(0.25)
            continue

        if status in {"created", "exited", "dead"}:
            if remove:
                try:
                    container.remove()
                except NotFound:
                    return
                except APIError as err:
                    if err.status_code != 409:
                        raise
                    last_conflict = str(err)
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"Timed out removing server container {server_id}: {last_conflict}") from err
                    time.sleep(0.25)
                    continue
            elif status == "exited" and not force:
                # If the server crashed before this stop request, disable its
                # pending restart. ensure_running restores this policy on start.
                try:
                    container.update(restart_policy={"Name": "no"})
                except APIError as err:
                    if err.status_code not in {304, 409}:
                        raise
                    last_conflict = str(err)
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"Timed out disabling restart for server container {server_id}: {last_conflict}") from err
                    time.sleep(0.25)
                    continue
            return

        if time.monotonic() >= deadline:
            detail = f": {last_conflict}" if last_conflict else ""
            raise RuntimeError(f"Timed out waiting for server container {server_id} to stop (Docker state: {status}){detail}")
        time.sleep(0.25)


def cancel_server_jobs(server_id: str, reason: str = "Cancelled by force exit") -> None:
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.get("server_id") != server_id or job.get("state") in {"complete", "failed", "cancelled"}:
                continue
            job["cancel_requested"] = True
            if job.get("state") == "queued":
                job["state"] = "cancelled"
                job["message"] = reason
            else:
                job["message"] = "Force exit requested; restart actions are disabled."


def submit_job(server_id: str, operation: str, fn) -> str:
    job_id = str(uuid.uuid4())
    job = {"id": job_id, "server_id": server_id, "operation": operation, "state": "queued", "executing": False, "message": "Queued", "created_at": __import__("datetime").datetime.now().isoformat()}
    with JOBS_LOCK:
        JOBS[job_id] = job

    def run():
        with JOBS_LOCK:
            if JOBS[job_id].get("cancel_requested"):
                JOBS[job_id]["state"] = "cancelled"
                JOBS[job_id]["message"] = "Cancelled before it started."
                return
            JOBS[job_id]["state"] = "running"
            JOBS[job_id]["message"] = "Working"
        try:
            with server_lock(server_id):
                with JOBS_LOCK:
                    if JOBS[job_id].get("cancel_requested"):
                        JOBS[job_id]["state"] = "cancelled"
                        JOBS[job_id]["message"] = "Cancelled before it started."
                        return
                    JOBS[job_id]["executing"] = True
                fn()
            with JOBS_LOCK:
                JOBS[job_id]["state"] = "cancelled" if JOBS[job_id].get("cancel_requested") else "complete"
                JOBS[job_id]["message"] = "Cancelled by force exit." if JOBS[job_id].get("cancel_requested") else "Complete"
        except Exception as err:
            LOG.exception("Server %s job failed", server_id)
            with JOBS_LOCK:
                cancelled = JOBS[job_id].get("cancel_requested")
                JOBS[job_id]["state"] = "cancelled" if cancelled else "failed"
                JOBS[job_id]["message"] = "Cancelled by force exit." if cancelled else str(err)[:500]

    POOL.submit(run)
    return job_id


def ensure_running(server_id: str) -> None:
    item = read_json(config_path(server_id))
    if item.get("force_stopped") or item.get("desired_state") != "running":
        return
    status, container = current_status(server_id)
    if container is not None:
        expected_source = host_server_data_dir(server_id).resolve()
        actual_source = next(
            (
                Path(mount["Source"]).resolve()
                for mount in container.attrs.get("Mounts", [])
                if mount.get("Destination") == "/data" and mount.get("Source")
            ),
            None,
        )
        if actual_source != expected_source:
            # Existing servers were created before the switch from private /data to
            # /share/game_server_manager. Recreate only the container; its world remains mounted
            # from the newly migrated host directory.
            stop_container(server_id, remove=True)
            status, container = "stopped", None
    if status in {"running", "restarting"}:
        return
    if container is not None and status == "paused":
        with server_container_lock(server_id):
            item = read_json(config_path(server_id))
            if item.get("force_stopped") or item.get("desired_state") != "running":
                return
            container.reload()
            if container.status == "paused":
                container.unpause()
            item = read_json(config_path(server_id))
            item["desired_state"] = "running"
            item["job_state"] = "running"
            item["auto_paused"] = False
            write_json(config_path(server_id), item)
        return
    if container is not None and status == "exited":
        with server_container_lock(server_id):
            item = read_json(config_path(server_id))
            if item.get("force_stopped") or item.get("desired_state") != "running":
                return
            container.reload()
            if container.status == "exited":
                container.update(restart_policy={"Name": "unless-stopped"})
                container.start()
            item = read_json(config_path(server_id))
            item["desired_state"] = "running"
            item["job_state"] = "running"
            item["auto_stopped"] = False
            item["auto_paused"] = False
            write_json(config_path(server_id), item)
        return
    run_container(server_id)


def wake_server_locked(server_id: str) -> None:
    with server_lock(server_id):
        ensure_running(server_id)


def encode_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    output = bytearray()
    while value & 0xFFFFFF80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value & 0x7F)
    return bytes(output)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    result = 0
    shift = 0
    while offset < len(data) and shift < 35:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
    raise ValueError("Invalid Minecraft VarInt.")


async def read_mc_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    prefix = bytearray()
    length = 0
    shift = 0
    while shift < 35:
        byte = (await reader.readexactly(1))[0]
        prefix.append(byte)
        length |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    if length < 0 or length > 2_097_152:
        raise ValueError("Minecraft packet is too large.")
    payload = await reader.readexactly(length)
    return bytes(prefix) + payload, payload


def parse_handshake(payload: bytes) -> int | None:
    try:
        packet_id, offset = decode_varint(payload)
        if packet_id != 0:
            return None
        _, offset = decode_varint(payload, offset)
        address_length, offset = decode_varint(payload, offset)
        offset += address_length + 2
        intent, _ = decode_varint(payload, offset)
        return intent
    except (ValueError, IndexError):
        return None


def mc_packet(packet_id: int, payload: bytes = b"") -> bytes:
    body = encode_varint(packet_id) + payload
    return encode_varint(len(body)) + body


def mc_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return encode_varint(len(encoded)) + encoded


async def close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass


async def send_login_disconnect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, reason: str) -> None:
    try:
        await asyncio.wait_for(read_mc_frame(reader), timeout=8)
        payload = json.dumps({"text": reason}, ensure_ascii=False, separators=(",", ":"))
        writer.write(mc_packet(0, mc_string(payload)))
        await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
        pass


async def send_offline_status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, message: str) -> None:
    try:
        frame, payload = await asyncio.wait_for(read_mc_frame(reader), timeout=5)
        packet_id, offset = decode_varint(payload)
        if packet_id == 0:
            status = {
                "version": {"name": "Game Server Manager", "protocol": -1},
                "players": {"max": 0, "online": 0, "sample": []},
                "description": {"text": message},
            }
            data = json.dumps(status, ensure_ascii=False, separators=(",", ":"))
            writer.write(mc_packet(0, mc_string(data)))
            await writer.drain()
            try:
                ping_frame, ping_payload = await asyncio.wait_for(read_mc_frame(reader), timeout=2)
                ping_id, ping_offset = decode_varint(ping_payload)
                if ping_id == 1:
                    writer.write(mc_packet(1, ping_payload[ping_offset:]))
                    await writer.drain()
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
                pass
        elif packet_id == 1 and len(payload) >= offset + 8:
            writer.write(mc_packet(1, payload[offset:offset + 8]))
            await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
        pass


def backend_address(server_id: str) -> tuple[str, int] | None:
    try:
        container = get_container(server_id)
        if container.status != "running":
            return None
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        details = networks.get(MINECRAFT_NETWORK_NAME, {})
        address = details.get("IPAddress")
        return (str(address), 25565) if address else None
    except (DockerException, NotFound):
        return None


async def proxy_to_backend(server_id: str, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter, handshake: bytes) -> bool:
    address = await asyncio.to_thread(backend_address, server_id)
    if not address:
        return False
    try:
        backend_reader, backend_writer = await asyncio.wait_for(asyncio.open_connection(*address), timeout=5)
        backend_writer.write(handshake)
        await backend_writer.drain()

        async def copy_stream(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
            while True:
                data = await source.read(65536)
                if not data:
                    break
                destination.write(data)
                await destination.drain()

        tasks = [
            asyncio.create_task(copy_stream(client_reader, backend_writer)),
            asyncio.create_task(copy_stream(backend_reader, client_writer)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        backend_writer.close()
        await backend_writer.wait_closed()
        return True
    except (asyncio.TimeoutError, ConnectionError, OSError, DockerException):
        return False


async def handle_minecraft_connection(server_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        handshake, payload = await asyncio.wait_for(read_mc_frame(reader), timeout=8)
        intent = parse_handshake(payload)
        if intent not in {1, 2}:
            return
        item = read_json(config_path(server_id))
        if not item or item.get("deleting"):
            return
        status, _ = current_status(server_id)
        if intent == 1:
            if status == "running" and not item.get("updating"):
                if await proxy_to_backend(server_id, reader, writer, handshake):
                    return
            message = UPDATE_DISCONNECT_REASON if item.get("updating") else (
                "Server is starting." if status in {"paused", "created", "restarting"} else "Server is stopped."
            )
            await send_offline_status(reader, writer, message)
            return
        if item.get("updating"):
            await send_login_disconnect(reader, writer, UPDATE_DISCONNECT_REASON)
            return
        if status == "paused":
            try:
                await asyncio.wait_for(asyncio.to_thread(wake_server_locked, server_id), timeout=8)
            except Exception:
                await send_login_disconnect(reader, writer, "Server is starting. Please try again shortly.")
                return
            status, _ = current_status(server_id)
        elif status in {"stopped", "exited", "created"} and item.get("wake_on_join", True):
            should_wake = not item.get("force_stopped") and (item.get("auto_stopped") or item.get("desired_state") == "running")
            if should_wake:
                item["desired_state"] = "running"
                item["auto_stopped"] = False
                item["job_state"] = "starting"
                write_json(config_path(server_id), item)
                submit_job(server_id, "wake on join", lambda: ensure_running(server_id))
                await send_login_disconnect(reader, writer, "Server is starting. Please try again shortly.")
                return
        if status == "running":
            if await proxy_to_backend(server_id, reader, writer, handshake):
                return
            await send_login_disconnect(reader, writer, "Server is starting. Please try again shortly.")
        else:
            await send_login_disconnect(reader, writer, "Server is stopped.")
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
        pass
    except Exception:
        LOG.exception("Minecraft gateway request failed for server %s", server_id)
    finally:
        await close_writer(writer)


def gateway_token(server_id: str) -> str:
    return hmac.new(GATEWAY_SECRET.encode("ascii"), server_id.encode("ascii"), hashlib.sha256).hexdigest()


async def handle_gateway_relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        header = await asyncio.wait_for(reader.readexactly(GATEWAY_RELAY_HEADER_SIZE), timeout=5)
        if header[:4] != GATEWAY_RELAY_MAGIC:
            return
        server_id = header[4:40].decode("ascii", errors="strict")
        token = header[40:].decode("ascii", errors="strict")
        try:
            server_id = str(uuid.UUID(server_id))
        except ValueError:
            return
        if not secrets.compare_digest(token, gateway_token(server_id)):
            return
        await handle_minecraft_connection(server_id, reader, writer)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, UnicodeDecodeError):
        pass
    except Exception:
        LOG.exception("Minecraft gateway relay failed")
    finally:
        await close_writer(writer)


async def start_gateway_relay() -> None:
    global GATEWAY_RELAY_SERVER
    if not MINECRAFT_NETWORK_IP:
        raise RuntimeError("The private Minecraft Docker network is not ready.")
    if GATEWAY_RELAY_SERVER:
        GATEWAY_RELAY_SERVER.close()
        await GATEWAY_RELAY_SERVER.wait_closed()
    GATEWAY_RELAY_SERVER = await asyncio.start_server(
        handle_gateway_relay,
        host=MINECRAFT_NETWORK_IP,
        port=GATEWAY_INTERNAL_PORT,
        reuse_address=True,
        backlog=128,
    )


async def stop_gateway_relay() -> None:
    global GATEWAY_RELAY_SERVER
    if GATEWAY_RELAY_SERVER:
        GATEWAY_RELAY_SERVER.close()
        await GATEWAY_RELAY_SERVER.wait_closed()
        GATEWAY_RELAY_SERVER = None


def _gateway_container_name(server_id: str) -> str:
    return f"{GATEWAY_CONTAINER_PREFIX}{server_id}"


def _start_gateway_container(container) -> None:
    container.start()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        container.reload()
        if container.status == "running":
            return
        if container.status in {"exited", "dead"}:
            logs = container.logs(tail=30).decode("utf-8", errors="replace")
            raise OSError(f"Gateway container exited while starting: {logs[-1200:]}")
        time.sleep(0.1)
    raise OSError("Gateway container did not become ready in time.")


def _ensure_gateway_container(server_id: str) -> None:
    item = read_json(config_path(server_id))
    if not item or item.get("deleting"):
        return
    if not MINECRAFT_NETWORK_IP or not MANAGER_IMAGE or not GATEWAY_SECRET:
        raise OSError("The private Minecraft gateway network is not ready.")
    port = int(item["port"])
    mode = "host" if HOST_NETWORKING_ENABLED else MINECRAFT_NETWORK_NAME
    listen_port = port if HOST_NETWORKING_ENABLED else GATEWAY_CONTAINER_PORT
    expected_env = {
        "MCM_SERVER_ID": server_id,
        "MCM_MANAGER_HOST": MINECRAFT_NETWORK_IP,
        "MCM_MANAGER_PORT": str(GATEWAY_INTERNAL_PORT),
        "MCM_GATEWAY_PORT": str(listen_port),
        "MCM_GATEWAY_TOKEN": gateway_token(server_id),
    }
    client = docker_client()
    name = _gateway_container_name(server_id)
    current = None
    try:
        current = client.containers.get(name)
        current.reload()
        labels = current.attrs.get("Config", {}).get("Labels") or {}
        if labels.get(GATEWAY_LABEL) != "true" or labels.get(f"{LEGACY_MINECRAFT_MANAGER_LABEL}.server_id") != server_id:
            raise OSError(f"Docker container name {name} is occupied by a container this app does not manage.")
        env = {}
        for value in current.attrs.get("Config", {}).get("Env") or []:
            if "=" in value:
                key, val = value.split("=", 1)
                env[key] = val
        host_config = current.attrs.get("HostConfig", {})
        current_mode = str(host_config.get("NetworkMode") or "")
        current_networks = current.attrs.get("NetworkSettings", {}).get("Networks", {})
        current_image = str(current.attrs.get("Config", {}).get("Image") or "")
        current_port_bindings = host_config.get("PortBindings") or {}
        bridge_port_matches = False
        if not HOST_NETWORKING_ENABLED:
            bindings = current_port_bindings.get(f"{GATEWAY_CONTAINER_PORT}/tcp") or []
            bridge_port_matches = any(str(binding.get("HostPort")) == str(port) for binding in bindings)
        network_matches = current_mode == "host" if HOST_NETWORKING_ENABLED else MINECRAFT_NETWORK_NAME in current_networks
        matches = (
            current.status == "running"
            and network_matches
            and current_image == MANAGER_IMAGE
            and all(env.get(key) == value for key, value in expected_env.items())
            and (HOST_NETWORKING_ENABLED or bridge_port_matches)
        )
        if matches:
            GATEWAYS[server_id] = {"port": port, "mode": mode, "manager_host": MINECRAFT_NETWORK_IP}
            return
        current.remove(force=True)
    except NotFound:
        current = None
    container = None
    network_options = (
        {"network_mode": "host"}
        if HOST_NETWORKING_ENABLED
        else {"network": MINECRAFT_NETWORK_NAME, "ports": {f"{GATEWAY_CONTAINER_PORT}/tcp": port}}
    )
    try:
        container = client.containers.create(
            image=MANAGER_IMAGE,
            name=name,
            labels={
                LEGACY_MINECRAFT_MANAGER_LABEL: "true",
                GATEWAY_LABEL: "true",
                f"{LEGACY_MINECRAFT_MANAGER_LABEL}.server_id": server_id,
            },
            entrypoint=["python", "-m", "manager.gateway"],
            environment=expected_env,
            restart_policy={"Name": "unless-stopped"},
            user="65534:65534",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            tmpfs={"/tmp": "rw,noexec,nosuid,size=16m"},
            **network_options,
        )
        _start_gateway_container(container)
        GATEWAYS[server_id] = {"port": port, "mode": mode, "manager_host": MINECRAFT_NETWORK_IP}
    except (DockerException, OSError) as err:
        if container is not None:
            try:
                container.remove(force=True)
            except DockerException:
                pass
        if isinstance(err, OSError):
            raise
        raise OSError(f"Could not start the Minecraft gateway on TCP port {port}: {err}") from err


async def ensure_gateway(server_id: str) -> None:
    try:
        await asyncio.to_thread(_ensure_gateway_container, server_id)
    except DockerException as err:
        raise OSError(f"Could not maintain gateway for server {server_id}: {err}") from err


async def ensure_game_networking(server_id: str) -> None:
    item = read_json(config_path(server_id))
    if not item:
        return
    driver = game_driver_for(item)
    if driver.networking_adapter == "minecraft_gateway":
        await ensure_gateway(server_id)
    elif driver.networking_adapter == "docker_ports":
        return
    else:
        raise OSError(f"Game {driver.display_name} has an unknown networking adapter.")



def cleanup_stale_gateways(valid_server_ids: set[str]) -> None:
    for container in docker_client().containers.list(all=True):
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if labels.get(GATEWAY_LABEL) != "true":
            continue
        server_id = str(labels.get(f"{LEGACY_MINECRAFT_MANAGER_LABEL}.server_id", ""))
        if server_id not in valid_server_ids:
            try:
                container.remove(force=True)
                LOG.info("Removed orphaned gateway container %s", container.name)
            except (DockerException, NotFound):
                LOG.exception("Could not remove orphaned gateway container %s", container.name)


def _remove_gateway_container(server_id: str) -> None:
    name = _gateway_container_name(server_id)
    try:
        container = docker_client().containers.get(name)
        container.reload()
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if labels.get(GATEWAY_LABEL) != "true" or labels.get(f"{LEGACY_MINECRAFT_MANAGER_LABEL}.server_id") != server_id:
            raise RuntimeError(f"Docker container name {name} is occupied by a container this app does not manage.")
        container.remove(force=True)
    except NotFound:
        pass


async def close_gateway(server_id: str) -> None:
    GATEWAYS.pop(server_id, None)
    await asyncio.to_thread(_remove_gateway_container, server_id)


async def close_game_networking(server_id: str) -> None:
    item = read_json(config_path(server_id))
    if item and game_driver_for(item).networking_adapter == "minecraft_gateway":
        await close_gateway(server_id)

def rcon_output(server_id: str, command: str) -> str:
    item = read_json(config_path(server_id))
    if not item:
        raise RuntimeError("Server configuration was not found.")
    driver = game_driver_for(item)
    if not driver.capabilities.supports_console:
        raise RuntimeError(f"{driver.display_name} does not provide a console command channel.")
    container = get_container(server_id)
    if container.status != "running":
        raise RuntimeError("The server is not running.")
    return driver.send_console_command(container, command)


def _command_player_name(name: str) -> str:
    # validate_world_and_access_options permits only simple names, so quoting is
    # sufficient for Bedrock gamertags containing spaces and cannot add commands.
    return '"' + name + '"'


def sync_server_access(server_id: str) -> None:
    """Apply configured allow-list, ban-list, and Bedrock world-rule changes."""
    try:
        item = read_json(config_path(server_id))
        if not item:
            return
        driver = game_driver_for(item)
        deadline = time.monotonic() + 300
        ready = False
        while time.monotonic() < deadline:
            status, container = current_status(server_id)
            if status != "running" or container is None:
                return
            try:
                if driver.game_id == "minecraft":
                    output = driver.send_console_command(container, "list")
                    ready = driver.parse_player_count(output) is not None
                else:
                    container.reload()
                    health = container.attrs.get("State", {}).get("Health") or {}
                    if health.get("Status"):
                        ready = health.get("Status") == "healthy"
                    else:
                        output = driver.send_console_command(container, "list")
                        ready = bool(output.strip())
                if ready:
                    break
            except (DockerException, RuntimeError, OSError):
                pass
            time.sleep(3)
        if not ready:
            LOG.warning("Could not reach %s console to apply access settings for %s", driver.display_name, item.get("name"))
            return

        with server_lock(server_id):
            item = read_json(config_path(server_id))
            if not item or current_status(server_id)[0] != "running":
                return
            driver = game_driver_for(item)
            previous = item.get("access_lists_applied") or {}
            old_whitelist = set(previous.get("whitelist_players", []))
            old_blacklist = set(previous.get("blacklist_players", []))
            old_operators = set(previous.get("operators", []))
            old_members = set(previous.get("members", []))
            old_visitors = set(previous.get("visitors", []))
            wanted_whitelist = set(item.get("whitelist_players", [])) if item.get("whitelist_enabled") else set()
            wanted_blacklist = set(item.get("blacklist_players", [])) if item.get("blacklist_enabled") else set()
            list_command = "allowlist" if driver.game_id == "minecraft_bedrock" else "whitelist"

            commands: list[tuple[str, str, str]] = []
            commands.extend(("operators", "deop", player) for player in sorted(old_operators - set(item.get("operators", []))))
            commands.extend(("operators", "op", player) for player in sorted(set(item.get("operators", [])) - old_operators))
            if driver.game_id == "minecraft_bedrock":
                commands.extend(("members", "remove", player) for player in sorted(old_members - set(item.get("members", []))))
                commands.extend(("members", "member", player) for player in sorted(set(item.get("members", [])) - old_members))
                commands.extend(("visitors", "remove", player) for player in sorted(old_visitors - set(item.get("visitors", []))))
                commands.extend(("visitors", "visitor", player) for player in sorted(set(item.get("visitors", [])) - old_visitors))
            commands.extend(("whitelist_players", "remove", player) for player in sorted(old_whitelist - wanted_whitelist))
            commands.extend(("whitelist_players", "add", player) for player in sorted(wanted_whitelist - old_whitelist))
            if bool(previous.get("whitelist_enabled", False)) != bool(item.get("whitelist_enabled")):
                commands.append(("whitelist_enabled", "on" if item.get("whitelist_enabled") else "off", ""))
            commands.extend(("blacklist_players", "pardon", player) for player in sorted(old_blacklist - wanted_blacklist))
            commands.extend(("blacklist_players", "ban", player) for player in sorted(wanted_blacklist - old_blacklist))
            for key, action, player in commands:
                if key == "whitelist_enabled":
                    command = f"{list_command} {action}"
                elif key == "whitelist_players":
                    command = f"{list_command} {action} {_command_player_name(player)}"
                elif key in {"operators", "blacklist_players"}:
                    command = f"{action} {_command_player_name(player)}"
                else:
                    permission = str(item.get("default_player_permission_level", "member")) if action == "remove" else action
                    command = f"permission set {_command_player_name(player)} {permission}"
                driver.send_console_command(get_container(server_id), command)
                if key == "whitelist_enabled":
                    previous["whitelist_enabled"] = action == "on"
                elif key == "whitelist_players":
                    current = set(previous.get("whitelist_players", []))
                    current.discard(player) if action == "remove" else current.add(player)
                    previous["whitelist_players"] = sorted(current)
                elif key == "blacklist_players":
                    current = set(previous.get("blacklist_players", []))
                    current.discard(player) if action == "pardon" else current.add(player)
                    previous["blacklist_players"] = sorted(current)
                elif key in {"members", "visitors"}:
                    current = set(previous.get(key, []))
                    current.discard(player) if action == "remove" else current.add(player)
                    previous[key] = sorted(current)
                else:
                    current = set(previous.get(key, []))
                    current.discard(player) if action == "deop" else current.add(player)
                    previous[key] = sorted(current)
                item["access_lists_applied"] = previous
                write_json(config_path(server_id), item)

            if driver.game_id == "minecraft_bedrock":
                applied_world = item.get("world_settings_applied") or {}
                wanted_pvp = bool(item.get("pvp_enabled", True))
                if applied_world.get("pvp_enabled") != wanted_pvp:
                    driver.send_console_command(get_container(server_id), f"gamerule pvp {str(wanted_pvp).lower()}")
                    item["world_settings_applied"] = {"pvp_enabled": wanted_pvp}
                    write_json(config_path(server_id), item)
    except Exception:
        LOG.exception("Could not apply access settings for server %s", server_id)


def online_player_count(server_id: str) -> int | None:
    item = read_json(config_path(server_id))
    if not item:
        return None
    driver = game_driver_for(item)
    if not driver.capabilities.supports_player_counts:
        return None
    try:
        output = rcon_output(server_id, "list")
    except (DockerException, RuntimeError):
        return None
    return driver.parse_player_count(output)


def _compatible_modrinth_version(version: dict[str, Any], game_version: str, loader: str) -> bool:
    games = version.get("game_versions") or []
    loaders = [str(value).lower() for value in version.get("loaders") or []]
    return game_version in games and (not loader or loader.lower() in loaders)


def provider_update(item: dict[str, Any], platform: str, project_id: str, current_id: str, kind: str) -> dict[str, Any] | None:
    game_version = str(item.get("minecraft_version", "LATEST"))
    loader = server_mod_loader(item).lower()
    try:
        if platform == "modrinth":
            response = httpx.get(
                f"{MODRINTH_BASE}/project/{project_id}/version",
                params={"game_versions": json.dumps([game_version]), "loaders": json.dumps([loader]) if loader else "[]"},
                timeout=15,
                headers={"User-Agent": "Gamerz32/MinecraftServerManager/2.1"},
            )
            response.raise_for_status()
            versions = response.json()
            for version in versions:
                if not _compatible_modrinth_version(version, game_version, loader):
                    continue
                latest_id = str(version.get("id", ""))
                if latest_id and latest_id != current_id:
                    return {"kind": kind, "platform": platform, "project_id": project_id,
                        "version_id": latest_id, "name": str(version.get("name") or version.get("version_number") or "New version")}
                break
        elif platform == "curseforge":
            api_key = current_curseforge_api_key()
            if not project_id.isdigit():
                return None
            if not api_key:
                raise ValueError("Add a CurseForge API key to check this project.")
            params: dict[str, Any] = {"pageSize": 50, "index": 0, "gameVersion": game_version, "sortField": 3, "sortOrder": "desc"}
            loader_id = CF_LOADER_IDS.get(loader.upper())
            if loader_id:
                params["modLoaderType"] = loader_id
            response = httpx.get(
                f"{CURSEFORGE_BASE}/mods/{project_id}/files",
                params=params,
                headers={"x-api-key": api_key, "Accept": "application/json"},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json().get("data") or []
            for version in data:
                if game_version not in [str(value) for value in (version.get("gameVersions") or [])]:
                    continue
                latest_id = str(version.get("id", ""))
                if latest_id and latest_id != current_id:
                    return {"kind": kind, "platform": platform, "project_id": project_id,
                        "file_id": latest_id, "name": str(version.get("displayName") or version.get("fileName") or "New version")}
                break
        elif platform == "hangar" and kind == "plugin" and project_id.isdigit():
            response = httpx.get(
                f"{HANGAR_BASE}/projects/{project_id}/versions",
                params={"limit": 50, "offset": 0, "platform": "PAPER"},
                timeout=15,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            versions = response.json().get("result") or []
            game = str(item.get("minecraft_version", "")).strip()
            if game.upper() in {"", "LATEST", "SNAPSHOT"}:
                return None
            for version in versions:
                if not isinstance(version, dict) or version.get("visibility") != "public":
                    continue
                record = hangar_version_record(version, game)
                latest_id = record["id"]
                if not record["compatible"] or not record["installable"]:
                    continue
                if latest_id and latest_id != current_id:
                    return {"kind": kind, "platform": platform, "project_id": project_id,
                        "version_id": latest_id, "download_url": record["download_url"],
                        "name": record["number"] or "New version"}
                break
    except (httpx.HTTPError, ValueError, TypeError) as err:
        LOG.warning("Could not check %s updates for project %s: %s", platform, project_id, err)
        raise
    return None


def parse_dockerhub_image_reference(image: str) -> tuple[str, str] | None:
    """Accept Docker Hub image references and return a safe repo path and tag."""
    if not isinstance(image, str):
        return None
    reference = image.strip()
    if not reference or any(character.isspace() for character in reference) or reference.count("@") > 1:
        return None
    if "@" in reference:
        reference, digest = reference.split("@", 1)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            return None

    last_slash = reference.rfind("/")
    last_colon = reference.rfind(":")
    if last_colon > last_slash:
        name, tag = reference[:last_colon], reference[last_colon + 1:]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            return None
    else:
        name, tag = reference, "latest"

    if not name or len(name) > 255:
        return None
    components = name.split("/")
    registry = components[0]
    if registry in {"docker.io", "index.docker.io"}:
        components = components[1:]
    elif registry == "localhost" or "." in registry or ":" in registry:
        # This helper only queries Docker Hub, never arbitrary registries.
        return None
    component_pattern = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")
    if not components or any(not component_pattern.fullmatch(part) for part in components):
        return None

    repository = "/".join(components)
    if len(components) == 1:
        repository = f"library/{repository}"
    return repository, tag


def dockerhub_latest_digest(image: str) -> str | None:
    parsed = parse_dockerhub_image_reference(image)
    if parsed is None:
        return None
    repository, tag = parsed
    try:
        auth = httpx.get(
            "https://auth.docker.io/token",
            params={"service": "registry.docker.io", "scope": f"repository:{repository}:pull"},
            timeout=12,
        )
        auth.raise_for_status()
        token = auth.json().get("token")
        if not token:
            return None
        response = httpx.head(
            f"https://registry-1.docker.io/v2/{repository}/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json",
            },
            timeout=12,
        )
        response.raise_for_status()
        return response.headers.get("Docker-Content-Digest")
    except httpx.HTTPError:
        return None


def check_server_updates(server_id: str) -> None:
    item = read_json(config_path(server_id))
    if not item:
        return
    updates: list[dict[str, Any]] = []
    check_errors: list[str] = []
    pack = item.get("modpack") or {}
    if pack:
        current_id = str(pack.get("version_id") or pack.get("file_id") or "")
        try:
            update = provider_update(item, str(pack.get("platform", "")), str(pack.get("project_id", "")), current_id, "modpack")
            if update:
                updates.append(update)
        except (httpx.HTTPError, ValueError, TypeError) as err:
            check_errors.append(f"Modpack check: {str(err)[:160]}")
    for mod in item.get("mods", []):
        current_id = str(mod.get("version_id") or mod.get("file_id") or "")
        try:
            update = provider_update(item, str(mod.get("platform", "")), str(mod.get("project_id", "")), current_id, "mod")
            if update:
                update["title"] = str(mod.get("title") or update.get("name") or "Mod")
                updates.append(update)
        except (httpx.HTTPError, ValueError, TypeError) as err:
            check_errors.append(f"{mod.get('title') or 'Mod'} check: {str(err)[:160]}")
    if str(item.get("server_type", "")).upper() in PLUGIN_SERVER_TYPES:
        for plugin in item.get("plugins", []):
            current_id = str(plugin.get("version_id") or plugin.get("file_id") or "")
            try:
                update = provider_update(item, str(plugin.get("platform", "")), str(plugin.get("project_id", "")), current_id, "plugin")
                if update:
                    update["title"] = str(plugin.get("title") or update.get("name") or "Plugin")
                    updates.append(update)
            except (httpx.HTTPError, ValueError, TypeError) as err:
                check_errors.append(f"{plugin.get('title') or 'Plugin'} check: {str(err)[:160]}")
    current_version = str(item.get("minecraft_version", "LATEST"))
    can_advance_minecraft = item.get("server_type") in {"VANILLA", "BUKKIT", "PAPER", "PURPUR"} and not item.get("mods") and not item.get("plugins")
    try:
        manifest_response = httpx.get(MINECRAFT_MANIFEST_URL, timeout=15)
        manifest_response.raise_for_status()
        manifest = manifest_response.json()
        latest = manifest.get("latest") or {}
        entries = manifest.get("versions") or []
        current_entry = next((entry for entry in entries if entry.get("id") == current_version), None)
        channel = current_entry.get("type") if current_entry else "release"
        target = latest.get("snapshot" if channel == "snapshot" or current_version.upper() == "SNAPSHOT" else "release")
        if can_advance_minecraft and target and current_version not in {"LATEST", "SNAPSHOT"} and target != current_version:
            updates.append({"kind": "minecraft", "minecraft_version": target})
        elif can_advance_minecraft and current_version.upper() == "SNAPSHOT" and latest.get("snapshot"):
            updates.append({"kind": "minecraft", "minecraft_version": latest["snapshot"]})
    except (httpx.HTTPError, ValueError, TypeError) as err:
        check_errors.append(f"Minecraft version check: {str(err)[:160]}")
        LOG.warning("Could not check Mojang version updates for server %s: %s", server_id, err)
    image = game_driver_for(item).image_for(item)
    remote_digest = dockerhub_latest_digest(image)
    try:
        local = docker_client().images.get(image)
        local_digests = [str(value).split("@")[-1] for value in (local.attrs.get("RepoDigests") or [])]
        if remote_digest and local_digests and remote_digest not in local_digests:
            updates.append({"kind": "server_image"})
    except (DockerException, ImageNotFound):
        pass
    item["updates_available"] = updates
    item["last_update_check"] = time.time()
    item["update_check_error"] = "; ".join(check_errors)[:500]
    write_json(config_path(server_id), item)


def apply_server_updates(server_id: str, force: bool = False) -> None:
    item = read_json(config_path(server_id))
    if not item:
        raise RuntimeError("Server configuration was not found.")
    updates = list(item.get("updates_available") or [])
    if not updates and not force:
        return
    status, _ = current_status(server_id)
    was_running = status in {"running", "paused", "exited"} and item.get("desired_state") == "running"
    item["updating"] = True
    item["update_error"] = ""
    item["job_state"] = "updating"
    write_json(config_path(server_id), item)
    try:
        for update in updates:
            if update.get("kind") == "minecraft":
                item["minecraft_version"] = str(update["minecraft_version"])
            elif update.get("kind") == "modpack":
                pack = item.get("modpack") or {}
                if update.get("version_id"):
                    pack["version_id"] = update["version_id"]
                if update.get("file_id"):
                    pack["file_id"] = update["file_id"]
                item["modpack"] = pack
            elif update.get("kind") in {"mod", "plugin"}:
                target_list = "plugins" if update.get("kind") == "plugin" else "mods"
                for project in item.get(target_list, []):
                    if project.get("platform") == update.get("platform") and str(project.get("project_id")) == str(update.get("project_id")):
                        if update.get("version_id"):
                            project["version_id"] = update["version_id"]
                        if update.get("file_id"):
                            project["file_id"] = update["file_id"]
                        if update.get("download_url") and project.get("platform") == "hangar":
                            project["download_url"] = hangar_direct_download_url(update["download_url"])
        write_json(config_path(server_id), item)
        stop_container(server_id, remove=True, timeout=120)
        image = game_driver_for(item).image_for(item)
        docker_client().images.pull(image)
        latest = read_json(config_path(server_id))
        force_stopped = bool(latest.get("force_stopped"))
        if was_running and not force_stopped:
            run_container(server_id)
            deadline = time.time() + 600
            while time.time() < deadline:
                if online_player_count(server_id) is not None:
                    break
                time.sleep(3)
            else:
                raise RuntimeError("The server did not become ready after the update. Check its console logs.")
        item = read_json(config_path(server_id))
        item["updating"] = False
        item["updates_available"] = []
        item["last_update_at"] = time.time()
        item["last_update_check"] = time.time()
        item["job_state"] = "force-stopped" if item.get("force_stopped") else "running" if was_running else "stopped"
        write_json(config_path(server_id), item)
    except Exception as err:
        item = read_json(config_path(server_id))
        item["updating"] = False
        item["update_error"] = str(err)[:500]
        item["job_state"] = "update-failed"
        write_json(config_path(server_id), item)
        raise


def maintain_server(server_id: str) -> None:
    with server_lock(server_id):
        item = read_json(config_path(server_id))
        if not item or item.get("force_stopped") or item.get("updating") or item.get("deleting"):
            return
        now = time.time()
        status, container = current_status(server_id)
        verified_empty = False
        if status == "running":
            players = online_player_count(server_id)
            if players is not None and players > 0:
                item["last_player_seen"] = now
                item["empty_since"] = None
                item["auto_paused"] = False
            elif players == 0:
                verified_empty = True
                if not item.get("empty_since"):
                    item["empty_since"] = now
                item["last_player_seen"] = item.get("last_player_seen") or now
                idle = now - float(item.get("empty_since") or now)
                if item.get("auto_pause_enabled") and idle >= int(item.get("auto_pause_minutes", 20)) * 60:
                    try:
                        container.stop(timeout=60)
                        item["auto_paused"] = True
                        item["job_state"] = "paused"
                        status = "paused"
                    except DockerException:
                        LOG.exception("Could not auto-pause server %s", server_id)
        elif status == "paused":
            verified_empty = bool(item.get("auto_paused"))
        elif status == "exited" and item.get("auto_paused"):
            verified_empty = True
        elif status in {"stopped", "exited"} and item.get("desired_state") == "stopped":
            verified_empty = True
        if item.get("auto_update_enabled") and now - float(item.get("last_update_check") or 0) >= UPDATE_CHECK_INTERVAL:
            write_json(config_path(server_id), item)
            check_server_updates(server_id)
            item = read_json(config_path(server_id))
        empty_since = float(item.get("empty_since") or item.get("last_player_seen") or now)
        idle_hours = (now - empty_since) / 3600
        if verified_empty and item.get("auto_update_enabled") and item.get("updates_available") and idle_hours >= int(item.get("auto_update_idle_hours", 3)):
            item["job_state"] = "update-queued"
            item["updating"] = True
            write_json(config_path(server_id), item)
            submit_job(server_id, "automatic update", lambda: apply_server_updates(server_id))
            return
        if verified_empty and item.get("auto_stop_enabled") and status in {"running", "paused", "exited"} and item.get("desired_state") == "running" and idle_hours >= int(item.get("auto_stop_days", 3)) * 24:
            item["desired_state"] = "stopped"
            item["auto_stopped"] = True
            item["auto_paused"] = False
            item["job_state"] = "auto-stopping"
            write_json(config_path(server_id), item)
            stop_container(server_id, remove=True, timeout=120)
            item["job_state"] = "auto-stopped"
            write_json(config_path(server_id), item)
            return
        write_json(config_path(server_id), item)

async def maintenance_loop() -> None:
    while True:
        try:
            ids = [item["id"] for item in all_configs()]
            await asyncio.gather(*(asyncio.to_thread(maintain_server, server_id) for server_id in ids))
            for server_id in ids:
                try:
                    await ensure_game_networking(server_id)
                except OSError:
                    LOG.exception("Could not maintain gateway for server %s", server_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Game-server maintenance cycle failed")
        await asyncio.sleep(MAINTENANCE_INTERVAL)


class StripHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = html.unescape(data).strip()
        if value:
            self.parts.append(value)


def clean_description(value: str) -> str:
    parser = StripHTML()
    parser.feed(value or "")
    return "\n\n".join(parser.parts)[:10000]


def modrinth_headers() -> dict[str, str]:
    return {"User-Agent": "Gamerz32/Home-Assistant-Gameservers-ADDONS/3.0.0 (Home Assistant app)"}


def modrinth_author_links(team_members: Any) -> list[dict[str, str]]:
    members = team_members if isinstance(team_members, list) else []
    ordered = sorted(
        (member for member in members if isinstance(member, dict)),
        key=lambda member: 0 if str(member.get("role", "")).lower() == "owner" else 1,
    )
    authors: list[dict[str, str]] = []
    seen: set[str] = set()
    for member in ordered:
        user = member.get("user")
        if not isinstance(user, dict):
            continue
        username = str(user.get("username", "")).strip()
        key = username.casefold()
        if not username or key in seen:
            continue
        seen.add(key)
        authors.append({
            "name": str(user.get("name") or username).strip()[:100],
            "url": f"https://modrinth.com/user/{quote(username, safe='')}",
        })
    return authors[:20]


def curseforge_author_links(project: dict[str, Any]) -> list[dict[str, str]]:
    raw_authors = project.get("authors", [])
    authors: list[dict[str, str]] = []
    if not isinstance(raw_authors, list):
        return authors
    for author in raw_authors:
        if not isinstance(author, dict):
            continue
        name = str(author.get("name", "")).strip()
        if not name:
            continue
        authors.append({"name": name[:100], "url": str(author.get("url", "")).strip()[:2048]})
    return authors[:20]


def require_curseforge_distribution(project: dict[str, Any]) -> None:
    if project.get("allowModDistribution") is False:
        raise HTTPException(
            status_code=403,
            detail="The author has disabled third-party distribution for this CurseForge project.",
        )


def canonical_mod_loader(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("_", "").replace("-", "")
    return {"fabric": "FABRIC", "forge": "FORGE", "neoforge": "NEOFORGE", "quilt": "QUILT"}.get(normalized, "")


def server_mod_loader(item: dict[str, Any]) -> str:
    # Older saved configs can have a lowercase or missing mod_loader field.
    # The native server type is authoritative for standalone loader installs.
    return canonical_mod_loader(item.get("mod_loader")) or canonical_mod_loader(item.get("server_type"))


def mod_compatibility(game_version: str, loader: str, game_versions: list[str], loaders: list[str]) -> bool | None:
    selected_loader = canonical_mod_loader(loader)
    if not game_version or not selected_loader:
        return None
    games = {str(value).strip().casefold() for value in game_versions}
    supported_loaders = {canonical_mod_loader(value) for value in loaders}
    return str(game_version).strip().casefold() in games and selected_loader in supported_loaders


async def minecraft_manifest() -> dict[str, Any]:
    now = time.monotonic()
    cached = MINECRAFT_VERSION_CACHE.get("payload")
    if cached and now < float(MINECRAFT_VERSION_CACHE.get("expires_at", 0)):
        return cached
    async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/json"}) as client:
        response = await client.get(MINECRAFT_MANIFEST_URL)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload.get("versions"), list):
        raise HTTPException(status_code=502, detail="Mojang returned an invalid Minecraft version list.")
    MINECRAFT_VERSION_CACHE.update({"payload": payload, "expires_at": now + 1800})
    return payload


async def require_release_for_plugin_server(server_type: str, version: str) -> None:
    if str(server_type).upper() not in PLUGIN_SERVER_TYPES:
        return
    selected = str(version).strip()
    snapshot_label = re.search(r"(?i)(?:^\d{2}w\d{2}[a-z](?:_or_[a-z])?$|(?:^|[-_.])(?:snapshot|pre|rc)(?:[-_.]?\d+|$))", selected)
    if selected.upper() == "SNAPSHOT" or snapshot_label:
        raise HTTPException(status_code=422, detail="Bukkit, Paper, and Purpur servers require a Minecraft release; snapshots are not supported.")
    try:
        manifest = await minecraft_manifest()
    except (httpx.HTTPError, HTTPException):
        return
    selected_version = next((entry for entry in manifest.get("versions", []) if entry.get("id") == selected), None)
    if selected_version and selected_version.get("type") == "snapshot":
        raise HTTPException(status_code=422, detail="Bukkit, Paper, and Purpur servers require a Minecraft release; snapshots are not supported.")


async def resolve_minecraft_version(item: dict[str, Any]) -> str:
    version = str(item.get("minecraft_version", "")).strip()
    if version.upper() not in {"", "LATEST", "SNAPSHOT"}:
        return version
    try:
        latest = (await minecraft_manifest()).get("latest", {})
        return str(latest.get("snapshot" if version.upper() == "SNAPSHOT" else "release", ""))
    except (httpx.HTTPError, HTTPException):
        return ""


def curseforge_game_versions(values: list[Any]) -> list[str]:
    loader_names = {"forge", "fabric", "quilt", "neoforge", "cauldron", "liteloader"}
    return [label for value in values if (label := str(value).strip())
            and label.lower() not in loader_names
            and not label.lower().startswith(("forge ", "fabric ", "quilt ", "neoforge "))]


def modrinth_version_record(version: dict[str, Any], kind: str, game_version: str, loader: str) -> dict[str, Any]:
    games = [str(value) for value in version.get("game_versions", [])]
    loaders = [str(value).lower() for value in version.get("loaders", [])]
    supported = [value for value in loaders if value.upper() in MOD_LOADERS]
    targets = [{"game_version": game, "loader": value} for game in games for value in supported]
    if kind == "modpack" and not supported:
        targets = [{"game_version": game, "loader": ""} for game in games]
    files = version.get("files", [])
    size = next((int(f.get("size", 0)) for f in files if f.get("primary")), int(files[0].get("size", 0)) if files else 0)
    compatible = mod_compatibility(game_version, loader, games, loaders) if kind == "mod" else (game_version in games if kind in {"plugin", *CATALOG_CONTENT_KINDS} and game_version else None)
    return {"id": str(version.get("id", "")), "number": str(version.get("version_number", "")),
        "name": str(version.get("name", "")), "game_versions": games, "loaders": loaders, "targets": targets,
        "compatible": compatible,
        "version_type": str(version.get("version_type", "")), "date_published": str(version.get("date_published", "")),
        "downloads": int(version.get("downloads", 0)), "size": size}


def curseforge_version_record(version: dict[str, Any], kind: str, game_version: str, loader: str) -> dict[str, Any]:
    games = curseforge_game_versions(version.get("gameVersions", []))
    try:
        loader_id = int(version.get("modLoaderType", version.get("modLoader", 0)) or 0)
    except (TypeError, ValueError):
        loader_id = 0
    version_loader = CF_LOADER_NAMES.get(loader_id, "")
    targets = [{"game_version": game, "loader": version_loader} for game in games]
    if kind == "mod" and game_version and loader:
        # CurseForge modLoaderType=0 means "Any", not an unsupported loader.
        compatible = game_version in games and (loader_id == 0 or loader.lower() == version_loader)
    elif kind == "plugin" and game_version:
        compatible = game_version in games
    elif kind in CATALOG_CONTENT_KINDS and game_version:
        compatible = game_version in games
    else:
        compatible = None
    try:
        release_type = {1: "release", 2: "beta", 3: "alpha"}.get(int(version.get("releaseType", 0) or 0), "")
    except (TypeError, ValueError):
        release_type = ""
    return {"id": str(version.get("id", "")), "number": str(version.get("displayName") or version.get("fileName") or version.get("id", "")),
        "name": str(version.get("fileName", "")), "game_versions": games, "loaders": [version_loader] if version_loader else [],
        "targets": targets, "compatible": compatible, "version_type": release_type,
        "date_published": str(version.get("fileDate", "")), "downloads": int(version.get("downloadCount", 0)),
        "size": int(version.get("fileLength", 0))}


def hangar_version_compatible(supported_versions: Any, game_version: str) -> bool:
    if not game_version or not isinstance(supported_versions, list):
        return False
    candidate = str(game_version).strip()
    candidate_parts = candidate.split(".")
    if candidate in {str(value).strip() for value in supported_versions}:
        return True
    if not all(part.isdigit() for part in candidate_parts):
        return False
    candidate_tuple = tuple(int(part) for part in candidate_parts)
    for raw in supported_versions:
        value = str(raw).strip()
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+){1,2})-([0-9]+(?:\.[0-9]+){1,2})", value)
        if not match:
            continue
        low_parts, high_parts = match.group(1).split("."), match.group(2).split(".")
        if not all(part.isdigit() for part in low_parts + high_parts):
            continue
        if len(low_parts) == len(high_parts) == 2:
            family = tuple(int(part) for part in candidate_parts[:2])
            if len(candidate_parts) >= 2 and tuple(map(int, low_parts)) <= family <= tuple(map(int, high_parts)):
                return True
            continue
        low = tuple(map(int, low_parts)) + (0,) * (3 - len(low_parts))
        high = tuple(map(int, high_parts)) + (0,) * (3 - len(high_parts))
        padded = candidate_tuple + (0,) * (3 - len(candidate_tuple))
        if low <= padded <= high:
            return True
    return False


def hangar_direct_download_url(value: Any) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if (parsed.scheme == "https" and parsed.hostname == "hangarcdn.papermc.io"
            and parsed.username is None and parsed.password is None
            and parsed.path.lower().endswith(".jar") and len(candidate) <= 2048):
        return candidate
    return ""


def hangar_version_record(version: dict[str, Any], game_version: str) -> dict[str, Any]:
    dependencies = version.get("platformDependenciesFormatted", {}).get("PAPER") or version.get("platformDependencies", {}).get("PAPER") or []
    download = (version.get("downloads") or {}).get("PAPER") or {}
    file_info = download.get("fileInfo") or {}
    url = hangar_direct_download_url(download.get("downloadUrl"))
    external_url = safe_icon_url(download.get("externalUrl")) or str(download.get("externalUrl") or "")
    stats = version.get("stats") or {}
    platform_downloads = stats.get("platformDownloads") or {}
    return {
        "id": str(version.get("id", "")),
        "number": str(version.get("name", "")),
        "name": str(version.get("name", "")),
        "game_versions": [str(value) for value in dependencies if isinstance(value, (str, int, float))],
        "loaders": ["paper"],
        "targets": [],
        "compatible": hangar_version_compatible(dependencies, game_version),
        "version_type": str((version.get("channel") or {}).get("name", "")),
        "date_published": str(version.get("createdAt", "")),
        "downloads": int(platform_downloads.get("PAPER", stats.get("totalDownloads", 0)) or 0),
        "size": int(file_info.get("size", 0) or 0),
        "download_url": url,
        "external_url": external_url,
        "installable": bool(url),
    }


async def validate_pack_version(platform: str, project_id: str, version_id: str, file_id: str, game_version: str, loader: str) -> None:
    if platform == "modrinth":
        if not version_id:
            raise HTTPException(status_code=422, detail="Select a Modrinth modpack version.")
        async with httpx.AsyncClient(timeout=20, headers=modrinth_headers()) as client:
            response = await client.get(f"{MODRINTH_BASE}/version/{version_id}")
            response.raise_for_status()
            version = response.json()
        if str(version.get("project_id", "")) != project_id:
            raise HTTPException(status_code=422, detail="The selected version does not belong to this modpack.")
        if game_version not in version.get("game_versions", []):
            raise HTTPException(status_code=409, detail="The selected modpack version does not support this Minecraft version.")
        if loader and loader.lower() not in {str(v).lower() for v in version.get("loaders", [])}:
            raise HTTPException(status_code=409, detail="The selected modpack version does not support this mod loader.")
        return
    if platform == "curseforge":
        if not file_id or not file_id.isdigit():
            raise HTTPException(status_code=422, detail="Select a CurseForge modpack file version.")
        key = current_curseforge_api_key()
        if not key:
            raise HTTPException(status_code=400, detail="Configure a CurseForge API key in Home Assistant add-on Configuration, or enter a temporary key under Catalog Settings.")
        async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
            project_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}")
            project_response.raise_for_status()
            require_curseforge_distribution(project_response.json().get("data", {}))
            response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files/{file_id}")
            response.raise_for_status()
            version = response.json().get("data", {})
        if str(version.get("modId", project_id)) != project_id:
            raise HTTPException(status_code=422, detail="The selected file does not belong to this CurseForge modpack.")
        if game_version not in curseforge_game_versions(version.get("gameVersions", [])):
            raise HTTPException(status_code=409, detail="The selected CurseForge file does not support this Minecraft version.")
        record = curseforge_version_record(version, "modpack", game_version, loader)
        if loader and loader.lower() not in record["loaders"]:
            raise HTTPException(status_code=409, detail="The selected CurseForge file does not support this mod loader.")
        return
    raise HTTPException(status_code=422, detail="Unsupported modpack platform.")


async def validate_mod_version(platform: str, project_id: str, version_id: str, file_id: str, game_version: str, loader: str) -> None:
    if platform == "modrinth":
        if not version_id:
            raise HTTPException(status_code=422, detail="Select a Modrinth mod version.")
        async with httpx.AsyncClient(timeout=20, headers=modrinth_headers()) as client:
            response = await client.get(f"{MODRINTH_BASE}/version/{version_id}")
            response.raise_for_status()
            version = response.json()
        if str(version.get("project_id", "")) != project_id:
            raise HTTPException(status_code=422, detail="The selected version does not belong to this mod.")
        if mod_compatibility(game_version, loader, version.get("game_versions", []), version.get("loaders", [])) is not True:
            raise HTTPException(status_code=409, detail="This mod version is not compatible with the server's Minecraft version and loader.")
        return
    if platform == "curseforge":
        if not file_id or not file_id.isdigit():
            raise HTTPException(status_code=422, detail="Select a CurseForge mod file version.")
        key = current_curseforge_api_key()
        if not key:
            raise HTTPException(status_code=400, detail="Configure a CurseForge API key in Home Assistant add-on Configuration, or enter a temporary key under Catalog Settings.")
        async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
            project_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}")
            project_response.raise_for_status()
            require_curseforge_distribution(project_response.json().get("data", {}))
            response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files/{file_id}")
            response.raise_for_status()
            version = response.json().get("data", {})
        games = curseforge_game_versions(version.get("gameVersions", []))
        record = curseforge_version_record(version, "mod", game_version, loader)
        if game_version not in games or record["compatible"] is not True:
            raise HTTPException(status_code=409, detail="This CurseForge file is not compatible with the server's Minecraft version and loader.")
        return
    raise HTTPException(status_code=422, detail="Unsupported mod platform.")


async def validate_plugin_version(platform: str, project_id: str, version_id: str, file_id: str, game_version: str) -> dict[str, Any] | None:
    if platform == "modrinth":
        if not version_id:
            raise HTTPException(status_code=422, detail="Select a Modrinth plugin version.")
        async with httpx.AsyncClient(timeout=20, headers=modrinth_headers()) as client:
            response = await client.get(f"{MODRINTH_BASE}/version/{version_id}")
            response.raise_for_status()
            version = response.json()
        if str(version.get("project_id", "")) != project_id:
            raise HTTPException(status_code=422, detail="The selected version does not belong to this plugin.")
        if game_version not in version.get("game_versions", []):
            raise HTTPException(status_code=409, detail="This plugin version does not support the server's Minecraft version.")
        return
    if platform == "curseforge":
        if not file_id or not file_id.isdigit():
            raise HTTPException(status_code=422, detail="Select a CurseForge plugin file version.")
        key = current_curseforge_api_key()
        if not key:
            raise HTTPException(status_code=400, detail="Configure a CurseForge API key in Home Assistant add-on Configuration, or enter a temporary key under Catalog Settings.")
        async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
            project_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}")
            project_response.raise_for_status()
            require_curseforge_distribution(project_response.json().get("data", {}))
            response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files/{file_id}")
            response.raise_for_status()
            version = response.json().get("data", {})
        if str(version.get("modId", project_id)) != project_id:
            raise HTTPException(status_code=422, detail="The selected file does not belong to this CurseForge plugin.")
        if game_version not in curseforge_game_versions(version.get("gameVersions", [])):
            raise HTTPException(status_code=409, detail="This CurseForge plugin file does not support the server's Minecraft version.")
        return
    if platform == "hangar":
        if not project_id.isdigit() or not version_id.isdigit():
            raise HTTPException(status_code=422, detail="Select a valid Hangar plugin version.")
        async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/json"}) as client:
            response = await client.get(f"{HANGAR_BASE}/projects/{project_id}/versions/{version_id}")
            response.raise_for_status()
            version = response.json()
        if str(version.get("projectId", "")) != project_id or str(version.get("id", "")) != version_id:
            raise HTTPException(status_code=422, detail="The selected version does not belong to this Hangar plugin.")
        record = hangar_version_record(version, game_version)
        if record["compatible"] is not True:
            raise HTTPException(status_code=409, detail="This Hangar plugin version does not support the server's Minecraft version.")
        if not record["installable"]:
            raise HTTPException(status_code=409, detail="This Hangar release links to an external download and cannot be installed automatically. Open its Hangar version page to download it from the author's selected source.")
        return record
    raise HTTPException(status_code=422, detail="Unsupported plugin platform.")


async def modrinth_search(kind: str, query: str, item: dict[str, Any], offset: int, sort: str) -> dict[str, Any]:
    if kind == "world":
        raise HTTPException(status_code=422, detail="Modrinth does not offer a world project type. Use CurseForge or import a map archive.")
    index = {"downloads": "downloads", "updated": "updated"}[sort]
    params = {"query": query, "facets": json.dumps([[f"project_type:{kind}"]]), "limit": 24, "offset": offset, "index": index}
    async with httpx.AsyncClient(timeout=20, headers=modrinth_headers()) as client:
        response = await client.get(f"{MODRINTH_BASE}/search", params=params)
        response.raise_for_status()
        data = response.json()
    game = await resolve_minecraft_version(item) if kind in {"mod", "plugin", *CATALOG_CONTENT_KINDS} else ""
    loader = server_mod_loader(item).lower()
    hits = []
    for project in data.get("hits", []):
        games = [str(v) for v in project.get("versions", [])]
        categories = [str(v).lower() for v in project.get("categories", [])]
        compatible = mod_compatibility(game, loader, games, categories) if kind == "mod" else (game in games if kind == "plugin" and game else (game in games if kind in CATALOG_CONTENT_KINDS and game else None))
        hits.append({"project_id": project["project_id"], "slug": project["slug"], "title": project["title"],
            "description": project["description"], "icon": project.get("icon_url", ""),
            "downloads": project.get("downloads", 0), "platform": "modrinth", "versions": games[:12],
            "loaders": categories, "compatible": compatible})
    return {"hits": hits, "total_hits": data.get("total_hits", 0)}


async def hangar_plugin_search(query: str, item: dict[str, Any], offset: int, sort: str) -> dict[str, Any]:
    game = await resolve_minecraft_version(item)
    params = {"q": query, "limit": 24, "offset": offset,
        "sort": "-downloads" if sort == "downloads" else "-updated"}
    async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/json"}) as client:
        response = await client.get(f"{HANGAR_BASE}/projects", params=params)
        response.raise_for_status()
        data = response.json()
    hits = []
    for project in data.get("result", []):
        if not isinstance(project, dict):
            continue
        namespace = project.get("namespace") or {}
        owner = str(namespace.get("owner", "")).strip()
        slug = str(namespace.get("slug", "")).strip()
        project_id = str(project.get("id", ""))
        if not project_id.isdigit() or not slug:
            continue
        platforms = project.get("supportedPlatforms") or {}
        supported = platforms.get("PAPER") or []
        stats = project.get("stats") or {}
        hits.append({
            "project_id": project_id, "slug": f"{owner}/{slug}" if owner else slug,
            "title": str(project.get("name", slug)), "description": str(project.get("description", "")),
            "icon": str(project.get("avatarUrl", "")), "downloads": int(stats.get("downloads", 0) or 0),
            "platform": "hangar", "url": f"https://hangar.papermc.io/{quote(owner, safe='')}/{quote(slug, safe='')}" if owner else "",
            "versions": [str(value) for value in supported[:12]],
            "compatible": hangar_version_compatible(supported, game),
        })
    total = int((data.get("pagination") or {}).get("count", len(hits)) or 0)
    return {"hits": hits, "total_hits": total}


async def curseforge_bedrock_addon_class_id() -> int:
    # CurseForge's API terms prohibit caching API data; resolve this only when the user browses Bedrock add-ons.
    key = current_curseforge_api_key()
    if not key:
        raise HTTPException(status_code=400, detail="Configure a CurseForge API key in Home Assistant add-on Configuration, or enter a temporary key under Catalog Settings.")
    async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
        response = await client.get(f"{CURSEFORGE_BASE}/categories", params={"gameId": MC_GAME_ID, "classesOnly": "true"})
        response.raise_for_status()
        categories = response.json().get("data", [])
    for category in categories:
        if not isinstance(category, dict):
            continue
        names = {re.sub(r"[^a-z0-9]", "", str(category.get(field, "")).lower()) for field in ("slug", "name")}
        if names & {"addon", "addons", "bedrockaddon", "bedrockaddons"}:
            try:
                return int(category.get("id", category.get("classId")))
            except (TypeError, ValueError):
                continue
    raise HTTPException(status_code=502, detail="CurseForge did not return the Minecraft Bedrock Add-ons category. Check the API key and try again.")


async def curseforge_content_filter(kind: str) -> tuple[int, int | None]:
    """Resolve CurseForge class/category IDs from its live category tree."""
    if kind not in CATALOG_CONTENT_KINDS:
        return CF_CLASS_IDS[kind], None
    key = current_curseforge_api_key()
    if not key:
        raise HTTPException(status_code=400, detail="Configure a CurseForge API key in the add-on options or Catalog settings to browse CurseForge.")
    headers = {"x-api-key": key, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        classes_response = await client.get(f"{CURSEFORGE_BASE}/categories", params={"gameId": MC_GAME_ID, "classesOnly": "true"})
        classes_response.raise_for_status()
        classes = classes_response.json().get("data", [])
        if kind == "datapack":
            class_id = CF_CLASS_IDS["mod"]
            categories_response = await client.get(f"{CURSEFORGE_BASE}/categories", params={"gameId": MC_GAME_ID, "classId": class_id})
            categories_response.raise_for_status()
            categories = categories_response.json().get("data", [])
            accepted = {"datapack", "datapacks", "datapacksandcrafting", "datapackandcrafting"}
            for category in categories:
                if not isinstance(category, dict):
                    continue
                names = {re.sub(r"[^a-z0-9]", "", str(category.get(field, "")).lower()) for field in ("slug", "name")}
                if names & accepted:
                    try:
                        return class_id, int(category["id"])
                    except (KeyError, TypeError, ValueError):
                        continue
            raise HTTPException(status_code=502, detail="CurseForge did not return a Data Packs category. Try Modrinth or import a data-pack ZIP.")
    accepted_names = {
        "resourcepack": {"texturepack", "texturepacks", "resourcepack", "resourcepacks"},
        "world": {"world", "worlds", "maps", "map"},
    }[kind]
    for category in classes:
        if not isinstance(category, dict):
            continue
        names = {re.sub(r"[^a-z0-9]", "", str(category.get(field, "")).lower()) for field in ("slug", "name")}
        if names & accepted_names:
            try:
                return int(category["id"]), None
            except (KeyError, TypeError, ValueError):
                continue
    label = "Texture Packs / Resource Packs" if kind == "resourcepack" else "Worlds / Maps"
    raise HTTPException(status_code=502, detail=f"CurseForge did not return its {label} class. Try another source or import an archive.")


async def curseforge_search(kind: str, query: str, item: dict[str, Any], offset: int, sort: str) -> dict[str, Any]:
    key = current_curseforge_api_key()
    if not key:
        raise HTTPException(status_code=400, detail="Configure a CurseForge API key in Home Assistant add-on Configuration, or enter a temporary key under Catalog Settings.")
    sort_field = 6 if sort == "downloads" else 3
    class_id, category_id = await curseforge_content_filter(kind)
    params = {"gameId": MC_GAME_ID, "classId": class_id, "searchFilter": query, "index": offset,
        "pageSize": 24, "sortField": sort_field, "sortOrder": "desc"}
    if category_id is not None:
        params["categoryId"] = category_id
    async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
        response = await client.get(f"{CURSEFORGE_BASE}/mods/search", params=params)
        response.raise_for_status()
        data = response.json()
    game = await resolve_minecraft_version(item) if kind in {"mod", "plugin", *CATALOG_CONTENT_KINDS} else ""
    loader_id = CF_LOADER_IDS.get(server_mod_loader(item))
    hits = []
    for project in data.get("data", []):
        if project.get("allowModDistribution") is False:
            continue
        indexes = project.get("latestFilesIndexes", []) or []
        compatible = None
        if kind == "mod" and game:
            for file in indexes:
                if not isinstance(file, dict):
                    continue
                file_games = file.get("gameVersion", []) or []
                if isinstance(file_games, str):
                    file_games = [file_games]
                try:
                    file_loader_id = int(file.get("modLoader", 0) or 0)
                except (TypeError, ValueError):
                    file_loader_id = 0
                # CurseForge loader type 0 means "Any" and is valid for this loader.
                if game in {str(value) for value in file_games} and (loader_id is None or file_loader_id in {0, loader_id}):
                    compatible = True
                    break
        elif kind == "plugin" and game:
            compatible = any(
                game in [str(value) for value in (file.get("gameVersion") if isinstance(file.get("gameVersion"), list) else [file.get("gameVersion")])]
                for file in indexes if isinstance(file, dict) and file.get("gameVersion")
            )
        elif kind in CATALOG_CONTENT_KINDS and game:
            compatible = any(
                game in [str(value) for value in (file.get("gameVersion") if isinstance(file.get("gameVersion"), list) else [file.get("gameVersion")])]
                for file in indexes if isinstance(file, dict) and file.get("gameVersion")
            )
        hits.append({"project_id": str(project["id"]), "slug": project.get("slug", str(project["id"])),
            "title": project.get("name", "CurseForge project"), "description": project.get("summary", ""),
            "icon": (project.get("logo") or {}).get("url", ""), "downloads": project.get("downloadCount", 0),
            "platform": "curseforge", "url": (project.get("links") or {}).get("websiteUrl", ""), "compatible": compatible})
    return {"hits": hits, "total_hits": data.get("pagination", {}).get("totalCount", 0)}


async def curseforge_bedrock_addon_search(query: str, offset: int, sort: str) -> dict[str, Any]:
    key = current_curseforge_api_key()
    if not key:
        raise HTTPException(status_code=400, detail="Configure a CurseForge API key in Home Assistant add-on Configuration, or enter a temporary key under Catalog Settings.")
    class_id = await curseforge_bedrock_addon_class_id()
    params = {"gameId": MC_GAME_ID, "classId": class_id, "searchFilter": query, "index": offset,
        "pageSize": 24, "sortField": 6 if sort == "downloads" else 3, "sortOrder": "desc"}
    async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
        response = await client.get(f"{CURSEFORGE_BASE}/mods/search", params=params)
        response.raise_for_status()
        data = response.json()
    hits = []
    for project in data.get("data", []):
        if project.get("allowModDistribution") is False:
            continue
        hits.append({"project_id": str(project["id"]), "slug": project.get("slug", str(project["id"])),
            "title": project.get("name", "CurseForge add-on"), "description": project.get("summary", ""),
            "icon": (project.get("logo") or {}).get("url", ""), "downloads": project.get("downloadCount", 0),
            "platform": "curseforge", "url": (project.get("links") or {}).get("websiteUrl", "")})
    return {"hits": hits, "total_hits": data.get("pagination", {}).get("totalCount", 0)}


async def curseforge_bedrock_addon_detail(project_id: str, offset: int) -> dict[str, Any]:
    key = current_curseforge_api_key()
    if not key:
        raise HTTPException(status_code=400, detail="Configure a CurseForge API key in Home Assistant add-on Configuration, or enter a temporary key under Catalog Settings.")
    class_id = await curseforge_bedrock_addon_class_id()
    headers = {"x-api-key": key, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        project_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}")
        project_response.raise_for_status()
        project = project_response.json().get("data", {})
        require_curseforge_distribution(project)
        try:
            project_class_id = int(project.get("classId", 0))
        except (TypeError, ValueError):
            project_class_id = 0
        if project_class_id != class_id:
            raise HTTPException(status_code=422, detail="This CurseForge project is not a Minecraft Bedrock add-on.")
        desc_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/description")
        desc_response.raise_for_status()
        description = clean_description(desc_response.json().get("data", ""))
        files_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files", params={"pageSize": 50, "index": offset})
        files_response.raise_for_status()
        files_payload = files_response.json()
    files = files_payload.get("data", [])
    versions = []
    for file in files:
        filename = str(file.get("fileName", ""))
        if not filename.lower().endswith((".mcaddon", ".mcpack")) or file.get("isAvailable") is False:
            continue
        versions.append({"id": str(file.get("id", "")), "number": str(file.get("displayName") or filename),
            "name": filename, "game_versions": curseforge_game_versions(file.get("gameVersions", [])), "loaders": [],
            "targets": [], "compatible": True, "version_type": "", "date_published": str(file.get("fileDate", "")),
            "downloads": int(file.get("downloadCount", 0)), "size": int(file.get("fileLength", 0))})
    total = int(files_payload.get("pagination", {}).get("totalCount", len(files)))
    next_offset = offset + len(files)
    return {"project_id": str(project.get("id", project_id)), "slug": project.get("slug", str(project_id)),
        "title": project.get("name", ""), "description": description or project.get("summary", ""),
        "icon": (project.get("logo") or {}).get("url", ""), "platform": "curseforge",
        "authors": curseforge_author_links(project), "versions": versions,
        "version_offset": offset, "next_offset": next_offset, "total_versions": total, "has_more": next_offset < total,
        "website_url": (project.get("links") or {}).get("websiteUrl", "")}


def validate_curseforge_download_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or not (host == "curseforge.com" or host.endswith(".curseforge.com") or host.endswith(".forgecdn.net")):
        raise HTTPException(status_code=502, detail="CurseForge returned an unsupported add-on download URL.")


async def download_curseforge_addon(url: str, api_key: str, max_bytes: int = BEDROCK_ADDON_MAX_BYTES) -> bytes:
    current_url = url
    for _ in range(6):
        validate_curseforge_download_url(current_url)
        host = (urlsplit(current_url).hostname or "").lower()
        headers = {"x-api-key": api_key} if host == "edge.forgecdn.net" or host.endswith(".forgecdn.net") else {}
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, headers=headers) as client:
            async with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise HTTPException(status_code=502, detail="CurseForge returned an incomplete add-on download redirect.")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                try:
                    content_length = int(response.headers.get("content-length", "0"))
                except ValueError:
                    content_length = 0
                if content_length > max_bytes:
                    raise HTTPException(status_code=413, detail=f"The downloaded file must be {max_bytes // (1024 * 1024)} MB or smaller.")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(status_code=413, detail=f"The downloaded file must be {max_bytes // (1024 * 1024)} MB or smaller.")
                    chunks.append(chunk)
                if not chunks:
                    raise HTTPException(status_code=422, detail="CurseForge returned an empty add-on file.")
                return b"".join(chunks)
    raise HTTPException(status_code=502, detail="CurseForge returned too many redirects while downloading this add-on.")


def safe_content_filename(filename: Any, kind: str) -> str:
    if not isinstance(filename, str):
        raise HTTPException(status_code=422, detail="The content archive filename is invalid.")
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if len(base) > 180 or any(ord(char) < 32 or ord(char) == 127 for char in base):
        raise HTTPException(status_code=422, detail="The content archive filename is invalid.")
    if Path(base).suffix.lower() != ".zip":
        raise HTTPException(status_code=422, detail="Java resource packs, data packs, and worlds must be ZIP archives.")
    safe_stem = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(base).stem).strip(" .")[:140] or kind
    return f"{safe_stem}.zip"


def inspect_content_archive(filename: Any, archive: bytes, kind: str) -> tuple[str, str]:
    safe_filename = safe_content_filename(filename, kind)
    if not archive or len(archive) > CONTENT_ARCHIVE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Content archives must be 128 MB or smaller.")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            infos = bundle.infolist()
            if len(infos) > CONTENT_ARCHIVE_MAX_FILES:
                raise HTTPException(status_code=422, detail="The archive contains too many files.")
            total_size = 0
            names: set[str] = set()
            markers: list[PurePosixPath] = []
            for info in infos:
                name = info.filename
                if not name or "\\" in name or "\x00" in name or info.flag_bits & 0x1:
                    raise HTTPException(status_code=422, detail="The archive contains an unsafe or encrypted entry.")
                path = PurePosixPath(name)
                if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts) or ":" in path.parts[0]:
                    raise HTTPException(status_code=422, detail="The archive contains a path outside its package folder.")
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise HTTPException(status_code=422, detail="The archive contains a link or special file.")
                if info.is_dir():
                    continue
                if name in names:
                    raise HTTPException(status_code=422, detail="The archive contains duplicate file paths.")
                names.add(name)
                total_size += int(info.file_size)
                if total_size > CONTENT_ARCHIVE_MAX_EXPANDED_BYTES:
                    raise HTTPException(status_code=413, detail="The expanded archive must be 512 MB or smaller.")
                if kind in {"resourcepack", "datapack"} and path.name == "pack.mcmeta":
                    markers.append(path.parent)
                elif kind == "world" and path.name.lower() == "level.dat":
                    markers.append(path.parent)
            if not markers:
                expected = "pack.mcmeta" if kind in {"resourcepack", "datapack"} else "level.dat"
                raise HTTPException(status_code=422, detail=f"The ZIP does not contain a {expected} file.")
            # A single wrapper directory is common in downloads. Strip it while
            # installing so the game sees the expected marker at the package root.
            root = min(markers, key=lambda value: len(value.parts))
            bad_file = bundle.testzip()
            if bad_file:
                raise HTTPException(status_code=422, detail="The ZIP archive failed its integrity check.")
            return safe_filename, root.as_posix() if root.parts else ""
    except zipfile.BadZipFile as err:
        raise HTTPException(status_code=422, detail="Choose a valid ZIP archive.") from err


def extract_content_archive(archive: bytes, destination: Path, root_prefix: str) -> None:
    root_parts = PurePosixPath(root_prefix).parts if root_prefix else ()
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            for info in bundle.infolist():
                path = PurePosixPath(info.filename)
                if path.parts[:len(root_parts)] != root_parts:
                    continue
                relative = path.parts[len(root_parts):]
                if not relative:
                    continue
                output = destination.joinpath(*relative)
                if destination.resolve() not in output.resolve().parents and output.resolve() != destination.resolve():
                    raise RuntimeError("The archive contains a path outside its package folder.")
                if info.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, output.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def content_archive_path(server_id: str, record: dict[str, Any]) -> Path:
    relative = str(record.get("archive_path", ""))
    root = server_dir(server_id).resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="The saved content archive was not found.")
    return path


async def download_catalog_content(platform: str, kind: str, project_id: str, version_id: str, file_id: str, game_version: str) -> tuple[str, bytes, dict[str, Any]]:
    api_key = current_curseforge_api_key()
    if platform == "modrinth":
        if not version_id:
            raise HTTPException(status_code=422, detail="Select a Modrinth content version.")
        async with httpx.AsyncClient(timeout=25, headers=modrinth_headers()) as client:
            response = await client.get(f"{MODRINTH_BASE}/version/{version_id}")
            response.raise_for_status()
            version = response.json()
        if str(version.get("project_id", "")) != project_id:
            raise HTTPException(status_code=422, detail="The selected version does not belong to this project.")
        record = modrinth_version_record(version, kind, game_version, "")
        if record["compatible"] is not True:
            raise HTTPException(status_code=409, detail="This content version does not list the server's Minecraft version.")
        files = version.get("files", [])
        selected_file = next((entry for entry in files if entry.get("primary")), files[0] if files else None)
        if not isinstance(selected_file, dict):
            raise HTTPException(status_code=422, detail="Modrinth did not provide a downloadable ZIP file.")
        filename = str(selected_file.get("filename", "content.zip"))
        url = str(selected_file.get("url", ""))
        if not url or (urlsplit(url).hostname or "").lower() != "cdn.modrinth.com" or urlsplit(url).scheme != "https":
            raise HTTPException(status_code=502, detail="Modrinth returned an unsupported download URL.")
        async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
            async with client.stream("GET", url) as download:
                download.raise_for_status()
                data = bytearray()
                async for chunk in download.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > CONTENT_ARCHIVE_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="Content archives must be 128 MB or smaller.")
        return filename, bytes(data), record
    if platform != "curseforge" or not project_id.isdigit() or not file_id.isdigit():
        raise HTTPException(status_code=422, detail="Choose a supported catalog version.")
    if not api_key:
        raise HTTPException(status_code=400, detail="Configure a CurseForge API key in the add-on options or Catalog settings to browse CurseForge.")
    expected_class, expected_category = await curseforge_content_filter(kind)
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=25, headers=headers) as client:
        project_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}")
        project_response.raise_for_status()
        project = project_response.json().get("data", {})
        require_curseforge_distribution(project)
        try:
            project_class = int(project.get("classId", 0))
            project_categories = {int(value.get("id")) for value in project.get("categories", []) if isinstance(value, dict) and value.get("id") is not None}
        except (TypeError, ValueError):
            project_class, project_categories = 0, set()
        if project_class != expected_class or (expected_category is not None and expected_category not in project_categories):
            raise HTTPException(status_code=422, detail="This CurseForge project is not the selected content type.")
        file_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files/{file_id}")
        file_response.raise_for_status()
        file_data = file_response.json().get("data", {})
        if str(file_data.get("modId", "")) != project_id or not file_data.get("isAvailable", True):
            raise HTTPException(status_code=422, detail="This CurseForge file is unavailable or belongs to another project.")
        version_record = curseforge_version_record(file_data, kind, game_version, "")
        if version_record["compatible"] is not True:
            raise HTTPException(status_code=409, detail="This content version does not list the server's Minecraft version.")
        filename = str(file_data.get("fileName", "content.zip"))
        url = str(file_data.get("downloadUrl", ""))
    if not url:
        raise HTTPException(status_code=422, detail="CurseForge did not provide a downloadable file for this release.")
    archive = await download_curseforge_addon(url, api_key, CONTENT_ARCHIVE_MAX_BYTES)
    return filename, archive, version_record


def apply_java_content_archive(server_id: str, content_id: str, kind: str, record: dict[str, Any], archive: bytes, root_prefix: str) -> None:
    server_root = server_dir(server_id)
    content_root = server_root / "content" / content_id
    content_root.mkdir(parents=True, exist_ok=False)
    archive_path = content_root / "archive.zip"
    archive_path.write_bytes(archive)
    record["archive_path"] = archive_path.relative_to(server_root).as_posix()
    record["size"] = len(archive)
    record["sha1"] = hashlib.sha1(archive).hexdigest()
    if kind == "datapack":
        world = world_data_targets(server_id, read_json(config_path(server_id)))[0]
        datapacks = world / "datapacks"
        datapacks.mkdir(parents=True, exist_ok=True)
        pack_folder = datapacks / content_id
        extract_content_archive(archive, pack_folder, root_prefix)
        record["install_path"] = pack_folder.relative_to(server_root).as_posix()
    elif kind == "world":
        staging = content_root / "world-staging"
        extract_content_archive(archive, staging, root_prefix)
        item = read_json(config_path(server_id))
        targets = world_data_targets(server_id, item)
        backup_root = server_root / "backups" / f"world-{content_id}"
        moved: list[tuple[Path, Path]] = []
        try:
            backup_root.mkdir(parents=True, exist_ok=False)
            for index, target in enumerate(targets):
                if target.exists() or target.is_symlink():
                    backup = backup_root / str(index)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    target.replace(backup)
                    moved.append((backup, target))
            targets[0].parent.mkdir(parents=True, exist_ok=True)
            staging.replace(targets[0])
            record["backup_path"] = backup_root.relative_to(server_root).as_posix()
            record["install_path"] = targets[0].relative_to(server_root).as_posix()
        except Exception:
            for backup, target in reversed(moved):
                if backup.exists() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    backup.replace(target)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    items = read_json(config_path(server_id))
    items.setdefault("content_items", []).append(record)
    write_json(config_path(server_id), items)


def queue_java_content_change(server_id: str, content_id: str, kind: str, record: dict[str, Any], archive: bytes, root_prefix: str) -> str:
    item = read_json(config_path(server_id))
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    keep_running = item.get("desired_state") == "running"
    item["updating"] = True
    item["world_resetting"] = kind == "world"
    item["job_state"] = "content-queued"
    write_json(config_path(server_id), item)

    def apply_content():
        restart_attempted = False
        try:
            stop_container(server_id, remove=True, timeout=120)
            apply_java_content_archive(server_id, content_id, kind, record, archive, root_prefix)
            latest = read_json(config_path(server_id))
            if latest and keep_running:
                latest["desired_state"] = "running"
                latest["force_stopped"] = False
                write_json(config_path(server_id), latest)
                restart_attempted = True
                run_container(server_id)
        except Exception:
            latest = read_json(config_path(server_id))
            if keep_running and latest and not restart_attempted and not latest.get("force_stopped"):
                try:
                    run_container(server_id)
                except Exception:
                    LOG.exception("Could not restart server %s after content installation failed", server_id)
            raise
        finally:
            latest = read_json(config_path(server_id))
            if latest:
                latest["updating"] = False
                latest["world_resetting"] = False
                latest["job_state"] = "running" if keep_running and not latest.get("force_stopped") else "stopped"
                write_json(config_path(server_id), latest)

    return submit_job(server_id, f"install {kind}", apply_content)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global DOCKER_ERROR, HOST_DATA_SOURCE, SHUTTING_DOWN, MAINTENANCE_TASK
    global HOST_NETWORKING_ENABLED, GATEWAY_SECRET
    cli = None
    try:
        cli = docker_client()
        cli.ping()
        share_source = docker_self_data_source(cli, "/share")
        if migrate_private_data_to_share(cli):
            HOST_DATA_SOURCE = share_source / "game_server_manager"
        else:
            set_data_root(LEGACY_DATA_DIR)
            HOST_DATA_SOURCE = docker_self_data_source(cli, "/data")
            LOG.warning("Continuing to use private /data because /share migration could not be completed safely.")
        DOCKER_ERROR = None
    except Exception as err:
        DOCKER_ERROR = DOCKER_UNAVAILABLE_MESSAGE
        if _storage_entries(LEGACY_DATA_DIR):
            set_data_root(LEGACY_DATA_DIR)
            LOG.error("Could not prepare /share/game_server_manager; keeping existing files in /data: %s", err)
            try:
                HOST_DATA_SOURCE = docker_self_data_source(cli, "/data") if cli is not None else None
            except Exception:
                HOST_DATA_SOURCE = None
        else:
            set_data_root(SHARE_DATA_DIR)
            try:
                HOST_DATA_SOURCE = (docker_self_data_source(cli, "/share") / "game_server_manager") if cli is not None else None
            except Exception:
                HOST_DATA_SOURCE = None

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SERVERS_DIR.mkdir(parents=True, exist_ok=True)
    scrub_legacy_settings()
    HOST_NETWORKING_ENABLED = bool(read_json(OPTIONS_FILE, {}).get("host_networking", False))
    GATEWAY_SECRET = secrets.token_hex(32)
    try:
        import_legacy_server()
    except Exception:
        LOG.exception("Could not import legacy server data; original files were left untouched.")
    try:
        if cli is None:
            raise RuntimeError("Docker Engine is unavailable or app storage is not mounted.")
        ensure_minecraft_network(cli)
        await start_gateway_relay()
        configs = all_configs()
        cleanup_stale_gateways({str(item["id"]) for item in configs})
        for item in configs:
            try:
                await ensure_game_networking(item["id"])
            except OSError:
                LOG.exception("Could not start Minecraft gateway for %s", item.get("name"))
            if item.get("desired_state") == "running" and not item.get("force_stopped"):
                submit_job(item["id"], "resume", lambda sid=item["id"]: ensure_running(sid))
    except Exception as err:
        DOCKER_ERROR = DOCKER_UNAVAILABLE_MESSAGE
        LOG.error("Docker Engine is unavailable or required game networking could not start: %s", err)
    MAINTENANCE_TASK = asyncio.create_task(maintenance_loop())
    yield
    SHUTTING_DOWN = True
    if MAINTENANCE_TASK:
        MAINTENANCE_TASK.cancel()
        try:
            await MAINTENANCE_TASK
        except asyncio.CancelledError:
            pass
    for server_id in list(GATEWAYS):
        try:
            await close_gateway(server_id)
        except Exception:
            LOG.exception("Could not stop gateway container for server %s", server_id)
    await stop_gateway_relay()
    POOL.shutdown(wait=True, cancel_futures=True)
    try:
        ids = [x["id"] for x in all_configs()]
        with ThreadPoolExecutor(max_workers=max(1, min(len(ids), 16))) as pool:
            futures = [pool.submit(stop_container, sid, False, 120) for sid in ids]
            for future in futures:
                try:
                    future.result(timeout=250)
                except Exception:
                    LOG.exception("A game server did not stop cleanly")
    except Exception:
        LOG.exception("Failed while stopping managed game servers")


app = FastAPI(title="Game Server Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.post("/api/servers/{server_id}/content/{kind}")
async def install_catalog_content(server_id: str, kind: str, payload: dict[str, Any]):
    if kind not in CATALOG_CONTENT_KINDS:
        raise HTTPException(status_code=422, detail="Choose a resource pack, data pack, or world.")
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if game_driver_for(item).game_id != "minecraft":
        raise HTTPException(status_code=409, detail="Java resource packs, data packs, and maps are currently available for Minecraft Java servers.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    platform = str(payload.get("platform", "")).lower()
    if platform not in {"modrinth", "curseforge"} or (platform == "modrinth" and kind == "world"):
        raise HTTPException(status_code=422, detail="Choose Modrinth or CurseForge for this content type.")
    project = payload.get("project") or {}
    project_id = str(project.get("project_id", ""))
    version_id = str(payload.get("version_id", ""))
    file_id = str(payload.get("file_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project_id):
        raise HTTPException(status_code=422, detail="Project details are missing.")
    if platform == "curseforge" and (not project_id.isdigit() or not file_id.isdigit()):
        raise HTTPException(status_code=422, detail="Choose a published CurseForge file version.")
    if platform == "modrinth" and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", version_id):
        raise HTTPException(status_code=422, detail="Choose a published Modrinth version.")
    game_version = await resolve_minecraft_version(item)
    try:
        filename, archive, version = await download_catalog_content(platform, kind, project_id, version_id, file_id, game_version)
        filename, root_prefix = inspect_content_archive(filename, archive, kind)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"{platform.title()} returned HTTP {err.response.status_code} while checking this file.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not download this content: {err}")
    content_id = str(uuid.uuid4())
    record = {"id": content_id, "kind": kind, "platform": platform, "project_id": project_id,
        "version_id": version_id, "file_id": file_id, "title": str(project.get("title", "Minecraft content"))[:120],
        "slug": str(project.get("slug", ""))[:120], "version": str(version.get("number", ""))[:120], "filename": filename}
    if kind == "resourcepack":
        apply_java_content_archive(server_id, content_id, kind, record, archive, root_prefix)
        return {"server": public_server(read_json(config_path(server_id))), "job_id": None,
            "download_url": f"api/servers/{server_id}/content/{content_id}/download"}
    job_id = queue_java_content_change(server_id, content_id, kind, record, archive, root_prefix)
    return {"server": public_server(read_json(config_path(server_id))), "job_id": job_id,
        "download_url": f"api/servers/{server_id}/content/{content_id}/download"}


@app.post("/api/servers/{server_id}/content-upload")
async def upload_java_content(server_id: str, payload: dict[str, Any]):
    kind = str(payload.get("kind", "")).lower()
    if kind not in CATALOG_CONTENT_KINDS:
        raise HTTPException(status_code=422, detail="Choose a resource pack, data pack, or world.")
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if game_driver_for(item).game_id != "minecraft":
        raise HTTPException(status_code=409, detail="Java resource packs, data packs, and maps are currently available for Minecraft Java servers.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    encoded = payload.get("content_base64")
    if not isinstance(encoded, str) or len(encoded) > (CONTENT_ARCHIVE_MAX_BYTES * 4 // 3) + 8:
        raise HTTPException(status_code=413, detail="Content archives must be 128 MB or smaller.")
    try:
        archive = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as err:
        raise HTTPException(status_code=422, detail="The archive upload data is invalid.") from err
    filename, root_prefix = inspect_content_archive(payload.get("filename"), archive, kind)
    content_id = str(uuid.uuid4())
    title = Path(filename).stem[:120]
    record = {"id": content_id, "kind": kind, "platform": "upload", "title": title,
        "filename": filename, "version": "Manual upload"}
    if kind == "resourcepack":
        apply_java_content_archive(server_id, content_id, kind, record, archive, root_prefix)
        return {"server": public_server(read_json(config_path(server_id))), "job_id": None}
    job_id = queue_java_content_change(server_id, content_id, kind, record, archive, root_prefix)
    return {"server": public_server(read_json(config_path(server_id))), "job_id": job_id}


@app.get("/api/servers/{server_id}/content/{content_id}/download")
async def download_installed_content(server_id: str, content_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    try:
        content_id = str(uuid.UUID(content_id))
    except ValueError as err:
        raise HTTPException(status_code=404, detail="Saved content not found.") from err
    record = next((entry for entry in item.get("content_items", []) if entry.get("id") == content_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="Saved content not found.")
    path = content_archive_path(server_id, record)
    filename = safe_content_filename(record.get("filename", path.name), str(record.get("kind", "resourcepack")))
    return FileResponse(path, media_type="application/zip", filename=filename, headers={"Cache-Control": "no-store"})


@app.delete("/api/servers/{server_id}/content/{content_id}")
async def remove_installed_content(server_id: str, content_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    try:
        content_id = str(uuid.UUID(content_id))
    except ValueError as err:
        raise HTTPException(status_code=404, detail="Saved content not found.") from err
    record = next((entry for entry in item.get("content_items", []) if entry.get("id") == content_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="Saved content not found.")
    kind = str(record.get("kind", ""))
    if kind == "world":
        raise HTTPException(status_code=409, detail="World archives stay available in the saved content library. Use Reset world to create a fresh world.")
    if kind not in {"resourcepack", "datapack"}:
        raise HTTPException(status_code=422, detail="Unsupported saved content type.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    keep_running = item.get("desired_state") == "running" and kind == "datapack"
    item["updating"] = keep_running
    item["job_state"] = "content-remove-queued"
    write_json(config_path(server_id), item)

    def remove_files():
        try:
            if kind == "datapack":
                stop_container(server_id, remove=True, timeout=120)
            root = server_dir(server_id).resolve()
            for path_field in ("install_path", "archive_path"):
                relative = str(record.get(path_field, ""))
                if not relative:
                    continue
                path = (root / relative).resolve()
                if root not in path.parents:
                    raise RuntimeError("Saved content path is outside this server folder.")
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            latest = read_json(config_path(server_id))
            latest["content_items"] = [entry for entry in latest.get("content_items", []) if entry.get("id") != content_id]
            write_json(config_path(server_id), latest)
            if keep_running:
                run_container(server_id)
        finally:
            latest = read_json(config_path(server_id))
            if latest:
                latest["updating"] = False
                latest["job_state"] = "running" if keep_running and not latest.get("force_stopped") else "stopped"
                write_json(config_path(server_id), latest)

    if keep_running:
        job_id = submit_job(server_id, "remove datapack", remove_files)
    else:
        remove_files()
        job_id = None
    return {"server": public_server(read_json(config_path(server_id))), "job_id": job_id}




@app.middleware("http")
async def ingress_only(request: Request, call_next):
    # Some Home Assistant ingress versions forward the configured "/" entry as "//".
    # Collapse duplicate leading slashes so the root page and API routes match.
    path = request.scope.get("path", "")
    if path.startswith("//"):
        request.scope["path"] = "/" + path.lstrip("/")
        raw_path = request.scope.get("raw_path")
        if isinstance(raw_path, bytes) and raw_path.startswith(b"//"):
            request.scope["raw_path"] = b"/" + raw_path.lstrip(b"/")

    peer = request.client.host if request.client else ""
    if peer != INGRESS_PEER:
        return JSONResponse({"detail": "This interface is only available through Home Assistant ingress."}, status_code=403)
    response = await call_next(request)
    return response


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/bootstrap")
async def bootstrap():
    items = [public_server(x) for x in all_configs()]
    api_ready = False
    error = DOCKER_ERROR
    eula_acceptances = settings().get("game_eula_accepted")
    if not isinstance(eula_acceptances, dict):
        eula_acceptances = {}
    eula_acceptances = dict(eula_acceptances)
    eula_acceptances["minecraft"] = game_eula_accepted("minecraft")
    try:
        docker_client().ping()
        api_ready = True
        error = None
    except Exception as err:
        error = DOCKER_UNAVAILABLE_MESSAGE
        LOG.warning("Docker Engine health check failed during dashboard bootstrap: %s", err)
    return {
        "eula_accepted": game_eula_accepted("minecraft"),
        "eula_acceptances": eula_acceptances,
        "games": registered_games(),
        "curseforge_key_set": bool(current_curseforge_api_key()),
        "docker_ready": api_ready,
        "docker_error": error,
        "host_networking_enabled": HOST_NETWORKING_ENABLED,
        "servers": items,
        "first_run": not bool(settings().get("eula_accepted")) or len(items) == 0,
    }


@app.post("/api/setup")
async def setup(payload: dict[str, Any]):
    game_id = str(payload.get("game_id") or "minecraft").strip().lower()
    try:
        driver = get_game_driver(game_id)
    except KeyError as err:
        raise HTTPException(status_code=422, detail=f"Unsupported game: {game_id or 'unspecified'}.") from err
    if not driver.requires_eula:
        return {"ok": True}
    if payload.get("eula_accepted") is not True:
        raise HTTPException(status_code=422, detail=f"Accept the {driver.display_name} license before creating a server.")
    current = settings()
    acceptances = current.get("game_eula_accepted")
    if not isinstance(acceptances, dict):
        acceptances = {}
    acceptances[driver.game_id] = True
    current["game_eula_accepted"] = acceptances
    if driver.game_id == "minecraft":
        current["eula_accepted"] = True
    save_settings(current)
    return {"ok": True}


@app.get("/api/settings")
async def get_settings():
    return {"curseforge_key_set": bool(current_curseforge_api_key())}


@app.put("/api/settings")
async def put_settings(payload: dict[str, Any]):
    global CURSEFORGE_API_KEY
    CURSEFORGE_API_KEY = str(payload.get("curseforge_api_key", "")).strip()
    return {"curseforge_key_set": bool(current_curseforge_api_key())}


@app.get("/api/games")
async def games():
    return registered_games()


@app.get("/api/servers")
async def servers():
    return [public_server(item) for item in all_configs()]


@app.post("/api/servers")
async def create_server(payload: dict[str, Any]):
    game_id = str(payload.get("game_id") or "minecraft").strip().lower()
    try:
        driver = get_game_driver(game_id)
    except KeyError as err:
        raise HTTPException(status_code=422, detail=f"Unsupported game: {game_id or 'unspecified'}.") from err
    if driver.requires_eula and not game_eula_accepted(driver.game_id):
        raise HTTPException(status_code=409, detail=f"Complete the first-run {driver.display_name} license step before creating a server.")
    clean = validate_config({**payload, "game_id": driver.game_id})
    default_type = "BEDROCK" if driver.game_id == "minecraft_bedrock" else "VANILLA"
    kind = str(payload.get("server_type", default_type)).upper()
    if kind not in driver.server_types:
        raise HTTPException(status_code=422, detail=f"Choose a supported {driver.display_name} server type.")
    if driver.game_id == "minecraft" and kind in PLUGIN_SERVER_TYPES:
        await require_release_for_plugin_server(kind, str(clean["minecraft_version"]))
    pack_payload = payload.get("modpack")
    if kind in {"MODRINTH", "AUTO_CURSEFORGE"} and not isinstance(pack_payload, dict):
        raise HTTPException(status_code=422, detail="Choose a modpack in the Types tab.")
    modpack = None
    if kind in {"MODRINTH", "AUTO_CURSEFORGE"}:
        project_id = str(pack_payload.get("project_id", ""))
        slug = str(pack_payload.get("slug", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project_id) or not slug:
            raise HTTPException(status_code=422, detail="The selected modpack project is invalid.")
        if kind == "MODRINTH":
            version_id = str(pack_payload.get("version_id", ""))
            if not version_id:
                raise HTTPException(status_code=422, detail="Select a Modrinth modpack version.")
            modpack = {"platform": "modrinth", "project_id": project_id, "slug": slug, "title": str(pack_payload.get("title", "Modrinth pack"))[:120], "icon": safe_icon_url(pack_payload.get("icon")), "version_id": version_id}
        else:
            if not project_id.isdigit():
                raise HTTPException(status_code=422, detail="CurseForge project ID must be numeric.")
            file_id = str(pack_payload.get("file_id", ""))
            if file_id and not file_id.isdigit():
                raise HTTPException(status_code=422, detail="CurseForge file ID must be numeric.")
            modpack = {"platform": "curseforge", "project_id": project_id, "slug": slug, "title": str(pack_payload.get("title", "CurseForge pack"))[:120], "icon": safe_icon_url(pack_payload.get("icon")), "file_id": file_id}
    server_id = str(uuid.uuid4())
    mod_loader = str(payload.get("mod_loader", "")).upper()
    if kind in MOD_LOADERS:
        mod_loader = kind
    if mod_loader and mod_loader not in MOD_LOADERS:
        raise HTTPException(status_code=422, detail="Choose Fabric, Forge, NeoForge, or Quilt as the mod loader.")
    if modpack:
        await validate_pack_version(
            str(modpack["platform"]), str(modpack["project_id"]),
            str(modpack.get("version_id", "")), str(modpack.get("file_id", "")),
            str(clean["minecraft_version"]), mod_loader.lower(),
        )
    item = {
        **clean, "id": server_id, "created_at": __import__("datetime").datetime.now().isoformat(),
        "game_id": driver.game_id, "server_type": kind, "mod_loader": mod_loader,
        "modpack": modpack, "mods": [], "plugins": [], "bedrock_addons": [], "desired_state": "running", "job_state": "queued",
        "last_player_seen": time.time(), "empty_since": None, "updates_available": [],
        "last_update_check": 0, "updating": False, "auto_paused": False, "auto_stopped": False,
        "control_generation": 0, "force_stopped": False,
    }
    server_dir(server_id).mkdir(parents=True, exist_ok=True)
    write_json(config_path(server_id), item)
    try:
        await ensure_game_networking(server_id)
    except OSError as err:
        shutil.rmtree(server_dir(server_id), ignore_errors=True)
        raise HTTPException(status_code=409, detail=f"Could not listen on TCP port {clean['port']}: {err}")
    job_id = submit_job(server_id, "create", lambda: run_container(server_id))
    return {"server": public_server(item), "job_id": job_id}


@app.get("/api/servers/{server_id}")
async def get_server(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    return public_server(item)


@app.patch("/api/servers/{server_id}")
async def update_server(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="Server settings cannot be changed while the server is updating, resetting, or being removed.")
    original = dict(item)
    merged = {**item, **payload}
    clean = validate_config(merged, existing_id=server_id)
    if item.get("game_id") == "minecraft" and item.get("server_type") in PLUGIN_SERVER_TYPES and clean["minecraft_version"] != item.get("minecraft_version"):
        await require_release_for_plugin_server(str(item["server_type"]), str(clean["minecraft_version"]))
    icon_bytes = decode_server_icon(payload.get("server_icon_data")) if payload.get("server_icon_data") is not None else None
    was_running = item.get("desired_state") == "running"
    driver = game_driver_for(item)
    restart_required = icon_bytes is not None or any(item.get(key) != clean.get(key) for key in ("name", "min_ram_mb", "max_ram_mb", *driver.restart_keys))
    icon_path = server_icon_path(server_id)
    old_icon = icon_path.read_bytes() if icon_path.is_file() else None
    if icon_bytes is not None:
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        icon_path.write_bytes(icon_bytes)
        item["server_icon"] = True
    item.update(clean)
    write_json(config_path(server_id), item)
    try:
        await ensure_game_networking(server_id)
    except OSError as err:
        write_json(config_path(server_id), original)
        if icon_bytes is not None:
            if old_icon is None:
                icon_path.unlink(missing_ok=True)
            else:
                icon_path.write_bytes(old_icon)
        raise HTTPException(status_code=409, detail=f"Could not listen on TCP port {clean['port']}: {err}")
    if was_running and restart_required:
        def update_and_start():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "update settings", update_and_start)
    elif restart_required:
        try:
            stop_container(server_id, remove=True)
        except DockerException:
            pass
        job_id = None
    else:
        job_id = None
    return {"server": public_server(item), "job_id": job_id}


def world_data_targets(server_id: str, item: dict[str, Any]) -> list[Path]:
    data_root = (server_dir(server_id) / "data").resolve()
    world_name = str(item.get("world_name", "Bedrock level" if item.get("game_id") == "minecraft_bedrock" else "world"))
    if item.get("game_id") == "minecraft_bedrock":
        targets = [data_root / "worlds" / world_name]
    else:
        targets = [data_root / world_name, data_root / f"{world_name}_nether", data_root / f"{world_name}_the_end"]
    for target in targets:
        resolved = target.resolve()
        if data_root not in resolved.parents:
            raise HTTPException(status_code=409, detail="The configured world path is outside this server's data folder.")
    return targets


@app.post("/api/servers/{server_id}/reset-world")
async def reset_server_world(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is already updating, resetting, or being removed.")
    targets = world_data_targets(server_id, item)
    keep_running = item.get("desired_state") == "running"
    item["world_resetting"] = True
    item["job_state"] = "reset-queued"
    write_json(config_path(server_id), item)

    def reset_world():
        try:
            stop_container(server_id, remove=True, timeout=120)
            for target in targets:
                if target.is_symlink():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            latest = read_json(config_path(server_id))
            if latest.get("game_id") == "minecraft_bedrock":
                installed_packs = [pack for addon in latest.get("bedrock_addons", []) for pack in addon.get("packs", [])]
                if installed_packs:
                    data_root = (server_dir(server_id) / "data").resolve()
                    changes = bedrock_world_pack_changes(data_root, str(latest.get("world_name", "Bedrock level")), installed_packs)
                    write_bedrock_world_pack_changes(changes)
            latest["world_resetting"] = False
            latest["world_settings_applied"] = {}
            latest["world_reset_at"] = time.time()
            latest["job_state"] = "starting" if keep_running else "stopped"
            write_json(config_path(server_id), latest)
            if keep_running:
                run_container(server_id)
        except Exception:
            latest = read_json(config_path(server_id))
            if latest:
                latest["world_resetting"] = False
                latest["job_state"] = "reset-failed"
                write_json(config_path(server_id), latest)
            raise

    job_id = submit_job(server_id, "reset world", reset_world)
    return {"job_id": job_id}


@app.get("/api/servers/{server_id}/icon")
async def get_server_icon(server_id: str):
    item = read_json(config_path(server_id))
    if not item or game_driver_for(item).game_id != "minecraft":
        raise HTTPException(status_code=404, detail="Java server icon not found.")
    path = server_icon_path(server_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Java server icon not found.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.put("/api/servers/{server_id}/icon")
async def put_server_icon(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if game_driver_for(item).game_id != "minecraft":
        raise HTTPException(status_code=409, detail="Server icons are supported for Java Edition servers only.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is busy and cannot change its icon right now.")
    image = decode_server_icon(payload.get("image_base64"))
    path = server_icon_path(server_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    item["server_icon"] = True
    write_json(config_path(server_id), item)
    was_running = item.get("desired_state") == "running"
    if was_running:
        def update_icon():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "update server icon", update_icon)
    else:
        (path.parent / "server-icon.png").unlink(missing_ok=True)
        stop_container(server_id, remove=True)
        job_id = None
    return {"server": public_server(item), "job_id": job_id}


@app.delete("/api/servers/{server_id}/icon")
async def delete_server_icon(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if game_driver_for(item).game_id != "minecraft":
        raise HTTPException(status_code=409, detail="Server icons are supported for Java Edition servers only.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is busy and cannot change its icon right now.")
    path = server_icon_path(server_id)
    path.unlink(missing_ok=True)
    (path.parent / "server-icon.png").unlink(missing_ok=True)
    item["server_icon"] = False
    write_json(config_path(server_id), item)
    was_running = item.get("desired_state") == "running"
    if was_running:
        def restart_without_icon():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "remove server icon", restart_without_icon)
    else:
        stop_container(server_id, remove=True)
        job_id = None
    return {"server": public_server(item), "job_id": job_id}


@app.post("/api/servers/{server_id}/start")
async def start_server(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    original = dict(item)
    item["control_generation"] = int(item.get("control_generation", 0)) + 1
    item["force_stopped"] = False
    item["desired_state"] = "running"
    item["auto_stopped"] = False
    item["auto_paused"] = False
    write_json(config_path(server_id), item)
    try:
        await ensure_game_networking(server_id)
    except OSError as err:
        write_json(config_path(server_id), original)
        raise HTTPException(status_code=409, detail=f"Could not listen on TCP port {item['port']}: {err}")
    job_id = submit_job(server_id, "start", lambda: ensure_running(server_id))
    return {"job_id": job_id}


@app.post("/api/servers/{server_id}/stop")
async def stop_server(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    item["desired_state"] = "stopped"
    item["auto_stopped"] = False
    item["auto_paused"] = False
    item["job_state"] = "stopping"
    write_json(config_path(server_id), item)
    job_id = submit_job(server_id, "stop", lambda: stop_container(server_id, False, 120))
    return {"job_id": job_id}


@app.post("/api/servers/{server_id}/force-exit")
async def force_exit_server(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")

    item["control_generation"] = int(item.get("control_generation", 0)) + 1
    force_generation = item["control_generation"]
    item["force_stopped"] = True
    item["desired_state"] = "stopped"
    item["job_state"] = "force-stopping"
    item["updating"] = False
    item["world_resetting"] = False
    item["auto_stopped"] = False
    item["auto_paused"] = False
    with JOBS_LOCK:
        delete_already_running = any(
            job.get("server_id") == server_id
            and job.get("operation") == "remove server"
            and job.get("state") == "running"
            and job.get("executing") is True
            for job in JOBS.values()
        )
    if not delete_already_running:
        item["deleting"] = False
    write_json(config_path(server_id), item)
    cancel_server_jobs(server_id)

    try:
        await close_game_networking(server_id)
    except Exception:
        LOG.exception("Could not close game networking during force exit for server %s", server_id)
    try:
        await asyncio.to_thread(stop_container, server_id, False, 0, True)
    except (DockerException, RuntimeError) as err:
        raise HTTPException(status_code=503, detail=f"Force exit disabled automatic restarts, but Docker could not stop the server: {err}") from err

    latest = read_json(config_path(server_id))
    if latest:
        force_applied = int(latest.get("control_generation", 0)) == force_generation
        if force_applied:
            latest["force_stopped"] = True
            latest["desired_state"] = "stopped"
            latest["job_state"] = "force-stopped"
            latest["updating"] = False
            latest["world_resetting"] = False
            write_json(config_path(server_id), latest)
        else:
            force_applied = bool(latest.get("force_stopped")) and latest.get("desired_state") == "stopped"
        return {"server": public_server(latest), "stopped": force_applied}
    return {"stopped": True, "deleted": True}


@app.post("/api/servers/{server_id}/update")
async def update_server_now(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    driver = require_game_capability(item, "supports_auto_update", "Manual updates")
    if driver.game_id != "minecraft":
        raise HTTPException(status_code=409, detail=f"Manual updates are not implemented for {driver.display_name} yet.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is already updating, resetting, or being removed.")
    item["updating"] = True
    item["job_state"] = "update-queued"
    write_json(config_path(server_id), item)
    job_id = submit_job(server_id, "manual update", lambda: apply_server_updates(server_id, force=True))
    return {"job_id": job_id}


@app.post("/api/servers/{server_id}/type")
async def install_type(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    driver = game_driver_for(item)
    if not driver.capabilities.supports_type_selection:
        raise HTTPException(status_code=409, detail=f"{driver.display_name} does not provide a server type selector.")
    if driver.game_id != "minecraft":
        raise HTTPException(status_code=409, detail=f"Server type installation is not implemented for {driver.display_name} yet.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    kind = str(payload.get("kind", "")).upper()
    if item.get("crossplay_enabled") is True and kind not in PLUGIN_SERVER_TYPES:
        raise HTTPException(status_code=422, detail="Bedrock cross-play needs Bukkit, Paper, or Purpur server software.")
    provider = str(payload.get("provider", "")).lower()
    requested_version = str(payload.get("minecraft_version", item.get("minecraft_version", "LATEST"))).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", requested_version):
        raise HTTPException(status_code=422, detail="Choose a valid Minecraft release or snapshot version.")
    await require_release_for_plugin_server(kind, requested_version)
    if kind in driver.server_types and kind not in {"MODRINTH", "AUTO_CURSEFORGE"}:
        item.update({"server_type": kind, "minecraft_version": requested_version,
            "modpack": None, "mod_loader": kind if kind in MOD_LOADERS else "", "mods": []})
    elif provider in {"modrinth", "curseforge"} and kind == "MODPACK":
        project = payload.get("project") or {}
        project_id = str(project.get("project_id", ""))
        slug = str(project.get("slug", ""))
        mod_loader = str(payload.get("mod_loader", "")).upper()
        if not slug or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project_id):
            raise HTTPException(status_code=422, detail=f"{provider.title()} pack details are missing.")
        if mod_loader and mod_loader not in MOD_LOADERS:
            raise HTTPException(status_code=422, detail="Choose a supported loader from the selected modpack version.")
        version_id = str(payload.get("version_id") or "")
        file_id = str(payload.get("file_id") or "")
        await validate_pack_version(provider, project_id, version_id, file_id, requested_version, mod_loader.lower())
        if provider == "modrinth":
            item.update({"server_type": "MODRINTH", "mod_loader": mod_loader, "mods": [],
                "minecraft_version": requested_version,
                "modpack": {"platform": "modrinth", "project_id": project_id, "slug": slug,
                    "title": str(project.get("title", "Modrinth pack"))[:120], "icon": safe_icon_url(project.get("icon")), "version_id": version_id}})
        else:
            if not project_id.isdigit():
                raise HTTPException(status_code=422, detail="CurseForge project ID must be numeric.")
            item.update({"server_type": "AUTO_CURSEFORGE", "mod_loader": mod_loader, "mods": [],
                "minecraft_version": requested_version,
                "modpack": {"platform": "curseforge", "project_id": project_id, "slug": slug,
                    "title": str(project.get("title", "CurseForge pack"))[:120], "icon": safe_icon_url(project.get("icon")), "file_id": file_id}})
    else:
        raise HTTPException(status_code=422, detail="Choose a supported server type or modpack.")
    was_running = item.get("desired_state") == "running"
    write_json(config_path(server_id), item)
    job_id = None
    if was_running:
        def change_type():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "install type", change_type)
    else:
        stop_container(server_id, remove=True)
    return {"server": public_server(item), "job_id": job_id}


@app.post("/api/servers/{server_id}/addons")
async def add_bedrock_addon(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    require_game_capability(item, "supports_addons", "Bedrock add-ons")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    addon = await asyncio.to_thread(parse_bedrock_addon_archive, payload.get("filename"), payload.get("content_base64"))
    installed_ids = {
        str(pack.get("uuid", "")).lower()
        for entry in item.get("bedrock_addons", [])
        for pack in entry.get("packs", [])
    }
    duplicate = next((pack["uuid"] for pack in addon["packs"] if pack["uuid"].lower() in installed_ids), None)
    if duplicate:
        raise HTTPException(status_code=409, detail=f"An add-on with pack UUID {duplicate} is already installed.")
    addon_id = str(uuid.uuid4())
    try:
        job_id, busy_item = queue_bedrock_addon_change(
            server_id,
            "add Bedrock add-on",
            lambda: install_bedrock_addon_files(server_id, addon_id, addon),
        )
    except HTTPException:
        raise
    return {"server": public_server(busy_item), "job_id": job_id}


@app.post("/api/servers/{server_id}/addons/curseforge")
async def add_curseforge_bedrock_addon(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    require_game_capability(item, "supports_addons", "Bedrock add-ons")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    project_id = str(payload.get("project_id", ""))
    file_id = str(payload.get("file_id", ""))
    if not project_id.isdigit() or not file_id.isdigit():
        raise HTTPException(status_code=422, detail="Select a valid CurseForge Bedrock add-on file.")
    key = current_curseforge_api_key()
    if not key:
        raise HTTPException(status_code=400, detail="Configure a CurseForge API key in Home Assistant add-on Configuration, or enter a temporary key under Catalog Settings.")
    try:
        class_id = await curseforge_bedrock_addon_class_id()
        headers = {"x-api-key": key, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
            project_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}")
            project_response.raise_for_status()
            project = project_response.json().get("data", {})
            require_curseforge_distribution(project)
            try:
                project_class_id = int(project.get("classId", 0))
            except (TypeError, ValueError):
                project_class_id = 0
            if project_class_id != class_id:
                raise HTTPException(status_code=422, detail="This CurseForge project is not a Minecraft Bedrock add-on.")
            file_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files/{file_id}")
            file_response.raise_for_status()
            file_record = file_response.json().get("data", {})
            if str(file_record.get("modId", "")) != project_id:
                raise HTTPException(status_code=422, detail="This file does not belong to the selected CurseForge add-on.")
            filename = str(file_record.get("fileName", ""))
            if not filename.lower().endswith((".mcaddon", ".mcpack")) or file_record.get("isAvailable") is False:
                raise HTTPException(status_code=422, detail="CurseForge does not have an available .mcaddon or .mcpack file for this version.")
            url_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files/{file_id}/download-url")
            url_response.raise_for_status()
            download_url = str(url_response.json().get("data", ""))
        if not download_url:
            raise HTTPException(status_code=502, detail="CurseForge did not provide a download URL for this add-on.")
        archive = await download_curseforge_addon(download_url, key)
        addon = await asyncio.to_thread(parse_bedrock_addon_archive, filename, base64.b64encode(archive).decode("ascii"))
        addon["display_name"] = str(project.get("name", "")).strip()[:120]
    except HTTPException:
        raise
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"CurseForge returned HTTP {err.response.status_code} while preparing this add-on.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not download this CurseForge add-on: {err}")
    installed_ids = {
        str(pack.get("uuid", "")).lower()
        for entry in item.get("bedrock_addons", [])
        for pack in entry.get("packs", [])
    }
    duplicate = next((pack["uuid"] for pack in addon["packs"] if pack["uuid"].lower() in installed_ids), None)
    if duplicate:
        raise HTTPException(status_code=409, detail=f"An add-on with pack UUID {duplicate} is already installed.")
    addon_id = str(uuid.uuid4())
    job_id, busy_item = queue_bedrock_addon_change(
        server_id,
        "add CurseForge Bedrock add-on",
        lambda: install_bedrock_addon_files(server_id, addon_id, addon),
    )
    return {"server": public_server(busy_item), "job_id": job_id}


@app.delete("/api/servers/{server_id}/addons/{addon_id}")
async def remove_bedrock_addon(server_id: str, addon_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    require_game_capability(item, "supports_addons", "Bedrock add-ons")
    try:
        addon_id = str(uuid.UUID(addon_id))
    except ValueError as err:
        raise HTTPException(status_code=404, detail="Bedrock add-on not found.") from err
    if not any(entry.get("id") == addon_id for entry in item.get("bedrock_addons", [])):
        raise HTTPException(status_code=404, detail="Bedrock add-on not found.")
    job_id, busy_item = queue_bedrock_addon_change(
        server_id,
        "remove Bedrock add-on",
        lambda: remove_bedrock_addon_files(server_id, addon_id),
    )
    return {"server": public_server(busy_item), "job_id": job_id}


def remove_uploaded_managed_files(server_id: str, entries: list[dict[str, Any]], folder_name: str) -> None:
    if folder_name not in {"mods", "plugins"}:
        return
    data_root = server_data_root(server_id)
    folder_path = data_root / folder_name
    allowed_root = folder_path.resolve()
    if allowed_root.parent != data_root or allowed_root.name != folder_name:
        return
    for entry in entries:
        if entry.get("platform") != "upload":
            continue
        relative = PurePosixPath(str(entry.get("manual_path", "")))
        if relative.is_absolute() or relative.parts[:1] != (folder_name,) or any(part in {"", ".", ".."} for part in relative.parts):
            continue
        path = (data_root.joinpath(*relative.parts)).resolve()
        if allowed_root not in path.parents:
            continue
        path.unlink(missing_ok=True)


def parse_local_modpack(filename: Any, archive: bytes) -> dict[str, Any]:
    if not isinstance(filename, str):
        raise HTTPException(status_code=422, detail="Choose a .mrpack or exported CurseForge ZIP file.")
    extension = Path(filename.replace("\\", "/").rsplit("/", 1)[-1]).suffix.lower()
    if extension not in {".mrpack", ".zip"} or not archive or len(archive) > CONTENT_ARCHIVE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Modpack archives must be .mrpack or .zip files no larger than 128 MB.")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            infos = bundle.infolist()
            if len(infos) > CONTENT_ARCHIVE_MAX_FILES:
                raise HTTPException(status_code=422, detail="The modpack contains too many files.")
            expanded = 0
            names: set[str] = set()
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                if not name or "\\" in name or "\x00" in name or info.flag_bits & 0x1 or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or (path.parts and ":" in path.parts[0]):
                    raise HTTPException(status_code=422, detail="The modpack archive contains an unsafe or encrypted file entry.")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise HTTPException(status_code=422, detail="The modpack archive contains a link or special file.")
                if not info.is_dir():
                    if name in names:
                        raise HTTPException(status_code=422, detail="The modpack archive contains duplicate paths.")
                    names.add(name)
                    expanded += int(info.file_size)
                    if expanded > CONTENT_ARCHIVE_MAX_EXPANDED_BYTES:
                        raise HTTPException(status_code=413, detail="The expanded modpack must be 512 MB or smaller.")
            if bundle.testzip():
                raise HTTPException(status_code=422, detail="The modpack ZIP failed its integrity check.")
            if extension == ".mrpack":
                manifest_path = "modrinth.index.json"
                if manifest_path not in names:
                    raise HTTPException(status_code=422, detail="The .mrpack is missing modrinth.index.json.")
                manifest = json.loads(bundle.read(manifest_path).decode("utf-8"))
                if manifest.get("formatVersion") != 1 or manifest.get("game") != "minecraft":
                    raise HTTPException(status_code=422, detail="This Modrinth modpack format is not supported.")
                dependencies = manifest.get("dependencies") or {}
                game_version = str(dependencies.get("minecraft", "")).strip()
                loader = next((loader for key, loader in (("fabric-loader", "FABRIC"), ("forge", "FORGE"), ("neoforge", "NEOFORGE"), ("quilt-loader", "QUILT")) if dependencies.get(key)), "")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", game_version):
                    raise HTTPException(status_code=422, detail="The Modrinth pack does not declare a valid Minecraft version.")
                return {"platform": "modrinth", "extension": extension, "game_version": game_version, "loader": loader, "filename": "modpack.mrpack"}
            manifest_path = "manifest.json"
            if manifest_path not in names:
                raise HTTPException(status_code=422, detail="The CurseForge export ZIP is missing manifest.json.")
            manifest = json.loads(bundle.read(manifest_path).decode("utf-8"))
            if manifest.get("manifestType") != "minecraftModpack" or manifest.get("manifestVersion") != 1:
                raise HTTPException(status_code=422, detail="This CurseForge export format is not supported.")
            minecraft = manifest.get("minecraft") or {}
            game_version = str(minecraft.get("version", "")).strip()
            loaders = minecraft.get("modLoaders") or []
            loader_id = str(next((entry.get("id", "") for entry in loaders if isinstance(entry, dict) and entry.get("primary")), (loaders[0] if loaders and isinstance(loaders[0], dict) else {}).get("id", "")))
            loader_prefix = loader_id.split("-", 1)[0].lower()
            loader = {"fabric": "FABRIC", "forge": "FORGE", "neoforge": "NEOFORGE", "quilt": "QUILT"}.get(loader_prefix, "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", game_version) or not loader:
                raise HTTPException(status_code=422, detail="The CurseForge pack must declare a supported Minecraft version and mod loader.")
            return {"platform": "curseforge", "extension": extension, "game_version": game_version, "loader": loader, "filename": "modpack.zip", "manifest": manifest}
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise HTTPException(status_code=422, detail="Choose a valid Modrinth .mrpack or CurseForge export ZIP.") from err


def install_local_modpack_overrides(server_id: str, content_id: str, archive: bytes) -> None:
    try:
        safe_content_id = str(uuid.UUID(content_id))
    except (ValueError, TypeError, AttributeError) as err:
        raise RuntimeError("Invalid modpack content ID.") from err
    data_root = server_data_root(server_id, create=True)
    staging = data_root / f".gsm-modpack-overrides-{safe_content_id}"
    try:
        staging.mkdir(exist_ok=False)
        staging_root = staging.resolve()
        if staging_root.parent != data_root or staging_root.name != staging.name:
            raise RuntimeError("Invalid modpack override staging directory.")
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            for info in bundle.infolist():
                path = PurePosixPath(info.filename)
                if path.parts[:1] != ("overrides",) or len(path.parts) == 1 or info.is_dir():
                    continue
                relative = PurePosixPath(*path.parts[1:])
                if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                    raise RuntimeError("The modpack override archive contains an unsafe path.")
                if relative.parts[0].lower() in {"world", "world_nether", "world_the_end", "server.properties", "ops.json", "whitelist.json", "banned-players.json", "banned-ips.json", "gsm-server-icon.png"}:
                    continue
                destination = staging_root.joinpath(*relative.parts)
                if staging_root not in destination.resolve().parents:
                    raise RuntimeError("The modpack override archive contains an unsafe path.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        files_to_install: list[tuple[Path, PurePosixPath]] = []
        for source in staging.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(staging)
            target = data_root / relative
            if target.exists() or target.is_symlink():
                raise RuntimeError(f"Modpack override would replace existing server data at {relative.as_posix()}.")
            parent = data_root
            for part in relative.parts[:-1]:
                parent = parent / part
                if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                    raise RuntimeError(f"Modpack override has an unsafe parent directory at {relative.as_posix()}.")
            files_to_install.append((source, relative))
        for source, relative in files_to_install:
            parent = data_root
            for part in relative.parts[:-1]:
                parent = parent / part
                if parent.is_symlink():
                    raise RuntimeError(f"Modpack override has an unsafe parent directory at {relative.as_posix()}.")
                parent.mkdir(exist_ok=True)
                if parent.is_symlink() or not parent.is_dir():
                    raise RuntimeError(f"Modpack override has an unsafe parent directory at {relative.as_posix()}.")
            target = parent / relative.parts[-1]
            if target.exists() or target.is_symlink():
                raise RuntimeError(f"Modpack override would replace existing server data at {relative.as_posix()}.")
            source.replace(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@app.post("/api/servers/{server_id}/modpack-upload")
async def upload_local_modpack(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if game_driver_for(item).game_id != "minecraft":
        raise HTTPException(status_code=409, detail="Local Java modpacks are available for Minecraft Java servers only.")
    if item.get("crossplay_enabled") or item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="Cross-play or a server operation prevents changing the modpack.")
    encoded = payload.get("content_base64")
    if not isinstance(encoded, str) or len(encoded) > (CONTENT_ARCHIVE_MAX_BYTES * 4 // 3) + 8:
        raise HTTPException(status_code=413, detail="Modpack archives must be 128 MB or smaller.")
    try:
        archive = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as err:
        raise HTTPException(status_code=422, detail="The modpack upload data is invalid.") from err
    details = parse_local_modpack(payload.get("filename"), archive)
    content_id = str(uuid.uuid4())
    if details["platform"] == "modrinth":
        modpack = {"platform": "upload", "project_id": content_id, "title": "Local Modrinth modpack", "local_path": f"/data/gsm-modpacks/{content_id}/{details['filename']}"}
        server_type = "MODRINTH"
    else:
        modpack = {"platform": "upload", "project_id": content_id, "title": str(details["manifest"].get("name", "Local CurseForge modpack"))[:120], "local_manifest_path": f"/data/gsm-modpacks/{content_id}/manifest.json"}
        server_type = "AUTO_CURSEFORGE"
    item.update({"server_type": server_type, "mod_loader": details["loader"], "minecraft_version": details["game_version"],
        "modpack": modpack, "mods": [], "plugins": [], "updating": True, "job_state": "modpack-import-queued"})
    write_json(config_path(server_id), item)
    keep_running = item.get("desired_state") == "running"

    def install_pack():
        try:
            stop_container(server_id, remove=True, timeout=120)
            data_root = server_data_root(server_id, create=True)
            modpacks_root = data_root / "gsm-modpacks"
            modpacks_root.mkdir(exist_ok=True)
            resolved_modpacks_root = modpacks_root.resolve()
            if resolved_modpacks_root.parent != data_root or resolved_modpacks_root.name != "gsm-modpacks":
                raise RuntimeError("Invalid modpack storage directory.")
            pack_root = resolved_modpacks_root / content_id
            pack_root.mkdir(exist_ok=False)
            archive_path = pack_root / details["filename"]
            with archive_path.open("xb") as target:
                target.write(archive)
            if details["platform"] == "curseforge":
                manifest_path = pack_root / "manifest.json"
                with manifest_path.open("x", encoding="utf-8") as target:
                    json.dump(details["manifest"], target, indent=2)
            if details["platform"] == "curseforge":
                install_local_modpack_overrides(server_id, content_id, archive)
            latest = read_json(config_path(server_id))
            if latest and keep_running:
                latest["desired_state"] = "running"
                latest["force_stopped"] = False
                write_json(config_path(server_id), latest)
                run_container(server_id)
        finally:
            latest = read_json(config_path(server_id))
            if latest:
                latest["updating"] = False
                latest["job_state"] = "running" if keep_running and not latest.get("force_stopped") else "stopped"
                write_json(config_path(server_id), latest)

    if keep_running:
        job_id = submit_job(server_id, "import modpack", install_pack)
    else:
        install_pack()
        job_id = None
    return {"server": public_server(read_json(config_path(server_id))), "job_id": job_id}


@app.post("/api/servers/{server_id}/managed-files/{kind}")
async def upload_managed_jar(server_id: str, kind: str, payload: dict[str, Any]):
    if kind not in {"mod", "plugin"}:
        raise HTTPException(status_code=422, detail="Choose a mod or plugin JAR.")
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    driver = require_game_capability(item, "supports_mods" if kind == "mod" else "supports_plugins", "Mods" if kind == "mod" else "Plugins")
    if driver.game_id != "minecraft":
        raise HTTPException(status_code=409, detail=f"{kind.title()} uploads are currently available for Minecraft Java servers.")
    if kind == "mod" and server_mod_loader(item) not in MOD_LOADERS:
        raise HTTPException(status_code=409, detail="Choose a compatible mod loader before importing a mod JAR.")
    if kind == "plugin" and str(item.get("server_type", "")).upper() not in PLUGIN_SERVER_TYPES:
        raise HTTPException(status_code=409, detail="Choose Bukkit, Paper, or Purpur before importing a plugin JAR.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    filename = payload.get("filename")
    if not isinstance(filename, str):
        raise HTTPException(status_code=422, detail="Choose a .jar file.")
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip(" .")[:160]
    if not filename.lower().endswith(".jar") or filename in {".jar", ""}:
        raise HTTPException(status_code=422, detail="Choose a file with the .jar extension.")
    encoded = payload.get("content_base64")
    if not isinstance(encoded, str) or len(encoded) > (MANAGED_JAR_MAX_BYTES * 4 // 3) + 8:
        raise HTTPException(status_code=413, detail="Mod and plugin JAR files must be 32 MB or smaller.")
    try:
        jar_bytes = base64.b64decode(encoded, validate=True)
        with zipfile.ZipFile(io.BytesIO(jar_bytes)) as bundle:
            infos = bundle.infolist()
            if len(infos) > CONTENT_ARCHIVE_MAX_FILES or sum(info.file_size for info in infos) > CONTENT_ARCHIVE_MAX_EXPANDED_BYTES:
                raise HTTPException(status_code=413, detail="The JAR archive is too large after expansion.")
            required = {"plugin.yml", "paper-plugin.yml"} if kind == "plugin" else {"fabric.mod.json", "quilt.mod.json", "mcmod.info"}
            names = {PurePosixPath(info.filename).name.lower() for info in infos if not info.is_dir()}
            if kind == "mod" and any(name.endswith("mods.toml") for name in names):
                required.add("mods.toml")
            if not names.intersection(required):
                raise HTTPException(status_code=422, detail=f"The JAR does not contain a recognized {kind} metadata file.")
    except zipfile.BadZipFile as err:
        raise HTTPException(status_code=422, detail="Choose a valid JAR archive.") from err
    except (binascii.Error, ValueError) as err:
        raise HTTPException(status_code=422, detail="The JAR upload data is invalid.") from err
    if not jar_bytes or len(jar_bytes) > MANAGED_JAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Mod and plugin JAR files must be 32 MB or smaller.")
    content_id = str(uuid.uuid4())
    folder_name = "mods" if kind == "mod" else "plugins"
    try:
        data_root = server_data_root(server_id)
    except RuntimeError as err:
        raise HTTPException(status_code=404, detail="Server not found.") from err
    install_folder = data_root / folder_name
    stored_filename = f"{content_id}.jar"
    item[f"{kind}s"] = list(item.get(f"{kind}s", []))
    item[f"{kind}s"].append({"platform": "upload", "project_id": content_id, "title": Path(filename).stem[:120], "filename": filename,
        "manual_path": f"{folder_name}/{stored_filename}"})
    keep_running = item.get("desired_state") == "running"
    item["updating"] = True
    item["job_state"] = f"{kind}-upload-queued"
    write_json(config_path(server_id), item)

    def install_jar():
        installed_path: Path | None = None
        try:
            stop_container(server_id, remove=True, timeout=120)
            resolved_data_root = server_data_root(server_id, create=True)
            install_folder.mkdir(parents=True, exist_ok=True)
            resolved_install_folder = install_folder.resolve()
            if resolved_install_folder.parent != resolved_data_root:
                raise RuntimeError("Invalid managed file install directory.")
            installed_path = resolved_install_folder / stored_filename
            with installed_path.open("xb") as target:
                target.write(jar_bytes)
            latest = read_json(config_path(server_id))
            if latest and keep_running:
                latest["desired_state"] = "running"
                latest["force_stopped"] = False
                write_json(config_path(server_id), latest)
                run_container(server_id)
        except Exception:
            if installed_path is not None:
                installed_path.unlink(missing_ok=True)
            latest = read_json(config_path(server_id))
            if latest:
                latest[f"{kind}s"] = [entry for entry in latest.get(f"{kind}s", []) if entry.get("project_id") != content_id]
                if keep_running and not latest.get("force_stopped"):
                    latest["desired_state"] = "running"
                write_json(config_path(server_id), latest)
            raise
        finally:
            latest = read_json(config_path(server_id))
            if latest:
                latest["updating"] = False
                latest["job_state"] = "running" if keep_running and not latest.get("force_stopped") else "stopped"
                write_json(config_path(server_id), latest)

    if keep_running:
        job_id = submit_job(server_id, f"import {kind}", install_jar)
    else:
        install_jar()
        job_id = None
    return {"server": public_server(read_json(config_path(server_id))), "job_id": job_id}


@app.post("/api/servers/{server_id}/mods")
async def add_mod(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    driver = require_game_capability(item, "supports_mods", "Mods")
    if driver.game_id != "minecraft":
        raise HTTPException(status_code=409, detail=f"Mod installation is not implemented for {driver.display_name} yet.")
    if server_mod_loader(item) not in MOD_LOADERS:
        raise HTTPException(status_code=409, detail="Choose a compatible mod loader or modpack in the Types tab before adding mods.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    platform = str(payload.get("platform", "")).lower()
    project = payload.get("project") or {}
    project_id = str(project.get("project_id", ""))
    if platform not in {"modrinth", "curseforge"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project_id):
        raise HTTPException(status_code=422, detail="Mod details are missing.")
    if platform == "curseforge" and not project_id.isdigit():
        raise HTTPException(status_code=422, detail="CurseForge project ID must be numeric.")
    version_id = str(payload.get("version_id") or "")
    file_id = str(payload.get("file_id") or "")
    if platform == "modrinth" and version_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", version_id):
        raise HTTPException(status_code=422, detail="Modrinth version ID is invalid.")
    if platform == "curseforge" and file_id and not file_id.isdigit():
        raise HTTPException(status_code=422, detail="CurseForge file ID is invalid.")
    game_version = await resolve_minecraft_version(item)
    try:
        await validate_mod_version(platform, project_id, version_id, file_id, game_version, server_mod_loader(item).lower())
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"{platform.title()} returned HTTP {err.response.status_code} while checking mod compatibility.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not verify mod compatibility: {err}")
    mods = list(item.get("mods", []))
    if any(m.get("platform") == platform and m.get("project_id") == project_id for m in mods):
        return {"server": public_server(item), "job_id": None, "already_installed": True}
    mods.append({"platform": platform, "project_id": project_id, "version_id": version_id, "file_id": file_id,
        "title": str(project.get("title", "Mod"))[:120], "slug": str(project.get("slug", ""))[:120]})
    item["mods"] = mods
    was_running = item.get("desired_state") == "running"
    write_json(config_path(server_id), item)
    job_id = None
    if was_running:
        def apply_mod():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "install mod", apply_mod)
    else:
        stop_container(server_id, remove=True)
    return {"server": public_server(item), "job_id": job_id}


@app.delete("/api/servers/{server_id}/mods/{platform}/{project_id}")
async def remove_mod(server_id: str, platform: str, project_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    driver = require_game_capability(item, "supports_mods", "Mods")
    if driver.game_id != "minecraft":
        raise HTTPException(status_code=409, detail=f"Mod removal is not implemented for {driver.display_name} yet.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    removed = [m for m in item.get("mods", []) if m.get("platform") == platform.lower() and m.get("project_id") == project_id]
    item["mods"] = [m for m in item.get("mods", []) if m not in removed]
    was_running = item.get("desired_state") == "running"
    write_json(config_path(server_id), item)
    job_id = None
    if was_running:
        def apply_mods():
            stop_container(server_id, remove=True)
            remove_uploaded_managed_files(server_id, removed, "mods")
            run_container(server_id)
        job_id = submit_job(server_id, "remove mod", apply_mods)
    else:
        stop_container(server_id, remove=True)
        remove_uploaded_managed_files(server_id, removed, "mods")
    return {"server": public_server(item), "job_id": job_id}


@app.post("/api/servers/{server_id}/plugins")
async def add_plugin(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    driver = require_game_capability(item, "supports_plugins", "Plugins")
    if driver.game_id != "minecraft":
        raise HTTPException(status_code=409, detail=f"Plugin installation is not implemented for {driver.display_name} yet.")
    if str(item.get("server_type", "")).upper() not in PLUGIN_SERVER_TYPES:
        raise HTTPException(status_code=409, detail="Choose Bukkit, Paper, or Purpur in the Types tab before adding plugins.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    platform = str(payload.get("platform", "")).lower()
    project = payload.get("project") or {}
    project_id = str(project.get("project_id", ""))
    if platform not in {"modrinth", "curseforge", "hangar"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project_id):
        raise HTTPException(status_code=422, detail="Plugin details are missing.")
    if platform in {"curseforge", "hangar"} and not project_id.isdigit():
        raise HTTPException(status_code=422, detail=f"{platform.title()} project ID must be numeric.")
    if platform == "hangar" and str(item.get("server_type", "")).upper() != "PAPER":
        raise HTTPException(status_code=409, detail="Hangar plugins can only be installed on Paper servers.")
    version_id = str(payload.get("version_id") or "")
    file_id = str(payload.get("file_id") or "")
    if platform == "modrinth" and version_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", version_id):
        raise HTTPException(status_code=422, detail="Modrinth version ID is invalid.")
    if platform == "curseforge" and file_id and not file_id.isdigit():
        raise HTTPException(status_code=422, detail="CurseForge file ID is invalid.")
    if platform == "hangar" and not version_id.isdigit():
        raise HTTPException(status_code=422, detail="Hangar version ID must be numeric.")
    game_version = await resolve_minecraft_version(item)
    try:
        hangar_version = await validate_plugin_version(platform, project_id, version_id, file_id, game_version)
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"{platform.title()} returned HTTP {err.response.status_code} while checking plugin compatibility.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not verify plugin compatibility: {err}")
    plugins = list(item.get("plugins", []))
    if any(plugin.get("platform") == platform and plugin.get("project_id") == project_id for plugin in plugins):
        return {"server": public_server(item), "job_id": None, "already_installed": True}
    plugin_record = {"platform": platform, "project_id": project_id, "version_id": version_id, "file_id": file_id,
        "title": str(project.get("title", "Plugin"))[:120], "slug": str(project.get("slug", ""))[:120]}
    if platform == "hangar" and hangar_version:
        plugin_record["download_url"] = hangar_version["download_url"]
    plugins.append(plugin_record)
    item["plugins"] = plugins
    was_running = item.get("desired_state") == "running"
    write_json(config_path(server_id), item)
    job_id = None
    if was_running:
        def apply_plugin():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "install plugin", apply_plugin)
    else:
        stop_container(server_id, remove=True)
    return {"server": public_server(item), "job_id": job_id}


@app.delete("/api/servers/{server_id}/plugins/{platform}/{project_id}")
async def remove_plugin(server_id: str, platform: str, project_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    driver = require_game_capability(item, "supports_plugins", "Plugins")
    if driver.game_id != "minecraft":
        raise HTTPException(status_code=409, detail=f"Plugin removal is not implemented for {driver.display_name} yet.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    removed = [plugin for plugin in item.get("plugins", []) if plugin.get("platform") == platform.lower() and plugin.get("project_id") == project_id]
    item["plugins"] = [plugin for plugin in item.get("plugins", []) if plugin not in removed]
    was_running = item.get("desired_state") == "running"
    write_json(config_path(server_id), item)
    job_id = None
    if was_running:
        def apply_plugins():
            stop_container(server_id, remove=True)
            remove_uploaded_managed_files(server_id, removed, "plugins")
            run_container(server_id)
        job_id = submit_job(server_id, "remove plugin", apply_plugins)
    else:
        stop_container(server_id, remove=True)
        remove_uploaded_managed_files(server_id, removed, "plugins")
    return {"server": public_server(item), "job_id": job_id}


@app.delete("/api/servers/{server_id}")
async def delete_server(server_id: str):
    path = server_dir(server_id)
    item = read_json(path / "server.json")
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or already being removed.")
    item["deleting"] = True
    write_json(path / "server.json", item)
    await close_game_networking(server_id)

    def remove_server():
        try:
            stop_container(server_id, remove=True, timeout=120)
            shutil.rmtree(path)
        except Exception:
            failed = read_json(path / "server.json")
            if failed:
                failed["deleting"] = False
                write_json(path / "server.json", failed)
            raise

    job_id = submit_job(server_id, "remove server", remove_server)
    return {"job_id": job_id, "deleted": server_id}


@app.post("/api/servers/{server_id}/console")
async def send_console_command(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("updating") or item.get("world_resetting") or item.get("deleting"):
        raise HTTPException(status_code=409, detail="The server is updating, resetting, or being removed.")
    command = str(payload.get("command", "")).strip()
    if not command or len(command) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in command):
        raise HTTPException(status_code=422, detail="Enter a single server command of up to 512 characters.")
    status, _ = current_status(server_id)
    if status != "running":
        raise HTTPException(status_code=409, detail="Start the server before sending commands.")
    try:
        output = await asyncio.to_thread(rcon_output, server_id, command)
    except (DockerException, RuntimeError) as err:
        raise HTTPException(status_code=503, detail=f"Could not reach the server console: {err}")
    return {"command": command, "output": output}


@app.get("/api/servers/{server_id}/logs", response_class=PlainTextResponse)
async def logs(server_id: str):
    if not read_json(config_path(server_id)):
        raise HTTPException(status_code=404, detail="Server not found.")
    try:
        return get_container(server_id).logs(tail=300, timestamps=True).decode("utf-8", errors="replace")
    except NotFound:
        return "This server has not started yet."
    except DockerException as err:
        raise HTTPException(status_code=503, detail=str(err))


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.get("/api/minecraft/versions")
async def minecraft_versions(channel: str = "all"):
    if channel not in {"all", "release", "snapshot"}:
        raise HTTPException(status_code=422, detail="Choose all, release, or snapshot versions.")
    try:
        manifest = await minecraft_manifest()
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"Mojang returned HTTP {err.response.status_code} while listing Minecraft versions.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not reach Mojang's version manifest: {err}")
    versions = [{"id": v.get("id", ""), "type": v.get("type", ""), "release_time": v.get("releaseTime", "")}
        for v in manifest.get("versions", []) if v.get("type") in {"release", "snapshot"}
        and (channel == "all" or v.get("type") == channel)]
    return {"latest": manifest.get("latest", {}), "versions": versions, "total": len(versions)}


@app.get("/api/catalog")
async def catalog(platform: str, kind: str, server_id: str = "", q: str = "", offset: int = 0, sort: str = "downloads"):
    if kind not in {"mod", "modpack", "plugin", "bedrock_addon", *CATALOG_CONTENT_KINDS} or platform not in {"modrinth", "curseforge", "hangar"}:
        raise HTTPException(status_code=422, detail="Choose a supported catalog and category.")
    if platform == "modrinth" and kind == "world":
        raise HTTPException(status_code=422, detail="Modrinth does not offer a world project type.")
    if platform == "hangar" and kind != "plugin":
        raise HTTPException(status_code=422, detail="Hangar is available for Paper plugins only.")
    if len(q) > 100 or offset < 0:
        raise HTTPException(status_code=422, detail="Search text or page offset is invalid.")
    if sort not in {"downloads", "updated"}:
        raise HTTPException(status_code=422, detail="Sort must be downloads or updated.")
    if kind == "bedrock_addon":
        if platform != "curseforge":
            raise HTTPException(status_code=422, detail="CurseForge is the supported catalog for Bedrock add-ons.")
        item = read_json(config_path(server_id)) if server_id else None
        if not item:
            raise HTTPException(status_code=404, detail="Bedrock server not found.")
        require_game_capability(item, "supports_addons", "Bedrock add-ons")
        try:
            return await curseforge_bedrock_addon_search(q, offset, sort)
        except HTTPException:
            raise
        except httpx.HTTPStatusError as err:
            raise HTTPException(status_code=502, detail=f"CurseForge returned HTTP {err.response.status_code}. Check API access and try again.")
        except httpx.HTTPError as err:
            raise HTTPException(status_code=502, detail=f"Could not reach CurseForge: {err}")
    item = read_json(config_path(server_id)) if server_id else {"minecraft_version": "LATEST", "mod_loader": ""}
    if server_id and not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if kind == "mod" and server_mod_loader(item) not in MOD_LOADERS:
        raise HTTPException(status_code=409, detail="Select a mod loader such as Fabric or Forge in Types before browsing mods.")
    if kind == "plugin" and str(item.get("server_type", "")).upper() not in PLUGIN_SERVER_TYPES:
        raise HTTPException(status_code=409, detail="Select Bukkit, Paper, or Purpur in Types before browsing plugins.")
    if platform == "hangar" and str(item.get("server_type", "")).upper() != "PAPER":
        raise HTTPException(status_code=409, detail="Hangar plugins are available when this server uses Paper.")
    try:
        if platform == "hangar":
            return await hangar_plugin_search(q, item, offset, sort)
        return await (modrinth_search(kind, q, item, offset, sort) if platform == "modrinth" else curseforge_search(kind, q, item, offset, sort))
    except HTTPException:
        raise
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"{platform.title()} returned HTTP {err.response.status_code}. Check API access and try again.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not reach {platform.title()}: {err}")


@app.get("/api/catalog/{platform}/{project_id}")
async def catalog_detail(platform: str, project_id: str, kind: str = "modpack", server_id: str = "", offset: int = 0):
    if platform not in {"modrinth", "curseforge", "hangar"} or kind not in {"mod", "modpack", "plugin", "bedrock_addon", *CATALOG_CONTENT_KINDS}:
        raise HTTPException(status_code=422, detail="Unsupported catalog item.")
    if platform == "modrinth" and kind == "world":
        raise HTTPException(status_code=422, detail="Modrinth does not offer a world project type.")
    if platform == "hangar" and kind != "plugin":
        raise HTTPException(status_code=422, detail="Hangar is available for Paper plugins only.")
    if offset < 0:
        raise HTTPException(status_code=422, detail="Version page offset cannot be negative.")
    item = read_json(config_path(server_id)) if server_id else {"minecraft_version": "LATEST", "mod_loader": ""}
    if server_id and not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project_id):
        raise HTTPException(status_code=422, detail="Catalog project ID is invalid.")
    if platform == "curseforge" and not project_id.isdigit():
        raise HTTPException(status_code=422, detail="CurseForge project ID must be numeric.")
    if platform == "hangar" and not project_id.isdigit():
        raise HTTPException(status_code=422, detail="Hangar project ID must be numeric.")
    if kind == "bedrock_addon":
        if platform != "curseforge":
            raise HTTPException(status_code=422, detail="CurseForge is the supported catalog for Bedrock add-ons.")
        item = read_json(config_path(server_id)) if server_id else None
        if not item:
            raise HTTPException(status_code=404, detail="Bedrock server not found.")
        require_game_capability(item, "supports_addons", "Bedrock add-ons")
        try:
            return await curseforge_bedrock_addon_detail(project_id, offset)
        except HTTPException:
            raise
        except httpx.HTTPStatusError as err:
            raise HTTPException(status_code=502, detail=f"CurseForge returned HTTP {err.response.status_code}.")
        except httpx.HTTPError as err:
            raise HTTPException(status_code=502, detail=f"Could not reach CurseForge: {err}")
    if kind == "plugin" and str(item.get("server_type", "")).upper() not in PLUGIN_SERVER_TYPES:
        raise HTTPException(status_code=409, detail="Select Bukkit, Paper, or Purpur in Types before browsing plugins.")
    if platform == "hangar" and str(item.get("server_type", "")).upper() != "PAPER":
        raise HTTPException(status_code=409, detail="Hangar plugins are available when this server uses Paper.")
    game = await resolve_minecraft_version(item) if kind in {"mod", "plugin", *CATALOG_CONTENT_KINDS} else ""
    loader = server_mod_loader(item).lower()
    try:
        if platform == "hangar":
            async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/json"}) as client:
                project_response = await client.get(f"{HANGAR_BASE}/projects/{project_id}")
                project_response.raise_for_status()
                project = project_response.json()
                all_project_types = {str(value) for value in (project.get("all_project_types") or [])}
                if kind in CATALOG_CONTENT_KINDS and project.get("project_type") not in {kind, None, ""} and kind not in all_project_types:
                    raise HTTPException(status_code=422, detail="This Modrinth project is not the selected content type.")
                versions_response = await client.get(
                    f"{HANGAR_BASE}/projects/{project_id}/versions",
                    params={"limit": 50, "offset": offset, "platform": "PAPER"},
                )
                versions_response.raise_for_status()
                payload = versions_response.json()
            namespace = project.get("namespace") or {}
            owner = str(namespace.get("owner", "")).strip()
            slug = str(namespace.get("slug", "")).strip()
            versions_data = [v for v in payload.get("result", []) if isinstance(v, dict) and v.get("visibility") == "public"]
            pagination = payload.get("pagination") or {}
            total = int(pagination.get("count", len(versions_data)) or 0)
            return {
                "project_id": str(project.get("id", project_id)),
                "slug": f"{owner}/{slug}" if owner else slug,
                "title": str(project.get("name", slug)),
                "description": str(project.get("description", "")),
                "icon": str(project.get("avatarUrl", "")),
                "platform": platform, "loaders": ["Paper"],
                "authors": ([{"name": owner, "url": f"https://hangar.papermc.io/{quote(owner, safe='')}"}] if owner else []),
                "versions": [hangar_version_record(v, game) for v in versions_data],
                "version_offset": offset, "total_versions": total,
                "has_more": offset + len(versions_data) < total,
                "website_url": f"https://hangar.papermc.io/{quote(owner, safe='')}/{quote(slug, safe='')}" if owner and slug else "",
            }
        if platform == "modrinth":
            async with httpx.AsyncClient(timeout=20, headers=modrinth_headers()) as client:
                project_response = await client.get(f"{MODRINTH_BASE}/project/{project_id}")
                project_response.raise_for_status()
                project = project_response.json()
                versions_response = await client.get(f"{MODRINTH_BASE}/project/{project_id}/version")
                versions_response.raise_for_status()
                all_versions = versions_response.json()
                members: list[Any] = []
                try:
                    members_response = await client.get(f"{MODRINTH_BASE}/project/{project_id}/members")
                    if members_response.is_success:
                        member_payload = members_response.json()
                        if isinstance(member_payload, list):
                            members = member_payload
                except (httpx.HTTPError, ValueError):
                    # Creator links are optional; a team endpoint failure should not block catalog use.
                    members = []
            total = len(all_versions)
            page = all_versions[offset:offset + 50]
            return {"project_id": project_id, "slug": project.get("slug", ""), "title": project.get("title", ""),
                "description": project.get("body", project.get("description", "")), "icon": project.get("icon_url", ""),
                "platform": platform, "loaders": project.get("loaders", []),
                "authors": modrinth_author_links(members),
                "versions": [modrinth_version_record(v, kind, game, loader) for v in page],
                "version_offset": offset, "total_versions": total, "has_more": offset + len(page) < total}
        key = current_curseforge_api_key()
        if not key:
            raise HTTPException(status_code=400, detail="Configure a CurseForge API key in Home Assistant add-on Configuration, or enter a temporary key under Catalog Settings.")
        headers = {"x-api-key": key, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
            project_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}")
            project_response.raise_for_status()
            project = project_response.json().get("data", {})
            require_curseforge_distribution(project)
            if kind in CATALOG_CONTENT_KINDS:
                expected_class_id, expected_category_id = await curseforge_content_filter(kind)
                try:
                    project_class_id = int(project.get("classId", 0))
                    project_categories = {int(value.get("id")) for value in project.get("categories", []) if isinstance(value, dict) and value.get("id") is not None}
                except (TypeError, ValueError):
                    project_class_id, project_categories = 0, set()
                if project_class_id != expected_class_id or (expected_category_id is not None and expected_category_id not in project_categories):
                    raise HTTPException(status_code=422, detail="This CurseForge project is not the selected content type.")
            desc_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/description")
            desc_response.raise_for_status()
            description = clean_description(desc_response.json().get("data", ""))
            files_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files", params={"pageSize": 50, "index": offset})
            files_response.raise_for_status()
            files_payload = files_response.json()
        files = files_payload.get("data", [])
        total = int(files_payload.get("pagination", {}).get("totalCount", len(files)))
        return {"project_id": str(project.get("id", project_id)), "slug": project.get("slug", str(project_id)),
            "title": project.get("name", ""), "description": description or project.get("summary", ""),
            "icon": (project.get("logo") or {}).get("url", ""), "platform": platform,
            "authors": curseforge_author_links(project),
            "versions": [curseforge_version_record(v, kind, game, loader) for v in files],
            "version_offset": offset, "total_versions": total, "has_more": offset + len(files) < total,
            "website_url": (project.get("links") or {}).get("websiteUrl", "")}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"{platform.title()} returned HTTP {err.response.status_code}.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not reach {platform.title()}: {err}")



