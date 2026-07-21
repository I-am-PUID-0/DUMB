# syntax=docker/dockerfile:1.7

ARG RCLONE_TAG=latest
ARG GO_VERSION=1.26.5

FROM golang:${GO_VERSION}-bookworm AS go-runtime
FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble AS dotnet-runtime
FROM mcr.microsoft.com/dotnet/sdk:10.0-noble AS dotnet-sdk

####################################################################################################################################################
# Stage 0: base (Ubuntu 26.04 with common tooling)
####################################################################################################################################################
FROM ubuntu:26.04 AS base

ARG APT_REFRESH=manual
ARG NPM_VERSION=12.0.1
ARG PNPM_VERSION=10.34.5

# ---- Environment ---------------------------------------------------------------------------------------------------------------------------------
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/usr/local/go/bin:/usr/lib/postgresql/16/bin:$PATH"

COPY --from=go-runtime /usr/local/go /usr/local/go
COPY --from=go-runtime /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=dotnet-runtime /usr/share/dotnet /usr/share/dotnet

# ---- Common packages & language runtimes ----------------------------------------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/root/.npm,sharing=locked \
    echo "Refreshing APT metadata for ${APT_REFRESH}" && \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    # Ubuntu 26.04's minimal image has the HTTPS method but not the
    # ca-certificates package. Point APT at the bootstrapped bundle explicitly
    # until the current Ubuntu package is installed during this layer.
    printf '%s\n' 'Acquire::https::CaInfo "/etc/ssl/certs/ca-certificates.crt";' \
      > /etc/apt/apt.conf.d/99https-ca-info && \
    # ARM runners in particular can have port 80 blocked while HTTPS remains
    # reachable. Use HTTPS for Ubuntu before the first metadata refresh.
    sed -i 's#http://ports.ubuntu.com#https://ports.ubuntu.com#g; s#http://archive.ubuntu.com#https://archive.ubuntu.com#g; s#http://security.ubuntu.com#https://security.ubuntu.com#g' \
      /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 -o APT::Update::Error-Mode=any update && \
    apt-get -o Acquire::Retries=5 upgrade -y && \
    # minimal helpers first
    apt-get install -y software-properties-common curl wget gnupg2 lsb-release ca-certificates && \
    # language / toolchain PPAs
    add-apt-repository ppa:deadsnakes/ppa -y && \
    # PostgreSQL APT repo
    install -d -m 0755 /etc/apt/keyrings && \
    wget -qO /etc/apt/keyrings/postgresql.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc && \
    echo "deb [signed-by=/etc/apt/keyrings/postgresql.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 -o APT::Update::Error-Mode=any update && \
    # core build/runtime packages shared by almost every stage
    apt-get install -y --no-install-recommends \
      build-essential libxml2-utils git jq tzdata nano locales python3 \
      python3.11 python3.11-venv python3.11-dev python3.12 python3.12-venv python3.12-dev libffi-dev libpython3.11 libpq-dev \
      fuse3 ffmpeg mesa-va-drivers mesa-vulkan-drivers openssl unzip pkg-config \
      libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev libpixman-1-dev librsvg2-dev \
      postgresql-client-16 postgresql-16 postgresql-contrib-16 pgagent \
      htop bash && \
    # Python convenience + locale
    locale-gen en_US.UTF-8 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 && \
    ln -sf /usr/lib/$(uname -m)-linux-gnu/libpython3.11.so.1 /usr/local/lib/libpython3.11.so.1 && \
    ln -sf /usr/lib/$(uname -m)-linux-gnu/libpython3.11.so.1.0 /usr/local/lib/libpython3.11.so.1.0 && \
    ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet && \
    # Node.js 24.x + global npm / pnpm (used by multiple builders)
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g "npm@${NPM_VERSION}" "pnpm@${PNPM_VERSION}" && \
    # Repository setup tools are not needed by the running image. Removing them
    # also removes their unused system Python dependency tree.
    apt-get purge -y software-properties-common gnupg2 lsb-release && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* && \
    go version && dotnet --info >/dev/null && node --version && npm --version && pnpm --version

# Keep login shells and setup subprocesses on a UTF-8 locale. PostgreSQL
# initialization also pins its locale and encoding explicitly below.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# make Postgres client binaries available in login shells
RUN echo "export PATH=/usr/lib/postgresql/16/bin:\$PATH" > /etc/profile.d/postgresql.sh
RUN echo "export PATH=/usr/lib/postgresql/16/bin:\$PATH" >> /root/.bashrc

