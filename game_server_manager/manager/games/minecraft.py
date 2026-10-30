from __future__ import annotations

import re
import secrets
from typing import Any
from urllib.parse import urlsplit

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
    restart_keys = (
        "java_version", "minecraft_version", "crossplay_enabled", "crossplay_port",
        "world_name", "world_seed", "game_mode", "difficulty", "max_players", "pvp_enabled",
        "op_permission_level", "whitelist_enabled", "whitelist_players", "blacklist_enabled",
        "blacklist_players", "operators",
    )
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
        crossplay_enabled = data.get("crossplay_enabled") is True
        crossplay_port = None
        if crossplay_enabled:
            if str(data.get("server_type", "")).upper() not in {"BUKKIT", "PAPER", "PURPUR"}:
                raise ValueError("Bedrock cross-play requires Bukkit, Paper, or Purpur server software.")
            try:
                crossplay_port = int(data.get("crossplay_port", 19132))
            except (TypeError, ValueError) as err:
                raise ValueError("The Bedrock cross-play port must be a whole number.") from err
            if crossplay_port < 10000 or crossplay_port > 65535:
                raise ValueError("Choose a Bedrock cross-play port between 10000 and 65535.")
            try:
                java_port = int(data.get("port", self.default_port))
            except (TypeError, ValueError):
                java_port = self.default_port
            if java_port == crossplay_port:
                raise ValueError("The Java and Bedrock cross-play ports must be different.")
            if any(data.get(flag) is True for flag in ("auto_pause_enabled", "auto_stop_enabled", "wake_on_join")):
                raise ValueError("Auto-pause, auto-stop, and wake-on-join are unavailable for Java cross-play servers.")
        return {
            "java_version": java,
            "minecraft_version": version,
            "crossplay_enabled": crossplay_enabled,
            "crossplay_port": crossplay_port,
        }

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
            "LEVEL": str(item.get("world_name", "world")),
            "SEED": str(item.get("world_seed", "")),
            "MODE": str(item.get("game_mode", "survival")),
            "DIFFICULTY": str(item.get("difficulty", "easy")),
            "MAX_PLAYERS": str(item.get("max_players", 10)),
            "PVP": str(bool(item.get("pvp_enabled", True))).lower(),
            "OP_PERMISSION_LEVEL": str(item.get("op_permission_level", 4)),
            "OPS": "\n".join(item.get("operators", [])),
            "ENABLE_WHITELIST": str(bool(item.get("whitelist_enabled", False))).lower(),
            "ENFORCE_WHITELIST": str(bool(item.get("whitelist_enabled", False))).lower(),
            "EXISTING_WHITELIST_FILE": "SYNCHRONIZE",
            "EXISTING_OPS_FILE": "SYNCHRONIZE",
            "ENABLE_RCON": "true",
            "RCON_PASSWORD": secrets.token_urlsafe(32),
        }
        if item.get("crossplay_enabled") is True:
            env["ENFORCE_SECURE_PROFILE"] = "false"
        if item.get("whitelist_enabled") and item.get("whitelist_players"):
            env["WHITELIST"] = "\n".join(item["whitelist_players"])
        if item.get("server_icon") is True:
            env["ICON"] = "/data/gsm-server-icon.png"
            env["OVERRIDE_ICON"] = "true"
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
            direct_plugins = []
            if item.get("crossplay_enabled") is True:
                # Download the official Paper/Spigot plugin builds directly from GeyserMC.
                direct_plugins.extend([
                    "https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest/downloads/spigot",
                    "https://download.geysermc.org/v2/projects/floodgate/versions/latest/builds/latest/downloads/spigot",
                ])
            for plugin in item.get("plugins", []):
                if plugin.get("platform") == "hangar":
                    url = str(plugin.get("download_url", "")).strip()
                    try:
                        parsed = urlsplit(url)
                    except ValueError:
                        continue
                    if (parsed.scheme == "https" and parsed.hostname == "hangarcdn.papermc.io"
                            and parsed.username is None and parsed.password is None
                            and parsed.path.lower().endswith(".jar")):
                        direct_plugins.append(url)
                    continue
                if plugin.get("platform") != "modrinth":
                    continue
                project = str(plugin.get("project_id", ""))
                version_id = str(plugin.get("version_id", ""))
                if version_id:
                    project = f"{project}:{version_id}"
                if project:
                    modrinth_projects.append(project)
            if direct_plugins:
                env["PLUGINS"] = "\n".join(dict.fromkeys(direct_plugins))
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
            env["MODRINTH_PROJECTS"] = "\n".join(dict.fromkeys(modrinth_projects))
        if curseforge_files:
            env["CURSEFORGE_FILES"] = "\n".join(curseforge_files)
        api_key = str(manager_settings.get("curseforge_api_key", "")).strip()
        if api_key:
            env["CF_API_KEY"] = api_key
        return env

    def container_port_bindings(self, item: dict[str, Any]) -> dict[str, Any] | None:
        # Java traffic is bound by the packet-aware gateway sidecar. Geyser also
        # listens directly on TCP and UDP, so publish its Bedrock port separately.
        if item.get("crossplay_enabled") is True:
            port = int(item.get("crossplay_port", 19132))
            return {"19132/tcp": port, "19132/udp": port}
        return None

    def send_console_command(self, container: Any, command: str) -> str:
        result = container.exec_run(["rcon-cli", command], stdout=True, stderr=True, demux=False)
        output = result.output or b""
        return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)

    def parse_player_count(self, output: str) -> int | None:
        match = re.search(r"There are\s+(\d+)\s+of a max", output, re.IGNORECASE)
        return int(match.group(1)) if match else None


MINECRAFT_DRIVER = MinecraftDriver()
