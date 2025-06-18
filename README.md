<div align="center" style="max-width: 100%; height: auto;">
  <h1>🎬 Debrid Media Bridge 🎬</h1>
  <a href="https://github.com/I-am-PUID-0/DMB">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://github.com/I-am-PUID-0/DMB/assets/36779668/d0cbc785-2e09-41da-b226-924fdfcc1f21">
      <img alt="DMB" src="https://github.com/I-am-PUID-0/DMB/assets/36779668/d0cbc785-2e09-41da-b226-924fdfcc1f21" style="max-width: 100%; height: auto;">
    </picture>
  </a>
</div>
<div
  align="center"
  style="display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-top: 1em;"
>
  <a href="https://github.com/I-am-PUID-0/DMB/stargazers">
    <img
      alt="GitHub Repo stars"
      src="https://img.shields.io/github/stars/I-am-PUID-0/DMB?style=for-the-badge"
    />
  </a>
  <a href="https://github.com/I-am-PUID-0/DMB/issues">
    <img
      alt="Issues"
      src="https://img.shields.io/github/issues/I-am-PUID-0/DMB?style=for-the-badge"
    />
  </a>
  <a href="https://github.com/I-am-PUID-0/DMB/blob/master/COPYING">
    <img
      alt="License"
      src="https://img.shields.io/github/license/I-am-PUID-0/DMB?style=for-the-badge"
    />
  </a>
  <a href="https://github.com/I-am-PUID-0/DMB/graphs/contributors">
    <img
      alt="Contributors"
      src="https://img.shields.io/github/contributors/I-am-PUID-0/DMB?style=for-the-badge"
    />
  </a>
  <a href="https://hub.docker.com/r/iampuid0/dmb">
    <img
      alt="Docker Pulls"
      src="https://img.shields.io/docker/pulls/iampuid0/dmb?style=for-the-badge&logo=docker&logoColor=white"
    />
  </a>
  <a href="https://discord.gg/8dqKUBtbp5">
    <img
      alt="Join Discord"
      src="https://img.shields.io/badge/Join%20us%20on%20Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white"
    />
  </a>
</div>

## 📜 Description

