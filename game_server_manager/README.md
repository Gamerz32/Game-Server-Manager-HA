# Game Server Manager

Manage dedicated game servers from a Home Assistant ingress dashboard. Minecraft Java, Bedrock, and Java/Bedrock cross-play are available now. Worms Armageddon appears in the game picker as WIP and is unavailable. Minecraft Java servers use <code>itzg/minecraft-server</code>; Bedrock Dedicated Servers use <code>itzg/minecraft-bedrock-server</code>. Each Minecraft server has its own persistent world directory.

## Dashboard

- Create independent Minecraft Java and Bedrock server cards as the host can support.
- Enable Bedrock cross-play on a Java server to install Geyser and Floodgate automatically.
- Each card shows its running or stopped state and a three-dot menu for Start/Stop, Terminal, and Settings.
- Use the Types, Mods, Plugins, and Add-ons tabs to select software, browse Modrinth, CurseForge, or Hangar content, and manage compatible mods, plugins, or Bedrock add-ons. Hangar is the default plugin catalog for Paper; Modrinth and CurseForge remain available. Tabs remain visible and are disabled when they do not apply to the selected server type.
- Use **Force exit** when a server is stuck booting or restarting, or when an operation is blocking normal controls. It cancels queued jobs, disables automatic container restarts, and removes the container immediately while preserving the server files in `/share`. Older operations cannot start it again until you choose **Start**.
- Browse Bedrock add-ons from CurseForge or upload a local <code>.mcaddon</code>/<code>.mcpack</code> file. Add-ons are enabled for the current Bedrock world and the server restarts to apply changes.
- Use the built-in console to view live logs and send commands. Java uses private RCON; Bedrock uses the upstream image's <code>send-command</code> helper.
- Set world name, seed, game mode, difficulty, player limit, and access controls before creating the server.
- Edit world settings and player permissions in Settings. Add operators, configure optional allow-lists, and set Java operator levels or Bedrock player permission groups. Java also supports an optional blacklist; vanilla Bedrock Dedicated Server has no native ban list, so use its allow-list instead.
- Reset a world from Settings to regenerate it with the saved world name, seed, and generation settings. This permanently deletes that server's existing world files.
- Upload a Java server-list image in Settings. The dashboard scales it to Minecraft's 64×64 PNG format.

## Automatic resource and update controls

Java servers can enable these policies in Settings. They are off by default and can be enabled per server. Bedrock Dedicated Servers do not use the Java RCON-based automation features. Bedrock cross-play servers do not offer auto-pause, auto-stop, or wake-on-join because the Geyser port is published directly.

- **Auto-pause** checks player activity through RCON and gracefully shuts down an empty server after the chosen number of minutes, saving the world and freeing its memory and CPU. The gateway starts it again on a login attempt; the player may need to reconnect once it is ready.
- **Auto-stop** removes the stopped child container after the chosen number of empty days while keeping its world files. The gateway recreates the container when a player tries to connect.
- **Auto-update** checks selected Minecraft versions, Modrinth and CurseForge pack/mod versions, and the <code>itzg/minecraft-server</code> image every four hours. If updates are available, it waits until the configured idle period has passed, then restarts and updates the server.
- **Update now** runs an update immediately. The public port stays open during the update and new logins receive the Minecraft disconnect reason <code>The Server is Updating, Please Wait</code>.

Java player counts and the Java console use RCON, which the manager enables privately inside each Java container. RCON is not published to the host. Bedrock commands use the container's local <code>send-command</code> helper.

## Networking and Docker access

The manager requires Docker API access to create and manage Minecraft containers. Turn off Protection mode and enable Docker API access in the app settings. The Docker socket can control host containers, so this remains the add-on's highest security risk even with host networking disabled.

The manager itself runs on Docker bridge networking. Java servers use a private bridge and small TCP gateway containers. The gateways preserve pause wake-up and update refusal while publishing each Java server's selected TCP port. Bedrock Dedicated Server containers publish their TCP signaling port and 16 UDP gameplay ports directly.

In the add-on's **Configuration** tab, **Minecraft gateway host networking** controls only Java gateway containers. It is off by default. With it off, Docker bridge networking publishes each assigned Java port for its gateway. With it on, the gateway binds the assigned port in the host network namespace. Home Assistant restarts the add-on when the setting is changed; the restart stops managed servers cleanly and recreates their gateways in the selected mode. The manager's web interface and server containers do not use host networking.

Java automatic update checks and scheduled updates work in either gateway network mode. Each Java server's **Auto-update** setting controls whether its releases and compatible content are checked every four hours and installed after the configured player-free interval. Catalog requests and downloads use the manager's bridge network. The gateway does not receive the Docker socket or server data mounts. Reserve TCP port 8099 for the manager web interface. Router port forwarding may be needed for players outside your home network.

For a Bedrock Dedicated Server, assign a TCP signaling port and a 16-port UDP gameplay range. The default advertised address is <code>&lt;public_ip&gt;</code>, which the upstream image resolves at startup. Set an IPv4 or IPv6 address reachable by the players if you need LAN-only access. Forward the TCP port and the entire UDP range through the host firewall and router. The app's Java gateway host-networking option does not change Bedrock port publishing.

A Java cross-play server uses the Java TCP port through the gateway and a separate Bedrock port exposed over both TCP and UDP. Forward both protocols for the Bedrock port. The port must be different from Java ports in use on the host.

The dashboard itself is available through Home Assistant ingress. Start the app, open its page, choose **Show in sidebar**, and select **Game Servers**.

## Install and first run

