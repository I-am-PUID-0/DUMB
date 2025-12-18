# Changelog

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
