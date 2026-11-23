FROM python:3.11-slim-bookworm

# ffmpeg is needed to play audio in voice chats. gcc is only a fallback in case
# a package has no ready-made wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ffmpeg gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /MusicPlayer

# Requirements first, so Docker can reuse this layer when only the code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir -r requirements.txt

# Your own code (this repo), not the upstream one
COPY . .

CMD ["python3", "main.py"]
