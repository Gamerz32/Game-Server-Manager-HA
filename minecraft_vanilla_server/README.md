# Minecraft Java Server for Home Assistant

A Home Assistant add-on for Java Edition servers. It uses the maintained itzg/minecraft-server runtime and exposes the main server settings through the Home Assistant add-on Configuration tab.

## Features

- Vanilla, Paper, Purpur, Fabric, Forge, NeoForge, and Quilt server software
- Modrinth modpacks by slug, ID, project or version URL, or .mrpack URL
- Automatically managed CurseForge modpacks by project URL or slug
- Custom server JAR URLs
- Java auto, 17, 21, or 25
- Minecraft version, initial and maximum memory, MOTD, player limit, difficulty, game mode, account verification, PvP, and world distances configured in Home Assistant
- Persistent worlds in the add-on's private storage
- Graceful stop and cold Home Assistant backups

The first installation builds its image on the Home Assistant device. This can take a while, especially on a small device. The base runtime supports amd64 and aarch64.

## Install

1. In Home Assistant, open Settings → Apps → App Store (called Add-on Store in older Home Assistant releases).
2. Open the menu and choose Repositories.
3. Add https://github.com/Gamerz32/Home-Assistant-Gameservers-ADDONS.
4. Install Minecraft Java Server.
5. Open its Configuration tab and review the settings.
6. Read the Minecraft EULA at https://aka.ms/MinecraftEULA, enable Accept the Minecraft EULA, save, then start the app.

## Configure a standard server

The defaults run a current vanilla release with 1 GB initial and 4 GB maximum Java memory. The auto option follows the Java runtime bundled with the latest upstream server image; it currently uses Java 25. Auto does not inspect a selected game version or modpack and switch to an older runtime. Select Java 17 or 21 when an older version or pack requires it. Some pre-1.18 Forge packs require Java 8 and are outside this add-on's supported Java choices. The required runtime is set by the Minecraft version, loader, and pack.

The Minecraft container port stays at 25565. To change the host port, open the app's Network settings in Home Assistant. Forward the selected host port on your router only if players outside your home network need to connect.

## Configure a modpack

### Modrinth

- Set Server software or modpack platform to MODRINTH.
- Enter a Modrinth project slug, project ID, project URL, version URL, or hosted .mrpack URL in Modpack.
- To pin a version, use the URL for that specific version.

### CurseForge

- Set Server software or modpack platform to AUTO_CURSEFORGE.
- Enter the modpack URL or slug in Modpack.
- The upstream Java 17+ image includes a CurseForge API key. If CurseForge rate-limits requests, add your own key in CurseForge API key.

Only modpacks with server files or server-install support can run as dedicated servers. The selected Java version and memory must also meet the pack's requirements.

## Existing vanilla add-on users

Version 1.0.0 changes the server runtime and moves data from the old shared folder into the add-on's persistent private storage. On first start, the app copies files from /share/minecraft-vanilla (or the configured Previous add-on data folder) if it finds an old server. It keeps the original folder untouched as a backup. If it finds conflicting files in the destination, it stops and asks you to resolve the conflict instead of overwriting anything.

The EULA checkbox starts disabled. Review it and enable it before starting, including when upgrading an existing server.

## Settings

| Setting | Purpose |
| --- | --- |
| server_type | Server software, modpack platform, or custom JAR mode |
| mc_version | LATEST, a release such as 1.21.1, or a supported snapshot |
| java_version | auto, 17, 21, or 25 |
| modpack | Modrinth project/version reference or CurseForge project reference |
| xms_mb / xmx_mb | Initial and maximum Java memory |
| motd, max_players | Server list message and player limit |
| difficulty, game_mode, online_mode, pvp | Common server properties |
| view_distance, simulation_distance | World rendering and simulation distances |
| server_jar_url | Custom server JAR URL when using CUSTOM |

Use the Home Assistant app log for live server output. World, configuration, and downloaded server files remain in the app's persistent data folder and are included in Home Assistant backups.