**Debrid Media Bridge (DMB)** is an All-In-One (AIO) docker image for the unified deployment of **[Riven Media's](https://github.com/rivenmedia)**, **[itsToggle's](https://github.com/itsToggle)**, **[godver3's](https://github.com/godver3)**, **[yowmamasita's](https://github.com/yowmamasita)**, **[iPromKnight's](https://github.com/iPromKnight/zilean)**, **[Mukhtar Akere's](https://github.com/sirrobot01)**, **[Nick Craig-Wood's](https://github.com/ncw)**, **[Michael Stonebraker's](https://en.wikipedia.org/wiki/Michael_Stonebraker)**, and **[Dave Page's](https://github.com/dpage)** projects -- **[Riven](https://github.com/rivenmedia/riven)**, **[plex_debrid](https://github.com/itsToggle/plex_debrid)**, **[cli_debrid](https://github.com/godver3/cli_debrid)**, **[Zurg](https://github.com/debridmediamanager/zurg-testing)**, **[Zilean](https://github.com/iPromKnight/zilean)**, **[Decypharr](https://github.com/sirrobot01/decypharr)**,**[rclone](https://github.com/rclone/rclone)**, **[PostgreSQL](https://www.postgresql.org/)**, and **[pgAdmin 4](https://www.pgadmin.org/)**.

> ⚠️ **IMPORTANT**: Docker Desktop **CANNOT** be used to run DMB. Docker Desktop does not support the [mount propagation](https://docs.docker.com/storage/bind-mounts/#configure-bind-propagation) required for rclone mounts.
>
> ![image](https://github.com/I-am-PUID-0/DMB/assets/36779668/aff06342-1099-4554-a5a4-72a7c82cb16e)
>
> See the DMB Docs for [alternative deployment options](https://i-am-puid-0.github.io/DMB/deployment/wsl) to run DMB on Windows through WSL2.

## 🌟 Features

See the DMB [Docs](https://i-am-puid-0.github.io/DMB/features) for a full list of features and settings.

## 🐳 Docker Hub

A prebuilt image is hosted on [Docker Hub](https://hub.docker.com/r/iampuid0/dmb).

## 🏷️ GitHub Container Registry

A prebuilt image is hosted on [GitHub Container Registry](https://github.com/I-am-PUID-0/DMB/pkgs/container/DMB).

## 🐳 Docker-compose

> [!NOTE]
> The below examples are not exhaustive and are intended to provide a starting point for deployment.

```YAML
services:
  DUMB:
    container_name: DUMB
    image: iampuid0/dmb:DUMB                                          ## Optionally, specify a specific version of DMB w/ image: iampuid0/dmb:2.0.0
    stop_grace_period: 30s                                            ## Adjust as need to allow for graceful shutdown of the container
    shm_size: 128mb                                                   ## Increased for PostgreSQL
    stdin_open: true                                                  ## docker run -i
    tty: true                                                         ## docker run -t
    volumes:
      - /home/username/docker/DUMB/config:/config                     ## Location of configuration files. If a Zurg config.yml and/or Zurg app is placed here, it will be used to override the default configuration and/or app used at startup.
      - /home/username/docker/DUMB/log:/log                           ## Location for logs
      - /home/username/docker/DUMB/Zurg/RD:/zurg/RD                   ## Location for Zurg RealDebrid active configuration
      - /home/username/docker/DUMB/Riven/data:/riven/backend/data     ## Location for Riven backend data
      - /home/username/docker/DUMB/Riven/mnt:/mnt                     ## Location for Riven symlinks
      - /home/username/docker/DUMB/PostgreSQL/data:/postgres_data     ## Location for PostgreSQL database
      - /home/username/docker/DUMB/pgAdmin4/data:/pgadmin/data        ## Location for pgAdmin 4 data
      - /home/username/docker/DUMB/Zilean/data:/zilean/app/data       ## Location for Zilean data
      - /home/username/docker/DUMB/plex_debrid:/plex_debrid/config    ## Location for plex_debrid data
      - /home/username/docker/DUMB/cli_debrid:/cli_debrid/data        ## Location for cli_debrid data
      - /home/username/docker/DUMB/phalanx_db:/phalanx_db/data        ## Location for phalanx_db data 
      - /home/username/docker/DUMB/decypharr:/decypharr               ## Location for decypharr data      
      - /home/username/docker/DUMB/plex:/plex                         ## Location for plex data 
    environment:
      - TZ=
      - PUID=
      - PGID=
      - DMB_LOG_LEVEL=INFO
      - ZURG_INSTANCES_REALDEBRID_API_KEY=
      - RIVEN_FRONTEND_ENV_ORIGIN=http://0.0.0.0:3000                ## See Riven documentation for more details
    # network_mode: container:gluetun                                ## Example to attach to gluetun vpn container if realdebrid blocks IP address
    ports:
      - "3005:3005"                                                  ## DMB Frontend
      - "3000:3000"                                                  ## Riven Frontend
      - "5050:5050"                                                  ## pgAdmin 4
      - "5000:5000"                                                  ## CLI Debrid Frontend      
      - "8282:8282"                                                  ## Decypharr Frontend         
      - "32400:32400"                                                ## Plex Media Server      
    devices:
      - /dev/fuse:/dev/fuse:rwm
      - /dev/dri:/dev/dri       
    cap_add:
      - SYS_ADMIN
    security_opt:
      - apparmor:unconfined
      - no-new-privileges
```

## 🌐 Environment Variables

The following table lists the required environment variables used by the container. The environment variables are set via the `-e` parameter or via the docker-compose file within the `environment:` section or with a .env file saved to the config directory. Value of this parameter is listed as `<VARIABLE_NAME>=<Value>`

Variables required by DMB:
| Variable       | Default  | Description                                                       | Required for DMB |
| -------------- | -------- | ------------------------------------------------------------------|----------------- |
| `PUID`         | `1000`   | Your User ID | :heavy_check_mark: |
| `PGID`         | `1000`   | Your Group ID |:heavy_check_mark: |
| `TZ`           | `(null)` | Your time zone listed as `Area/Location` | :heavy_check_mark: |
| `ZURG_INSTANCES_REALDEBRID_API_KEY` | `(null)` | Enter your Real-Debrid API Token | :heavy_check_mark: |
| `RIVEN_FRONTEND_ENV_ORIGIN` | `http://0.0.0.0:3000` | The IP address used to access the DMB frontend.  Change this to the IP address you use in the browser for Riven Frontend. | :heavy_check_mark: |

See the [.env.example](https://github.com/I-am-PUID-0/DMB/blob/master/.env.example)

## 🌐 Ports Used

> [!NOTE]
> The below examples are default and may be configurable with the use of additional environment variables.

The following table describes the ports used by the container. The mappings are set via the `-p` parameter or via the docker-compose file within the `ports:` section. Each mapping is specified with the following format: `<HOST_PORT>:<CONTAINER_PORT>[:PROTOCOL]`.

| Container port | Protocol | Description                                                                          |
| -------------- | -------- | ------------------------------------------------------------------------------------ |
| `3005`         | TCP      | DMB frontend - a web UI is accessible at the assigned port                           |
| `3000`         | TCP      | Riven frontend - A web UI is accessible at the assigned port                         |
| `8080`         | TCP      | Riven backend - The API is accessible at the assigned port                           |
| `5432`         | TCP      | PostgreSQL - The SQL server is accessible at the assigned port                       |
| `5050`         | TCP      | pgAdmin 4 - A web UI is accessible at the assigned port                              |
| `8182`         | TCP      | Zilean - The API and Web Ui (/swagger/index.html) is accessible at the assigned port |
| `9090`         | TCP      | Zurg - A web UI is accessible at the assigned port                                   |
| `5000`         | TCP      | CLI Debrid - A web UI is accessible at the assigned port                             |
| `8888`         | TCP      | Phalanx DB - The API is accessible at the assigned port                              |
| `8282`         | TCP      | Decypharr - A web UI is accessible at the assigned port                              |
| `32400`        | TCP      | Plex Media Server - PMS is accessible at the assigned port                           |

## 📂 Data Volumes

The following table describes the data volumes used by the container. The mappings
are set via the `-v` parameter or via the docker-compose file within the `volumes:` section. Each mapping is specified with the following
format: `<HOST_DIR>:<CONTAINER_DIR>[:PERMISSIONS]`.

| Container path        | Permissions | Description                                                                                                                                                                                                                                 |
| --------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/config`             | rw          | This is where the application stores the rclone.conf, and any files needing persistence. CAUTION: rclone.conf is overwritten upon start/restart of the container. Do NOT use an existing rclone.conf file if you have other rclone services |
| `/log`                | rw          | This is where the application stores its log files                                                                                                                                                                                          |
| `/data`               | rshared     | This is where rclone will be mounted.                                                                                                                                                                                                       |
| `/zurg/RD`            | rw          | This is where Zurg will store the active configuration and data for RealDebrid.                                                                                                    |
| `/riven/data`         | rw          | This is where Riven will store its data.                                                                                                                                                               |
| `/riven/mnt`          | rw          | This is where Riven will set its symlinks.                                                                                                                                                             |
| `/postgres_data`      | rw          | This is where PostgreSQL will store its data.                                                                                                                                                          |
| `/pgadmin/data`       | rw          | This is where pgAdmin 4 will store its data.                                                                                                                                                          |
| `/plex_debrid/config` | rw          | This is where plex_debrid will store its data.                                                                                                                                                   |
| `/cli_debrid/data` | rw             | This is where cli_debrid will store its data.                                                                                                                                                    |       
| `/phalanx_db/data` | rw             | This is where phalanx_db will store its data.                                                                                                                                                    |  
| `/decypharr`       | rw             | This is where decypharr will store its data.                                                                                                                                                     |  
| `/plex`            | rw             | This is where Plex Media Server will store its data.                                                                                                                                                    |    

## 📝 TODO

See the [DMB roadmap](https://github.com/users/I-am-PUID-0/projects/6) for a list of planned features and enhancements.

## 🛠️ DEV

### Tracking current development for an upcoming release:

- [Pre-Release Changes](https://gist.github.com/I-am-PUID-0/7e02c2cb4a5211d810a913f947861bc2#file-pre-release_changes-md)
- [Pre-Release TODO](https://gist.github.com/I-am-PUID-0/7e02c2cb4a5211d810a913f947861bc2#file-pre-release_todo-md)

### Development support:

- The repo contains a devcontainer for use with vscode.
- Bind mounts will need to be populated with content from this repo

## 🚀 Deployment

DMB allows for the simultaneous or individual deployment of any of the services

For additional details on deployment, see the [DMB Docs](https://i-am-puid-0.github.io/DMB/services/)

## 🌍 Community

### DMB

- For questions related to DMB, see the GitHub [discussions](https://github.com/I-am-PUID-0/DMB/discussions)
- or create a new [issue](https://github.com/I-am-PUID-0/DMB/issues) if you find a bug or have an idea for an improvement.
- or join the DMB [discord server](https://discord.gg/8dqKUBtbp5)

### Riven Media

- For questions related to Riven, see the GitHub [discussions](https://github.com/orgs/rivenmedia/discussions)
- or create a new [issue](https://github.com/rivenmedia/riven/issues) if you find a bug or have an idea for an improvement.
- or join the Riven [discord server](https://discord.gg/VtYd42mxgb)

### plex_debrid
- For questions related to plex_debrid, see the GitHub [discussions](https://github.com/itsToggle/plex_debrid/discussions) 
- or create a new [issue](https://github.com/itsToggle/plex_debrid/issues) if you find a bug or have an idea for an improvement.
- or join the plex_debrid [discord server](https://discord.gg/u3vTDGjeKE) 

### cli_debrid & phalanx_db
- For questions related to cli_debrid or phalanx_db, join the cli_debrid [discord server](https://discord.gg/jAmqZJCZJ4) 
- or create a new [issue](https://github.com/godver3/cli_debrid/issues) if you find a bug or have an idea for an improvement. 

### Decypharr
- For questions related to decypharr, check out the [Docs](https://sirrobot01.github.io/decypharr/) 
- or create a new [issue](https://github.com/sirrobot01/decypharr/issues) if you find a bug or have an idea for an improvement. 


## 🍻 Buy **[Riven Media](https://github.com/rivenmedia)** a beer/coffee? :)

If you enjoy the underlying projects and want to buy Riven Media a beer/coffee, feel free to use the [GitHub sponsor link](https://github.com/sponsors/dreulavelle/)

## 🍻 Buy **[itsToggle](https://github.com/itsToggle)** a beer/coffee? :)

If you enjoy the underlying projects and want to buy itsToggle a beer/coffee, feel free to use the real-debrid [affiliate link](http://real-debrid.com/?id=5708990) or send a virtual beverage via [PayPal](https://www.paypal.com/paypalme/oidulibbe) :)

## 🍻 Buy **[godver3](https://github.com/godver3)** a beer/coffee? :)

If you enjoy the underlying projects and want to buy godver3 a beer/coffee, feel free to use the [GitHub sponsor link](https://github.com/sponsors/godver3)

## 🍻 Buy **[Mukhtar Akere](https://github.com/sirrobot01)** a beer/coffee? :)

If you enjoy the underlying projects and want to buy Mukhtar Akere a beer/coffee, feel free to use the [GitHub sponsor link](https://github.com/sponsors/sirrobot01)

## 🍻 Buy **[yowmamasita](https://github.com/yowmamasita)** a beer/coffee? :)

If you enjoy the underlying projects and want to buy yowmamasita a beer/coffee, feel free to use the [GitHub sponsor link](https://github.com/sponsors/debridmediamanager)

## 🍻 Buy **[Nick Craig-Wood](https://github.com/ncw)** a beer/coffee? :)

If you enjoy the underlying projects and want to buy Nick Craig-Wood a beer/coffee, feel free to use the website's [sponsor links](https://rclone.org/sponsor/)

## 🍻 Buy **[PostgreSQL](https://www.postgresql.org)** a beer/coffee? :)

If you enjoy the underlying projects and want to buy PostgreSQL a beer/coffee, feel free to use the [sponsor link](https://www.postgresql.org/about/donate/)

## ✅ GitHub Workflow Status

![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/I-am-PUID-0/DMB/docker-image.yml)
