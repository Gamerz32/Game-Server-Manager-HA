# Manager for Game Servers Home Assistant

Editable application source for the Game Server Manager Home Assistant app.

The Home Assistant app-store entry remains in [Gamerz32/Game-Server-Manager-HA](https://github.com/Gamerz32/Game-Server-Manager-HA). Its app build downloads this repository's `main` branch. To publish a source change to an installed app, bump the version in `game_server_manager/config.yaml` in the app-store repository, then update the app in Home Assistant.

The user guide and Home Assistant setup instructions are in the [app-store repository](https://github.com/Gamerz32/Game-Server-Manager-HA/blob/main/game_server_manager/README.md).

## Source layout

- `manager/`: FastAPI application, game server management, and dashboard assets.
- `requirements.txt`: Python runtime dependencies.
- `run.sh`: app process entry point.
- `tests/`: unit tests for server configuration and game features.

## Development checks

```sh
python -m pip install -r requirements.txt
python -m compileall -q manager
python -m unittest discover -s tests -v
node --check manager/static/app.js
docker build -t manager-for-game-servers:dev .
```

The app manages containers through the Home Assistant Docker API. Enabling that access allows it to control containers on the HAOS host; review the app-store guide before enabling it.

## License

MIT. See [LICENSE](LICENSE).
