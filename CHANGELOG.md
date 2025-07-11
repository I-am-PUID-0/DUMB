# Changelog

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
