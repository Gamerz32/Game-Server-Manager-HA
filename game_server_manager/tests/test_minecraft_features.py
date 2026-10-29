from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import uuid
import zipfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manager import main
from manager.games.minecraft import MINECRAFT_DRIVER
from manager.games.minecraft_bedrock import MINECRAFT_BEDROCK_DRIVER


def make_pack_archive(pack_uuid: str, module_type: str = "data") -> bytes:
    manifest = {
        "format_version": 2,
        "header": {"name": "Test Pack", "uuid": pack_uuid, "version": [1, 2, 3]},
        "modules": [{"type": module_type, "uuid": str(uuid.uuid4()), "version": [1, 0, 0]}],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("content/example.json", "{}")
    return buffer.getvalue()


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class MinecraftDriverTests(unittest.TestCase):
    def test_crossplay_requires_supported_java_server_and_distinct_ports(self) -> None:
        options = MINECRAFT_DRIVER.validate_options({
            "java_version": "21",
            "minecraft_version": "1.21.4",
            "server_type": "PAPER",
            "port": 25565,
            "crossplay_enabled": True,
            "crossplay_port": 19132,
        })
        self.assertTrue(options["crossplay_enabled"])
        self.assertEqual(options["crossplay_port"], 19132)

        with self.assertRaisesRegex(ValueError, "requires Bukkit, Paper, or Purpur"):
            MINECRAFT_DRIVER.validate_options({"server_type": "VANILLA", "crossplay_enabled": True})
        with self.assertRaisesRegex(ValueError, "must be different"):
            MINECRAFT_DRIVER.validate_options({
                "server_type": "PAPER", "port": 19132,
                "crossplay_enabled": True, "crossplay_port": 19132,
            })

    def test_crossplay_installs_geyser_and_floodgate_and_publishes_bedrock_port(self) -> None:
        item = {"server_type": "PAPER", "crossplay_enabled": True, "crossplay_port": 19133}
        environment = MINECRAFT_DRIVER.environment_for(item, {})
        self.assertEqual(environment["MODRINTH_PROJECTS"], "geyser\nfloodgate")
        self.assertEqual(MINECRAFT_DRIVER.container_port_bindings(item), {
            "19132/tcp": 19133,
            "19132/udp": 19133,
        })


class BedrockDriverTests(unittest.TestCase):
    def test_driver_normalizes_options_and_maps_world_settings(self) -> None:
        options = MINECRAFT_BEDROCK_DRIVER.validate_options({
            "bedrock_version": "latest", "udp_port_start": "19140", "advertised_address": "<public_ip>"
        })
        self.assertEqual(options["bedrock_version"], "LATEST")
        self.assertEqual(options["udp_port_start"], 19140)
        env = MINECRAFT_BEDROCK_DRIVER.environment_for({
            "name": "Bedrock Test", "port": 19132, "world_name": "test-world", "world_seed": "42",
            "game_mode": "creative", "difficulty": "normal", "max_players": 12,
            "default_player_permission_level": "visitor", "whitelist_enabled": True,
            "whitelist_players": ["Alex"], "udp_port_start": 19140,
        }, {})
        self.assertEqual(env["LEVEL_NAME"], "test-world")
        self.assertEqual(env["LEVEL_SEED"], "42")
        self.assertEqual(env["ALLOW_LIST"], "true")
        self.assertEqual(env["ALLOW_LIST_USERS"], "Alex")
        self.assertEqual(env["DEFAULT_PLAYER_PERMISSION_LEVEL"], "visitor")
        self.assertEqual(MINECRAFT_BEDROCK_DRIVER.container_port_bindings({"port": 19132, "udp_port_start": 19140})["19132/tcp"], 19132)

    def test_driver_rejects_invalid_udp_ranges_and_addresses(self) -> None:
        with self.assertRaisesRegex(ValueError, "UDP port range"):
            MINECRAFT_BEDROCK_DRIVER.validate_options({"udp_port_start": 65530})
        with self.assertRaisesRegex(ValueError, "IPv4 or IPv6 literal"):
            MINECRAFT_BEDROCK_DRIVER.validate_options({"advertised_address": "not-an-address"})


class BedrockAddonTests(unittest.TestCase):
    def test_installs_metadata_from_mcpack_archive(self) -> None:
        pack_uuid = str(uuid.uuid4())
        result = main.parse_bedrock_addon_archive("test.mcpack", encoded(make_pack_archive(pack_uuid)))
        self.assertEqual(result["extension"], ".mcpack")
        self.assertEqual(result["packs"][0]["uuid"], pack_uuid)
        self.assertEqual(result["packs"][0]["kind"], "behavior")

    def test_expands_mcpack_inside_mcaddon_bundle(self) -> None:
        pack_uuid = str(uuid.uuid4())
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr("packs/behavior.mcpack", make_pack_archive(pack_uuid))
        result = main.parse_bedrock_addon_archive("bundle.mcaddon", encoded(outer.getvalue()))
        self.assertEqual(result["packs"][0]["uuid"], pack_uuid)
        self.assertTrue(result["packs"][0]["root"].startswith("__gsm_embedded_pack_0000"))

    def test_rejects_archive_path_traversal(self) -> None:
        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../manifest.json", "{}")
        with self.assertRaises(main.HTTPException) as raised:
            main.parse_bedrock_addon_archive("bad.mcpack", encoded(unsafe.getvalue()))
        self.assertEqual(raised.exception.status_code, 422)

    def test_curseforge_download_urls_are_limited_to_https_hosts(self) -> None:
        for url in (
            "https://www.curseforge.com/files/addon.mcaddon",
            "https://edge.forgecdn.net/files/addon.mcpack",
        ):
            main.validate_curseforge_download_url(url)
        for url in (
            "http://www.curseforge.com/files/addon.mcaddon",
            "https://curseforge.com.evil.example/addon.mcpack",
            "https://user:password@curseforge.com/addon.mcaddon",
        ):
            with self.subTest(url=url), self.assertRaises(main.HTTPException):
                main.validate_curseforge_download_url(url)


class WorldAndSnapshotTests(unittest.TestCase):
    def test_bedrock_world_access_options_enforce_edition_rules(self) -> None:
        options = main.validate_world_and_access_options({
            "game_mode": "creative", "world_name": "flat-world", "level_type": "FLAT",
            "default_player_permission_level": "visitor", "whitelist_enabled": True,
            "whitelist_players": ["Alex"],
        }, MINECRAFT_BEDROCK_DRIVER)
        self.assertEqual(options["world_name"], "flat-world")
        self.assertEqual(options["level_type"], "FLAT")
        self.assertEqual(options["whitelist_players"], ["Alex"])
        with self.assertRaisesRegex(main.HTTPException, "no native blacklist"):
            main.validate_world_and_access_options({"blacklist_enabled": True}, MINECRAFT_BEDROCK_DRIVER)

    def test_plugin_servers_reject_snapshot_labels(self) -> None:
        with self.assertRaisesRegex(main.HTTPException, "snapshots are not supported"):
            asyncio.run(main.require_release_for_plugin_server("PAPER", "25w04a"))
        asyncio.run(main.require_release_for_plugin_server("VANILLA", "25w04a"))

    def test_plugin_servers_reject_versions_marked_as_snapshots(self) -> None:
        manifest = {"versions": [{"id": "1.22-test", "type": "snapshot"}]}
        with patch.object(main, "minecraft_manifest", new=AsyncMock(return_value=manifest)):
            with self.assertRaisesRegex(main.HTTPException, "snapshots are not supported"):
                asyncio.run(main.require_release_for_plugin_server("BUKKIT", "1.22-test"))


if __name__ == "__main__":
    unittest.main()
