####################################################################################################################################################
# Stage 0: base (Ubuntu 24.04 with common tooling)
####################################################################################################################################################
FROM ubuntu:24.04 AS base

# ---- Environment ---------------------------------------------------------------------------------------------------------------------------------
ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/usr/lib/postgresql/16/bin:$PATH"

# ---- Common packages & language runtimes ----------------------------------------------------------------------------------------------------------
RUN apt-get update && \
    # minimal helpers first
    apt-get install -y software-properties-common curl wget gnupg2 lsb-release ca-certificates && \
    # language / toolchain PPAs
    add-apt-repository ppa:deadsnakes/ppa -y && \
    add-apt-repository ppa:dotnet/backports -y && \
    # PostgreSQL APT repo
    wget -qO- https://www.postgresql.org/media/keys/ACCC4CF8.asc | apt-key add - && \
    echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && \
    # core build/runtime packages shared by almost every stage
    apt-get install -y --no-install-recommends \
      build-essential linux-headers-generic libxml2-utils git jq tzdata nano locales python3-dev python3 \
      python3.11 python3.11-venv python3.11-dev python3-pip libffi-dev libpython3.11 libpq-dev golang \
      fuse3 ffmpeg openssl unzip pkg-config libboost-filesystem-dev libboost-thread-dev libsqlite3-dev \
      postgresql-client-16 postgresql-16 postgresql-contrib-16 postgresql-server-dev-16 pgagent libpq-dev \
      dotnet-sdk-9.0 htop bash make g++ git && \
    # Python convenience + locale
    locale-gen en_US.UTF-8 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 && \
    update-alternatives --install /usr/bin/pip pip /usr/bin/pip3 1 && \
    ln -sf /usr/lib/$(uname -m)-linux-gnu/libpython3.11.so.1 /usr/local/lib/libpython3.11.so.1 && \
    ln -sf /usr/lib/$(uname -m)-linux-gnu/libpython3.11.so.1.0 /usr/local/lib/libpython3.11.so.1.0 && \
    # Node.js 22.x + global npm / pnpm (used by multiple builders)
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g npm@10 pnpm@latest-10 && \
    # clean
    rm -rf /var/lib/apt/lists/*

# make Postgres client binaries available in login shells
RUN echo "export PATH=/usr/lib/postgresql/16/bin:\$PATH" > /etc/profile.d/postgresql.sh
RUN echo "export PATH=/usr/lib/postgresql/16/bin:$PATH" >> /root/.bashrc

####################################################################################################################################################
# Stage 1: pgadmin-builder
####################################################################################################################################################
FROM base AS pgadmin-builder
RUN python3.11 -m venv /pgadmin/venv && \
    /pgadmin/venv/bin/python -m pip install --upgrade pip && \
    /pgadmin/venv/bin/python -m pip install pgadmin4

####################################################################################################################################################
# Stage 2: systemstats-builder
####################################################################################################################################################
FROM base AS systemstats-builder
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
FROM base AS zilean-builder
ARG TARGETARCH
ARG ZILEAN_TAG
WORKDIR /tmp
RUN curl -L https://github.com/iPromKnight/zilean/archive/refs/tags/${ZILEAN_TAG}.zip -o zilean.zip && \
    unzip zilean.zip && mv zilean-* /zilean && echo ${ZILEAN_TAG} > /zilean/version.txt && \
    cd /zilean && dotnet restore -a ${TARGETARCH} && \
    cd /zilean/src/Zilean.ApiService && dotnet publish -c Release --no-restore -a ${TARGETARCH} -o /zilean/app/ && \
    cd /zilean/src/Zilean.Scraper && dotnet publish -c Release --no-restore -a ${TARGETARCH} -o /zilean/app/ && \
    cd /zilean && python3.11 -m venv /zilean/venv && . /zilean/venv/bin/activate && pip install -r /zilean/requirements.txt && \
    rm -rf /tmp/zilean*

####################################################################################################################################################
# Stage 4: dumb-frontend-builder
####################################################################################################################################################
FROM base AS dumb-frontend-builder
ARG DUMB_FRONTEND_TAG
RUN curl -L https://github.com/nicocapalbo/dmbdb/archive/refs/tags/${DUMB_FRONTEND_TAG}.zip -o dumb-frontend.zip && \
    unzip dumb-frontend.zip && mkdir -p /dumb/frontend && mv dmbdb*/* /dumb/frontend && rm dumb-frontend.zip
