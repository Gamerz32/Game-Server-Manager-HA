from __future__ import annotations

import asyncio
import logging
import os

LOG = logging.getLogger("minecraft-gateway")
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")


async def _pipe(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await source.read(65536)
            if not data:
                return
            destination.write(data)
            await destination.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        return


async def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        host = os.environ["MCM_MANAGER_HOST"]
        manager_port = int(os.environ["MCM_MANAGER_PORT"])
        server_id = os.environ["MCM_SERVER_ID"].encode("ascii")
        token = os.environ["MCM_GATEWAY_TOKEN"].encode("ascii")
        if len(server_id) != 36 or len(token) != 64:
            raise RuntimeError("The Minecraft gateway received invalid manager credentials.")
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, manager_port),
            timeout=5,
        )
        upstream_writer.write(b"MCG1" + server_id + token)
        await upstream_writer.drain()
        tasks = [
            asyncio.create_task(_pipe(reader, upstream_writer)),
            asyncio.create_task(_pipe(upstream_reader, writer)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done, return_exceptions=True)
    except (KeyError, ValueError, RuntimeError, asyncio.TimeoutError, ConnectionError, OSError):
        LOG.debug("Could not relay a client connection to the manager", exc_info=True)
    except Exception:
        LOG.exception("Unexpected gateway relay error")
    finally:
        await _close(upstream_writer)
        await _close(writer)


async def main() -> None:
    manager_host = os.environ["MCM_MANAGER_HOST"]
    manager_port = int(os.environ["MCM_MANAGER_PORT"])
    _, probe_writer = await asyncio.wait_for(asyncio.open_connection(manager_host, manager_port), timeout=5)
    probe_writer.close()
    await probe_writer.wait_closed()
    port = int(os.environ.get("MCM_GATEWAY_PORT", "25565"))
    server = await asyncio.start_server(handle_client, host="0.0.0.0", port=port, backlog=128)
    LOG.info("Minecraft gateway listening on TCP port %s", port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
