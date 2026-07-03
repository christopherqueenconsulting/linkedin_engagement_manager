# Changelog

## [0.18.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.17.3...v0.18.0) (2026-07-01)


### Features

* **login:** email-reply verification-PIN flow for LinkedIn challenges ([c7afa96](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c7afa96534180dced9284e7ac880adc5452a4c1b))
* **login:** email-reply verification-PIN flow for LinkedIn challenges ([58c5cd1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/58c5cd115a380845071954e0d7fcec1be2af136b))
* **proxy:** support credentialed proxies via MV3 auth extension ([dfd1281](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/dfd128133e6c83f7895c3082436225b67d6d0029))
* **proxy:** support credentialed proxies via MV3 auth extension ([b9df844](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b9df8447f12b42c50f7294e4ace80aaf28bd82ee))


### Bug Fixes

* **api:** dashboard stats 500s in the first days of a month ([fa7a213](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fa7a213ba3fd680add299b515fe6fd690b6468a3))
* **automation:** add shared 429 circuit breaker to pause Selenium engagement ([27b09a8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/27b09a821ae81909db8b2691ff9b3e864bac1b9f))
* **automation:** shared 429 circuit breaker to pause Selenium engagement ([0a24259](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0a24259d3d44e988a2de9a42b207b607a24eeaac))
* **login:** recover from stale-cookie redirect loop after egress-IP change ([2dbc19c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2dbc19c5a24f4f628fc053daee84d30bf648fe79))
* **login:** recover from stale-cookie redirect loop after egress-IP change ([9cb2968](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9cb2968b490209b343d846e378d5132a5fbaa658))


### Documentation

* add egress & LinkedIn access at-scale build-vs-buy decision doc ([df8f80d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/df8f80d1af4d0cc87240289efe90b04df083d5e5))
* egress & LinkedIn access at-scale decision doc ([d1f62ee](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d1f62eeb25f2eb9dc33c883b05f4dd39e5526964))

## [0.17.3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.17.2...v0.17.3) (2026-06-30)


### Bug Fixes

* **automation:** make auto-commenting resilient to LinkedIn 429/auth-wall ([27d4e95](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/27d4e958237eaaf96883fe31d6ae53179c014113))
* **automation:** make auto-commenting resilient to LinkedIn 429/auth-wall ([a2e7280](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a2e7280ef2c01071a773635f9dd7daedb98e20cd))

## [0.17.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.17.1...v0.17.2) (2026-06-30)


### Bug Fixes

* **carousel:** self-heal stale/errored carousels into branded slides ([69431c0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/69431c02577b38d84ea5d5e86c3d9d52e9e79039))
* **carousel:** self-heal stale/errored carousels into branded slides ([0fd5976](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0fd5976568e5f75417bc432ff1e11bcacca7f113))

## [0.17.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.17.0...v0.17.1) (2026-06-30)


### Bug Fixes

* **carousel:** no placeholder image fallback; flag 'error' when images unavailable ([5ebf73a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5ebf73aefeb1d61faf5b1e9b8a107a1ab15f9784))
* **carousel:** remove placeholder fallback; flag 'error' when images unavailable ([c12388b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c12388b6d4d9936d704aa3d050d13f0549ab746e))

## [0.17.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.16.1...v0.17.0) (2026-06-30)


### Features

* **company-page:** let users set a LinkedIn company page for monthly invites ([19ac6c0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/19ac6c0ed633b2ffc96e4fda6083e2981edf2811))
* **company-page:** user-settable LinkedIn company page + monthly invite gating ([198d9e9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/198d9e91934e21dfcd5965bbd596431408c48b51))


### Bug Fixes

* **scheduler:** post at the user's intended local time + close enqueue gap ([850d83c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/850d83cfbed7e8a085fc8e602c855dd41c3e7abf))
* **scheduler:** post at user's local time + close enqueue blind-window ([55616b3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/55616b32f03177580fee44774bb56c5082bd2035))

## [0.16.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.16.0...v0.16.1) (2026-06-30)


### Bug Fixes

* **linkedin:** carousel posts no longer crash on missing fallback image ([9a9523e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9a9523ec824d37440361cac3abca014855190825))
* **linkedin:** stop carousel posts crashing on missing fallback image ([afca881](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/afca881cf4a183fbf531e7f20a212399844b57f1))

