FROM python:3.11-slim

# gosu lets the entrypoint drop privileges from root → claude after it
# heals any root-owned files left over from earlier runs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# Get Docker CLI binary for docker exec calls to agent containers
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app

COPY router/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY router/ ./router/
COPY config/ ./config/

# Match the agent containers' uid/gid so files written into the shared
# /config volume are owned consistently (issue #116).
RUN groupadd -g 1000 claude \
    && useradd -m -u 1000 -g 1000 -s /bin/bash claude

COPY router/entrypoint.sh /usr/local/bin/router-entrypoint.sh
RUN chmod +x /usr/local/bin/router-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/router-entrypoint.sh"]
CMD ["python", "-m", "router.app"]
