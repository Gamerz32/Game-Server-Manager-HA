# Game Server Manager for Home Assistant

Game Server Manager adds a Home Assistant dashboard for creating and managing multiple game servers. **Minecraft Java is the first supported game**; support for other game servers may be added later.

Each Minecraft server runs in its own [itzg/minecraft-server](https://docker-minecraft-server.readthedocs.io/en/latest/) container with persistent world data.

## Features

- Create and manage multiple independent Minecraft Java servers from the Home Assistant dashboard.
- Start, stop, inspect logs, and send commands through each server's interactive console.
- Configure memory, Java and Minecraft versions, and network ports per server.
- Choose Vanilla, supported server software, or a Modrinth or CurseForge modpack and version.
- Browse compatible mods and, where supported by the selected server software, plugins.
- Configure per-server automation for pausing or stopping idle servers, waking them on a connection attempt, and checking for updates.
- Install updates manually, or enable each server's automatic update policy.
- Open the manager through Home Assistant's sidebar using app ingress.

## Install

1. In Home Assistant, open **Settings → Apps → App Store → Repositories**.
2. Add this repository:

   `https://github.com/Gamerz32/Game-Server-Manager-HA`

3. Install **Game Server Manager** from the app store.
4. In the app's configuration, set the gateway networking option as desired. It is off by default; Home Assistant restarts the app when this setting changes.
5. Enable Docker API access and turn off Protection mode for the app, then start it.
6. Open the app's web interface, accept the Minecraft EULA, and create your first server.

For detailed setup, networking, backup, migration, and troubleshooting instructions, see the [Game Server Manager guide](https://github.com/Gamerz32/Game-Server-Manager-HA/blob/main/game_server_manager/README.md).

## Docker access and networking

The manager uses the Docker API to create and manage separate game-server containers. Docker API access can control containers on the Home Assistant host, so enabling it carries elevated privileges. Review the [Home Assistant app security guidance](https://developers.home-assistant.io/docs/apps/guides/security/) before enabling it.

The manager app itself uses bridge networking. The **Minecraft gateway host networking** option applies only to the small gateway containers that publish Minecraft server ports. It is off by default. Automatic update checks can still be enabled per server in bridge mode. Reserve TCP port **8099** for the manager interface, and assign a free TCP port to each Minecraft server. Players connecting from outside your home network may also need a router port-forward.

## Data and migration

Server worlds and files are stored under the app's `/data/servers/` directory and are included in Home Assistant app backups. Make a backup before removing the app or changing its data.

The Home Assistant app slug is `game_server_manager`. This is a new app identity compared with the earlier Vanilla server app, so Home Assistant uses a new hostname and the old app's data is not automatically moved. The previous Vanilla server's files can be imported from `/share/minecraft-vanilla`; the import keeps the original share copy intact. Keep a backup until the new server and world have been verified.

## Current scope

Minecraft Java is the only supported game today. Other games are not installable through the manager yet. This repository contains only the Game Server Manager app.

## References

- [itzg Minecraft server documentation](https://docker-minecraft-server.readthedocs.io/en/latest/)
- [Home Assistant app configuration](https://developers.home-assistant.io/docs/apps/configuration/)
- [License](LICENSE)

This community project is not affiliated with Minecraft, Mojang, Microsoft, Modrinth, CurseForge, or Home Assistant. Their names and marks belong to their respective owners.