####################################################################################################################################################
# Stage 1: pgadmin-builder
####################################################################################################################################################
FROM base AS pgadmin-builder
ARG PGADMIN_VERSION=9.16
RUN --mount=type=cache,target=/root/.cache/pip,sharing=shared \
    python3.11 -m venv /pgadmin/venv && \
    /pgadmin/venv/bin/python -m pip install --upgrade pip setuptools wheel && \
    /pgadmin/venv/bin/python -m pip install "pgadmin4==${PGADMIN_VERSION}" && \
    # pgAdmin 9.16 pins setuptools to 82.x on Python >3.9, but setuptools 83
    # contains the CVE-2026-59890 fix and remains runtime-compatible. Widen
    # the installed package metadata before upgrading so pip check remains a
    # meaningful compatibility gate.
    PGADMIN_METADATA="$(find /pgadmin/venv/lib/python3.11/site-packages \
      -path '*/pgadmin4-*.dist-info/METADATA' -print -quit)" && \
    test -n "${PGADMIN_METADATA}" && \
    sed -i 's/setuptools==82\.\*/setuptools>=83,<84/' "${PGADMIN_METADATA}" && \
    grep -q 'Requires-Dist: setuptools>=83,<84; python_version > "3.9"' "${PGADMIN_METADATA}" && \
    /pgadmin/venv/bin/python -m pip install --upgrade --no-deps \
      "setuptools>=83,<84" && \
    /pgadmin/venv/bin/python -m pip check && \
    /pgadmin/venv/bin/python -c \
      'import importlib.metadata, pgadmin4; assert importlib.metadata.version("setuptools").startswith("83.")' && \
    find /pgadmin/venv/lib/python3.11/site-packages -type d \
      \( -name tests -o -name test \) -prune -exec rm -rf '{}' +

####################################################################################################################################################
# Stage 2: systemstats-builder
####################################################################################################################################################
FROM base AS postgres-build-base
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    rm -rf /var/lib/apt/lists/* && \
    apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 -o APT::Update::Error-Mode=any update && \
    apt-get install -y --no-install-recommends postgresql-server-dev-16 && \
    rm -rf /var/lib/apt/lists/*

FROM postgres-build-base AS systemstats-builder
ARG SYS_STATS_TAG
WORKDIR /tmp
RUN curl -L https://github.com/EnterpriseDB/system_stats/archive/refs/tags/${SYS_STATS_TAG}.zip -o system_stats.zip && \
    unzip system_stats.zip && mv system_stats-* system_stats && \
    cd system_stats && make USE_PGXS=1 && make install USE_PGXS=1 && \
    mkdir -p /usr/share/postgresql/16/extension && \
    cp system_stats.control /usr/share/postgresql/16/extension/ && \
    cp system_stats--*.sql /usr/share/postgresql/16/extension/ && \
    cd / && rm -rf /tmp/system_stats*

####################################################################################################################################################
# Stage 3: zilean-builder
####################################################################################################################################################
FROM base AS dotnet-build-base
COPY --from=dotnet-sdk /usr/share/dotnet /usr/share/dotnet

FROM dotnet-build-base AS zilean-builder
ARG TARGETARCH
ARG ZILEAN_TAG
COPY utils/zilean_dotnet.py /tmp/zilean_dotnet.py
WORKDIR /tmp
RUN --mount=type=cache,target=/root/.cache/pip,sharing=shared \
    --mount=type=cache,target=/root/.nuget/packages,sharing=locked \
    curl -L https://github.com/iPromKnight/zilean/archive/refs/tags/${ZILEAN_TAG}.zip -o zilean.zip && \
    unzip zilean.zip && mv zilean-* /zilean && echo ${ZILEAN_TAG} > /zilean/version.txt && \
    # Use the same compatibility transform as runtime release/branch installs.
    python3 /tmp/zilean_dotnet.py /zilean && \
    cd /zilean && dotnet restore -a ${TARGETARCH} && \
    cd /zilean/src/Zilean.ApiService && dotnet publish -c Release --no-restore -a ${TARGETARCH} -o /zilean/app/ && \
    cd /zilean/src/Zilean.Scraper && dotnet publish -c Release --no-restore -a ${TARGETARCH} -o /zilean/app/ && \
    grep -q '"tfm": "net10.0"' /zilean/app/zilean-api.runtimeconfig.json && \
    grep -q '"tfm": "net10.0"' /zilean/app/scraper.runtimeconfig.json && \
    cd /zilean && python3.11 -m venv /zilean/venv && . /zilean/venv/bin/activate && \
    pip install --upgrade pip setuptools wheel && pip install -r /zilean/requirements.txt && \
    find /zilean/venv/lib/python3.11/site-packages -type d \
      \( -name tests -o -name test \) -prune -exec rm -rf '{}' + && \
    rm -rf /zilean/src /zilean/tests /zilean/docs /zilean/eng /zilean/.github /zilean/.run && \
    find /zilean -maxdepth 1 -type f ! -name version.txt -delete && \
    rm -rf /tmp/zilean*

####################################################################################################################################################
# Stage 4: dumb-frontend-builder
####################################################################################################################################################
FROM base AS dumb-frontend-builder
ARG DUMB_FRONTEND_TAG
RUN curl -L https://github.com/nicocapalbo/dmbdb/archive/refs/tags/${DUMB_FRONTEND_TAG}.zip -o dumb-frontend.zip && \
    unzip dumb-frontend.zip && mkdir -p /dumb/frontend && mv dmbdb*/* /dumb/frontend && rm dumb-frontend.zip
