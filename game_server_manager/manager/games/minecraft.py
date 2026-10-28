from __future__ import annotations

import re
import secrets
from typing import Any

from .base import GameCapabilities


JAVA_TAGS = {"auto": "latest", "8": "java8", "17": "java17", "21": "java21", "25": "java25"}


class MinecraftDriver:
    game_id = "minecraft"
    display_name = "Minecraft"
    default_port = 25565
    requires_eula = True
    eula_url = "https://aka.ms/MinecraftEULA"
    networking_adapter = "minecraft_gateway"
    container_network = "ha-minecraft-manager"
    legacy_manager_label = "io.gamerz32.minecraft-manager"
    server_types = frozenset({"VANILLA", "BUKKIT", "PAPER", "PURPUR", "FABRIC", "FORGE", "NEOFORGE", "QUILT", "MODRINTH", "AUTO_CURSEFORGE"})
    restart_keys = ("java_version", "minecraft_version")
    capabilities = GameCapabilities(
        supports_modpacks=True,
        supports_mods=True,
        supports_plugins=True,
        supports_type_selection=True,
        supports_console=True,
        supports_player_counts=True,
        supports_auto_pause=True,
        supports_auto_stop=True,
        supports_auto_update=True,
        supports_wake_on_join=True,
    )
    java_tags = JAVA_TAGS

    def validate_options(self, data: dict[str, Any]) -> dict[str, Any]:
        java = str(data.get("java_version", "auto"))
        if java not in JAVA_TAGS:
            raise ValueError("Choose Java auto, 8, 17, 21, or 25.")
        version = str(data.get("minecraft_version", "LATEST")).strip() or "LATEST"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", version):
            raise ValueError("Use a Minecraft version such as LATEST, 1.21.4, or SNAPSHOT.")
        if version.lower() == "latest":
            version = "LATEST"
        return {"java_version": java, "minecraft_version": version}

    def container_name(self, server_id: str) -> str:
        # Preserve the existing name so upgrades continue managing existing worlds in place.
        return f"ha-minecraft-{server_id}"

    def image_for(self, item: dict[str, Any]) -> str:
        return f"itzg/minecraft-server:{JAVA_TAGS.get(str(item.get('java_version', 'auto')), 'latest')}"

    @staticmethod
    def _mod_loader(item: dict[str, Any]) -> str:
        value = str(item.get("mod_loader") or item.get("server_type") or "").strip().lower().replace("_", "").replace("-", "")
        return {"fabric": "fabric", "forge": "forge", "neoforge": "neoforge", "quilt": "quilt"}.get(value, "")

    def environment_for(self, item: dict[str, Any], manager_settings: dict[str, Any]) -> dict[str, str]:
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
        loader_prefix = self._mod_loader(item)
        for mod in item.get("mods", []):
            if mod.get("platform") != "modrinth":
                continue
            project = str(mod.get("project_id", ""))
            version_id = str(mod.get("version_id", ""))
            if loader_prefix:
                project = f"{loader_prefix}:{project}"
            if version_id:
                project = f"{project}:{version_id}"
            if project:
                modrinth_projects.append(project)
        plugin_types = {"BUKKIT", "PAPER", "PURPUR"}
        if str(item.get("server_type", "")).upper() in plugin_types:
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
        if str(item.get("server_type", "")).upper() in plugin_types:
            curseforge_files.extend(
                f"{entry.get('project_id')}:{entry.get('file_id')}"
                for entry in item.get("plugins", [])
                if entry.get("platform") == "curseforge" and entry.get("project_id") and entry.get("file_id")
            )
        if modrinth_projects:
            env["MODRINTH_PROJECTS"] = "\n".join(modrinth_projects)
        if curseforge_files:
            env["CURSEFORGE_FILES"] = "\n".join(curseforge_files)
        api_key = str(manager_settings.get("curseforge_api_key", "")).strip()
        if api_key:
            env["CF_API_KEY"] = api_key
        return env

    def container_port_bindings(self, item: dict[str, Any]) -> dict[str, Any] | None:
        # Minecraft public traffic is bound by the packet-aware gateway sidecar.
        return None

    def send_console_command(self, container: Any, command: str) -> str:
        result = container.exec_run(["rcon-cli", command], stdout=True, stderr=True, demux=False)
        output = result.output or b""
        return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)

    def parse_player_count(self, output: str) -> int | None:
        match = re.search(r"There are\s+(\d+)\s+of a max", output, re.IGNORECASE)
        return int(match.group(1)) if match else None


MINECRAFT_DRIVER = MinecraftDriver()
