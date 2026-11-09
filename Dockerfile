# --- Pruebas: `docker build --target test .` ejecuta pyflakes y pytest en el mismo Python que la imagen final. -------
# La imagen final NO depende de esta etapa (una compilación normal no la ejecuta ni lleva pytest). Se fija a la
# plataforma del que compila para que, con una imagen multi-arquitectura, las pruebas corran una sola vez y nativas.
FROM --platform=$BUILDPLATFORM python:3.14-slim AS test
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-test.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt -r requirements-test.txt
COPY main.py schemas.py ./
COPY core ./core
COPY services ./services
COPY static ./static
COPY tests ./tests
RUN python -m pyflakes main.py core services && python -m pytest -q -p no:cacheprovider

# --- Imagen final (la última etapa es la que se construye por defecto) --------------------------------------------
FROM python:3.14-slim AS app

ARG APP_VERSION=2026.11.09.01
LABEL org.opencontainers.image.title="app-integration-icloud-sync" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC APP_VERSION=${APP_VERSION}
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# deps antes que código: cachea la capa de pip aunque cambie el código
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY main.py schemas.py ./
COPY core ./core
COPY services ./services
COPY static ./static

# El uid real lo fija "user:" en el compose (dueño de la carpeta del NAS); este es el de por defecto.
RUN useradd -u 1000 -m appuser && chown -R appuser /app
USER appuser
STOPSIGNAL SIGTERM

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
