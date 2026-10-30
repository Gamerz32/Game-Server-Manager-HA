# Game Server Manager for Home Assistant

Game Server Manager adds a Home Assistant dashboard for creating and managing game servers. Minecraft Java, Bedrock, and cross-play are available now; Worms Armageddon appears as a work-in-progress option and cannot be created yet.

Java servers run in their own [itzg/minecraft-server](https://docker-minecraft-server.readthedocs.io/en/latest/) container. Bedrock servers run in [itzg/minecraft-bedrock-server](https://github.com/itzg/docker-minecraft-bedrock-server). Each server has persistent world data.

## Features

- Create and manage independent Minecraft Java and Bedrock servers from the Home Assistant dashboard.
- Create a Java cross-play server with Geyser and Floodgate so Bedrock players can join the Java world.
- Start, stop, inspect logs, and send commands through each server's interactive console.
- Configure memory, Java or Bedrock versions, and network ports per server.
- Set world name, seed, game mode, difficulty, player limit, and player access controls before creating a server.
- Manage operators, optional allow-lists, Java-only blacklists, and edition-specific player permission levels in server settings. Vanilla Bedrock servers use their allow-list because they do not provide a native ban list.
- Reset a server world from Settings, or upload a Java server-list icon.
- Browse Bedrock add-ons on CurseForge or upload local `.mcaddon` and `.mcpack` files for a Bedrock Dedicated Server.
- Choose Vanilla, supported server software, or a Modrinth or CurseForge modpack and version.
- Use the Types, Mods, Plugins, and Add-ons tabs to browse compatible content; Hangar is the default plugin catalog for Paper, with Modrinth and CurseForge available as alternatives.
- Use **Force exit** to cancel queued operations, disable Docker restart retries, and immediately remove the container if a server is stuck booting or restarting. It preserves the files in `/share`; choose Start to create a new container when you are ready.
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
6. Open the app's web interface, accept the Minecraft EULA for the edition you choose, and create your first server.

For detailed setup, networking, backup, migration, and troubleshooting instructions, see the [Game Server Manager guide](https://github.com/Gamerz32/Game-Server-Manager-HA/blob/main/game_server_manager/README.md).

## Docker access and networking

The manager uses the Docker API to create and manage separate game-server containers. Docker API access can control containers on the Home Assistant host, so enabling it carries elevated privileges. Review the [Home Assistant app security guidance](https://developers.home-assistant.io/docs/apps/guides/security/) before enabling it.

The manager app itself uses bridge networking. The **Minecraft gateway host networking** option applies only to the small gateway containers that publish Java server ports. It is off by default. Bedrock Dedicated Servers publish a TCP signaling port and a configured range of 16 UDP gameplay ports directly from their container. The dashboard defaults the advertised address to `<public_ip>`; set a reachable local IP for LAN-only access. A Java cross-play server uses its assigned Java TCP port plus a separate Bedrock port that must be forwarded for both TCP and UDP. Reserve TCP port **8099** for the manager interface. Players connecting from outside your home network may also need router port forwarding.

## Data and migration

Server files are stored in Home Assistant's shared folder at `/share/game_server_manager/servers/<server-id>/data/`. In File Editor, open `/share/game_server_manager/`. Existing files are moved from the app's private `/data/` storage the first time the updated app starts. Home Assistant's own `/data/options.json` remains in its platform-managed location. Since the server files are now in the Share folder, include Share in backups; Home Assistant's recommended automatic backup setup excludes it by default. Make a backup before removing the app or changing its data.

The Home Assistant app slug is `game_server_manager`. This is a new app identity compared with the earlier Vanilla server app, so Home Assistant uses a new hostname and the old app's data is not automatically moved. The previous Vanilla server's files can be imported from `/share/minecraft-vanilla`; the import keeps the original share copy intact. Keep a backup until the new server and world have been verified.

## Editions and cross-play

Java servers support Vanilla, plugins, mod loaders, and modpacks. Bedrock servers use the upstream Bedrock Dedicated Server image and can be configured to use a stable or preview release. Cross-play creates a Paper-compatible Java server with Geyser and Floodgate installed and sets `enforce-secure-profile=false` for Bedrock-to-Java chat compatibility. The Java port is TCP; the Bedrock cross-play port needs both TCP and UDP. Bedrock and cross-play players connect to the same Java world, with compatibility limits described by [GeyserMC](https://geysermc.org/wiki/geyser/).

Java server automation does not apply to Bedrock Dedicated Servers. Bedrock add-on browsing uses the CurseForge API key saved in catalog settings, and local `.mcaddon` or `.mcpack` files can be uploaded directly. Bedrock cross-play servers disable auto-pause, auto-stop, and wake-on-join because Bedrock traffic uses a direct port. The manager app and its Home Assistant setup remain the same.

## AI-assisted development

This project uses AI tools for assisting with planning, interface design, implementation, documentation, and troubleshooting on this project. The project maintainer is responsible for reviewing and maintaining the work. AI-assisted changes can contain errors, so report issues and verify behavior in your own Home Assistant setup before relying on it.

## References

- [itzg Minecraft server documentation](https://docker-minecraft-server.readthedocs.io/en/latest/)
- [itzg Bedrock server image documentation](https://github.com/itzg/docker-minecraft-bedrock-server)
- [GeyserMC setup and port requirements](https://geysermc.org/wiki/geyser/setup/)
- [Floodgate setup](https://geysermc.org/wiki/floodgate/setup/)
- [Home Assistant app configuration](https://developers.home-assistant.io/docs/apps/configuration/)
- [License](LICENSE)

This community project is not affiliated with Minecraft, Mojang, Microsoft, Modrinth, CurseForge, or Home Assistant. Their names and marks belong to their respective owners.
