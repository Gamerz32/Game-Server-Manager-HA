from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import docker
import httpx
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
LOG = logging.getLogger("minecraft-manager")
DATA_DIR = Path("/data")
SERVERS_DIR = DATA_DIR / "servers"
SETTINGS_FILE = DATA_DIR / "manager-settings.json"
STATIC_DIR = Path(__file__).parent / "static"
DOCKER_SOCKET = "unix:///run/docker.sock"
SERVER_IMAGE = "itzg/minecraft-server"
MANAGER_LABEL = "io.gamerz32.minecraft-manager"
INGRESS_PEER = "172.30.32.2"
MC_GAME_ID = 432
CF_CLASS_IDS = {"modpack": 4471, "mod": 6, "plugin": 5}
MODRINTH_BASE = "https://api.modrinth.com/v2"
CURSEFORGE_BASE = "https://api.curseforge.com/v1"
JAVA_TAGS = {"auto": "latest", "8": "java8", "17": "java17", "21": "java21", "25": "java25"}
UPDATE_CHECK_INTERVAL = 4 * 60 * 60
MAINTENANCE_INTERVAL = 60
UPDATE_DISCONNECT_REASON = "The Server is Updating, Please Wait"
GATEWAYS: dict[str, tuple[asyncio.AbstractServer, int]] = {}
MAINTENANCE_TASK: asyncio.Task | None = None
SERVER_TYPES = {"VANILLA", "BUKKIT", "PAPER", "PURPUR", "FABRIC", "FORGE", "NEOFORGE", "QUILT", "MODRINTH", "AUTO_CURSEFORGE"}
MOD_LOADERS = {"FABRIC", "FORGE", "NEOFORGE", "QUILT"}
PLUGIN_SERVER_TYPES = {"BUKKIT", "PAPER", "PURPUR"}
CF_LOADER_IDS = {"FORGE": 1, "FABRIC": 4, "QUILT": 5, "NEOFORGE": 6}
CF_LOADER_NAMES = {value: key.lower() for key, value in CF_LOADER_IDS.items()}
MINECRAFT_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
MINECRAFT_VERSION_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="minecraft-job")
HOST_DATA_SOURCE: Path | None = None
DOCKER_ERROR: str | None = None
DOCKER_CLIENT: docker.DockerClient | None = None
DOCKER_CLIENT_LOCK = threading.Lock()
SERVER_LOCKS: dict[str, threading.Lock] = {}
SERVER_LOCKS_LOCK = threading.Lock()
SHUTTING_DOWN = False


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


def read_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else (fallback or {})
    except (OSError, json.JSONDecodeError):
        return fallback or {}


def write_json(path: Path, value: dict[str, Any], secret: bool = False) -> None:
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
    return read_json(SETTINGS_FILE, {"eula_accepted": False, "curseforge_api_key": ""})


def save_settings(value: dict[str, Any]) -> None:
    write_json(SETTINGS_FILE, value, secret=True)


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


def docker_self_data_source(client: docker.DockerClient) -> Path:
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
    for mount in own.attrs.get("Mounts", []):
        if mount.get("Destination") == "/data" and mount.get("Source"):
            return Path(mount["Source"])
    raise RuntimeError("Home Assistant's persistent /data mount was not found in the Docker container details.")


def host_server_data_dir(server_id: str) -> Path:
    global HOST_DATA_SOURCE
    if HOST_DATA_SOURCE is None:
        cli = docker_client()
        HOST_DATA_SOURCE = docker_self_data_source(cli)
    path = (HOST_DATA_SOURCE / "servers" / server_id / "data").resolve()
    root = (HOST_DATA_SOURCE / "servers").resolve()
    if root not in path.parents:
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
    old_options = read_json(DATA_DIR / "options.json")
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
    LOG.info("Imported the previous vanilla server to /data; its original /share copy was kept intact.")


def get_container(server_id: str):
    container = docker_client().containers.get(f"ha-minecraft-{server_id}")
    container.reload()
    labels = container.attrs.get("Config", {}).get("Labels") or {}
    if labels.get(MANAGER_LABEL) != "true" or labels.get(f"{MANAGER_LABEL}.server_id") != server_id:
        raise RuntimeError("The Docker container name is occupied by a container this app does not manage.")
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
    port = item.get("port")
    return {**item, "status": status, "job": job}