WORKDIR /dumb/frontend
RUN --mount=type=cache,target=/root/.local/share/pnpm/store,sharing=locked \
    printf '%s\n' \
      'store-dir=/root/.local/share/pnpm/store' \
      'child-concurrency=1' \
      'fetch-retries=10' \
      'fetch-retry-factor=3' \
      'fetch-retry-mintimeout=15000' > /dumb/frontend/.npmrc && \
    pnpm install --reporter=verbose && \
    pnpm run build --log-level verbose && \
    rm -rf node_modules .nuxt node-compile-cache .pnpm-store

####################################################################################################################################################
# Stage 5: cli_debrid-builder
####################################################################################################################################################
FROM base AS cli_debrid-builder
ARG CLI_DEBRID_TAG
RUN curl -L https://github.com/godver3/cli_debrid/archive/refs/tags/${CLI_DEBRID_TAG}.zip -o cli_debrid.zip && \
    unzip cli_debrid.zip && mkdir -p /cli_debrid && mv cli_debrid-*/* /cli_debrid && rm -rf cli_debrid.zip cli_debrid-*/*
RUN --mount=type=cache,target=/root/.cache/pip,sharing=shared \
    python3.11 -m venv /cli_debrid/venv && \
    /cli_debrid/venv/bin/python -m pip install --upgrade pip setuptools wheel && \
    /cli_debrid/venv/bin/python -m pip install -r /cli_debrid/requirements-linux.txt && \
    /cli_debrid/venv/bin/python -m pip install --upgrade \
      "certifi>=2026.5.20" \
      "Flask>=3.1.3,<4" \
      "Flask-Cors>=6.0.0" \
      "idna>=3.7" \
      "Markdown>=3.8.1" \
      "Pillow>=12.2.0" \
      "protobuf>=5.29.6,<6" \
      "requests>=2.33.0" \
      "urllib3>=2.7.0" \
      "Werkzeug>=3.0.6" && \
    # nyaapy 0.7 caps lxml below 6, but its HTML parsing remains compatible.
    # Override the stale metadata so lxml includes the CVE-2026-41066 fix.
    NYAAPY_METADATA="$(find /cli_debrid/venv/lib/python3.11/site-packages \
      -path '*/nyaapy-*.dist-info/METADATA' -print -quit)" && \
    test -n "${NYAAPY_METADATA}" && \
    sed -i 's/lxml (>=5.2.2,<6.0.0)/lxml (>=5.2.2,<7.0.0)/' "${NYAAPY_METADATA}" && \
    grep -q 'Requires-Dist: lxml (>=5.2.2,<7.0.0)' "${NYAAPY_METADATA}" && \
    /cli_debrid/venv/bin/python -m pip install --upgrade --no-deps \
      "lxml>=6.1.0,<7.0.0" && \
    /cli_debrid/venv/bin/python -c \
      'import flask, flask_cors, flask_login, flask_sqlalchemy, lxml, nyaapy; from bs4 import BeautifulSoup; assert BeautifulSoup("<p>ok</p>", "lxml").p.text == "ok"' && \
    /cli_debrid/venv/bin/python -m pip check && \
    find /cli_debrid/venv/lib/python3.11/site-packages -type d \
      \( -name tests -o -name test \) -prune -exec rm -rf '{}' + && \
    rm -rf /cli_debrid/tests /cli_debrid/windows_build.spec /cli_debrid/windows_wrapper.py

