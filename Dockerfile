FROM node:22-bookworm-slim AS node-runtime
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/ /usr/local/

RUN node --version \
    && node -e "if (Number(process.versions.node.split('.')[0]) < 22) process.exit(1)"

WORKDIR /app

COPY server/ .

RUN python -m pip install --no-cache-dir certifi .

CMD ["python", "start.py"]