def validate_config(data: dict[str, Any], existing_id: str | None = None) -> dict[str, Any]:
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=422, detail="Enter a server name with 1 to 60 characters.")
    try:
        port = int(data.get("port", 25565))
        min_ram = int(data.get("min_ram_mb", 1024))
        max_ram = int(data.get("max_ram_mb", 4096))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Port and RAM values must be whole numbers.")
    if port < 1024 or port > 65535 or port == 8099:
        raise HTTPException(status_code=422, detail="Choose a TCP port between 1024 and 65535, excluding 8099 (the manager UI port).")
    if min_ram < 512 or max_ram < min_ram or max_ram > 131072:
        raise HTTPException(status_code=422, detail="RAM must be at least 512 MB, with maximum RAM greater than or equal to minimum RAM.")
    java = str(data.get("java_version", "auto"))
    if java not in JAVA_TAGS:
        raise HTTPException(status_code=422, detail="Choose Java auto, 8, 17, 21, or 25.")
    mc_version = str(data.get("minecraft_version", "LATEST")).strip() or "LATEST"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", mc_version):
        raise HTTPException(status_code=422, detail="Use a Minecraft version such as LATEST, 1.21.4, or SNAPSHOT.")
    if mc_version.lower() == "latest":
        mc_version = "LATEST"
    for item in all_configs():
        try:
            assigned_port = int(item.get("port", 0))
        except (ValueError, TypeError):
            continue
        if item.get("id") != existing_id and assigned_port == port:
            raise HTTPException(status_code=409, detail=f"Port {port} is already assigned to {item.get('name', 'another server')}.")
    def bounded_int(key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(data.get(key, default))
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"{key.replace('_', ' ').title()} must be a whole number.")
        if value < low or value > high:
            raise HTTPException(status_code=422, detail=f"{key.replace('_', ' ').title()} must be between {low} and {high}.")
        return value

    return {
        "name": name,
        "port": port,
        "min_ram_mb": min_ram,
        "max_ram_mb": max_ram,
        "java_version": java,
        "minecraft_version": mc_version,
        "auto_pause_enabled": data.get("auto_pause_enabled", False) is True,
        "auto_pause_minutes": bounded_int("auto_pause_minutes", 20, 5, 1440),
        "auto_stop_enabled": data.get("auto_stop_enabled", False) is True,
        "auto_stop_days": bounded_int("auto_stop_days", 3, 1, 365),
        "auto_update_enabled": data.get("auto_update_enabled", False) is True,
        "auto_update_idle_hours": bounded_int("auto_update_idle_hours", 3, 1, 168),
        "wake_on_join": data.get("wake_on_join", True) is not False,
    }


def env_for_server(item: dict[str, Any]) -> dict[str, str]:
    env = {
        "EULA": "TRUE",
        "TYPE": "PAPER" if str(item.get("server_type", "")).upper() == "BUKKIT" else str(item.get("server_type", "VANILLA")),
        "VERSION": str(item.get("minecraft_version", "LATEST")),
        "INIT_MEMORY": f"{item.get('min_ram_mb', 1024)}M",
        "MAX_MEMORY": f"{item.get('max_ram_mb', 4096)}M",
        "MOTD": str(item.get("name", "Minecraft Server")),
        "ONLINE_MODE": "TRUE",
        "ENABLE_RCON": "true",
        "RCON_PASSWORD": secrets.token_urlsafe(32),
    }
    pack = item.get("modpack") or {}
    if item.get("server_type") == "MODRINTH" and pack:
        env["MODRINTH_MODPACK"] = str(pack.get("project_id") or pack.get("slug"))
        if pack.get("version_id"):
            env["MODRINTH_VERSION"] = str(pack["version_id"])
    elif item.get("server_type") == "AUTO_CURSEFORGE" and pack:
        env["CF_SLUG"] = str(pack.get("slug") or pack.get("project_id"))
        if pack.get("file_id"):
            env["CF_FILE_ID"] = str(pack["file_id"])
    modrinth_projects = []
    for mod in item.get("mods", []):
        if mod.get("platform") != "modrinth":
            continue
        project = str(mod.get("project_id", ""))
        version_id = str(mod.get("version_id", ""))
        loader_prefix = str(item.get("mod_loader", "")).lower()
        if loader_prefix in {"fabric", "forge", "neoforge", "quilt"}:
            project = f"{loader_prefix}:{project}"
        if version_id:
            project = f"{project}:{version_id}"
        if project:
            modrinth_projects.append(project)
    if str(item.get("server_type", "")).upper() in PLUGIN_SERVER_TYPES:
        for plugin in item.get("plugins", []):
            if plugin.get("platform") != "modrinth":
                continue
            project = str(plugin.get("project_id", ""))
            version_id = str(plugin.get("version_id", ""))
            if version_id:
                project = f"{project}:{version_id}"
            if project:
                modrinth_projects.append(project)
    curseforge_files = [
        f"{entry.get('project_id')}:{entry.get('file_id')}"
        for entry in item.get("mods", [])
        if entry.get("platform") == "curseforge" and entry.get("project_id") and entry.get("file_id")
    ]
    if str(item.get("server_type", "")).upper() in PLUGIN_SERVER_TYPES:
        curseforge_files.extend(
            f"{entry.get('project_id')}:{entry.get('file_id')}"
            for entry in item.get("plugins", [])
            if entry.get("platform") == "curseforge" and entry.get("project_id") and entry.get("file_id")
        )
    if modrinth_projects:
        env["MODRINTH_PROJECTS"] = "\n".join(modrinth_projects)
    if curseforge_files:
        env["CURSEFORGE_FILES"] = "\n".join(curseforge_files)
    api_key = settings().get("curseforge_api_key", "").strip()
    if api_key:
        env["CF_API_KEY"] = api_key
    return env


