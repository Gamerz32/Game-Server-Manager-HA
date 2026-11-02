from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class GameCapabilities:
    supports_modpacks: bool = False
    supports_mods: bool = False
    supports_plugins: bool = False
    supports_addons: bool = False
    supports_type_selection: bool = False
    supports_console: bool = False
    supports_player_counts: bool = False
    supports_auto_pause: bool = False
    supports_auto_stop: bool = False
    supports_auto_update: bool = False
    supports_wake_on_join: bool = False


class GameDriver(Protocol):
    """Contract implemented by each game integration."""

    game_id: str
    display_name: str
    default_port: int
    requires_eula: bool
    eula_url: str
    networking_adapter: str
    container_network: str | None
    legacy_manager_label: str | None
    server_types: frozenset[str]
    restart_keys: tuple[str, ...]
    capabilities: GameCapabilities

    def validate_options(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def container_name(self, server_id: str) -> str: ...
    def image_for(self, item: dict[str, Any]) -> str: ...
    def environment_for(self, item: dict[str, Any], manager_settings: dict[str, Any]) -> dict[str, str]: ...
    def container_port_bindings(self, item: dict[str, Any]) -> dict[str, Any] | None: ...
    def send_console_command(self, container: Any, command: str) -> str: ...
    def parse_player_count(self, output: str) -> int | None: ...
