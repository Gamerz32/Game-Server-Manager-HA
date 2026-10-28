# Minecraft Server Manager

Manage multiple Minecraft Java servers from a Home Assistant ingress dashboard. Each server has its own <code>itzg/minecraft-server</code> container and persistent world directory.

## Dashboard

- Create as many server cards as the host can support.
- Each card shows a play or pause state control and a three-dot menu for Start/Stop, Terminal, and Settings.
- Open the Types and Mods tabs to select Vanilla, server software, Modrinth modpacks, CurseForge modpacks, and compatible mods.
- Use the built-in console to view live logs and send server commands through private RCON.
- Edit memory, Java version, Minecraft version, port, and automation policies in the dashboard.

## Automatic resource and update controls

Each server can enable these policies in Settings. They are off by default and can be enabled per server.

- **Auto-pause** checks player activity through RCON and gracefully shuts down an empty server after the chosen number of minutes, saving the world and freeing its memory and CPU. The gateway starts it again on a login attempt; the player may need to reconnect once it is ready.
- **Auto-stop** removes the stopped child container after the chosen number of empty days while keeping its world files. The gateway recreates the container when a player tries to connect.
- **Auto-update** checks selected Minecraft versions, Modrinth and CurseForge pack/mod versions, and the <code>itzg/minecraft-server</code> image every four hours. If updates are available, it waits until the configured idle period has passed, then restarts and updates the server.
- **Update now** runs an update immediately. The public port stays open during the update and new logins receive the Minecraft disconnect reason <code>The Server is Updating, Please Wait</code>.

The player count and online console depend on RCON, which the manager enables privately inside each server container. RCON is not published to the host.

## Networking and Docker access

The manager requires Docker API access to create and manage Minecraft containers. Turn off Protection mode and enable Docker API access in the app settings. Docker API access can control host containers, so Home Assistant assigns this app its lowest security rating.

The manager uses <code>host_network: true</code> so its lightweight TCP gateway can own each server's configured public port. Game containers stay on a private Docker bridge and do not publish their own ports. The gateway routes traffic to a running server, wakes a paused server on a login attempt, and returns an update disconnect while a server is updating. Reserve TCP port 8099 for the manager web interface; it cannot be used as a Minecraft port. Router TCP port forwarding may be needed for players outside your home network.

The dashboard itself is available through Home Assistant ingress. Start the app, open its page, choose **Show in sidebar**, and select **Minecraft Servers**.

## Install and first run

1. Add https://github.com/Gamerz32/Home-Assistant-Gameservers-ADDONS to **Settings → Apps → App Store → Repositories**.
2. Install **Minecraft Server Manager**.
3. Turn off Protection mode and enable Docker API access for the app, then start it.
4. Open **Minecraft Servers** from the sidebar or the app's **Open Web UI** button.
5. Accept the Minecraft EULA in the first-run dialog.
6. Choose Vanilla, server software, or a Modrinth or CurseForge pack, then choose a Minecraft version, name, port, Java runtime, and RAM.

The EULA is shown at [aka.ms/MinecraftEULA](https://aka.ms/MinecraftEULA). The manager will not create a server before it is accepted.

## Versions and catalogs

Vanilla, Paper, Purpur, and mod loaders use the selected Minecraft version. Modrinth packs and mods are filtered for the selected game version and loader. CurseForge browsing and file lookups require an API key from the CurseForge developer console; save it in the dashboard's catalog settings.

A pack or loader change can make an existing world or installed mods incompatible. The dashboard asks for confirmation before applying a type change. Paper and Purpur use plugins, which are not managed by the Mods tab.

## Backups, migration, and removal

Worlds are stored under the app's /data/servers/ directory and are included in Home Assistant app backups. Backups are cold: when Home Assistant stops the manager for a backup, it stops managed Minecraft containers cleanly first.

On upgrade from the previous Vanilla app, the manager imports /share/minecraft-vanilla into a managed server folder and leaves the original files untouched.

Removing a server deletes its container and world directory after confirmation. Docker images may remain cached on the host.

## Troubleshooting

- **Docker unavailable:** enable Docker API access, turn off Protection mode, and restart the app.
- **Gateway cannot bind a port:** choose a free TCP port; port 8099 is reserved for the manager interface.
- **Players cannot connect externally:** check the host firewall and forward the chosen TCP port on your router.
- **CurseForge search fails:** add a valid CurseForge API key in catalog settings.
- **Server or pack startup fails:** open the server's Terminal and check the latest logs. A pack may require a different Java runtime or may not support dedicated servers.

## References

- [itzg Minecraft server documentation](https://docker-minecraft-server.readthedocs.io/en/latest/)
- [itzg RCON and console commands](https://docker-minecraft-server.readthedocs.io/en/latest/sending-commands/commands/)
- [itzg RCON settings](https://docker-minecraft-server.readthedocs.io/en/latest/configuration/server-properties/)
- [Home Assistant app security](https://developers.home-assistant.io/docs/apps/guides/security/)