def run_container(server_id: str) -> None:
    if SHUTTING_DOWN:
        return
    item = read_json(config_path(server_id))
    if not item:
        raise RuntimeError("Server configuration was not found.")
    cli = docker_client()
    try:
        existing = get_container(server_id)
        if existing.status == "running":
            return
        existing.remove(force=True)
    except NotFound:
        pass
    image = f"{SERVER_IMAGE}:{JAVA_TAGS.get(str(item.get('java_version', 'auto')), 'latest')}"
    item["job_state"] = "pulling"
    write_json(config_path(server_id), item)
    try:
        cli.images.get(image)
    except ImageNotFound:
        cli.images.pull(image)
    if SHUTTING_DOWN:
        return
    item["job_state"] = "starting"
    write_json(config_path(server_id), item)
    data_source = host_server_data_dir(server_id)
    labels = {MANAGER_LABEL: "true", f"{MANAGER_LABEL}.server_id": server_id}
    env = env_for_server(item)
    container = cli.containers.create(
        image=image,
        name=f"ha-minecraft-{server_id}",
        labels=labels,
        environment=env,
        network_mode="bridge",
        volumes={str(data_source): {"bind": "/data", "mode": "rw"}},
        restart_policy={"Name": "unless-stopped"},
        tty=True,
        stdin_open=True,
    )
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


def stop_container(server_id: str, remove: bool = False, timeout: int = 120) -> None:
    try:
        container = get_container(server_id)
    except NotFound:
        return
    try:
        container.reload()
        if container.status == "paused":
            container.unpause()
            container.reload()
        if container.status == "running":
            container.stop(timeout=timeout)
        if remove:
            container.remove()
    except NotFound:
        return


def submit_job(server_id: str, operation: str, fn) -> str:
    job_id = str(uuid.uuid4())
    job = {"id": job_id, "server_id": server_id, "operation": operation, "state": "queued", "message": "Queued", "created_at": __import__("datetime").datetime.now().isoformat()}
    with JOBS_LOCK:
        JOBS[job_id] = job

    def run():
        with JOBS_LOCK:
            JOBS[job_id]["state"] = "running"
            JOBS[job_id]["message"] = "Working"
        try:
            with server_lock(server_id):
                fn()
            with JOBS_LOCK:
                JOBS[job_id]["state"] = "complete"
                JOBS[job_id]["message"] = "Complete"
        except Exception as err:
            LOG.exception("Server %s job failed", server_id)
            with JOBS_LOCK:
                JOBS[job_id]["state"] = "failed"
                JOBS[job_id]["message"] = str(err)[:500]

    POOL.submit(run)
    return job_id


def ensure_running(server_id: str) -> None:
    status, container = current_status(server_id)
    if status == "running":
        return
    if container is not None and status == "paused":
        container.unpause()
        item = read_json(config_path(server_id))
        item["desired_state"] = "running"
        item["job_state"] = "running"
        item["auto_paused"] = False
        write_json(config_path(server_id), item)
        return
    if container is not None and status == "exited":
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
                "version": {"name": "Minecraft Server Manager", "protocol": -1},
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
        for details in networks.values():
            address = details.get("IPAddress")
            if address:
                return str(address), 25565
        address = container.attrs.get("NetworkSettings", {}).get("IPAddress")
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
            should_wake = item.get("auto_stopped") or item.get("desired_state") == "running"
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


async def ensure_gateway(server_id: str) -> None:
    item = read_json(config_path(server_id))
    if not item or item.get("deleting"):
        return
    port = int(item["port"])
    current = GATEWAYS.get(server_id)
    if current and current[1] == port and current[0].is_serving():
        return
    for other_id, (_, other_port) in GATEWAYS.items():
        if other_id != server_id and other_port == port:
            raise OSError(f"TCP port {port} is already managed by another server.")
    server = await asyncio.start_server(
        lambda reader, writer: handle_minecraft_connection(server_id, reader, writer),
        host="0.0.0.0",
        port=port,
        reuse_address=True,
        backlog=128,
    )
    old = GATEWAYS.get(server_id)
    GATEWAYS[server_id] = (server, port)
    if old:
        old[0].close()
        await old[0].wait_closed()


