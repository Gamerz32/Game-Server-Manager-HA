from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
import shutil
import threading
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
CF_CLASS_IDS = {"modpack": 4471, "mod": 6}
MODRINTH_BASE = "https://api.modrinth.com/v2"
CURSEFORGE_BASE = "https://api.curseforge.com/v1"
JAVA_TAGS = {"auto": "latest", "8": "java8", "17": "java17", "21": "java21", "25": "java25"}
SERVER_TYPES = {"VANILLA", "PAPER", "PURPUR", "FABRIC", "FORGE", "NEOFORGE", "QUILT", "MODRINTH", "AUTO_CURSEFORGE"}
MOD_LOADERS = {"FABRIC", "FORGE", "NEOFORGE", "QUILT"}

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
    if port < 1024 or port > 65535:
        raise HTTPException(status_code=422, detail="Choose a TCP port between 1024 and 65535.")
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
    return {
        "name": name,
        "port": port,
        "min_ram_mb": min_ram,
        "max_ram_mb": max_ram,
        "java_version": java,
        "minecraft_version": mc_version,
    }


def env_for_server(item: dict[str, Any]) -> dict[str, str]:
    env = {
        "EULA": "TRUE",
        "TYPE": str(item.get("server_type", "VANILLA")),
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
    modrinth_mods = []
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
            modrinth_mods.append(project)
    curseforge_mods = [
        f"{mod.get('project_id')}:{mod.get('file_id')}"
        for mod in item.get("mods", [])
        if mod.get("platform") == "curseforge" and mod.get("project_id") and mod.get("file_id")
    ]
    if modrinth_mods:
        env["MODRINTH_PROJECTS"] = "\n".join(modrinth_mods)
    if curseforge_mods:
        env["CURSEFORGE_FILES"] = "\n".join(curseforge_mods)
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
        ports={"25565/tcp": int(item["port"])},
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
    if container is not None and status == "exited":
        container.start()
        item = read_json(config_path(server_id))
        item["desired_state"] = "running"
        item["job_state"] = "running"
        write_json(config_path(server_id), item)
        return
    run_container(server_id)


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


async def modrinth_search(kind: str, query: str, item: dict[str, Any], offset: int) -> dict[str, Any]:
    facets: list[list[str]] = [[f"project_type:{kind}"]]
    version = str(item.get("minecraft_version", "")).strip()
    if version and version.upper() != "LATEST":
        facets.append([f"versions:{version}"])
    if kind == "mod":
        loader = str(item.get("mod_loader", "")).lower()
        if loader in {"fabric", "forge", "neoforge", "quilt"}:
            facets.append([f"categories:{loader}"])
    params = {"query": query, "facets": json.dumps(facets), "limit": 24, "offset": offset, "index": "downloads"}
    async with httpx.AsyncClient(timeout=20, headers=modrinth_headers()) as client:
        response = await client.get(f"{MODRINTH_BASE}/search", params=params)
        response.raise_for_status()
        data = response.json()
    return {
        "hits": [{
            "project_id": x["project_id"], "slug": x["slug"], "title": x["title"],
            "description": x["description"], "icon": x.get("icon_url", ""),
            "downloads": x.get("downloads", 0), "platform": "modrinth",
            "versions": x.get("versions", [])[:12],
        } for x in data.get("hits", [])],
        "total_hits": data.get("total_hits", 0),
    }


async def curseforge_search(kind: str, query: str, item: dict[str, Any], offset: int) -> dict[str, Any]:
    key = settings().get("curseforge_api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Add a CurseForge API key in the manager's Settings to browse CurseForge.")
    params: dict[str, Any] = {
        "gameId": MC_GAME_ID, "classId": CF_CLASS_IDS[kind],
        "searchFilter": query, "index": offset, "pageSize": 24,
    }
    version = str(item.get("minecraft_version", "")).strip()
    if version and version.upper() != "LATEST":
        params["gameVersion"] = version
    if kind == "mod":
        loader_map = {"FORGE": 1, "FABRIC": 4, "QUILT": 5, "NEOFORGE": 6}
        loader = loader_map.get(str(item.get("mod_loader", "")).upper())
        if loader:
            params["modLoaderType"] = loader
    async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json"}) as client:
        response = await client.get(f"{CURSEFORGE_BASE}/mods/search", params=params)
        response.raise_for_status()
        data = response.json()
    return {
        "hits": [{
            "project_id": str(x["id"]), "slug": x.get("slug", str(x["id"])),
            "title": x.get("name", "CurseForge project"),
            "description": x.get("summary", ""), "icon": (x.get("logo") or {}).get("url", ""),
            "downloads": x.get("downloadCount", 0), "platform": "curseforge",
            "url": (x.get("links") or {}).get("websiteUrl", ""),
        } for x in data.get("data", [])],
        "total_hits": data.get("pagination", {}).get("totalCount", 0),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global DOCKER_ERROR, HOST_DATA_SOURCE, SHUTTING_DOWN
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
            if item.get("desired_state") == "running":
                submit_job(item["id"], "resume", lambda sid=item["id"]: ensure_running(sid))
    except Exception as err:
        DOCKER_ERROR = str(err)
        LOG.error("Docker Engine is unavailable: %s", err)
    yield
    # Finish active container work before stopping children for an upgrade or backup.
    SHUTTING_DOWN = True
    POOL.shutdown(wait=True, cancel_futures=True)
    # A cold Home Assistant backup or a normal app stop must let worlds save first.
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
    item = {
        **clean, "id": server_id, "created_at": __import__("datetime").datetime.now().isoformat(),
        "server_type": kind, "mod_loader": mod_loader,
        "modpack": modpack, "mods": [], "desired_state": "running", "job_state": "queued",
    }
    server_dir(server_id).mkdir(parents=True, exist_ok=True)
    write_json(config_path(server_id), item)
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
    merged = {**item, **payload}
    clean = validate_config(merged, existing_id=server_id)
    was_running = item.get("desired_state") == "running"
    item.update(clean)
    write_json(config_path(server_id), item)
    if was_running:
        def update_and_start():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "update settings", update_and_start)
    else:
        try:
            stop_container(server_id, remove=True)
        except DockerException:
            pass
        job_id = None
    return {"server": public_server(item), "job_id": job_id}


@app.post("/api/servers/{server_id}/start")
async def start_server(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    item["desired_state"] = "running"
    write_json(config_path(server_id), item)
    job_id = submit_job(server_id, "start", lambda: ensure_running(server_id))
    return {"job_id": job_id}


@app.post("/api/servers/{server_id}/stop")
async def stop_server(server_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    item["desired_state"] = "stopped"
    item["job_state"] = "stopping"
    write_json(config_path(server_id), item)
    job_id = submit_job(server_id, "stop", lambda: stop_container(server_id, False, 120))
    return {"job_id": job_id}


@app.post("/api/servers/{server_id}/type")
async def install_type(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    provider = str(payload.get("provider", "")).lower()
    kind = str(payload.get("kind", "")).upper()
    if kind == "VANILLA":
        item.update({"server_type": "VANILLA", "modpack": None, "mod_loader": "", "mods": []})
    elif kind in MOD_LOADERS | {"PAPER", "PURPUR"}:
        item.update({"server_type": kind, "modpack": None, "mod_loader": kind if kind in MOD_LOADERS else "", "mods": []})
    elif provider == "modrinth":
        project = payload.get("project") or {}
        if not project.get("project_id") or not project.get("slug") or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(project.get("project_id"))):
            raise HTTPException(status_code=422, detail="Modrinth pack details are missing.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(payload.get("version_id", ""))):
            raise HTTPException(status_code=422, detail="Choose a valid Modrinth pack version.")
        mod_loader = str(payload.get("mod_loader", "")).upper()
        if mod_loader not in MOD_LOADERS:
            raise HTTPException(status_code=422, detail="Choose a supported loader from the selected Modrinth pack version.")
        item.update({
            "server_type": "MODRINTH", "mod_loader": mod_loader, "mods": [],
            "modpack": {
                "platform": "modrinth", "project_id": str(project["project_id"]),
                "slug": str(project["slug"]), "title": str(project.get("title", "Modrinth pack")),
                "version_id": str(payload.get("version_id") or ""),
            },
        })
    elif provider == "curseforge":
        project = payload.get("project") or {}
        if not project.get("project_id") or not project.get("slug") or not str(project.get("project_id")).isdigit():
            raise HTTPException(status_code=422, detail="CurseForge pack details are missing.")
        mod_loader = str(payload.get("mod_loader", "")).upper()
        if mod_loader and mod_loader not in MOD_LOADERS:
            raise HTTPException(status_code=422, detail="Choose Fabric, Forge, NeoForge, or Quilt for the selected pack.")
        item.update({
            "server_type": "AUTO_CURSEFORGE", "mod_loader": mod_loader, "mods": [],
            "modpack": {
                "platform": "curseforge", "project_id": str(project["project_id"]),
                "slug": str(project["slug"]), "title": str(project.get("title", "CurseForge pack")),
                "file_id": str(payload.get("file_id") or ""),
            },
        })
    else:
        raise HTTPException(status_code=422, detail="Choose a supported server type or modpack.")
    item["desired_state"] = "running"
    write_json(config_path(server_id), item)
    def change_type():
        stop_container(server_id, remove=True)
        run_container(server_id)
    job_id = submit_job(server_id, "install type", change_type)
    return {"server": public_server(item), "job_id": job_id}


@app.post("/api/servers/{server_id}/mods")
async def add_mod(server_id: str, payload: dict[str, Any]):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if item.get("mod_loader") not in MOD_LOADERS:
        raise HTTPException(status_code=409, detail="Choose Fabric, Forge, NeoForge, or Quilt in the Types tab before adding mods.")
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
        raise HTTPException(status_code=422, detail="CurseForge file ID must be numeric.")
    if platform == "curseforge" and not file_id:
        raise HTTPException(status_code=422, detail="Select a compatible CurseForge file version.")
    mods = list(item.get("mods", []))
    if any(m.get("platform") == platform and m.get("project_id") == project_id for m in mods):
        return {"server": public_server(item), "job_id": None, "already_installed": True}
    mods.append({
        "platform": platform, "project_id": project_id,
        "version_id": version_id, "file_id": file_id,
        "title": str(project.get("title", "Mod")), "slug": str(project.get("slug", "")),
    })
    item["mods"] = mods
    was_running = item.get("desired_state") == "running"
    write_json(config_path(server_id), item)
    job_id = None
    if was_running:
        def apply_mod():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "install mod", apply_mod)
    return {"server": public_server(item), "job_id": job_id}


@app.delete("/api/servers/{server_id}/mods/{platform}/{project_id}")
async def remove_mod(server_id: str, platform: str, project_id: str):
    item = read_json(config_path(server_id))
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    item["mods"] = [m for m in item.get("mods", []) if not (m.get("platform") == platform.lower() and m.get("project_id") == project_id)]
    was_running = item.get("desired_state") == "running"
    write_json(config_path(server_id), item)
    job_id = None
    if was_running:
        def apply_mods():
            stop_container(server_id, remove=True)
            run_container(server_id)
        job_id = submit_job(server_id, "remove mod", apply_mods)
    return {"server": public_server(item), "job_id": job_id}


@app.delete("/api/servers/{server_id}")
async def delete_server(server_id: str):
    path = server_dir(server_id)
    item = read_json(path / "server.json")
    if not item:
        raise HTTPException(status_code=404, detail="Server not found.")

    def remove_server():
        stop_container(server_id, remove=True, timeout=120)
        shutil.rmtree(path)

    job_id = submit_job(server_id, "remove server", remove_server)
    return {"job_id": job_id, "deleted": server_id}


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


@app.get("/api/catalog")
async def catalog(platform: str, kind: str, server_id: str = "", q: str = "", offset: int = 0):
    if kind not in {"mod", "modpack"} or platform not in {"modrinth", "curseforge"}:
        raise HTTPException(status_code=422, detail="Choose Modrinth or CurseForge and a supported category.")
    if len(q) > 100 or offset < 0:
        raise HTTPException(status_code=422, detail="Search text or page offset is invalid.")
    item = read_json(config_path(server_id)) if server_id else {"minecraft_version": "LATEST", "mod_loader": ""}
    if server_id and not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if kind == "mod" and item.get("mod_loader") not in MOD_LOADERS:
        raise HTTPException(status_code=409, detail="Select a mod loader such as Fabric or Forge in Types before browsing mods.")
    try:
        return await (modrinth_search(kind, q, item, offset) if platform == "modrinth" else curseforge_search(kind, q, item, offset))
    except HTTPException:
        raise
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"{platform.title()} returned HTTP {err.response.status_code}. Check API access and try again.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not reach {platform.title()}: {err}")


@app.get("/api/catalog/{platform}/{project_id}")
async def catalog_detail(platform: str, project_id: str, kind: str = "modpack", server_id: str = ""):
    if platform not in {"modrinth", "curseforge"} or kind not in {"mod", "modpack"}:
        raise HTTPException(status_code=422, detail="Unsupported catalog item.")
    item = read_json(config_path(server_id)) if server_id else {"minecraft_version": "LATEST", "mod_loader": ""}
    if server_id and not item:
        raise HTTPException(status_code=404, detail="Server not found.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project_id):
        raise HTTPException(status_code=422, detail="Catalog project ID is invalid.")
    if platform == "curseforge" and not project_id.isdigit():
        raise HTTPException(status_code=422, detail="CurseForge project ID must be numeric.")
    try:
        if platform == "modrinth":
            async with httpx.AsyncClient(timeout=20, headers=modrinth_headers()) as client:
                response = await client.get(f"{MODRINTH_BASE}/project/{project_id}")
                response.raise_for_status()
                project = response.json()
                versions_response = await client.get(f"{MODRINTH_BASE}/project/{project_id}/version")
                versions_response.raise_for_status()
                versions = versions_response.json()
            loader = str(item.get("mod_loader", "")).lower()
            game = str(item.get("minecraft_version", "")).strip()
            if game and game.upper() != "LATEST":
                versions = [v for v in versions if game in v.get("game_versions", [])]
            if kind == "mod" and loader:
                versions = [v for v in versions if loader in v.get("loaders", [])]
            return {
                "project_id": project_id, "slug": project.get("slug", ""),
                "title": project.get("title", ""), "description": project.get("body", project.get("description", "")),
                "icon": project.get("icon_url", ""), "platform": platform,
                "loaders": project.get("loaders", []), "versions": [{
                    "id": v["id"], "number": v.get("version_number", ""),
                    "name": v.get("name", ""), "url": f"https://modrinth.com/{'modpack' if kind == 'modpack' else 'mod'}/{project.get('slug')}/version/{v.get('version_number', v['id'])}",
                    "game_versions": v.get("game_versions", []), "loaders": v.get("loaders", []),
                } for v in versions[:20]],
            }
        key = settings().get("curseforge_api_key", "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Add a CurseForge API key in the manager's Settings to browse CurseForge.")
        headers = {"x-api-key": key, "Accept": "application/json"}
        file_params = {"pageSize": 50, "index": 0}
        game = str(item.get("minecraft_version", "")).strip()
        if game and game.upper() != "LATEST":
            file_params["gameVersion"] = game
        if kind == "mod":
            loader_map = {"FORGE": 1, "FABRIC": 4, "QUILT": 5, "NEOFORGE": 6}
            loader = loader_map.get(str(item.get("mod_loader", "")).upper())
            if loader:
                file_params["modLoaderType"] = loader
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
            project_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}")
            project_response.raise_for_status()
            project = project_response.json().get("data", {})
            desc_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/description")
            desc_response.raise_for_status()
            description = clean_description(desc_response.json().get("data", ""))
            files_response = await client.get(f"{CURSEFORGE_BASE}/mods/{project_id}/files", params=file_params)
            files_response.raise_for_status()
            files = files_response.json().get("data", [])
        return {
            "project_id": project_id, "slug": project.get("slug", str(project_id)),
            "title": project.get("name", ""), "description": description or project.get("summary", ""),
            "icon": (project.get("logo") or {}).get("url", ""), "platform": platform,
            "versions": [{
                "id": str(f["id"]), "number": f.get("displayName", ""),
                "name": f.get("fileName", ""), "url": "",
                "game_versions": f.get("gameVersions", []),
                "loader_type": f.get("modLoaderType"),
            } for f in files[:20]],
            "website_url": (project.get("links") or {}).get("websiteUrl", ""),
        }
    except HTTPException:
        raise
    except httpx.HTTPStatusError as err:
        raise HTTPException(status_code=502, detail=f"{platform.title()} returned HTTP {err.response.status_code}.")
    except httpx.HTTPError as err:
        raise HTTPException(status_code=502, detail=f"Could not reach {platform.title()}: {err}")
