# Build for the Geekom's architecture with `--platform linux/amd64`; Compose
# pins it too. The Dockerfile itself stays portable so the suite can build here.
FROM python:3.14-slim

# curl is available for a mounted host notifier that calls it. The image
# does not include a notifier; a missing command must not stop the service.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# A world-traversable HOME, so any UID Compose selects can read the read-only
# Telegram credentials bind-mounted into it.
RUN useradd --create-home --home-dir /home/app --uid 10001 app \
 && chmod 755 /home/app

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY raindrop_rss ./raindrop_rss

# Bind-mount targets only: config (ro), subscriptions (rw dir), and data (rw).
# Compose runs the container as the host user who owns those paths, so nothing
# here is chowned or opened up. Running without the mounts fails loudly
# instead of writing into the image. A host notifier is an optional extra mount.
RUN mkdir -p /app/data /app/subscriptions
USER app
ENV PYTHONUNBUFFERED=1 HOME=/home/app RAINDROP_RSS_CONFIG=/app/config/settings.toml
EXPOSE 38471
ENTRYPOINT ["python", "-m", "raindrop_rss"]
