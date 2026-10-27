FROM ghcr.io/itzg/minecraft-server:latest

# The upstream image provides the Minecraft runtime and launchers. jq is used
# only to translate Home Assistant's /data/options.json into those settings.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*

COPY run.sh /run.sh
RUN chmod 0755 /run.sh

ENTRYPOINT ["/run.sh"]