WORKDIR /dumb/frontend
RUN echo "store-dir=./.pnpm-store\nchild-concurrency=1\nfetch-retries=10\nfetch-retry-factor=3\nfetch-retry-mintimeout=15000" > /dumb/frontend/.npmrc && \
    pnpm install --reporter=verbose && pnpm run build --log-level verbose

####################################################################################################################################################
# Stage 5: plex_debrid-builder
####################################################################################################################################################
FROM base AS plex_debrid-builder
ARG PLEX_DEBRID_TAG
RUN curl -L https://github.com/elfhosted/plex_debrid/archive/refs/heads/main.zip -o plex_debrid.zip && \
    unzip plex_debrid.zip && mkdir -p /plex_debrid && mv plex_debrid-main/* /plex_debrid && rm -rf plex_debrid.zip plex_debrid-main
ADD https://raw.githubusercontent.com/I-am-PUID-0/pd_zurg/master/plex_debrid_/settings-default.json /plex_debrid/settings-default.json
RUN python3.11 -m venv /plex_debrid/venv && \
    /plex_debrid/venv/bin/python -m pip install --upgrade pip && \
    /plex_debrid/venv/bin/python -m pip install -r /plex_debrid/requirements.txt

####################################################################################################################################################
# Stage 6: cli_debrid-builder
####################################################################################################################################################
FROM base AS cli_debrid-builder
ARG CLI_DEBRID_TAG
RUN curl -L https://github.com/godver3/cli_debrid/archive/refs/tags/${CLI_DEBRID_TAG}.zip -o cli_debrid.zip && \
    unzip cli_debrid.zip && mkdir -p /cli_debrid && mv cli_debrid-*/* /cli_debrid && rm -rf cli_debrid.zip cli_debrid-*/*
RUN python3.11 -m venv /cli_debrid/venv && \
    /cli_debrid/venv/bin/python -m pip install --upgrade pip && \
    /cli_debrid/venv/bin/python -m pip install -r /cli_debrid/requirements-linux.txt

####################################################################################################################################################
# Stage 7: requirements-builder
####################################################################################################################################################
FROM base AS requirements-builder
COPY pyproject.toml poetry.lock ./
RUN python3.11 -m venv /venv && \
    . /venv/bin/activate && \
    pip install --upgrade pip && pip install poetry && \
    poetry config virtualenvs.create false && poetry install --no-root

####################################################################################################################################################
# Stage 8: final-stage
####################################################################################################################################################
FROM base AS final-stage
ARG TARGETARCH
LABEL name="DUMB" \
      description="Debrid Unlimited Media Bridge" \
      url="https://github.com/I-am-PUID-0/DUMB" \
      maintainer="I-am-PUID-0"

# Copy artifacts from builder stages ---------------------------------------------------------------------------------------------------------------
COPY --from=requirements-builder /venv /venv
COPY --from=pgadmin-builder /pgadmin/venv /pgadmin/venv
COPY --from=systemstats-builder /usr/share/postgresql/16/extension/system_stats* /usr/share/postgresql/16/extension/
COPY --from=systemstats-builder /usr/lib/postgresql/16/lib/system_stats.so /usr/lib/postgresql/16/lib/
COPY --from=zilean-builder /zilean /zilean
COPY --from=dumb-frontend-builder /dumb/frontend /dumb/frontend
COPY --from=plex_debrid-builder /plex_debrid /plex_debrid
COPY --from=cli_debrid-builder /cli_debrid /cli_debrid
COPY --from=rclone/rclone:latest /usr/local/bin/rclone /usr/local/bin/rclone

# Zurg default config tweaks
ADD https://raw.githubusercontent.com/debridmediamanager/zurg-testing/main/config.yml /zurg/
ADD https://raw.githubusercontent.com/debridmediamanager/zurg-testing/main/scripts/plex_update.sh /zurg/
RUN sed -i 's/^on_library_update: sh plex_update.sh.*$/# &/' /zurg/config.yml

# Project code
COPY . /./

ENV XDG_CONFIG_HOME=/config \
    TERM=xterm

WORKDIR /

HEALTHCHECK --interval=60s --timeout=10s CMD ["/bin/bash","-c",". /venv/bin/activate && python /healthcheck.py"]
ENTRYPOINT ["/bin/bash","-c",". /venv/bin/activate && python /main.py"]