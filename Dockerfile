FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORGANIZER_HOST=0.0.0.0 \
    ORGANIZER_PORT=8000

RUN apt-get update \
    && apt-get install --no-install-recommends -y unrar-free \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN pip install --no-cache-dir .
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/config", "/data"]
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["organizer-web"]
