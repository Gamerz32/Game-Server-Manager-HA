# Manager for Game Servers Home Assistant

Editable application source for the Game Server Manager Home Assistant app.

The Home Assistant app-store entry remains in [Gamerz32/Game-Server-Manager-HA](https://github.com/Gamerz32/Game-Server-Manager-HA). Its app build downloads this repository's `main` branch. To publish an app update after changing the source, bump the version in `game_server_manager/config.yaml` in the app-store repository and update the app in Home Assistant.

## Development

Install the dependencies with `python -m pip install -r requirements.txt`, run the unit tests with `python -m unittest discover -s tests -v`, and check the dashboard JavaScript with `node --check manager/static/app.js`.

The app manages game server containers through the Home Assistant Docker API. Review the app documentation in the app-store repository before enabling it on a Home Assistant host.

## License

MIT. See [LICENSE](LICENSE).