## [0.16.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.15.1...v0.16.0) (2026-06-30)


### Features

* **ui:** reorganize Account page into clear grouped sections ([624b353](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/624b353d5c922d2d007490f7f79d628f9bfe5d73))
* **ui:** reorganize Account page into grouped sections ([2b61b8b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2b61b8be3c262522dbcedfa7574cd7719e3ae596))

## [0.15.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.15.0...v0.15.1) (2026-06-30)


### Bug Fixes

* **ci:** make releases merge-queue-safe and deploys resilient ([6216af3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6216af3db83ed6fba7cbb4e4ec03cd05d21a561f))
* **ci:** merge-queue-safe releases + resilient deploys ([839c788](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/839c78844f2a0f7b1e49b99460e014833ee6372b))

## [0.15.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.14.0...v0.15.0) (2026-06-30)


### Features

* **ui:** account-readiness gating + LinkedIn session card + required marks ([96c3982](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/96c3982fb9613ff5f37b5ee31d70a738a95b5218))

## [0.14.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.13.0...v0.14.0) (2026-06-30)


### Features

* **account:** LinkedIn-session emails + auto-detect + readiness API ([1776eba](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1776eba628ff4eb966da6641dd557ee2ce27ce36))
* **account:** LinkedIn-session emails, auto-detect, and account-readiness API ([4382088](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4382088765a1795df895bdd1635be2965a926545))
* **linkedin:** session-cookie (li_at) reuse ([f00b36c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f00b36c0cd21cb27810141c75e268e4f9ca9449e))
* **linkedin:** session-cookie (li_at) reuse to skip new-device login challenge ([7c377c9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7c377c953b154396e734331e91e75e9429d8d5ee))

## [0.13.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.12.5...v0.13.0) (2026-06-29)


### Features

* **proxy:** zero-setup region-based egress proxy ([55d4adf](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/55d4adf1aa10ee3d4e427c4b670eafe4e4809afe))
* **proxy:** zero-setup region-based egress proxy resolution ([8a13676](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8a13676411e2bc1619760080c19fdf9aaaf96b6d))

## [0.12.5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.12.4...v0.12.5) (2026-06-29)


### Bug Fixes

* **security:** harden safeMediaUrl (URL-parser allowlist) ([ccb1ad1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ccb1ad1c24e848e0d06fea1c15a2c836419b9dec))
* **security:** harden safeMediaUrl with URL-parser scheme allowlist ([8a2cafb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8a2cafb8e43b895e637d116a1eb760f114b0f595))

## [0.12.4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.12.3...v0.12.4) (2026-06-29)


### Bug Fixes

* **security:** resolve open CodeQL alerts ([62d2bc7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/62d2bc78adf53ddd6b9a8ed1bfa949b54bfa8004))
* **security:** resolve open CodeQL alerts ([5432d68](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5432d68ee523d5ecd2e015bf3a737f5ecfc1874d))

## [0.12.3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.12.2...v0.12.3) (2026-06-29)


### Bug Fixes

* **security:** source debug-harness email from get_user_email ([721440f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/721440fa7840358a33bea4d92ed4da3ab55f0f7d))
* **security:** source debug-harness email from get_user_email (untaint) ([e0767f5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e0767f5950fedad9857dfd00e3369ecef5024280))

## [0.12.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.12.1...v0.12.2) (2026-06-29)


### Bug Fixes

* **security:** drop password entirely from debug harness log ([d25f556](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d25f556226065d41c7774318c057fed30fa72023))
* **security:** drop password entirely from debug harness log ([02b7b97](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/02b7b9704a20852f86c104aac30ee62f1706dde3))

## [0.12.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.12.0...v0.12.1) (2026-06-29)


### Bug Fixes

* **security:** don't log password-derived data in debug harness ([fb5d41b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fb5d41be18931955dcc084535cc7e6e5ffe56e58))
* **security:** don't log password-derived data in debug harness ([f8319e6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f8319e68a51e3bf80e91716d66403b4f3e540ab8))

## [0.12.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.11.0...v0.12.0) (2026-06-29)


### Features

* **linkedin:** notify user on device-approval + browser anti-detection ([9a284e9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9a284e9e78f841de388981129e7026a3438c5287))


