from __future__ import annotations

import ipaddress
import re
from typing import Any

from .base import GameCapabilities


BEDROCK_UDP_PORT_COUNT = 16
BEDROCK_CONTAINER_PORT = 19132


class MinecraftBedrockDriver:
    game_id = "minecraft_bedrock"
    display_name = "Minecraft Bedrock"
    default_port = 19132
    requires_eula = True
    eula_url = "https://minecraft.net/terms"
    networking_adapter = "docker_ports"
    container_network = None
    legacy_manager_label = None
    server_types = frozenset({"BEDROCK"})
    restart_keys = (
        "port", "bedrock_version", "udp_port_start", "advertised_address", "world_name", "world_seed",
        "game_mode", "difficulty", "max_players", "allow_cheats", "level_type",
        "default_player_permission_level", "whitelist_enabled", "whitelist_players",
        "blacklist_enabled", "blacklist_players", "operators", "members", "visitors",
    )
    capabilities = GameCapabilities(supports_console=True, supports_addons=True)

    def validate_options(self, data: dict[str, Any]) -> dict[str, Any]:
        version = str(data.get("bedrock_version", "LATEST")).strip() or "LATEST"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", version):
            raise ValueError("Use a Bedrock version such as LATEST, PREVIEW, or 1.26.50.02.")
        if version.lower() == "latest":
            version = "LATEST"
        elif version.lower() == "preview":
            version = "PREVIEW"
        elif version.lower() == "existing":
            version = "EXISTING"

        try:
            udp_port_start = int(data.get("udp_port_start", 19140))
        except (TypeError, ValueError) as err:
            raise ValueError("The Bedrock gameplay UDP port start must be a whole number.") from err
        if udp_port_start < 1024 or udp_port_start + BEDROCK_UDP_PORT_COUNT - 1 > 65535:
            raise ValueError("Choose a Bedrock UDP port range that fits between 1024 and 65535.")

        advertised_address = str(data.get("advertised_address", "<public_ip>")).strip() or "<public_ip>"
        if advertised_address != "<public_ip>":
            try:
                ipaddress.ip_address(advertised_address)
            except ValueError as err:
                raise ValueError("Use an IPv4 or IPv6 literal, or leave <public_ip> to advertise the server's public address.") from err

        env = {
            "bedrock_version": version,
            "udp_port_start": udp_port_start,
            "advertised_address": advertised_address,
        }
        return env

    def container_name(self, server_id: str) -> str:
        return f"ha-minecraft-bedrock-{server_id}"

    def image_for(self, item: dict[str, Any]) -> str:
        return "itzg/minecraft-bedrock-server:latest"

    def environment_for(self, item: dict[str, Any], manager_settings: dict[str, Any]) -> dict[str, str]:
        udp_start = int(item.get("udp_port_start", 19140))
        udp_end = udp_start + BEDROCK_UDP_PORT_COUNT - 1
        env = {
            "EULA": "TRUE",
            "VERSION": str(item.get("bedrock_version", "LATEST")),
            "SERVER_NAME": str(item.get("name", "Minecraft Bedrock Server")),
            "SERVER_PORT": str(BEDROCK_CONTAINER_PORT),
            "SERVER_UDP_PORTS": f"{item.get('advertised_address', '<public_ip>')}:{udp_start}-{udp_end}:{udp_start}-{udp_end}",
            "LEVEL_NAME": str(item.get("world_name", "Bedrock level")),
            "LEVEL_SEED": str(item.get("world_seed", "")),
            "LEVEL_TYPE": str(item.get("level_type", "DEFAULT")),
            "GAMEMODE": str(item.get("game_mode", "survival")),
            "FORCE_GAMEMODE": "true",
            "DIFFICULTY": str(item.get("difficulty", "easy")),
            "ALLOW_CHEATS": str(bool(item.get("allow_cheats", False))).lower(),
            "MAX_PLAYERS": str(item.get("max_players", 10)),
            "DEFAULT_PLAYER_PERMISSION_LEVEL": str(item.get("default_player_permission_level", "member")),
            "ALLOW_LIST": str(bool(item.get("whitelist_enabled", False))).lower(),
            "OPS": "\n".join(item.get("operators", [])),
            "MEMBERS": "\n".join(item.get("members", [])),
            "VISITORS": "\n".join(item.get("visitors", [])),
        }
        if item.get("whitelist_enabled") and item.get("whitelist_players"):
            env["ALLOW_LIST_USERS"] = "\n".join(item["whitelist_players"])
        return env

    def container_port_bindings(self, item: dict[str, Any]) -> dict[str, Any]:
        udp_start = int(item.get("udp_port_start", 19140))
        bindings: dict[str, Any] = {f"{BEDROCK_CONTAINER_PORT}/tcp": int(item["port"])}
        bindings.update({f"{port}/udp": port for port in range(udp_start, udp_start + BEDROCK_UDP_PORT_COUNT)})
        return bindings

    def send_console_command(self, container: Any, command: str) -> str:
        # The upstream image provides send-command for its interactive stdin console.
        result = container.exec_run(["send-command", command], stdout=True, stderr=True, demux=False)
        output = result.output or b""
        return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)

    def parse_player_count(self, output: str) -> int | None:
        return None


MINECRAFT_BEDROCK_DRIVER = MinecraftBedrockDriver()
