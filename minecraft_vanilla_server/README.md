# Minecraft Server Manager

Manage as many separate Minecraft Java servers as your Home Assistant hardware can run. The manager provides a Home Assistant ingress dashboard and starts one maintained [itzg/minecraft-server](https://docker-minecraft-server.readthedocs.io/en/latest/) container per server.

## What the dashboard does

- Shows a card for every server with status, port, Minecraft version, and memory.
- Uses the gear at the lower left of each card to edit minimum RAM, maximum RAM, port, Java runtime, Minecraft version, and server name.
- Opens the server’s **Types** and **Mods** tabs from the lower right of its card.
- Offers Vanilla first, plus Paper, Purpur, Fabric, Forge, NeoForge, and Quilt.
- Searches Modrinth and CurseForge for modpacks and compatible standalone mods.
- Opens a rounded detail dialog with catalog artwork, description, versions, and an install button.
- Includes a first-run wizard that asks the user to accept the Minecraft EULA and choose a type for the first server.
- Keeps each server’s world and settings in its own directory inside the app’s backed-up data folder.

This is an ingress panel, so it can be added to the Home Assistant sidebar. Start the app, open its page, choose **Show in sidebar**, and select **Minecraft Servers**.

## Docker access and security

The manager creates, starts, stops, and removes Docker containers. Home Assistant exposes the Docker API only when the app has Docker API access enabled and Protection mode is off. Docker API access grants control of the host’s containers, so Home Assistant assigns this app its lowest security rating. Enable it only if you accept that access. See [Home Assistant app security](https://developers.home-assistant.io/docs/apps/guides/security/).

The manager’s web interface accepts connections through Home Assistant ingress. The Minecraft server containers do not receive the Docker socket. Each server port is published directly on the Home Assistant host.

## Install and first run

1. Add https://github.com/Gamerz32/Home-Assistant-Gameservers-ADDONS to **Settings → Apps → App Store → Repositories**.
2. Install **Minecraft Server Manager**.
3. Turn off Protection mode for this app, then start it.
4. Open **Minecraft Servers** from the sidebar or the app’s **Open Web UI** button.
5. Review and accept the Minecraft EULA in the first-run dialog.
6. Choose Vanilla, server software, or a Modrinth or CurseForge pack, then set the name, port, Java runtime, and RAM.

The EULA is shown at [aka.ms/MinecraftEULA](https://aka.ms/MinecraftEULA). The manager does not create a server before the user accepts it.

## Servers, ports, and memory

Each server runs in a separate itzg/minecraft-server container, with a distinct directory under /data/servers/. Choose a unique TCP port for each instance; port 25565 is the usual default. The selected port must also be unused by Home Assistant or another device using the host. Router port forwarding may be needed for players outside your home network.

There is no artificial server-count limit. CPU, memory, storage, and network capacity on the Home Assistant host are the limits. The sum of all configured server memory can exceed the hardware’s physical RAM, so set these values conservatively.

The **Auto** Java option uses itzg’s **latest** image tag. That tag follows the Java version required by the newest Minecraft release. Java 8, 17, 21, and 25 are available when a modpack or older Minecraft release needs a specific runtime. Check [itzg’s Java tag table](https://docker-minecraft-server.readthedocs.io/en/latest/versions/java/) and the pack’s requirements before pinning a version.

## Modpacks and mods

The Types tab can install Modrinth modpacks with TYPE=MODRINTH and CurseForge packs with TYPE=AUTO_CURSEFORGE. The manager stores the selected project and version so the itzg container installs it on startup. Some packs restrict automated file access or have files that are not compatible with a dedicated server.

The Mods tab is available for Fabric, Forge, NeoForge, and Quilt. It searches the same two catalogs, filters by the server’s Minecraft version and loader where the provider supports it, and pins the selected project version or CurseForge file. Paper and Purpur use plugins, which are not managed by this Mods tab.

Modrinth searches do not need an API key. CurseForge browsing needs a key created in the CurseForge developer console; save it in the dashboard’s catalog settings. The key is stored in the app’s private settings file and passed to a server container if required by the chosen Java image.

Changing a server’s type recreates its container while preserving its data directory. A pack or loader change can make an existing world or installed mods incompatible; the dashboard asks for confirmation before applying the change.

## Backups and migration

Every world is stored below the add-on’s /data directory, so Home Assistant app backups include the managed servers. Backups are configured as cold backups; when Home Assistant stops the manager for a backup, it asks the Minecraft containers to stop cleanly first.

On upgrade from the previous Vanilla add-on, the manager looks for /share/minecraft-vanilla, copies that data into a managed server folder, and keeps the original /share files intact. The imported server starts stopped until the user completes the EULA step. The old folder is not deleted.

Removing a server in the dashboard deletes that server’s container and data directory after confirmation. Downloaded Docker images may remain cached on the host.

## Troubleshooting

- **Docker unavailable:** turn off Protection mode for this app and restart it.
- **Port already assigned:** edit the server’s port in its gear settings and choose a free TCP port.
- **CurseForge search fails:** add or replace the CurseForge API key in catalog settings.
- **Server or pack install fails:** open the server’s logs from its card. The chosen modpack may require a different Java version or may not provide a server-compatible pack.

## References

- [itzg Minecraft server documentation](https://docker-minecraft-server.readthedocs.io/en/latest/)
- [itzg data directory](https://docker-minecraft-server.readthedocs.io/en/latest/data-directory/)
- [itzg Modrinth modpacks](https://docker-minecraft-server.readthedocs.io/en/latest/types-and-platforms/mod-platforms/modrinth-modpacks/)
- [itzg Auto CurseForge](https://docker-minecraft-server.readthedocs.io/en/latest/types-and-platforms/mod-platforms/auto-curseforge/)
- [itzg Modrinth mods](https://docker-minecraft-server.readthedocs.io/en/latest/mods-and-plugins/modrinth/)
- [itzg CurseForge files](https://docker-minecraft-server.readthedocs.io/en/latest/mods-and-plugins/curseforge-files/)
- [Home Assistant ingress](https://developers.home-assistant.io/docs/apps/guides/ingress/)
- [Home Assistant app security](https://developers.home-assistant.io/docs/apps/guides/security/)