### Bug Fixes

* **linkedin:** repair login selectors for redesigned login page ([da9e9f9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/da9e9f9885e2cfefc85c4594d11f785e09c4d273))
* **linkedin:** repair login selectors for redesigned login page ([b21fd97](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b21fd975621e6b3167dff450c3ebe9437e6c49c8))

## [0.11.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.10.0...v0.11.0) (2026-06-29)


### Features

* **api:** structured inputs + dual-auth on engagement test endpoints; doc/Postman polish ([233c017](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/233c01728d2b65bae735837b163542073a3c7f76))
* **api:** structured query-param inputs + dual-auth on engagement test endpoints; doc/Postman polish ([9218a6a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9218a6a5332487f326cfa5200f73057df761539e))

## [0.10.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.9.2...v0.10.0) (2026-06-29)


### Features

* **api:** admin test-run endpoints for comment/reply/DM + Postman & VNC guide ([060acf4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/060acf484acd96bed762acaef51f58905c065cf7))
* **api:** admin test-run endpoints for comment/reply/DM + Postman & VNC guide ([ea8217a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ea8217a327248b89a402fb03a36fba23b3859024))

## [0.9.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.9.1...v0.9.2) (2026-06-29)


### Bug Fixes

* **ci:** verify CDN cache auto-purge on deploy ([20e9018](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/20e9018d12e1c71cb5bbb20154b9021ba9aa3e08))
* **ci:** verify CDN cache auto-purge on deploy ([3bcf695](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3bcf695cc82b679833ba982f83f836d2c5b0fd5d))

## [0.9.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.9.0...v0.9.1) (2026-06-29)


### Bug Fixes

* **api:** never cache the SPA index.html shell ([3c6741c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3c6741cbd3fab4fd25351c331fa5116de7504ba9))
* **api:** never cache the SPA index.html shell; cache hashed assets forever ([4346a17](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4346a175e54460cdcca3b893cbf72e94aee794f2))

## [0.9.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.8.0...v0.9.0) (2026-06-29)


### Features

* **automation:** per-user geo/timezone/locale spoofing for LinkedIn login ([e24a5b7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e24a5b7e9ca35759cc6f94a373ab4a3c73c8a5f0))
* **automation:** per-user geo/timezone/locale spoofing for LinkedIn login ([f6f8acc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f6f8acc5084fa6bc8fdc799d9d0b2af3d5fd4c2e))

## [0.8.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.7.1...v0.8.0) (2026-06-29)


### Features

* **ui:** embed video + carousel in post preview card ([8663f39](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8663f39d413a96dd31f2cb2d9c9f1641e6835e72))
* **ui:** embed video + carousel in post preview card ([ee90508](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ee9050848d33b8fcfe70a0c98c6d0a9a63f0feee))

## [0.7.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.7.0...v0.7.1) (2026-06-28)


### Bug Fixes

* **assets:** make worker-written media reliable + backfill missing assets ([8364817](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/83648172f7f89d510dc3d30144c054c7477c5265))
* **assets:** reliable worker media writes + missing-asset backfill ([52271f2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/52271f2ccf9eff8b2e1f0d90620b87394751d19c))

## [0.7.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.6.0...v0.7.0) (2026-06-26)


### Features

* **video:** premium video tiers with a credit system ([e346e72](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e346e72eb2ca5b4271dd8f3caa21159f45cc4c6a))
* **video:** premium video tiers with a credit system ([a87b3de](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a87b3debe931dfa924e8ed828acc6d228eee29ec))

## [0.6.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.5.0...v0.6.0) (2026-06-25)


### Features

* **media:** Gen-4 Turbo migration, profile-aligned prompts, variant review tool ([f36e685](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f36e685b0390a8b3ead1734457046aefe0f08de8))
* **media:** migrate to Gen-4 Turbo, profile-aligned prompts, variant tool ([23006cf](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/23006cfc4e15988f9d198de17942cb5854330532))


### Bug Fixes

* **script:** surface API errors + detect not-deployed (404/405) in variant script ([b4118c1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b4118c1942edf4c34cf96ff444ad75909b39696b))

## [0.5.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.4.0...v0.5.0) (2026-06-25)


### Features

