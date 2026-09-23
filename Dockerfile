FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/ .

RUN python -m pip install --no-cache-dir certifi .

CMD ["python", "start.py"]