####################################################################################################################################################
# Stage 6: requirements-builder
####################################################################################################################################################
FROM base AS poetry-builder
ARG POETRY_VERSION=2.4.1
RUN --mount=type=cache,target=/root/.cache/pip,sharing=shared \
    python3.11 -m venv /opt/poetry && \
    /opt/poetry/bin/python -m pip install --upgrade pip setuptools wheel && \
    /opt/poetry/bin/python -m pip install "poetry==${POETRY_VERSION}"

FROM base AS requirements-builder
COPY pyproject.toml poetry.lock ./
COPY --from=poetry-builder /opt/poetry /opt/poetry
RUN --mount=type=cache,target=/root/.cache/pip,sharing=shared \
    python3.11 -m venv /venv && \
    /venv/bin/python -m pip install --upgrade pip setuptools wheel && \
    VIRTUAL_ENV=/venv /opt/poetry/bin/poetry install --only main --no-root --no-interaction && \
    # Ensure working crypto stack in /venv so PyJWT never falls back to broken system bindings.
    /venv/bin/python -m pip install --upgrade --force-reinstall "cffi>=1.16,<3.0" "cryptography>=48.0.1,<49.0.0" && \
    find /venv/lib/python3.11/site-packages -type d \
      \( -name tests -o -name test \) -prune -exec rm -rf '{}' + && \
    rm -rf /opt/poetry

####################################################################################################################################################
# Stage 7: final-stage
####################################################################################################################################################
FROM rclone/rclone:${RCLONE_TAG} AS rclone-binary

FROM base AS final-stage
ARG TARGETARCH
ARG DEV_VERSION
ARG ZURG_REF=main
LABEL name="DUMB" \
      description="Debrid Unlimited Media Bridge" \
      url="https://github.com/I-am-PUID-0/DUMB" \
      maintainer="I-am-PUID-0" \
      org.opencontainers.image.licenses="GPL-3.0-only"

# Copy artifacts from builder stages ---------------------------------------------------------------------------------------------------------------
COPY --from=requirements-builder /venv /venv
COPY --from=pgadmin-builder /pgadmin/venv /pgadmin/venv
COPY --from=systemstats-builder /usr/share/postgresql/16/extension/system_stats* /usr/share/postgresql/16/extension/
COPY --from=systemstats-builder /usr/lib/postgresql/16/lib/system_stats.so /usr/lib/postgresql/16/lib/
COPY --from=zilean-builder /zilean/app /zilean/app
COPY --from=zilean-builder /zilean/venv /zilean/venv
COPY --from=zilean-builder /zilean/version.txt /zilean/version.txt
COPY --from=dumb-frontend-builder /dumb/frontend /dumb/frontend
COPY --from=cli_debrid-builder /cli_debrid /cli_debrid
COPY --from=rclone-binary /usr/local/bin/rclone /usr/local/bin/rclone

RUN LAVAPIPE_ICD="$(find /usr/share/vulkan/icd.d -name 'lvp_icd*.json' -print -quit)" && \
    test -n "$LAVAPIPE_ICD" && \
    ln -sf "$LAVAPIPE_ICD" /usr/share/vulkan/icd.d/lavapipe_icd.json

# Zurg default config tweaks
ADD https://raw.githubusercontent.com/debridmediamanager/zurg-testing/${ZURG_REF}/config.yml /zurg/
ADD https://raw.githubusercontent.com/debridmediamanager/zurg-testing/${ZURG_REF}/scripts/plex_update.sh /zurg/
RUN sed -i 's/^on_library_update: sh plex_update.sh.*$/# &/' /zurg/config.yml

# Project code
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/dumb/
COPY . /./

ENV XDG_CONFIG_HOME=/config \
    TERM=xterm \
    DUMB_VERSION=${DEV_VERSION}

ENV PATH="/venv/bin:$PATH"

WORKDIR /

HEALTHCHECK --interval=60s --timeout=10s CMD ["python", "/healthcheck.py"]
ENTRYPOINT ["python", "/main.py"]
