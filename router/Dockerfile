FROM python:3.11-slim

# Get Docker CLI binary for docker exec calls to agent containers
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app

COPY router/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY router/ ./router/
COPY config/ ./config/

CMD ["python", "-m", "router.app"]