async def close_gateway(server_id: str) -> None:
    current = GATEWAYS.pop(server_id, None)
    if current:
        current[0].close()
        await current[0].wait_closed()


def rcon_output(server_id: str, command: str) -> str:
    container = get_container(server_id)
    if container.status != "running":
        raise RuntimeError("The server is not running.")
    result = container.exec_run(["rcon-cli", command], stdout=True, stderr=True, demux=False)
    output = result.output or b""
    return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)


def online_player_count(server_id: str) -> int | None:
    try:
        output = rcon_output(server_id, "list")
    except (DockerException, RuntimeError):
        return None
    match = re.search(r"There are\s+(\d+)\s+of a max", output, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _compatible_modrinth_version(version: dict[str, Any], game_version: str, loader: str) -> bool:
    games = version.get("game_versions") or []
    loaders = [str(value).lower() for value in version.get("loaders") or []]
    return game_version in games and (not loader or loader.lower() in loaders)


def provider_update(item: dict[str, Any], platform: str, project_id: str, current_id: str, kind: str) -> dict[str, Any] | None:
    game_version = str(item.get("minecraft_version", "LATEST"))
    loader = str(item.get("mod_loader", "")).lower()
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
        else:
            api_key = settings().get("curseforge_api_key", "").strip()
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
    except (httpx.HTTPError, ValueError, TypeError) as err:
        LOG.warning("Could not check %s updates for project %s: %s", platform, project_id, err)
        raise
    return None


def dockerhub_latest_digest(image: str) -> str | None:
    tag = image.rsplit(":", 1)[-1]
    try:
        auth = httpx.get(
            "https://auth.docker.io/token",
            params={"service": "registry.docker.io", "scope": "repository:itzg/minecraft-server:pull"},
            timeout=12,
        )
        auth.raise_for_status()
        token = auth.json().get("token")
        if not token:
            return None
        response = httpx.head(
            f"https://registry-1.docker.io/v2/itzg/minecraft-server/manifests/{tag}",
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
    image = f"{SERVER_IMAGE}:{JAVA_TAGS.get(str(item.get('java_version', 'auto')), 'latest')}"
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
        write_json(config_path(server_id), item)
        stop_container(server_id, remove=True, timeout=120)
        image = f"{SERVER_IMAGE}:{JAVA_TAGS.get(str(item.get('java_version', 'auto')), 'latest')}"
        docker_client().images.pull(image)
        if was_running:
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
        item["job_state"] = "running" if was_running else "stopped"
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
        if not item or item.get("updating") or item.get("deleting"):
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
                    await ensure_gateway(server_id)
                except OSError:
                    LOG.exception("Could not maintain gateway for server %s", server_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Minecraft maintenance cycle failed")
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
    return {"User-Agent": "Gamerz32/Home-Assistant-Gameservers-ADDONS/2.0.0 (Home Assistant app)"}



def mod_compatibility(game_version: str, loader: str, game_versions: list[str], loaders: list[str]) -> bool | None:
    if not game_version or not loader:
        return None
    return game_version in game_versions and loader.lower() in {str(value).lower() for value in loaders}


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
    compatible = mod_compatibility(game_version, loader, games, loaders) if kind == "mod" else (game_version in games if kind == "plugin" and game_version else None)
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
    compatible = (game_version in games and loader.lower() == version_loader) if kind == "mod" and game_version and loader else (game_version in games if kind == "plugin" and game_version else None)
    try:
        release_type = {1: "release", 2: "beta", 3: "alpha"}.get(int(version.get("releaseType", 0) or 0), "")
    except (TypeError, ValueError):
        release_type = ""
    return {"id": str(version.get("id", "")), "number": str(version.get("displayName") or version.get("fileName") or version.get("id", "")),
        "name": str(version.get("fileName", "")), "game_versions": games, "loaders": [version_loader] if version_loader else [],
        "targets": targets, "compatible": compatible, "version_type": release_type,
        "date_published": str(version.get("fileDate", "")), "downloads": int(version.get("downloadCount", 0)),
        "size": int(version.get("fileLength", 0))}


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
        key = settings().get("curseforge_api_key", "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Add a CurseForge API key in the manager's Settings to use CurseForge.")
        async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
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
        key = settings().get("curseforge_api_key", "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Add a CurseForge API key in the manager's Settings to use CurseForge.")
        async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
            response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files/{file_id}")
            response.raise_for_status()
            version = response.json().get("data", {})
        games = curseforge_game_versions(version.get("gameVersions", []))
        record = curseforge_version_record(version, "mod", game_version, loader)
        if game_version not in games or record["compatible"] is not True:
            raise HTTPException(status_code=409, detail="This CurseForge file is not compatible with the server's Minecraft version and loader.")
        return
    raise HTTPException(status_code=422, detail="Unsupported mod platform.")


async def validate_plugin_version(platform: str, project_id: str, version_id: str, file_id: str, game_version: str) -> None:
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
        key = settings().get("curseforge_api_key", "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Add a CurseForge API key in the manager's Settings to use CurseForge.")
        async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
            response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files/{file_id}")
            response.raise_for_status()
            version = response.json().get("data", {})
        if str(version.get("modId", project_id)) != project_id:
            raise HTTPException(status_code=422, detail="The selected file does not belong to this CurseForge plugin.")
        if game_version not in curseforge_game_versions(version.get("gameVersions", [])):
            raise HTTPException(status_code=409, detail="This CurseForge plugin file does not support the server's Minecraft version.")
        return
    raise HTTPException(status_code=422, detail="Unsupported plugin platform.")


async def modrinth_search(kind: str, query: str, item: dict[str, Any], offset: int, sort: str) -> dict[str, Any]:
    index = {"downloads": "downloads", "updated": "updated"}[sort]
    params = {"query": query, "facets": json.dumps([[f"project_type:{kind}"]]), "limit": 24, "offset": offset, "index": index}
    async with httpx.AsyncClient(timeout=20, headers=modrinth_headers()) as client:
        response = await client.get(f"{MODRINTH_BASE}/search", params=params)
        response.raise_for_status()
        data = response.json()
    game = await resolve_minecraft_version(item) if kind in {"mod", "plugin"} else ""
    loader = str(item.get("mod_loader", "")).lower()
    hits = []
    for project in data.get("hits", []):
        games = [str(v) for v in project.get("versions", [])]
        categories = [str(v).lower() for v in project.get("categories", [])]
        compatible = mod_compatibility(game, loader, games, categories) if kind == "mod" else (game in games if kind == "plugin" and game else None)
        hits.append({"project_id": project["project_id"], "slug": project["slug"], "title": project["title"],
            "description": project["description"], "icon": project.get("icon_url", ""),
            "downloads": project.get("downloads", 0), "platform": "modrinth", "versions": games[:12],
            "loaders": categories, "compatible": compatible})
    return {"hits": hits, "total_hits": data.get("total_hits", 0)}


async def curseforge_search(kind: str, query: str, item: dict[str, Any], offset: int, sort: str) -> dict[str, Any]:
    key = settings().get("curseforge_api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Add a CurseForge API key in the manager's Settings to browse CurseForge.")
    sort_field = 6 if sort == "downloads" else 3
    params = {"gameId": MC_GAME_ID, "classId": CF_CLASS_IDS[kind], "searchFilter": query, "index": offset,
        "pageSize": 24, "sortField": sort_field, "sortOrder": "desc"}
    async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
        response = await client.get(f"{CURSEFORGE_BASE}/mods/search", params=params)
        response.raise_for_status()
        data = response.json()
    game = await resolve_minecraft_version(item) if kind in {"mod", "plugin"} else ""
    loader_id = CF_LOADER_IDS.get(str(item.get("mod_loader", "")).upper())
    hits = []
    for project in data.get("data", []):
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
                if game in {str(value) for value in file_games} and (loader_id is None or file_loader_id == loader_id):
                    compatible = True
                    break
        elif kind == "plugin" and game:
            compatible = any(
                game in [str(value) for value in (file.get("gameVersion") if isinstance(file.get("gameVersion"), list) else [file.get("gameVersion")])]
                for file in indexes if isinstance(file, dict) and file.get("gameVersion")
            )
        hits.append({"project_id": str(project["id"]), "slug": project.get("slug", str(project["id"])),
            "title": project.get("name", "CurseForge project"), "description": project.get("summary", ""),
            "icon": (project.get("logo") or {}).get("url", ""), "downloads": project.get("downloadCount", 0),
            "platform": "curseforge", "url": (project.get("links") or {}).get("websiteUrl", ""), "compatible": compatible})
    return {"hits": hits, "total_hits": data.get("pagination", {}).get("totalCount", 0)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global DOCKER_ERROR, HOST_DATA_SOURCE, SHUTTING_DOWN, MAINTENANCE_TASK
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SERVERS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import_legacy_server()
    except Exception:
        LOG.exception("Could not import legacy server data; original files were left untouched.")
    try:
        cli = docker_client()
        cli.ping()
        HOST_DATA_SOURCE = docker_self_data_source(cli)
        DOCKER_ERROR = None
        for item in all_configs():
            try:
                await ensure_gateway(item["id"])
            except OSError:
                LOG.exception("Could not bind Minecraft gateway for %s", item.get("name"))
            if item.get("desired_state") == "running":
                submit_job(item["id"], "resume", lambda sid=item["id"]: ensure_running(sid))
    except Exception as err:
        DOCKER_ERROR = str(err)
        LOG.error("Docker Engine is unavailable: %s", err)
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
        await close_gateway(server_id)
    POOL.shutdown(wait=True, cancel_futures=True)
    try:
        ids = [x["id"] for x in all_configs()]
        with ThreadPoolExecutor(max_workers=max(1, min(len(ids), 16))) as pool:
            futures = [pool.submit(stop_container, sid, False, 120) for sid in ids]
            for future in futures:
                try:
                    future.result(timeout=250)
                except Exception:
                    LOG.exception("A Minecraft server did not stop cleanly")
    except Exception:
        LOG.exception("Failed while stopping managed Minecraft servers")


app = FastAPI(title="Minecraft Server Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def ingress_only(request: Request, call_next):
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
    try:
        docker_client().ping()
        api_ready = True
        error = None
    except Exception as err:
        error = str(err)
    return {
        "eula_accepted": bool(settings().get("eula_accepted")),
        "curseforge_key_set": bool(settings().get("curseforge_api_key")),
        "docker_ready": api_ready,
        "docker_error": error,
        "servers": items,
        "first_run": not bool(settings().get("eula_accepted")) or len(items) == 0,
    }


@app.post("/api/setup")
async def setup(payload: dict[str, Any]):
    if payload.get("eula_accepted") is not True:
        raise HTTPException(status_code=422, detail="Accept the Minecraft EULA before creating a server.")
    current = settings()
    current["eula_accepted"] = True
    save_settings(current)
    return {"ok": True}


@app.get("/api/settings")
async def get_settings():
    value = settings()
    return {"curseforge_key_set": bool(value.get("curseforge_api_key"))}


@app.put("/api/settings")
async def put_settings(payload: dict[str, Any]):
    current = settings()
    api_key = str(payload.get("curseforge_api_key", "")).strip()
    current["curseforge_api_key"] = api_key
    save_settings(current)
    return {"curseforge_key_set": bool(api_key)}


@app.get("/api/servers")
async def servers():
    return [public_server(item) for item in all_configs()]


@app.post("/api/servers")
async def create_server(payload: dict[str, Any]):
    if not settings().get("eula_accepted"):
        raise HTTPException(status_code=409, detail="Complete the first-run EULA step before creating a server.")
    clean = validate_config(payload)
    kind = str(payload.get("server_type", "VANILLA")).upper()
    if kind not in SERVER_TYPES:
        raise HTTPException(status_code=422, detail="Choose a supported server type.")
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
            modpack = {"platform": "modrinth", "project_id": project_id, "slug": slug, "title": str(pack_payload.get("title", "Modrinth pack"))[:120], "version_id": version_id}
        else:
            if not project_id.isdigit():
                raise HTTPException(status_code=422, detail="CurseForge project ID must be numeric.")
            file_id = str(pack_payload.get("file_id", ""))
            if file_id and not file_id.isdigit():
                raise HTTPException(status_code=422, detail="CurseForge file ID must be numeric.")
            modpack = {"platform": "curseforge", "project_id": project_id, "slug": slug, "title": str(pack_payload.get("title", "CurseForge pack"))[:120], "file_id": file_id}
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
        "server_type": kind, "mod_loader": mod_loader,
        "modpack": modpack, "mods": [], "plugins": [], "desired_state": "running", "job_state": "queued",
        "last_player_seen": time.time(), "empty_since": None, "updates_available": [],
        "last_update_check": 0, "updating": False, "auto_paused": False, "auto_stopped": False,
    }
    server_dir(server_id).mkdir(parents=True, exist_ok=True)
    write_json(config_path(server_id), item)
    try:
        await ensure_gateway(server_id)
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
    if item.get("updating"):
        raise HTTPException(status_code=409, detail="Server settings cannot be changed while the server is updating.")
    original = dict(item)
    merged = {**item, **payload}
    clean = validate_config(merged, existing_id=server_id)
    was_running = item.get("desired_state") == "running"
    restart_required = any(item.get(key) != clean.get(key) for key in ("name", "min_ram_mb", "max_ram_mb", "java_version", "minecraft_version"))
    item.update(clean)
    write_json(config_path(server_id), item)
    try:
        await ensure_gateway(server_id)
    except OSError as err:
        write_json(config_path(server_id), original)
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


@app.post("/api/servers/{server_id}/start")
async def start_server(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("updating"):
        raise HTTPException(status_code=409, detail="The server is updating.")
    original = dict(item)
    item["desired_state"] = "running"
    item["auto_stopped"] = False
    item["auto_paused"] = False
    write_json(config_path(server_id), item)
    try:
        await ensure_gateway(server_id)
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
    if item.get("updating"):
        raise HTTPException(status_code=409, detail="The server is updating.")
    item["desired_state"] = "stopped"
    item["auto_stopped"] = False
    item["auto_paused"] = False
    item["job_state"] = "stopping"
    write_json(config_path(server_id), item)
    job_id = submit_job(server_id, "stop", lambda: stop_container(server_id, False, 120))
    return {"job_id": job_id}


@app.post("/api/servers/{server_id}/update")
async def update_server_now(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("updating"):
        raise HTTPException(status_code=409, detail="This server is already updating.")
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
    if item.get("updating"):
        raise HTTPException(status_code=409, detail="The server is updating.")
    kind = str(payload.get("kind", "")).upper()
    provider = str(payload.get("provider", "")).lower()
    requested_version = str(payload.get("minecraft_version", item.get("minecraft_version", "LATEST"))).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", requested_version):
        raise HTTPException(status_code=422, detail="Choose a valid Minecraft release or snapshot version.")
    if kind in {"VANILLA", "BUKKIT", "PAPER", "PURPUR", *MOD_LOADERS}:
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
                    "title": str(project.get("title", "Modrinth pack"))[:120], "version_id": version_id}})
        else:
            if not project_id.isdigit():
                raise HTTPException(status_code=422, detail="CurseForge project ID must be numeric.")
            item.update({"server_type": "AUTO_CURSEFORGE", "mod_loader": mod_loader, "mods": [],
                "minecraft_version": requested_version,
                "modpack": {"platform": "curseforge", "project_id": project_id, "slug": slug,
                    "title": str(project.get("title", "CurseForge pack"))[:120], "file_id": file_id}})
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


@app.post("/api/servers/{server_id}/mods")
async def add_mod(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("mod_loader") not in MOD_LOADERS:
        raise HTTPException(status_code=409, detail="Choose a compatible mod loader or modpack in the Types tab before adding mods.")
    if item.get("updating"):
        raise HTTPException(status_code=409, detail="The server is updating.")
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
        await validate_mod_version(platform, project_id, version_id, file_id, game_version, str(item.get("mod_loader", "")).lower())
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
    if item.get("updating"):
        raise HTTPException(status_code=409, detail="The server is updating.")
    item["mods"] = [m for m in item.get("mods", []) if not (m.get("platform") == platform.lower() and m.get("project_id") == project_id)]
    was_running = item.get("desired_state") == "running"
    write_json(config_path(server_id), item)
    job_id = None
    if was_running:
        def apply_mods():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "remove mod", apply_mods)
    else:
        stop_container(server_id, remove=True)
    return {"server": public_server(item), "job_id": job_id}


@app.post("/api/servers/{server_id}/plugins")
async def add_plugin(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if str(item.get("server_type", "")).upper() not in PLUGIN_SERVER_TYPES:
        raise HTTPException(status_code=409, detail="Choose Bukkit, Paper, or Purpur in the Types tab before adding plugins.")
    if item.get("updating"):
        raise HTTPException(status_code=409, detail="The server is updating.")
    platform = str(payload.get("platform", "")).lower()
    project = payload.get("project") or {}
    project_id = str(project.get("project_id", ""))
    if platform not in {"modrinth", "curseforge"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project_id):
        raise HTTPException(status_code=422, detail="Plugin details are missing.")
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
        await validate_plugin_version(platform, project_id, version_id, file_id, game_version)
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"{platform.title()} returned HTTP {err.response.status_code} while checking plugin compatibility.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not verify plugin compatibility: {err}")
    plugins = list(item.get("plugins", []))
    if any(plugin.get("platform") == platform and plugin.get("project_id") == project_id for plugin in plugins):
        return {"server": public_server(item), "job_id": None, "already_installed": True}
    plugins.append({"platform": platform, "project_id": project_id, "version_id": version_id, "file_id": file_id,
        "title": str(project.get("title", "Plugin"))[:120], "slug": str(project.get("slug", ""))[:120]})
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
    if item.get("updating"):
        raise HTTPException(status_code=409, detail="The server is updating.")
    item["plugins"] = [plugin for plugin in item.get("plugins", []) if not (plugin.get("platform") == platform.lower() and plugin.get("project_id") == project_id)]
    was_running = item.get("desired_state") == "running"
    write_json(config_path(server_id), item)
    job_id = None
    if was_running:
        def apply_plugins():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "remove plugin", apply_plugins)
    else:
        stop_container(server_id, remove=True)
    return {"server": public_server(item), "job_id": job_id}


@app.delete("/api/servers/{server_id}")
async def delete_server(server_id: str):
    path = server_dir(server_id)
    item = read_json(path / "server.json")
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("updating"):
        raise HTTPException(status_code=409, detail="The server is updating.")
    item["deleting"] = True
    write_json(path / "server.json", item)
    await close_gateway(server_id)

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
    if not read_json(config_path(server_id)):
        raise HTTPException(status_code=404, detail="Server not found.")
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
    if kind not in {"mod", "modpack", "plugin"} or platform not in {"modrinth", "curseforge"}:
        raise HTTPException(status_code=422, detail="Choose Modrinth or CurseForge and a supported category.")
    if len(q) > 100 or offset < 0:
        raise HTTPException(status_code=422, detail="Search text or page offset is invalid.")
    if sort not in {"downloads", "updated"}:
        raise HTTPException(status_code=422, detail="Sort must be downloads or updated.")
    item = read_json(config_path(server_id)) if server_id else {"minecraft_version": "LATEST", "mod_loader": ""}
    if server_id and not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if kind == "mod" and item.get("mod_loader") not in MOD_LOADERS:
        raise HTTPException(status_code=409, detail="Select a mod loader such as Fabric or Forge in Types before browsing mods.")
    if kind == "plugin" and str(item.get("server_type", "")).upper() not in PLUGIN_SERVER_TYPES:
        raise HTTPException(status_code=409, detail="Select Bukkit, Paper, or Purpur in Types before browsing plugins.")
    try:
        return await (modrinth_search(kind, q, item, offset, sort) if platform == "modrinth" else curseforge_search(kind, q, item, offset, sort))
    except HTTPException:
        raise
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"{platform.title()} returned HTTP {err.response.status_code}. Check API access and try again.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not reach {platform.title()}: {err}")


@app.get("/api/catalog/{platform}/{project_id}")
async def catalog_detail(platform: str, project_id: str, kind: str = "modpack", server_id: str = "", offset: int = 0):
    if platform not in {"modrinth", "curseforge"} or kind not in {"mod", "modpack", "plugin"}:
        raise HTTPException(status_code=422, detail="Unsupported catalog item.")
    if offset < 0:
        raise HTTPException(status_code=422, detail="Version page offset cannot be negative.")
    item = read_json(config_path(server_id)) if server_id else {"minecraft_version": "LATEST", "mod_loader": ""}
    if server_id and not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project_id):
        raise HTTPException(status_code=422, detail="Catalog project ID is invalid.")
    if platform == "curseforge" and not project_id.isdigit():
        raise HTTPException(status_code=422, detail="CurseForge project ID must be numeric.")
    if kind == "plugin" and str(item.get("server_type", "")).upper() not in PLUGIN_SERVER_TYPES:
        raise HTTPException(status_code=409, detail="Select Bukkit, Paper, or Purpur in Types before browsing plugins.")
    game = await resolve_minecraft_version(item) if kind in {"mod", "plugin"} else ""
    loader = str(item.get("mod_loader", "")).lower()
    try:
        if platform == "modrinth":
            async with httpx.AsyncClient(timeout=20, headers=modrinth_headers()) as client:
                project_response = await client.get(f"{MODRINTH_BASE}/project/{project_id}")
                project_response.raise_for_status()
                project = project_response.json()
                versions_response = await client.get(f"{MODRINTH_BASE}/project/{project_id}/version")
                versions_response.raise_for_status()
                all_versions = versions_response.json()
            total = len(all_versions)
            page = all_versions[offset:offset + 50]
            return {"project_id": project_id, "slug": project.get("slug", ""), "title": project.get("title", ""),
                "description": project.get("body", project.get("description", "")), "icon": project.get("icon_url", ""),
                "platform": platform, "loaders": project.get("loaders", []),
                "versions": [modrinth_version_record(v, kind, game, loader) for v in page],
                "version_offset": offset, "total_versions": total, "has_more": offset + len(page) < total}
        key = settings().get("curseforge_api_key", "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Add a CurseForge API key in the manager's Settings to browse CurseForge.")
        headers = {"x-api-key": key, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
            project_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}")
            project_response.raise_for_status()
            project = project_response.json().get("data", {})
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
            "versions": [curseforge_version_record(v, kind, game, loader) for v in files],
            "version_offset": offset, "total_versions": total, "has_more": offset + len(files) < total,
            "website_url": (project.get("links") or {}).get("websiteUrl", "")}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"{platform.title()} returned HTTP {err.response.status_code}.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not reach {platform.title()}: {err}")