1. Add https://github.com/Gamerz32/Game-Server-Manager-HA to **Settings → Apps → App Store → Repositories**.
2. Install **Game Server Manager**.
3. Turn off Protection mode and enable Docker API access for the app, then start it.
4. Open **Game Servers** from the sidebar or the app's **Open Web UI** button.
5. Accept the Minecraft EULA in the first-run dialog. Bedrock servers request edition acceptance when first created.
6. Choose Java Edition, Bedrock Edition, or Java cross-play. Java servers can use Vanilla, server software, or a Modrinth or CurseForge pack. Choose the version, name, ports, Java runtime, and memory values.

The manager requires EULA acceptance before it creates a Java or Bedrock server. Bedrock cross-play installs Geyser and Floodgate on Paper-compatible Java server software. Cross-play translates Bedrock connections into Java connections; it does not make Java-only mods or mechanics available to Bedrock clients.

## Versions and catalogs

Java Vanilla, Paper, Purpur, and mod loaders use the selected Minecraft version. Snapshot selection is disabled for Bukkit, Paper, and Purpur servers. Modrinth packs and mods are filtered for the selected game version and loader. Paper plugin browsing defaults to Hangar and checks Hangar's Paper version compatibility; Modrinth and CurseForge remain available from the same Plugins tab. Hangar versions hosted on Hangar's CDN can be installed directly. Versions that link to an external download show the author's chosen source and require manual installation. CurseForge browsing and file lookups require an API key from the CurseForge developer console; save it in Home Assistant add-on Configuration or use a temporary Catalog Settings override. Bedrock add-on browsing and downloads use that same key, and the catalog offers files packaged as <code>.mcaddon</code> or <code>.mcpack</code>. Bedrock servers accept <code>LATEST</code>, <code>PREVIEW</code>, or a specific Bedrock Dedicated Server version.

A pack, loader, or server type change can make an existing world or installed content incompatible. The dashboard asks for confirmation before applying a type change. Bukkit, Paper, and Purpur plugin management is in the dedicated Plugins tab; Mods is for compatible mod loaders.

## Content catalogs and local imports

The Content tab browses compatible Modrinth and CurseForge resource packs, data packs, and worlds/maps. Paper's Plugins tab defaults to Hangar and offers Modrinth and CurseForge too. The Mods tab includes a local JAR import area. Resource packs, data packs, and maps can also be imported from local ZIP files.

Each catalog location also has a local import area. Drop modpack archives in Types, .jar files in Mods or Plugins, .mcaddon/.mcpack files in Add-ons, and .zip files for worlds, data packs, or resource packs in Content. These files are checked before installation. Installed archives are available to download from the Content library.

CurseForge uses an API key saved in the Home Assistant add-on Configuration. The manager also supports an optional in-memory override in Catalog Settings; that override resets when the add-on restarts. Resource pack archives are saved in /share/game_server_manager/servers/SERVER-ID/content/. For Java clients to download a pack, host its ZIP at a public HTTPS URL and set the URL (and optional SHA-1) under that server's Settings.
## Backups, migration, and removal

Server files are stored in Home Assistant's shared folder at /share/game_server_manager/servers/<server-id>/data/. In File Editor, open /share/game_server_manager/. Existing files are moved from the app's private /data/ storage the first time the updated app starts. Home Assistant's own /data/options.json remains in its platform-managed location. Include the Share folder in backups; Home Assistant's recommended automatic backup setup excludes it by default. Backups are cold: when Home Assistant stops the manager for a backup, it stops managed game-server containers cleanly first.

On upgrade from the previous Vanilla app, the manager imports /share/minecraft-vanilla into a managed server folder and leaves the original files untouched.

Removing a server deletes its container and world directory after confirmation. Docker images may remain cached on the host.

## Troubleshooting

- **Docker unavailable:** enable Docker API access, turn off Protection mode, and restart the app.
- **Gateway cannot bind a port:** choose a free Java TCP port; port 8099 is reserved for the manager interface.
- **Bedrock players cannot connect:** forward the Bedrock TCP signaling port and every assigned UDP gameplay port, and check the advertised IP address.
- **Cross-play players cannot connect:** forward the cross-play port for both TCP and UDP. It is separate from the Java TCP port.
- **Players cannot connect externally:** check the host firewall and forward the chosen TCP port on your router.
- **CurseForge search fails:** add a valid CurseForge API key in catalog settings.
- **Server or pack startup fails:** open the server's Terminal and check the latest logs. A pack may require a different Java runtime or may not support dedicated servers.

## References

- [itzg Minecraft server documentation](https://docker-minecraft-server.readthedocs.io/en/latest/)
- [itzg Bedrock server image documentation](https://github.com/itzg/docker-minecraft-bedrock-server)
- [GeyserMC setup and port requirements](https://geysermc.org/wiki/geyser/setup/)
- [Floodgate setup](https://geysermc.org/wiki/floodgate/setup/)
- [itzg RCON and console commands](https://docker-minecraft-server.readthedocs.io/en/latest/sending-commands/commands/)
- [itzg RCON settings](https://docker-minecraft-server.readthedocs.io/en/latest/configuration/server-properties/)
- [Home Assistant app security](https://developers.home-assistant.io/docs/apps/guides/security/)

## Extending the manager with another game

Server records now carry a separate game_id, and game integrations are registered under manager/games. The shared manager uses a driver contract for game-specific settings validation, container image and environment, data container naming, published ports or network adapter, console commands, player-count parsing, and feature capabilities. The game registry is exposed at /api/games for future UI selection.

Minecraft Java and Bedrock are selectable drivers. Older records are treated as Minecraft Java, and its current Docker container names and gateway labels remain compatible with existing installs. A future game still needs its driver plus game-specific routes and UI; registering metadata alone does not enable an unsupported game.

