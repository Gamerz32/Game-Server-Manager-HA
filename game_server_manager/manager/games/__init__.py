from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .base import GameDriver
from .minecraft import MINECRAFT_DRIVER


_DRIVERS: dict[str, GameDriver] = {
    MINECRAFT_DRIVER.game_id: MINECRAFT_DRIVER,
}


def get_game_driver(game_id: str) -> GameDriver:
    key = str(game_id or "").strip().lower() or "minecraft"
    try:
        return _DRIVERS[key]
    except KeyError as err:
        raise KeyError(f"No game driver is registered for '{key}'.") from err


def registered_games() -> list[dict[str, Any]]:
    """Return UI-safe metadata for games with an installed driver."""
    return [
        {
            "id": driver.game_id,
            "name": driver.display_name,
            "available": True,
            "default_port": driver.default_port,
            "server_types": sorted(driver.server_types),
            "capabilities": asdict(driver.capabilities),
        }
        for driver in _DRIVERS.values()
    ]


__all__ = ["get_game_driver", "registered_games"]
