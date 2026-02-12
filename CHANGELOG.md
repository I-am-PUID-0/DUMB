# Changelog

## [2.2.0](https://github.com/I-am-PUID-0/DUMB/compare/2.1.0...2.2.0) (2026-02-12)


### ✨ Features

* adds frontend build fingerprinting to Decypharr ([d5d923f](https://github.com/I-am-PUID-0/DUMB/commit/d5d923f9f4633ae67f1a54a42c610c53cc9b1563))
* **api:** adds a new endpoint for visualizing service dependencies ([dcc4ab9](https://github.com/I-am-PUID-0/DUMB/commit/dcc4ab93c57970f729b95be569715c026703d6de))
* **auto-update:** Introduce auto-update start time ([b9133a5](https://github.com/I-am-PUID-0/DUMB/commit/b9133a54f8d5749f8734cc92c96b1cc15faa5d64))
* **decypharr:** Introduces mount type selection and DFS support ([f18886b](https://github.com/I-am-PUID-0/DUMB/commit/f18886b6da8b47079e64e323cae70c91086feb8f))
* **dependency_graph:** Implements conditional dependency mapping ([f32e35a](https://github.com/I-am-PUID-0/DUMB/commit/f32e35a54c5cf352f7f6328ed41aae20c9e7a6d7))
* **geek mode:** expands geek mode and process start time ([9910c8e](https://github.com/I-am-PUID-0/DUMB/commit/9910c8edbc2dfb6c4301bfb6c862d49f675ba635))
* **profilarr:** adds core_service support for Profilarr ([d05eb20](https://github.com/I-am-PUID-0/DUMB/commit/d05eb204c9f82a7553100defdb42e7696a89e556))
* **profilarr:** adds Profilarr service ([99b91a6](https://github.com/I-am-PUID-0/DUMB/commit/99b91a6ba10887fde3d4cf1ebcbe3f0f0b60df40))
* **symlinks:** adds symlink manifest management endpoints and enhances the symlink utility ([644a67b](https://github.com/I-am-PUID-0/DUMB/commit/644a67bd3d6da823cfd0890459d344907583fde9))
* **symlinks:** adds symlink manifest preview endpoint ([451e04e](https://github.com/I-am-PUID-0/DUMB/commit/451e04ee3a8ab3ddb244ac27f4d734d770b35cf9))
* **symlinks:** alpha (do not use yet) implementation of scheduled symlink backups and repair endpoints ([4656a40](https://github.com/I-am-PUID-0/DUMB/commit/4656a404abac239a8dd5050e35d480f49bce5df6))
* **symlinks:** Implements asynchronous symlink repair endpoint ([3f09aea](https://github.com/I-am-PUID-0/DUMB/commit/3f09aea7608f123bec3b6b1af5652677348aada6))
* **ui:** adds sidebar config options ([73b7f9e](https://github.com/I-am-PUID-0/DUMB/commit/73b7f9e637c573c39ba6fefdff00aa7695a81a4e))


### 🐛 Bug Fixes

* **decypharr:** consolidate Decypharr mount path logic and prioritize explicit configuration. ([c3ec1ab](https://github.com/I-am-PUID-0/DUMB/commit/c3ec1ab87bca323def44af4d5afd73d728a2b5b6))
* **decypharr:** enhance mount path handling in configuration for beta consolidated mounts ([ea9ee13](https://github.com/I-am-PUID-0/DUMB/commit/ea9ee13381b58d080206c562f3d831f638fa8f4f))
* **process:** prevent duplicate setup during service restart by delegating to updater ([b6a73c3](https://github.com/I-am-PUID-0/DUMB/commit/b6a73c3c4296d480cab01138b41314ed968b8e92))


### 🤡 Other Changes

* **deps:** updates poetry.lock with the latest dependency versions ([1f33a87](https://github.com/I-am-PUID-0/DUMB/commit/1f33a879e58e012ed5b2af2c01b9ae0a3d93b74a))

## [2.1.0](https://github.com/I-am-PUID-0/DUMB/compare/2.0.0...2.1.0) (2026-02-04)


### ✨ Features

* **announce-dev:** add Discord announcement for dev branch updates ([fb6e3c0](https://github.com/I-am-PUID-0/DUMB/commit/fb6e3c066ac56120249687068d541eecaebd26bb))
* **arrs:** enable GitHub releases for Arr services ([8e720f5](https://github.com/I-am-PUID-0/DUMB/commit/8e720f56440764588ccc6772206ded65b28386f4))
* **auto_update:** Adds manual update functionality ([fd58a71](https://github.com/I-am-PUID-0/DUMB/commit/fd58a71696b038361793a1b4249b8f213d467ded))
* **auto-update:** add force update check option to auto_update method ([fbfce8f](https://github.com/I-am-PUID-0/DUMB/commit/fbfce8fa75ed6716cabcaf15882538ed7e6d0c4f))
* **connection_manager:** initialize event loop if closed during websocket connection ([16a8879](https://github.com/I-am-PUID-0/DUMB/commit/16a88795e2bae492cd1d87e7faff335285f9f79d))
* **connection_manager:** initialize event loop if closed during websocket connection ([71ff724](https://github.com/I-am-PUID-0/DUMB/commit/71ff7244f4d6f433da5ba065b944989bfc2071d9))
* **dev:** Implements dev branch versioning and tagging ([9fddaa2](https://github.com/I-am-PUID-0/DUMB/commit/9fddaa248d1f92a94a72971c55bf6192865ad199))
* Enhances commit message with links to GitHub ([1d4467a](https://github.com/I-am-PUID-0/DUMB/commit/1d4467aefd3db669ea3c3ac2faec12feffc1bb75))
* **process:** add seerr_sync capability to capabilities response ([cbb2c0e](https://github.com/I-am-PUID-0/DUMB/commit/cbb2c0e76317eac273ab79b07deb5a2867b5486f))
* **seerr_sync:** adds Seerr sync service ([c525a1b](https://github.com/I-am-PUID-0/DUMB/commit/c525a1b04112f24a983470dd32d2a8f7c01c1369))
* **seerr_sync:** adds Seerr sync test endpoint ([059c884](https://github.com/I-am-PUID-0/DUMB/commit/059c884c0494bb42cb1d9fd27522212941f3d5dc))
* **setup:** create backup directory and patch backup_routes.py for Huntarr ([fbfce8f](https://github.com/I-am-PUID-0/DUMB/commit/fbfce8fa75ed6716cabcaf15882538ed7e6d0c4f))


### 🐛 Bug Fixes

* **clid:** sets the CLI debrid and battery ports to their correct config values. ([fb6db21](https://github.com/I-am-PUID-0/DUMB/commit/fb6db2157a58168fd88daf96043742f2c08bc960))
* **dependencies:** remove websocket close calls on authentication errors ([16a8879](https://github.com/I-am-PUID-0/DUMB/commit/16a88795e2bae492cd1d87e7faff335285f9f79d))
* **dependencies:** remove websocket close calls on authentication errors ([71ff724](https://github.com/I-am-PUID-0/DUMB/commit/71ff7244f4d6f433da5ba065b944989bfc2071d9))
* **logger:** streamline websocket logging with existing event loop ([16a8879](https://github.com/I-am-PUID-0/DUMB/commit/16a88795e2bae492cd1d87e7faff335285f9f79d))
* **logger:** streamline websocket logging with existing event loop ([71ff724](https://github.com/I-am-PUID-0/DUMB/commit/71ff7244f4d6f433da5ba065b944989bfc2071d9))
* **processes:** treat quick success exits as non-errors ([b87c788](https://github.com/I-am-PUID-0/DUMB/commit/b87c78821ebf6c5c6eb2ebefaf96000ddd0b0dfa))
* **setup:** save configuration after project setup for dumb frontend ([aa26910](https://github.com/I-am-PUID-0/DUMB/commit/aa269108bad976b542097075221a7e26c85814e2))
* **traefik:** disable version check in global configuration ([68f2a3b](https://github.com/I-am-PUID-0/DUMB/commit/68f2a3bf8816ed39f668643777efd9762ace094c))
* **zilean:** updates Zilean base URL handling and configuration ([b2dd794](https://github.com/I-am-PUID-0/DUMB/commit/b2dd794c28729fc8b0b652343e2a738b540b007b))


### 🤡 Other Changes

* **announce-dev:** update announcement message to use a wrench emoji for dev branch updates ([8727a60](https://github.com/I-am-PUID-0/DUMB/commit/8727a60f1f7f4ff59f9405a13f980f7848a86035))
* **deps:** updates dependencies ([8ae9383](https://github.com/I-am-PUID-0/DUMB/commit/8ae9383662c127740e10afe38f293a13a7eadd83))
* **docs:** update documentation links for NzbDAV, Seerr, and Huntarr services ([19b0a51](https://github.com/I-am-PUID-0/DUMB/commit/19b0a5164445976c498330cabcafc1f012d1fd89))
* **docs:** update documentation links to point to new domain ([1901698](https://github.com/I-am-PUID-0/DUMB/commit/19016986bc97d4cce1b9939a391e3d2c2e2449db))


### 🛠️ Refactors

* **nzbdav:** use backend port for rclone WebDAV mount ([c275c71](https://github.com/I-am-PUID-0/DUMB/commit/c275c7118267892244c46780a80384b102ae3b23))

## [2.0.0](https://github.com/I-am-PUID-0/DUMB/compare/1.7.0...2.0.0) (2026-01-26)


### ⚠ BREAKING CHANGES

* **docker-image:** remove riven frontend and backend builder stages from Dockerfile

### ✨ Features

* add in-process ffprobe monitor for Sonarr/Radarr hangs ([0ff51a8](https://github.com/I-am-PUID-0/DUMB/commit/0ff51a850b54dac1b2f4dfd8044fde2497c303af))
* **api process:** add capabilities endpoint to retrieve optional service options ([aab889d](https://github.com/I-am-PUID-0/DUMB/commit/aab889d847cac45d9afa0464fa8a16b529dfefb7))
* **api process:** add support for optional service configurations in UnifiedStartRequest ([6c0308c](https://github.com/I-am-PUID-0/DUMB/commit/6c0308c7fa2dac33459633e5dde0a536bc7ee9b5))
* **api:** add size attribute to log file response ([7696c22](https://github.com/I-am-PUID-0/DUMB/commit/7696c22836c7a3c4aaeaceba1abd7d42318b0546))
* **arr:** implement pinned version support and update logic for ArrInstaller ([f60c1bd](https://github.com/I-am-PUID-0/DUMB/commit/f60c1bda1fa5605dc185e6677af1983b0f201cfb))
* **auth:** implement JWT authentication with user management ([7596137](https://github.com/I-am-PUID-0/DUMB/commit/75961373b2ad405f046be0d78a20f95f1607927b))
* **auto_update:** add support for Jellyfin and Emby updates with pinned versions ([f60c1bd](https://github.com/I-am-PUID-0/DUMB/commit/f60c1bda1fa5605dc185e6677af1983b0f201cfb))
* **auto-restart:** implement auto-restart configuration and monitoring for processes ([a40e8e7](https://github.com/I-am-PUID-0/DUMB/commit/a40e8e7fabe5f7260020c174a0fa08344fcb432e)), closes [#57](https://github.com/I-am-PUID-0/DUMB/issues/57)
* **auto-update:** enhance initial update check and preinstall handling ([39ab554](https://github.com/I-am-PUID-0/DUMB/commit/39ab5549def996da872f3d1b220f2782afdec3d9))
* **config:** add support for INI configuration file parsing and writing ([50d8b2d](https://github.com/I-am-PUID-0/DUMB/commit/50d8b2de42befa085cb6703cdea1f31d6e18ddb3))
* **config:** add UI log timestamp configuration options ([0c86aec](https://github.com/I-am-PUID-0/DUMB/commit/0c86aec19145a8b3858a5a20e6df423835806878))
* **core-services:** add Zilean sponsorship URL and enhance core service handling in wait entries ([6ba1299](https://github.com/I-am-PUID-0/DUMB/commit/6ba12994129f20aeeeaf43a38cc63e7d69208d09))
* **core-services:** support multi-core arr instances and conditional combined roots ([27cea3d](https://github.com/I-am-PUID-0/DUMB/commit/27cea3ddf0cc6c0c45edd487c4aaa8d60e5960a5))
* **dbrepair:** implement Plex DBRepair functionality and configuration ([6ca1f67](https://github.com/I-am-PUID-0/DUMB/commit/6ca1f672788d5d37fe52582761f41ca8f67d484e)), closes [#97](https://github.com/I-am-PUID-0/DUMB/issues/97)
* **emby:** add Emby Media Server port configuration ([ad2340e](https://github.com/I-am-PUID-0/DUMB/commit/ad2340e9fb90e3bd47380d9f92ccbd3a71d5a929))
* **emby:** add use_system_ffmpeg option and relink binaries ([7f35c9e](https://github.com/I-am-PUID-0/DUMB/commit/7f35c9e017f68c4113663dd4ebcda8e2cf8e9f31))
* **healthcheck:** adds process port verification ([7be53a0](https://github.com/I-am-PUID-0/DUMB/commit/7be53a08ce7f718d9524bb96a164907e02e50681))
* **jellyfin:** add Jellyfin Media Server port configuration ([ad2340e](https://github.com/I-am-PUID-0/DUMB/commit/ad2340e9fb90e3bd47380d9f92ccbd3a71d5a929))
* **logger:** Adds subprocess logging to files ([51de776](https://github.com/I-am-PUID-0/DUMB/commit/51de776f02fc67fff99b578b5faa3f3f1916d5aa))
* **logging:** add log file configuration for various services and implement access logging ([80f466c](https://github.com/I-am-PUID-0/DUMB/commit/80f466cc4d0ccaf0c8b88cd9c390acefa4f4a397))
* **logging:** keep file logging when suppressing console/websocket ([218fa2c](https://github.com/I-am-PUID-0/DUMB/commit/218fa2cd63d5eca09009289f7d4fd249a8d374e8))
* **logs:** add Traefik log file discovery ([ad2340e](https://github.com/I-am-PUID-0/DUMB/commit/ad2340e9fb90e3bd47380d9f92ccbd3a71d5a929))
* **metrics:** add history series endpoint and enhance metrics collection ([6f78400](https://github.com/I-am-PUID-0/DUMB/commit/6f7840033e13ea9199ff9db76372e19a8788d3ca))
* **metrics:** add process connections collection to MetricsCollector ([629a853](https://github.com/I-am-PUID-0/DUMB/commit/629a8535815545119e48146180fc54154a10de6e))
* **metrics:** Extends metrics history management ([01bd593](https://github.com/I-am-PUID-0/DUMB/commit/01bd593356ded720d8fb82de1a2d7a7c768649f8))
* **metrics:** implement metrics collection and history tracking ([dff26de](https://github.com/I-am-PUID-0/DUMB/commit/dff26de5066036375009edd8ea105b3aa53971e5))
* **nzbdav:** add core service support, config, and setup flow ([20b9595](https://github.com/I-am-PUID-0/DUMB/commit/20b9595add59a2da0bc2469888b1fa13060452c8))
* **plex:** add installation logic for Plex Media Server if not found during auto_update ([6364281](https://github.com/I-am-PUID-0/DUMB/commit/6364281b0bc73dc35212d73a050bd53332e6c96f))
* **plex:** add support for pinned version in Plex installation and update checks ([cf29b7e](https://github.com/I-am-PUID-0/DUMB/commit/cf29b7e657c4742bf3c6db57b29d43f7fa96be73)), closes [#15](https://github.com/I-am-PUID-0/DUMB/issues/15)
* **port-management:** implement comprehensive port availability checking ([ad2340e](https://github.com/I-am-PUID-0/DUMB/commit/ad2340e9fb90e3bd47380d9f92ccbd3a71d5a929))
* **port-management:** implement global port reservation and configuration handling for NzbDAV ([827f1c8](https://github.com/I-am-PUID-0/DUMB/commit/827f1c83794b8ed8a8147aee37dd7f457d6456c4))
* **processes:** prioritize stopping media servers before other processes ([2fdc98b](https://github.com/I-am-PUID-0/DUMB/commit/2fdc98b0b7e457ffd7cd750cb850eb618c7c0081))
* **prowlarr:** auto-sync Arr applications on service lifecycle ([20b9595](https://github.com/I-am-PUID-0/DUMB/commit/20b9595add59a2da0bc2469888b1fa13060452c8))
* **seerr:** add Seerr support and configuration options ([336cbec](https://github.com/I-am-PUID-0/DUMB/commit/336cbece18145c655c4049055c78a694879fb1b6))
* **service-ui:** add endpoint to retrieve service UI mapping ([a52f4c6](https://github.com/I-am-PUID-0/DUMB/commit/a52f4c664e6d93e86470fcba0f233d39ab2e57f9))
* **setup:** add riven bootstrap and plexapi dependency management to patch riven backend v0.23.6 pinned dep issue ([2bd5fd1](https://github.com/I-am-PUID-0/DUMB/commit/2bd5fd16eefa2664d57639da07ffbda3081ee735))
* **setup:** Adds rclone unmount to decypharr ([5b4114f](https://github.com/I-am-PUID-0/DUMB/commit/5b4114fbc517385fb94240db8f92d7795a63da9c))
* **setup:** implement project setup with preinstall support and locking mechanism ([8c62590](https://github.com/I-am-PUID-0/DUMB/commit/8c625908bbb47cffe5d6717432167ab108cfb23f))
* **startup:** add dependency-aware orchestration and Huntarr support ([b04b9cf](https://github.com/I-am-PUID-0/DUMB/commit/b04b9cfe37513081b979769e3760ed525ad4e9de))
* **startup:** add dependency-aware parallel startup and shutdown-safe waits ([a382f60](https://github.com/I-am-PUID-0/DUMB/commit/a382f60fad1bb01c53c44cecc1796aaa9cc6e167))
* **status:** add websocket status endpoint and improve process health checks ([02a4d60](https://github.com/I-am-PUID-0/DUMB/commit/02a4d60279f9c9077c1ebfa155ad2918eb765f30))
* **tautulli:** add Tautulli support and configuration options ([2d61f27](https://github.com/I-am-PUID-0/DUMB/commit/2d61f270a29dee92cdd0ea4a27b6ad4d95767f44)), closes [#16](https://github.com/I-am-PUID-0/DUMB/issues/16)
* **traefik:** add Traefik reverse proxy integration for service UIs ([ad2340e](https://github.com/I-am-PUID-0/DUMB/commit/ad2340e9fb90e3bd47380d9f92ccbd3a71d5a929))
* **traefik:** enhance service name sanitization for URL safety ([daa2dda](https://github.com/I-am-PUID-0/DUMB/commit/daa2dda76ada20c3931807bcdb641c2d39d722e6))


### 🐛 Bug Fixes

* **api:** correct OpenAPI URL reference in Scalar docs ([ad2340e](https://github.com/I-am-PUID-0/DUMB/commit/ad2340e9fb90e3bd47380d9f92ccbd3a71d5a929))
* **arr:** add force option to install method to skip installation if binary exists ([f613120](https://github.com/I-am-PUID-0/DUMB/commit/f6131208b4c2a8b191490dcd99aacd1a59ac5366))
* **arr:** improve error handling and extraction process in install method ([647c3d5](https://github.com/I-am-PUID-0/DUMB/commit/647c3d5018bfb28835fa243d4857ddcc2a1b0467))
* **cli_debrid:** chown utilities on startup and during setup ([420eefb](https://github.com/I-am-PUID-0/DUMB/commit/420eefb516dc92453b2a99b4150faff117b3b889))
* **config:** enhance XML config handling in save_config_file and write_rclone_config functions ([90a4554](https://github.com/I-am-PUID-0/DUMB/commit/90a4554e9fceb23ceba58e7b95018d003fe0f01a))
* **config:** improves arr, decypharr, and nzbdav configuration handling ([2ad20b0](https://github.com/I-am-PUID-0/DUMB/commit/2ad20b0c9a78df250426fa1e1f43f17745735914))
* **dbrepair:** defer first scheduled run by configured interval and log next run time ([7579908](https://github.com/I-am-PUID-0/DUMB/commit/7579908b2e550b35898fe2faf42244d16106a359))
* **decypharr:** update arrs handling in patch_decypharr_config to use desired_arrs - avoiding external arrs ([2a65e2d](https://github.com/I-am-PUID-0/DUMB/commit/2a65e2dcebaee496cef6b251d3788950040e9650))
* **deps:** update psutil to version 7.2.0 and adjust dependencies ([c43b45b](https://github.com/I-am-PUID-0/DUMB/commit/c43b45b63eb99f33c5e6e783e13bc37af31b8653))
* **devcontainer:** change default terminal profile to bash and update postCreateCommand for poetry installation ([5ea1521](https://github.com/I-am-PUID-0/DUMB/commit/5ea1521b90f41efbff4741182808e0cd1e38f759))
* **docker:** update dependencies in Dockerfile for improved compatibility ([d05a990](https://github.com/I-am-PUID-0/DUMB/commit/d05a990662cc898e5135b2833d7804882369aa0d)), closes [#98](https://github.com/I-am-PUID-0/DUMB/issues/98)
* **jellyfin:** allow installation of specific Jellyfin versions ([f60c1bd](https://github.com/I-am-PUID-0/DUMB/commit/f60c1bda1fa5605dc185e6677af1983b0f201cfb))
* **main:** start media server(s) after other services ([9a858a4](https://github.com/I-am-PUID-0/DUMB/commit/9a858a47f9426614a5ec0643a2134539043fe84c))
* **nzbdav:** add chown_single call for parent directory in ensure_symlink_roots ([8db318b](https://github.com/I-am-PUID-0/DUMB/commit/8db318baa4265c045f512e81d5b3d756181d5c60))
* **nzbdav:** enhance chown_single logic with error handling and logging ([629a853](https://github.com/I-am-PUID-0/DUMB/commit/629a8535815545119e48146180fc54154a10de6e))
* **poetry.lock:** update ruamel.yaml version to 0.18.17 and adjust python version constraints ([5ea1521](https://github.com/I-am-PUID-0/DUMB/commit/5ea1521b90f41efbff4741182808e0cd1e38f759))
* **processes:** improve error handling during process startup and logging ([cd544b1](https://github.com/I-am-PUID-0/DUMB/commit/cd544b1a58ab4e85e7bb5f6d8579d14c83d5ea03))
* **processes:** update core_service description for clarity ([fdda589](https://github.com/I-am-PUID-0/DUMB/commit/fdda58958372b8f18cb549eac0d3bdcf2d11fb12))
* **processes:** update stdout and stderr handling in start_process method for suppress_logging ([3bd3777](https://github.com/I-am-PUID-0/DUMB/commit/3bd37770eabb0b27c8ac20aff95a44610374a77b))
* **rclone:** add default flags to rclone setup for missing parsed flags ([395557e](https://github.com/I-am-PUID-0/DUMB/commit/395557e8521502521112d9b1cd98009789fe9601))
* **setup:** conditionally change ownership of Plex config directory ([9a858a4](https://github.com/I-am-PUID-0/DUMB/commit/9a858a47f9426614a5ec0643a2134539043fe84c))
* **setup:** enhance version handling logic for release and branch setups ([0818bf0](https://github.com/I-am-PUID-0/DUMB/commit/0818bf034d977ef8ace78f9ea243aaf5ce515280)), closes [#101](https://github.com/I-am-PUID-0/DUMB/issues/101)


### 🤡 Other Changes

* **config:** add pinned_version field to configuration files and schema ([f60c1bd](https://github.com/I-am-PUID-0/DUMB/commit/f60c1bda1fa5605dc185e6677af1983b0f201cfb))
* **config:** update Plex DBRepair default interval to weekly ([ad2340e](https://github.com/I-am-PUID-0/DUMB/commit/ad2340e9fb90e3bd47380d9f92ccbd3a71d5a929))
* **deps:** update fastapi to version 0.127.0 and uvicorn to version 0.40.0 ([d05e2ed](https://github.com/I-am-PUID-0/DUMB/commit/d05e2ed61c6bf2ffa86f08ed7cae9e65ed1ddac7))
* **nzbdav:** update service description ([ad2340e](https://github.com/I-am-PUID-0/DUMB/commit/ad2340e9fb90e3bd47380d9f92ccbd3a71d5a929))


### 🛠️ Refactors

* **docker-image:** comment out riven-backend-builder job and update dependencies in digest builds ([0750a71](https://github.com/I-am-PUID-0/DUMB/commit/0750a71528addbc7dd5e2abdea2319942edad454))
* **docker-image:** remove riven frontend and backend builder stages from Dockerfile ([bfbbb7d](https://github.com/I-am-PUID-0/DUMB/commit/bfbbb7d256b54d11779fc420c54a45eca7418611))
* **Dockerfile:** comment out riven-backend-builder stage for future review ([503a739](https://github.com/I-am-PUID-0/DUMB/commit/503a73943304f689a9690d9e024a57a40ff4ad2c))
* **setup.py:** remove unused import of 'key' from tomlkit ([5ea1521](https://github.com/I-am-PUID-0/DUMB/commit/5ea1521b90f41efbff4741182808e0cd1e38f759))
* **setup:** harden environment and rclone setup ([20b9595](https://github.com/I-am-PUID-0/DUMB/commit/20b9595add59a2da0bc2469888b1fa13060452c8)), closes [#56](https://github.com/I-am-PUID-0/DUMB/issues/56)
* **startup:** split install/config phases; add arr per-instance installs for pinned versions ([0080e29](https://github.com/I-am-PUID-0/DUMB/commit/0080e29263e1a2ee49c1644b1246855d7cf115fb))


### 🛠️ Build System

* **deps:** Updates dependencies ([75a47cf](https://github.com/I-am-PUID-0/DUMB/commit/75a47cfebc111a24852a2f28ec52ac2525c23c74))

## [1.7.0](https://github.com/I-am-PUID-0/DUMB/compare/1.6.0...1.7.0) (2025-12-18)


### ✨ Features

* **api:** add config schema endpoints ([30065fb](https://github.com/I-am-PUID-0/DUMB/commit/30065fb89299e674d308e5f524ffdae5111b799d))
* **api:** add sponsorship urls for processes ([99373fc](https://github.com/I-am-PUID-0/DUMB/commit/99373fc847311f487b6c346085f2510bfe534bf2))
* **api:** adds cursor-based pagination for logs endpoint ([3339e66](https://github.com/I-am-PUID-0/DUMB/commit/3339e66d111ebb795af35be829ad22e8b0da0874))
* **api:** enhances the start-core-service endpoint to support instance based core services ([99373fc](https://github.com/I-am-PUID-0/DUMB/commit/99373fc847311f487b6c346085f2510bfe534bf2))
* **config:** add logic to prune extraneous keys from config files during updates ([fdc063d](https://github.com/I-am-PUID-0/DUMB/commit/fdc063dfbef746e85df45e706fc637cc5d58865c))
* **core:** Add Jellyfin media server support ([e055a10](https://github.com/I-am-PUID-0/DUMB/commit/e055a10b23a78f8efcef214c447b2671d32f5b53)), closes [#13](https://github.com/I-am-PUID-0/DUMB/issues/13)
* **core:** add support for multi-instance debrid ([fdc063d](https://github.com/I-am-PUID-0/DUMB/commit/fdc063dfbef746e85df45e706fc637cc5d58865c))
* **core:** adds Emby, Sonarr, Radarr, Lidarr, Prowlarr, and Whisparr support ([99373fc](https://github.com/I-am-PUID-0/DUMB/commit/99373fc847311f487b6c346085f2510bfe534bf2))
* **decipher:** Improve Decypharr config ([8211f7f](https://github.com/I-am-PUID-0/DUMB/commit/8211f7f49d644f1bc3f7e38382997a7295df0461))
* **decypharr:** adds support for decypharr rclone mounts ([99373fc](https://github.com/I-am-PUID-0/DUMB/commit/99373fc847311f487b6c346085f2510bfe534bf2))
* **decypharr:** enhance Decypharr configuration ([f404d33](https://github.com/I-am-PUID-0/DUMB/commit/f404d335ae57acdbffb6d30aa2833ab5c8fb3dc4))
* **decypharr:** enhance Decypharr setup ([bb074fb](https://github.com/I-am-PUID-0/DUMB/commit/bb074fbbc163524f740e0f5d2edd6e4a3331b78b))
* **download:** add support for deb packages ([99373fc](https://github.com/I-am-PUID-0/DUMB/commit/99373fc847311f487b6c346085f2510bfe534bf2))


### 🐛 Bug Fixes

* **arrs:** Updates InstanceName in arr config ([f427cfd](https://github.com/I-am-PUID-0/DUMB/commit/f427cfd1bd9da559a422050edff5b56e0f6b74de))
* **ci:** add jq installation step and improve cache pruning logic ([e13a2cf](https://github.com/I-am-PUID-0/DUMB/commit/e13a2cf5a49e0186ef9eaf4d54924d12331138d7))
* **ci:** correct syntax error in cleanup-cache job ([4bfa6c2](https://github.com/I-am-PUID-0/DUMB/commit/4bfa6c2d9ccd8855e271f3b250a9140a42aa0a36))
* **ci:** enhance cache pruning logic to manage manifest retention ([3ac894b](https://github.com/I-am-PUID-0/DUMB/commit/3ac894b798ce9ea040a9441974c8592962ed0fdc))
* **ci:** enhance cleanup-cache job with new parameters and improved pruning logic ([9b51c7b](https://github.com/I-am-PUID-0/DUMB/commit/9b51c7bc3557b28f0f8cc5528173fc6eb44cc96b))
* **ci:** enhance outputs in build jobs with OCI media types and compression options ([53f9351](https://github.com/I-am-PUID-0/DUMB/commit/53f93515db8245b8d7a9ea4e0d805e7ddf8de22e))
* **ci:** improve error handling and manifest processing in cleanup-cache job ([7c2da17](https://github.com/I-am-PUID-0/DUMB/commit/7c2da1761eb276342af4cf786135fb250b06e27f))
* **config:** eliminate unsafe exec() from python config handling ([99373fc](https://github.com/I-am-PUID-0/DUMB/commit/99373fc847311f487b6c346085f2510bfe534bf2))
* **core:** add ToS for Plex Media Server and Emby ([99373fc](https://github.com/I-am-PUID-0/DUMB/commit/99373fc847311f487b6c346085f2510bfe534bf2))
* **dumb_config:** set use_embedded_rclone false as default ([20465f6](https://github.com/I-am-PUID-0/DUMB/commit/20465f6e10b9107101db298dc2a863ef904396d5))
* **dumb_config:** set use_embedded_rclone true as default ([8f3b610](https://github.com/I-am-PUID-0/DUMB/commit/8f3b6105d1be40ca2ea118f8c9ae3eac3a484765))
* **rclone:** sets the key_type for the rclone config ([ca2893f](https://github.com/I-am-PUID-0/DUMB/commit/ca2893fb465cf2862fde521b3b63bf39dc79fa8f))


### 🤡 Other Changes

* Fixes typo in description ([7720489](https://github.com/I-am-PUID-0/DUMB/commit/7720489a331c14856961886487dbe9c605b52001))


### 📖 Documentation

* **api:** Updates rclone description ([6665952](https://github.com/I-am-PUID-0/DUMB/commit/666595294c1b33d53aff267291b12dbf1636974c))
* update docker-compose.yml ([d47e632](https://github.com/I-am-PUID-0/DUMB/commit/d47e6326512a189c2d7bdefe826dc91bd4bcdd6c))


### 🚀 CI/CD Pipeline

* **docker:** Refactor Docker build workflow ([d2aafc2](https://github.com/I-am-PUID-0/DUMB/commit/d2aafc2b3065f078c2545297f6e97d00c477fc7f))
* **workflow:** fix cleanup-cache job in docker-image.yml ([4294668](https://github.com/I-am-PUID-0/DUMB/commit/42946681dff53312f3e843478be0b2fa86585e72))


### 🛠️ Refactors

* **core:** enhance service startup logic ([18f24a6](https://github.com/I-am-PUID-0/DUMB/commit/18f24a66f79b1b285e5e6f14ee964467f21d43b9))
* **decypharr:** Improve provider folder mapping and fix embedded rclone mounts ([188a86e](https://github.com/I-am-PUID-0/DUMB/commit/188a86e9112059a6f380ca2284492b12ca75a6e4))
* **main:** simplify service configuration ([204b7f7](https://github.com/I-am-PUID-0/DUMB/commit/204b7f7e57c50b8005bcab920bc375e5c178841c))


### 🛠️ Build System

* **deps:** Update dependencies ([0f4fab0](https://github.com/I-am-PUID-0/DUMB/commit/0f4fab0f81c549d7a5a0eda2a42118830191d79a))
* **deps:** update dependencies in poetry.lock ([5b8af2c](https://github.com/I-am-PUID-0/DUMB/commit/5b8af2ce718e9931a931c46a896c9ffcac794cdd))

## [1.6.0](https://github.com/I-am-PUID-0/DUMB/compare/1.5.0...1.6.0) (2025-07-31)


### ✨ Features

* **config:** add data_root to dumb_config and schema; enhance symlink migration logic ([33dbadb](https://github.com/I-am-PUID-0/DUMB/commit/33dbadb5a37769df9bc580cb2078db40ee47b09e))

## [1.5.0](https://github.com/I-am-PUID-0/DUMB/compare/1.4.3...1.5.0) (2025-07-31)


### ✨ Features

* **bind-mounts:** consolidates bind mounts ([39f30e4](https://github.com/I-am-PUID-0/DUMB/commit/39f30e4c894195f30d35c5f63908d9b8f088a6c7))
* **migrate_and_symlink:** enable symlink support in data migration ([d1f5ee3](https://github.com/I-am-PUID-0/DUMB/commit/d1f5ee301b850a7c624f4f595e08eb3296cb59fa))

## [1.4.3](https://github.com/I-am-PUID-0/DUMB/compare/1.4.2...1.4.3) (2025-07-28)


### 🐛 Bug Fixes

* **docs:** update service documentation URLs to reflect new structure ([3ef7915](https://github.com/I-am-PUID-0/DUMB/commit/3ef791525b070346620966195cb65655365f04b5))


### 🛠️ Refactors

* **process:** refactor service start/stop/restart ([22c267d](https://github.com/I-am-PUID-0/DUMB/commit/22c267ddbcbd182194ddf6301cc961f3cd5cd7ce))

## [1.4.2](https://github.com/I-am-PUID-0/DUMB/compare/1.4.1...1.4.2) (2025-07-25)


### 🐛 Bug Fixes

* **config:** fixes null strings in config ([86fb9bc](https://github.com/I-am-PUID-0/DUMB/commit/86fb9bc6883c29f1e1a38c5a658454e09b78177e)), closes [#40](https://github.com/I-am-PUID-0/DUMB/issues/40)

## [1.4.1](https://github.com/I-am-PUID-0/DUMB/compare/1.4.0...1.4.1) (2025-07-23)


### 🐛 Bug Fixes

* **decypharr:** correct return value when config file is not found ([4ec1b68](https://github.com/I-am-PUID-0/DUMB/commit/4ec1b68dbd8ce768ac39d7663996fcbc8a3523fd))

## [1.4.0](https://github.com/I-am-PUID-0/DUMB/compare/1.3.2...1.4.0) (2025-07-23)


### ✨ Features

* **api:** expand API functionality ([f081132](https://github.com/I-am-PUID-0/DUMB/commit/f081132a134d47fe5f689c882d2d8bd9988028d6))
* **config:** add origin field to config ([f081132](https://github.com/I-am-PUID-0/DUMB/commit/f081132a134d47fe5f689c882d2d8bd9988028d6))
* **config:** enhance service management ([f081132](https://github.com/I-am-PUID-0/DUMB/commit/f081132a134d47fe5f689c882d2d8bd9988028d6))


### 🐛 Bug Fixes

* **decypharr:** handle missing config file gracefully ([f081132](https://github.com/I-am-PUID-0/DUMB/commit/f081132a134d47fe5f689c882d2d8bd9988028d6))
* **rclone:** prevent overwriting config with multiple instances ([f081132](https://github.com/I-am-PUID-0/DUMB/commit/f081132a134d47fe5f689c882d2d8bd9988028d6))
* **workflows:** correct echo command in cache cleanup ([a5124c6](https://github.com/I-am-PUID-0/DUMB/commit/a5124c686f55f056963efc293a264a75f8b0a34c))
* **zurg:** add version comparison during setup ([f081132](https://github.com/I-am-PUID-0/DUMB/commit/f081132a134d47fe5f689c882d2d8bd9988028d6))


### 🤡 Other Changes

* **deps:** update backend dependencies ([f081132](https://github.com/I-am-PUID-0/DUMB/commit/f081132a134d47fe5f689c882d2d8bd9988028d6))


### 🛠️ Refactors

* **rclone:** streamline setup logic and reduce redundancy ([f081132](https://github.com/I-am-PUID-0/DUMB/commit/f081132a134d47fe5f689c882d2d8bd9988028d6))

## [1.3.2](https://github.com/I-am-PUID-0/DUMB/compare/1.3.1...1.3.2) (2025-07-11)


### 🐛 Bug Fixes

* **docker:** correct environment variable reference for buildx cache root path ([5ded5fe](https://github.com/I-am-PUID-0/DUMB/commit/5ded5fea1b6e30f86ed2082c9b078d32d62793a4))
* **docker:** remove unnecessary variable assignment in cleanup-cache job ([08ddd0e](https://github.com/I-am-PUID-0/DUMB/commit/08ddd0ea1482afd6fa2695203ff06eb9dc3f8051))


### 🚀 CI/CD Pipeline

* **workflows:** parallel docker build ([bd3f4bc](https://github.com/I-am-PUID-0/DUMB/commit/bd3f4bcd99df6352e6d769b7a3cf3a1b50af6a1e))


### 🛠️ Build System

* **docker:** add checkout action to setup job for version and repository variables ([af043f5](https://github.com/I-am-PUID-0/DUMB/commit/af043f55ede69bcdbc125ff1d1fcb4fd6b18ca1a))
* **docker:** add cleanup-cache job to prune buildx cache after builds ([1474db1](https://github.com/I-am-PUID-0/DUMB/commit/1474db1078c25cdd91614b4a0015126112b89883))
* **docker:** add image tag selection and pull step for version extraction ([4804ca5](https://github.com/I-am-PUID-0/DUMB/commit/4804ca5e9f970a4678eb093d156acd0a59840779))
* **docker:** add python3 make g++ git ca-certificates to dumb frontend build ([f4cfc3f](https://github.com/I-am-PUID-0/DUMB/commit/f4cfc3f69bf0be2edbe38c3f717a5655d6ff7b25))
* **docker:** change to use /dev/shm ([9b5ac34](https://github.com/I-am-PUID-0/DUMB/commit/9b5ac3428c4d3da9da7df8d029e98528a791f381))
* **docker:** enhance CI workflow by adding setup job for version and repository variables ([56afd5d](https://github.com/I-am-PUID-0/DUMB/commit/56afd5dc80969d4d99905c9121527f55eff98e92))
* **docker:** fix cleanup command in cli_debrid-builder to remove all extracted files ([7e8f92d](https://github.com/I-am-PUID-0/DUMB/commit/7e8f92d1e56f6ac20e8754febc6fb14cc0295c85))
* **docker:** optimize pnpm configuration for frontend builds ([7021918](https://github.com/I-am-PUID-0/DUMB/commit/70219182f16377410e1f32ada8991fd85e9d6091))
* **docker:** refactor Dockerfile to consolidate base image and streamline build stages ([44e646e](https://github.com/I-am-PUID-0/DUMB/commit/44e646e5d3147129dbb6f722d8d9e9c4dde84bb0))
* **docker:** refactor frontend build steps and improve npm configuration ([ee09571](https://github.com/I-am-PUID-0/DUMB/commit/ee095715758d44dcc3811577f802e00eff1dfeaa))
* **docker:** split pnpm build ([92c85cf](https://github.com/I-am-PUID-0/DUMB/commit/92c85cf20c3db9bcba0bfdf936c92b5d17570bc4))
* **docker:** test ramdisk for builds ([051d6b0](https://github.com/I-am-PUID-0/DUMB/commit/051d6b09375f61049b9330e388b8bd2feb24e39f))
* **docker:** update environment variable handling to use GITHUB_OUTPUT instead of GITHUB_ENV ([76494d6](https://github.com/I-am-PUID-0/DUMB/commit/76494d6abc80d24bb8a2c900be0f7395874cf187))

## [1.3.1](https://github.com/I-am-PUID-0/DUMB/compare/1.3.0...1.3.1) (2025-06-30)


### 🤡 Other Changes

* **deps:** bump fastapi from 0.115.12 to 0.115.14 ([#19](https://github.com/I-am-PUID-0/DUMB/issues/19)) ([4fa13c8](https://github.com/I-am-PUID-0/DUMB/commit/4fa13c88c507df9d88893a48d56a8487eb8a0f79))
* update docker-compose.yml ([e67185e](https://github.com/I-am-PUID-0/DUMB/commit/e67185e0b8045dab2f870186ef8eddc6f1ab37a8))


### 📖 Documentation

* **readme:** Minor readme tweak ([#21](https://github.com/I-am-PUID-0/DUMB/issues/21)) ([e1283bc](https://github.com/I-am-PUID-0/DUMB/commit/e1283bcc25695cc2a1a072a1b6014949b1d15f6e))
* **readme:** refine project list and update usage notes ([2d7a98c](https://github.com/I-am-PUID-0/DUMB/commit/2d7a98c5f004c00c32d25276e9a4b5eb28e0921c))
* **readme:** update compose to include /mnt/debrid ([1d29a0f](https://github.com/I-am-PUID-0/DUMB/commit/1d29a0fe7ddace329e1c29f855d950b1905b8fe9))
* **readme:** update project list with new entries and correct Discord link ([fa63133](https://github.com/I-am-PUID-0/DUMB/commit/fa63133fa842be97760b35ea250911b84eb12d1e))


### 🛠️ Build System

* **deps:** Upgrade dependencies ([d2f1d10](https://github.com/I-am-PUID-0/DUMB/commit/d2f1d10e97bdcae84ec306d3f6cf129feeee2b22))

## [1.3.0](https://github.com/I-am-PUID-0/DUMB/compare/1.2.1...1.3.0) (2025-06-27)


### ✨ Features

* **core:** Improve core service startup ([8226f42](https://github.com/I-am-PUID-0/DUMB/commit/8226f42c0ebee34a828309a06e4f4be57bf45ea9))
* **decypharr:** Adds patching to Decypharr config for default configuration ([8226f42](https://github.com/I-am-PUID-0/DUMB/commit/8226f42c0ebee34a828309a06e4f4be57bf45ea9))

## [1.2.1](https://github.com/I-am-PUID-0/DUMB/compare/1.2.0...1.2.1) (2025-06-26)


### 🐛 Bug Fixes

* **phalanx:** Updates Phalanx DB setup to support v0.55 ([353e21f](https://github.com/I-am-PUID-0/DUMB/commit/353e21f393ee0cc177673711ec8197caf7f635b4))
* **setup_pnpm_environment:** Addresses potential EAGAIN errors during pnpm install by checking both stdout and stderr. ([353e21f](https://github.com/I-am-PUID-0/DUMB/commit/353e21f393ee0cc177673711ec8197caf7f635b4))


### 🤡 Other Changes

* **gitignore:** Adds decypharr to gitignore. ([353e21f](https://github.com/I-am-PUID-0/DUMB/commit/353e21f393ee0cc177673711ec8197caf7f635b4))

## [1.2.0](https://github.com/I-am-PUID-0/DUMB/compare/1.1.1...1.2.0) (2025-06-25)


### ✨ Features

* **postgres:** add migration from legacy role 'DMB' to 'DUMB' ([63a5a05](https://github.com/I-am-PUID-0/DUMB/commit/63a5a055afc31b7e5928a160433e2d20b2ea2191))

## [1.1.1](https://github.com/I-am-PUID-0/DUMB/compare/1.1.0...1.1.1) (2025-06-24)


### 🐛 Bug Fixes

* **logs:** correct logical condition for process name checks ([5f86762](https://github.com/I-am-PUID-0/DUMB/commit/5f86762b57ca3b89f4fe7912faaa6fd8a2133ba4))

## [1.1.0](https://github.com/I-am-PUID-0/DUMB/compare/1.0.2...1.1.0) (2025-06-24)


### ✨ Features

* **plex:** add Plex server FriendlyName configuration ([666e2a1](https://github.com/I-am-PUID-0/DUMB/commit/666e2a17284675f7c92d37a6dc92882dc173879e))


### 🐛 Bug Fixes

* **api:** Add static plex url for frontend settings page ([666e2a1](https://github.com/I-am-PUID-0/DUMB/commit/666e2a17284675f7c92d37a6dc92882dc173879e))
* **plex:** claiming functionality ([666e2a1](https://github.com/I-am-PUID-0/DUMB/commit/666e2a17284675f7c92d37a6dc92882dc173879e))

## [1.0.2](https://github.com/I-am-PUID-0/DUMB/compare/1.0.1...1.0.2) (2025-06-24)


### 🐛 Bug Fixes

* **api:** add temp patches for dmbdb frontend ([6b76806](https://github.com/I-am-PUID-0/DUMB/commit/6b76806ea4f88b73d51743b7c41025db3bb032a6))

## [1.0.1](https://github.com/I-am-PUID-0/DUMB/compare/1.0.0...1.0.1) (2025-06-24)


### 🐛 Bug Fixes

* **config:** rename config files ([394e929](https://github.com/I-am-PUID-0/DUMB/commit/394e9298b29f66aaf8808cd709c189733792a97d))


### 🤡 Other Changes

* **deps:** bump python-dotenv from 1.1.0 to 1.1.1 ([#7](https://github.com/I-am-PUID-0/DUMB/issues/7)) ([5945b54](https://github.com/I-am-PUID-0/DUMB/commit/5945b5403829ac1f0c2fe84f0d5a1104d65c773a))

## [1.0.0](https://github.com/I-am-PUID-0/DUMB/commit/91ecaccf3d58b647b2ee1278b47f2767758582a6) (2025-06-20)


### ⚠ BREAKING CHANGES

* **DUMB:** initial DUMB push

### ✨ Features

* **DUMB:** initial DUMB push ([e212248](https://github.com/I-am-PUID-0/DUMB/commit/e2122487a50af15714929ffc5d0e3bd9d73fb160))
