# syntax=docker/dockerfile:1
# Deno provides the JS runtime yt-dlp uses to solve signature challenges.
FROM denoland/deno:alpine AS deno

FROM python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /usr/local/bin/deno /usr/local/bin/deno

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY ytmusic_mirror ./ytmusic_mirror
RUN pip install --no-cache-dir ".[web]"

ENV PYTHONUNBUFFERED=1 \
    YTMUSIC_MIRROR_MUSIC=/music

VOLUME ["/music", "/config"]
EXPOSE 8000

ENTRYPOINT ["ytmusic-mirror"]
CMD ["serve", "-c", "/config/config.json", "--host", "0.0.0.0", "--port", "8000"]