* **assets:** purge post media after publish to bound the assets volume ([#148](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/148)) ([98dacbb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/98dacbb6b1dcf4c4e9d4707b80a4a967da068f11))

## [0.4.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.3.2...v0.4.0) (2026-06-25)


### Features

* **api:** add /api/admin/regenerate-video (asset-only) ([#146](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/146)) ([06458a0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/06458a0b9049a1b53b26da7f1367b5a45a709d9e))

## [0.3.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.3.1...v0.3.2) (2026-06-25)


### Bug Fixes

* **prod:** shared persistent volume for generated assets ([#144](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/144)) ([fdf8fe4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fdf8fe4517a2d5d2afbaebacfc34f05905fa6376))

## [0.3.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.3.0...v0.3.1) (2026-06-25)


### Bug Fixes

* **api:** make /api/assets public so LinkedIn can fetch post media ([#142](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/142)) ([c67b495](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c67b495720062e7ceb9f0b6b8d8bcec8e16c632e))

## [0.3.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.2.0...v0.3.0) (2026-06-25)


### Features

* **config:** add PUBLIC_BASE_URL taking precedence over ngrok URLs ([#140](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/140)) ([46222f4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/46222f431abbef636c9ce19bdbb1a6dbf6f39359))

## [0.2.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.1.3...v0.2.0) (2026-06-25)


### Features

* **ui+prod:** app title; run celery/flower from image (drop src bind-mount) ([#138](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/138)) ([5a66105](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5a66105992b765ce848e78bd1894f2fc818cc277))

## [0.1.3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.1.2...v0.1.3) (2026-06-25)


### Bug Fixes

* **prod:** drop dev src bind-mount that masked the built SPA ([#136](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/136)) ([a90eff1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a90eff174dc28d2f9581a4732fc33d543a3ff152))

## [0.1.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.1.1...v0.1.2) (2026-06-25)


### Bug Fixes

* **build:** ensure SPA dist is in the image; expose MySQL on loopback ([#134](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/134)) ([46166bc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/46166bce66c38b76de5c42e225b05a78923c067b))

## [0.1.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.1.0...v0.1.1) (2026-06-25)


### Bug Fixes

* **build:** commit UI package-lock.json (Docker npm ci requires it) ([#132](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/132)) ([7d53fa2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d53fa22fa1449be0241ad9932c5949454ecf5e2))
* **ci:** correct trivy-action tag (v0.28.0) in release workflow ([#129](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/129)) ([38c8aea](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/38c8aeaaad4ff2025399eea678c046c86cde15ce))
* **ci:** remove advisory Trivy scan blocking the release build ([#131](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/131)) ([cbc6e8a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cbc6e8a60d5fbbb69bf8109bedc6ad41229e055b))
* **ops:** logs dir ACL for mixed-uid containers + dynamic backup volume ([#133](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/133)) ([077ea27](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/077ea277586e79d5f2101c2cf5d4773b89888242))

## 0.1.0 (2026-06-25)


### Features

* add Replicate avatar training with Stripe credit system ([f2ca54f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f2ca54fff7dd993b9e3fb1a009992e7fcf075a94))
* **ai/video:** add Perplexity Sonar research and Pexels stock video fallback ([fa585d1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fa585d1eb67f147b3625990bbceaf0bdb311d178))
* **ai/video:** Perplexity Sonar research + Pexels stock video fallback ([3399f08](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3399f0804c802172c2dc3fc53f002cf741d5685c))
* **billing/tests:** fix Stripe duplicate subscription on upgrade, add coverage ([66f1e45](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/66f1e45f5cb85c4b4a5e56a41bf7d5bd708c6271))
* carousel slides (Pillow), video URL fix, LinkedIn markdown cleanup ([d6683c1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d6683c1f1f6c230e4770277cd7437bf8913dc338))
* carousel slides with Pillow, video URL fix, LinkedIn markdown cleanup ([c0bfe8f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c0bfe8ff8c804fa8fd9044b1282d28ecaea90958))
* **carousel:** 5 distinct template layouts + AI generation in manual scheduler ([0866500](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0866500296888d5f17bdb430d1a67c3dc073a5d6))
* **coverage:** raise coverage 40% → 58% — exclusions + 160 new unit tests ([a1ff690](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a1ff690d1e2868a4121027e14524c89e09992877))
* **coverage:** raise coverage 58% → 67% — 175 new unit tests across 5 files ([93f4bc9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/93f4bc9b0ae518c0cd9162b74f04754147f5576a))
* **deploy:** add Hostinger VPS deploy stack with Cloudflare Tunnel ([932cc37](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/932cc3738f9795179d32b5a50396785f38552b23))
* **deploy:** Hostinger VPS deploy pipeline with Cloudflare Tunnel ([0674af3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0674af3ed547a61714289d24d2ce5b9f6065fe80))
* **deploy:** make vps_bootstrap.sh a guided idempotent first-run ([67d2912](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/67d29126314c1146d062a1c1ae8ac406b854fa73))
* **m0:** add developer context files and full CI/CD suite ([f300e10](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f300e1013688399a2c5cc88bbb4fa27caf86f311))
* **m0:** Developer context files and full CI/CD suite ([616bedb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/616bedb3ba618df60ff3066bd626b80bba33dca6))
* **m0:** Developer context files and full CI/CD suite ([616bedb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/616bedb3ba618df60ff3066bd626b80bba33dca6))
* **m1:** infrastructure modernization — standalone-chrome, LiteLLM, PostHog ([662e623](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/662e6236c9748d5b252b256f0bc9545604af8047))
* **m1:** Infrastructure modernization — standalone-chrome, LiteLLM, PostHog ([04c2123](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/04c21234e15c798126809544ec3ca479218c8ad5))
* **m1:** Infrastructure modernization — standalone-chrome, LiteLLM, PostHog, Ollama Cloud ([04c2123](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/04c21234e15c798126809544ec3ca479218c8ad5))
* **m1:** switch LiteLLM to Ollama-first model routing ([11a3ea0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/11a3ea04d084ee9bbcdc57f9367e7285cea51def))
* **m2:** Real unit and integration tests replacing pass-body stubs ([0068538](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0068538a1d70eed2d33aa7d519ec7a72ba5f0df8))
* **m2:** write real unit and integration tests replacing pass-body stubs ([9ac3839](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9ac38395949bd23812630d51ea7188e49de1db24))
* **m4:** React + TailwindCSS SPA — Dashboard, Schedule, Review, Account, LinkedIn Preview ([c695483](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c69548384d3f6329b584a0aae9fd00a9935a018e))
* **m4:** React + TailwindCSS SPA with 4 pages and LinkedIn preview ([3d22aa2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3d22aa202e7cb84ae570653d6be951219acb1b86)), closes [#17](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/17) [#18](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/18) [#19](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/19) [#20](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/20) [#21](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/21) [#22](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/22) [#23](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/23)
* **m4:** replace Streamlit with React SPA served via FastAPI ([#85](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/85)) ([0ae657d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0ae657dd3050cfcefec23865352dde21194a3ee0))
* **m5:** Feature completion — carousel handlers, article posting, active users, invite logging ([439f504](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/439f504301d6d95d8f3a125c67668408d782ddf3))
* **m5:** feature completion — carousel handlers, article posting, invite logging, user update ([c8742be](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c8742be3bb60a6d1af846985142bb0f361259840)), closes [#24](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/24) [#25](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/25) [#28](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/28) [#29](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/29) [#31](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/31)
* Review Posts read-only for posted status, CapSolver CAPTCHA, codecov config ([bb62cf5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bb62cf5296697e6d59936ee6fa16679f17c1e208))
* **tests:** CapSolver CAPTCHA e2e + /post_url endpoint + codecov config ([f683f81](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f683f81230c2f99b7b1de8c25a9f1094317838fa))
* **ui:** complete SaaS landing page, protected routing, schedule improvements ([#86](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/86)) ([8d6ca6c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8d6ca6cff750f61fd0c0dc9109c57d432282f9f1))


### Bug Fixes

* address remaining CodeQL alerts and broken integration test ([796190e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/796190e0e9b6a73aa0e3ac2ba12be240c59ecca4))
* **auth/ui/infra:** account save, OAuth email handoff, Docker UI build, DM/carousel features ([#94](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/94)) ([76b0c09](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/76b0c0953a33d75b413b3e428276e4668f15367c))
* **avatar:** correct Replicate training API call — create destination model first ([22877d7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/22877d76e702c157dc5b6c31c9d0213f8402e09e))
* **avatar:** update destination hardware SKU to gpu-l40s ([d6e32db](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d6e32db420c31b22f321b9d22e77d41a170f35ce))
* **avatar:** upload ZIP via replicate.files.create() to avoid SSL TLS errors ([b575bdc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b575bdcfc06d388fe8de04299f827974283af276))
* **backend:** deprecated API stubs, active user detection, geolocation, logging, integration tests ([#87](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/87)) ([070ad68](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/070ad6810bd5a60c1e5ba3acfc08695d11a57ddc))
* **carousel+scheduler+ops:** URL-slide upload, inactive-user pre-post skip, cascade deletes, selenium healthcheck ([a07df7c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a07df7c727fa95a6c9d2c5a7c85f087bfa672d5d))
* **carousel:** redesign slide renderer — large fonts, visual hierarchy, clean chars ([2c79ced](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2c79ced823debc9f372315ce272a018e4c6bbc6f))
* **carousel:** URL slides bypassed to upload_media instead of Pexels fallback ([5901ad9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5901ad9dfebf483f8e69069205e6235a2df37750))
* **ci:** resolve CodeQL warnings and integration test failures ([0326c40](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0326c40deca9ed42751ada3c2f73d914f72d1eda))
* **ci:** resolve E2E test failures and CodeQL security alert ([4dec003](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4dec003588b1a603bc8495d9d1493e92a557d998))
* **codecov:** make project coverage informational, use auto baseline ([5d6e06c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5d6e06cba8efd5bb144fca6c43378a21f4f8b2e4))
* correct /api/assets URL path and add backward-compat redirect ([3413974](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3413974e24cbb661bcb997b6831c0af6037a125b))
* correct avatar DB connection pattern and seed blog/sitemap from API ([ce58da3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ce58da3765c3e8bde93b754657bc33ef0ad2d1d6))
* **db+scheduler:** cascade-delete user FKs and skip pre-post tasks for inactive users ([d677c71](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d677c7138f4722e4149f3fbd44e78c0aef5bf0b3))
* **deploy:** correct GHCR namespace in .env.prod.example ([1765919](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/176591901efc81d1e1fdb930390c3b893c6aeed3))
* **docker:** disable flower persistence to fix corrupted shelve DB at startup ([#89](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/89)) ([eeb39f0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/eeb39f0eef57279f904fa59f0a21fc838da9236a))
* **flower:** remove hardcoded persistence flags causing startup crash ([#90](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/90)) ([00d1424](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/00d1424aa6466533d76f56746dbafec88bf253ef))
* **infra/auth:** litellm healthcheck, LinkedIn OAuth initiation, dynamic redirect URL, PostHog silencing ([#88](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/88)) ([685fbcd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/685fbcde7445635081762249bd6dc81b768e5fed))
* **m0:** code review agent, workflow bug fixes, and permissions hardening ([723da11](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/723da115ada082777be792da71cb030e8e8bcf7d))
* **m1:** use custom_callbacks for complexity router in LiteLLM config ([5a38c01](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5a38c015d0bde7a826d6b133813780488e40d87e))
* **m2:** make all 88 unit tests pass with production code fixes ([c2b250a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c2b250a71c6a38c90b690271be4e8f41ee346a04))
* **m3:** Critical bug fixes — media type, token expiry, scraper prefix, hardcoded data ([057829f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/057829f9f70247a958c7bd6ec3bafc595a17e8e1))
* **m3:** fix 4 critical bugs in poster, scrapper, db, and test files ([3a771de](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3a771de02e2e672adffed6e2c84ac3a6f905f0b6)), closes [#29](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/29) [#30](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/30) [#32](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/32) [#33](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/33)
* **ngrok/run:** correct all port and URL references in templates and run.sh ([#93](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/93)) ([7f6f70d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7f6f70d9231578d2be682dbe25fcf9f9d3a49cb6))
* **ngrok:** fix ERR_NGROK_108 session conflict and STREAMLIT_PORT reference ([#91](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/91)) ([ac0d506](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ac0d5069563d6805ea05c9fc956d64e594f81e85))
* **observability:** repair PostHog event delivery and LinkedIn login error visibility ([b4a2b20](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b4a2b20bcbbd3d218148d82df01b775a77a479a2))
* **observability:** repair PostHog event delivery and LinkedIn login error visibility ([c63ac37](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c63ac375ac5553f167ab832225badd6bc5173e47))
* **ops:** extend run.sh Celery restart check to cover celery_worker_selenium ([3b329af](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3b329af815df7c4d253a761b026dc692cf5e39d1))
* **ops:** fix selenium worker healthcheck hostname and timeout ([ecbef6c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ecbef6ceb2119d284139a935d8adcdb276251160))
* **ops:** stop Selenium task collisions + fix 3 log errors from last 48h ([80b47d0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/80b47d0a746c7c7785b7ed87f2eba986a8824ab8))
* **ops:** stop Selenium task collisions + fix 3 recurring log errors ([529f3f2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/529f3f29ecfbebc5c6ad1d2827e9e18dd77ddfbd))
* **pipeline/security:** fix post schedule pipeline, auth security, and billing ([950ef0f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/950ef0f015fdd19141521d437a2903b1f5b89a0a))
* **pipeline/security:** fix post schedule pipeline, auth security, and billing correctness ([04e2290](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/04e2290bb45e708ec1cd4aace2a731d5ad633d48))
* **posting:** repair token_expiry SQL bug, add orphaned-post recovery, fix beat healthcheck ([6f5f8ee](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6f5f8ee2d0efedaaf06f30bf6d952f13f5595bad))
* resolve CodeQL security alerts and update GitHub Actions to Node.js 24 ([e3591d4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e3591d482920b7bb9bf3ba27988e2e9e90ca3271))
* resolve CodeQL security alerts and update GitHub Actions to Node.js 24 ([866e411](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/866e411f79c875dbe31f77e476de27f52d203a11))
* **security:** clear remaining CodeQL path-injection and static-analysis alerts ([de8d316](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/de8d316b5568f66d6c06c14ad72869b19eb699d4))
* **security:** resolve 8 CodeQL alerts introduced by new test files ([12cbb51](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/12cbb516a37bb9d542e3ea07f40eca8a28cfd346))
* **security:** resolve CodeQL alerts and make codecov components informational ([ce7cbd4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ce7cbd4aa1126eaaaf60c80c5b54fb35cffee5d7))
* **security:** resolve CodeQL alerts in CAPTCHA integration code ([fd6d5d4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fd6d5d4ed56ac6a250d92c01ef986d3b7f781a04))
* **test:** correct Stripe webhook integration test for avatar credits ([5809e75](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5809e75cfb4afeda25f3abe5674a02bb4039e6cc))
* **test:** defer api.main import to fixtures to avoid collection-time OpenAI key ([6c00dce](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6c00dce7f54535bde353fea626571b64b721957d))
* **test:** pin datetime.now() to Monday in content-plan unit tests ([5fd8506](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5fd8506682ceb4bc4614aa06dfa3d08fb7a5bd83))
* **tests:** correct mock patch locations and CI env vars for integration tests ([f7e7645](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f7e764591ebfff470a2f1bf763fe84ffc4f6c419))
* **test:** use startswith with trailing slash for Stripe URL assertion ([5507da8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5507da8d9b62ae2361d8d5fc5ead784383f9c1a2))
* **timezone:** end-to-end timezone correctness for post scheduling and display ([5d39749](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5d39749599deb27b1c405cb7d5a5330e25249584))
* **timezone:** end-to-end timezone correctness for post scheduling and display ([b427093](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b427093741b4f4094c91430c900a6ec3fc15ee30))
* update test to use timezone-aware datetime and ignore .vscode/mcp.json ([e2172d4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e2172d42d07456684a2b0894303b53d0ef155792))
* **webhook:** robust checkout.session.completed + charge.refunded handling ([c027587](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c0275879ef1ae14860b1282acf8ae9bf3a245cdd))


### Documentation

* **deploy:** add manual go-live setup checklist ([b1d61ca](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b1d61ca4df5b7aa7efa65bd1b846f15c1ca290d0))
* update README for React/FastAPI/LiteLLM/PostHog stack ([76b0c09](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/76b0c0953a33d75b413b3e428276e4668f15367c))
