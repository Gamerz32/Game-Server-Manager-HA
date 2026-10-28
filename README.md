# Game Server Manager for Home Assistant

This fork contains one Home Assistant add-on: **Game Server Manager**. It currently provides an ingress dashboard for creating and managing multiple Minecraft Java servers, each running in its own [itzg/minecraft-server](https://docker-minecraft-server.readthedocs.io/en/latest/) container, with room to add other games later.

## Install

Add this repository to **Settings → Apps → App Store → Repositories**:

https://github.com/Gamerz32/Home-Assistant-Gameservers-ADDONS

Then install **Game Server Manager** from the app store.

## Home Assistant slug transition

The app slug is now `game_server_manager`, which gives Home Assistant a new app ID and hostname. The new app uses a separate data location; existing server files are not moved automatically. Stop the old app and make a full Home Assistant backup before switching. Keep the old app data or backup until the servers and worlds have been migrated and verified under the new app.

## Dashboard features

- Create multiple independent servers and manage their status and logs.
- Configure RAM, Java runtime, Minecraft version, and ports per server.
- Choose Vanilla or supported server software and install Modrinth or CurseForge modpacks.
- Search and install compatible Modrinth or CurseForge mods for supported mod loaders.
- Keep Types, Mods, and Plugins tabs visible, with incompatible catalogs disabled; browse and install Modrinth or CurseForge plugins on Bukkit, Paper, and Purpur servers.
- The “Bukkit” choice runs the maintained Paper engine, which supports Bukkit plugins; upstream [itzg documentation](https://docker-minecraft-server.readthedocs.io/en/latest/types-and-platforms/server-types/bukkit-spigot/) notes automated Bukkit downloads are no longer supported.
- Use Home Assistant ingress to open the dashboard in the sidebar.

See the [Minecraft add-on guide](https://github.com/Gamerz32/Home-Assistant-Gameservers-ADDONS/tree/main/minecraft_vanilla_server) for setup, backups, migration, and troubleshooting.

## Docker access and security

The manager needs Docker API access to create and manage the separate game-server containers. This requires turning off Protection mode for the add-on. Docker API access can control containers on the Home Assistant host, so enable it only if you accept that access. See the [Home Assistant security guide](https://developers.home-assistant.io/docs/apps/guides/security/).

## License and trademarks

See [LICENSE](LICENSE). This community project is not affiliated with Minecraft, Mojang, Microsoft, Modrinth, CurseForge, or Home Assistant. Their names and marks belong to their respective owners.
