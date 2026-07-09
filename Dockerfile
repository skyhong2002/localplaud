# localplaud — CPU image (profiles: cpu, mac).
#
# Extras are overridable at build time, e.g.:
#   docker build --build-arg EXTRAS="cloud" -t localplaud .
ARG EXTRAS="faster-whisper,cloud,local-llm"

# --------------------------------------------------------------------------- #
# Build stage: compile wheels into a self-contained venv.
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS build
ARG EXTRAS

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[${EXTRAS}]"

# --------------------------------------------------------------------------- #
# Runtime stage: slim image with ffmpeg and the prebuilt venv.
# --------------------------------------------------------------------------- #
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 localplaud
WORKDIR /app
RUN mkdir -p /app/data && chown -R localplaud:localplaud /app
USER localplaud

VOLUME /app/data
EXPOSE 8080

CMD ["localplaud", "run"]
