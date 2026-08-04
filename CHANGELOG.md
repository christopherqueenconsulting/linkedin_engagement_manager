# Changelog

## [0.131.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.130.0...v0.131.0) (2026-08-04)


### Features

* **auth:** add agent-scoped session tokens for headless automation ([#1027](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1027)) ([d344b8f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d344b8ff1bd6943c77ebd9e922262bf79515ffb2))

## [0.130.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.129.2...v0.130.0) (2026-08-03)


### Features

* **selenium:** probe-cover every SDUI surface + weekly drift cron (closes [#1013](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1013)) ([#1022](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1022)) ([41a4819](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/41a48195f114f011cdaa10d459218a5e8cd3ea8f))

## [0.129.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.129.1...v0.129.2) (2026-08-03)


### Bug Fixes

* **analytics:** format avg engagement as a percentage in Best Times to Post (closes [#1004](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1004)) ([#1016](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1016)) ([9d21df8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9d21df8bf30eada53e3b2ced826dd98221f07f33))
* **analytics:** show Best Times avg engagement as a percentage when it is a rate ([9d21df8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9d21df8bf30eada53e3b2ced826dd98221f07f33)), closes [#1004](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1004)
* **celery:** fill task defaults into the celery-once dedup key (closes [#989](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/989)) ([#1018](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1018)) ([1b7fc3b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1b7fc3b528b074ebf120369098ca15ad97645240))

## [0.129.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.129.0...v0.129.1) (2026-08-03)


### Bug Fixes

* **automation:** connect invites clicked the suggestion rail — open the invite dialog by URL ([#1012](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1012)) ([4e6e514](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4e6e51486245955748c1985b93bbee79f6ea4fd5))


### Documentation

* **claude:** trim CLAUDE.md back under the drift threshold (closes [#1010](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1010)) ([#1014](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1014)) ([53076e8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/53076e8fd173687c2ef539f7eeeb5838ed5b05e0))

## [0.129.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.128.0...v0.129.0) (2026-08-03)


### Features

* **analytics:** 12-hour time + timezone label in Best Times to Post (closes [#1003](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1003)) ([#1011](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1011)) ([a9e459a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a9e459a129b4cbc3c86b107d10c8ac69cab5552d))
* **dm:** implement recommendation + collaboration appreciation-DM sources (closes [#968](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/968)) ([#982](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/982)) ([7531ed5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7531ed51aef954654a487dfdc9000c6710413df1))
* **outreach:** implement clean_stale_invites — withdraw aged pending invites (closes [#969](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/969)) ([#983](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/983)) ([a97adde](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a97adde7870f32556410033b66321598dc6e9355))


### Bug Fixes

* **automation:** profile viewer walk finds zero viewers — ground on live SDUI DOM ([#1009](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/1009)) ([13ca520](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/13ca5202ac804534b6050952003097e3bf134dd5))

## [0.128.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.127.0...v0.128.0) (2026-08-03)


### Features

* **engagement:** flag roster targets LEM can't comment on + opt-in paced auto-follow (closes [#962](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/962)) ([#963](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/963)) ([b9607fd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b9607fde8fccfc807046379fde5404c5295d46b0))
* **newsletter:** implement blog-align — align_with_blog now reaches the generator (closes [#967](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/967)) ([#981](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/981)) ([766088a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/766088a9a47b71af6e5389fb6c87172c49d73fc2))


### Bug Fixes

* **ai:** ride out a LiteLLM proxy that is not accepting connections (closes [#986](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/986)) ([#997](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/997)) ([5447760](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5447760d2c0290ec897a83c5c64620670b3fd9d4))
* **errors:** a deploy that quits a live browser session is not a defect (closes [#988](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/988)) ([#999](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/999)) ([004cc7c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/004cc7c3d34eef8632f73217f52056da22e71ee4))
* **errors:** a roster author with no recent post is a no-op, not a warning (closes [#987](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/987)) ([#998](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/998)) ([ead5ea2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ead5ea2b0b4734130ebf381ea61bb2a63ef40815))
* **errors:** an empty connection-targeting scan is a no-op, not a warning ([f8ae7b7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f8ae7b7c2e136510fba3e386a2a82768772c6068)), closes [#985](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/985)
* **errors:** an empty connection-targeting scan is a no-op, not a warning (closes [#985](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/985)) ([#994](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/994)) ([f8ae7b7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f8ae7b7c2e136510fba3e386a2a82768772c6068))
* **errors:** an empty outreach-funnel scan is a no-op, not a warning (closes [#995](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/995)) ([#996](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/996)) ([801d6ee](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/801d6eedcbc0a55701f0d7bcbf280d9307ad39d0))
* **security:** require X-LEM-Client on cookie-authenticated /api writes — restores the CSRF layer [#950](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/950) removed (closes [#957](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/957)) ([#959](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/959)) ([4e9209e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4e9209e8fd8067ed5da422c92c033dd117fdd3bd))


### Documentation

* retire TODO_PROJECT_TIMELINE.md (closes [#975](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/975)) ([#993](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/993)) ([c2bef23](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c2bef238da7cb233968d86eed47e27242e679ca1))

## [0.127.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.126.1...v0.127.0) (2026-08-03)


### Features

* **hygiene:** weekly TODO-&gt;issues sweep cron + trim stale-branch exemptions ([#976](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/976)) ([6e7e12f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6e7e12f573426d0f825c9f233a2323cd04d9ac41))


### Bug Fixes

* **security:** retire the SPA's shared /api bearer token — the session is the identity (closes [#950](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/950)) ([#958](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/958)) ([e7b4379](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e7b4379c2f478fcc11facc9b13b37b710d344732))

## [0.126.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.126.0...v0.126.1) (2026-08-03)


### Bug Fixes

* **automation:** catch-up scan finds zero cards — ground card locators on the live SDUI DOM ([#964](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/964)) ([e5db1f1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e5db1f181d5111bb66d3ee42efc971234d78e7b1))
* **ui:** label the Engagement Roster per-week field (closes [#956](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/956)) ([#960](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/960)) ([8ef5379](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8ef53790b733dcb27e078585feb81bd5b971dffe))

## [0.126.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.125.1...v0.126.0) (2026-08-03)


### Features

* **agent-pipeline:** phase-guard holds route to MODE=phasefix instead of the owner ([#949](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/949)) ([4a6868c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4a6868c5cc673c16b0c7dfbf0fdea4d04b0756bc))


### Bug Fixes

* **billing:** daily caps stop resetting — the brand phase seeds, it no longer re-asserts (closes [#952](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/952)) ([#955](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/955)) ([c16bb85](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c16bb850108497b1e276d5fe2ba1e6989e12c275))

## [0.125.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.125.0...v0.125.1) (2026-08-03)


### Bug Fixes

* **api:** reply-notification webhook — pass sweep_slot to QueueOnce enqueue + log a verdict per inbound email ([c919f5e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c919f5edb1027296c85ce2c0e285bb875c7e2643))
* **api:** reply-notification webhook — sweep_slot QueueOnce KeyError + per-email verdict logging ([#951](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/951)) ([c919f5e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c919f5edb1027296c85ce2c0e285bb875c7e2643))
* **litellm:** weekly catalog PR describes its own diff + keeps the offline fixture in lockstep ([#920](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/920)) ([4ed0fa8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4ed0fa8219078fff854faae6ca737c2997ba52e8))

## [0.125.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.124.0...v0.125.0) (2026-08-03)


### Features

* **agents:** add Claude Code skills for recurring dev workflows ([#947](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/947)) ([8c1cb39](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8c1cb39ee60d418d5c3f34c15458dc9cbb47f38c))


### Bug Fixes

* **security:** /api endpoints resolve the caller from the session, not an email/user_id parameter (closes [#914](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/914)) ([#918](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/918)) ([ed58375](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ed58375989bfcfc48aedc698806b4d805bf96b47))

## [0.124.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.123.3...v0.124.0) (2026-08-02)


### Features

* **images:** encode the FLUX prompting research into the ONE brief engine ([#944](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/944)) ([02c15b3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/02c15b3cca8c3e96374783d53b7af76f9d2c6948))
* **security:** mandatory strong-factor enrolment + extension-session narrowing — Phase 2c.1 (closes [#905](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/905)) ([#915](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/915)) ([1a2b53a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1a2b53ab07fe84fd5fe3e77c82dc97e0a8167909))


### Bug Fixes

* **images:** steer hands low-risk in the brief; gate inspects fingers closely ([#946](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/946)) ([0655291](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/065529178e7bfebe824348dc7a533ff061979ee3))

## [0.123.3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.123.2...v0.123.3) (2026-08-02)


### Bug Fixes

* **images:** a stranger's face is the stock-photo look, not the fix for it ([#942](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/942)) ([54b750d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/54b750da06d1a1c0e444ca4e1d4396e74ff50544))

## [0.123.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.123.1...v0.123.2) (2026-08-02)


### Bug Fixes

* **images:** no brand marks in a render — gpt-image inserts logos on its own ([#939](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/939)) ([b3571b9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b3571b96bf60bc98dee480d499bf99281e7aee3e))

## [0.123.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.123.0...v0.123.1) (2026-08-02)


### Bug Fixes

* **images:** refusal filter rejected every AI-topic brief; gate avatar renders too ([#937](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/937)) ([9cee71d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9cee71d86f2b5bf1a5c0f0a1ceae97e63cd1bcca))

## [0.123.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.122.0...v0.123.0) (2026-08-02)


### Features

* **engagement:** preview and edit the weekly group post before it publishes (closes [#932](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/932)) ([#935](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/935)) ([94ce9ce](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/94ce9cea3c128ece0a372c672bca2c66a65092d8))
* **images:** overhaul image generation — gpt-image-2 engine, avatar-aware newsletter covers, image posts ([#936](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/936)) ([a67ee6d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a67ee6d076698962bbeb6b9d835b4b7c0c7b5d9e))


### Bug Fixes

* **engagement:** resolve a post's comment composer beside the card, not only inside it (closes [#916](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/916)) ([#929](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/929)) ([e889a96](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e889a9665e71f1dd823a39e82cd21f8b15392d93))
* **errors:** a stored automation pause is INFO, not a warning that files a defect (closes [#917](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/917)) ([#931](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/931)) ([f28f8bd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f28f8bd1110a30aeaa81b06937388b85cc6c608d))

## [0.122.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.121.0...v0.122.0) (2026-08-02)


### Features

* **models:** the catalog scan sees a build swapped under an unchanged tag (closes [#925](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/925)) ([#927](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/927)) ([9a096d0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9a096d030109e4fa8354f1fb0acb7e3704dc8608))


### Bug Fixes

* **benchmarks:** a harness-wide outage is refused, not published as 0% (closes [#923](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/923)) ([#926](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/926)) ([d281e93](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d281e9316685926bc094bf27b6c9ce230e87393f))
* **benchmarks:** recalibrate the deterministic floor so the gate can open (closes [#910](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/910)) ([#922](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/922)) ([7ce3adc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7ce3adccd4d587baf814acab9cced21a0c8ca046))
* **ci:** CodeQL PR gate waits for THIS commit's analysis, not any analysis (closes [#904](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/904)) ([#913](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/913)) ([08501f3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/08501f37a3118cec1e3d383f4d391ab332937ab1))
* **ui:** mobile responsiveness — tables scroll, nav shrinks, modals fit (closes [#894](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/894)) ([#911](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/911)) ([b8d27da](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b8d27dae2e945f9e5a679e5d1e250f803b018589))


### Documentation

* **models:** the [#842](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/842) benchmark run — keep qwen3.5:397b and gpt-oss:120b (closes [#842](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/842)) ([#919](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/919)) ([6bf3fae](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6bf3fae37863741d94c28a93885e4d6429add192))
* **models:** the [#921](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/921) tag scan — decline deepseek-v4-flash:0731 and kimi-k3 (closes [#921](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/921)) ([#924](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/924)) ([7c14f08](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7c14f08d79da9c75454f5980631fd4e2edf54483))

## [0.121.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.120.0...v0.121.0) (2026-08-02)


### Features

* **marketing:** affiliate reward pays per activated referral, not for joining (closes [#737](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/737)) ([#903](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/903)) ([9c1d714](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9c1d71439610266c37a657c8832674b32db68074))
* **newsletter:** cover image upload + opt-in AI generation (closes [#893](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/893)) ([#909](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/909)) ([8812917](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/88129176246b531670b5a089783ae362d05e071f))
* **security:** passkeys + TOTP + recovery codes + step-up gate — Phase 2c (closes [#897](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/897)) ([#906](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/906)) ([04a65cd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/04a65cda49de0796bba73a1e44e0a2a67608b2b4))


### Bug Fixes

* **ops:** /health/deep must require a CONSUMING worker, not just a registered one ([#907](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/907)) ([a65a4c2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a65a4c2424914db894f5b1292db1928a284c3741))

## [0.120.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.119.0...v0.120.0) (2026-08-02)


### Features

* **ops:** host watchdog + deep health check for the failure healthchecks cannot see ([#900](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/900)) ([a7d5ecc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a7d5ecc0ed6512d9d2a035b162f90b2ab9a70461))


### Bug Fixes

* **ci:** repair claude-code-review workflow — invalid YAML meant it never ran ([#896](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/896)) ([ce62891](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ce62891caed2ad53c7ad17d5bc4b1bbad24d75d1))
* **deploy:** sweep rename-orphan containers so one bad converge can't wedge every deploy (refs [#831](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/831)) ([#895](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/895)) ([d8ece2f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d8ece2f3a8eafce939a3ff606e530549c64cbaee))
* **engagement:** multi-route comment + reaction locator chains, verified end-to-end (closes [#816](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/816), closes [#901](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/901)) ([#899](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/899)) ([20634ad](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/20634ada79502a5cc2f8ece209102b30591ae53f))

## [0.119.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.118.0...v0.119.0) (2026-08-01)


### Features

* **security:** identity + session hardening — public_uid, hashed sessions, httpOnly cookie, auth limits (refs [#745](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/745)) ([#869](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/869)) ([4b3d370](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4b3d3708a5cc685cd0215c595f564972ebcecd90))


### Bug Fixes

* **engagement:** re-ground the feed sort control and record the sort each scan ran against (closes [#817](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/817)) ([#861](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/861)) ([b6dc01b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b6dc01ba24e8d886d0121eeb6f145e3e940f9f96))
* **errors:** stop the caller restating a reaction failure that already warned ([6956ec5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6956ec5b5eacaf5dd881f3abd75c809e095213c9)), closes [#878](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/878)
* **errors:** stop the caller restating a reaction failure that already warned (closes [#878](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/878)) ([#892](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/892)) ([6956ec5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6956ec5b5eacaf5dd881f3abd75c809e095213c9))

## [0.118.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.117.0...v0.118.0) (2026-08-01)


### Features

* **auth:** renew LinkedIn tokens daily and show the expiry countdown (closes [#600](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/600)) ([#854](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/854)) ([4e30859](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4e3085912fe35c752bb4e2667ee7bba1128e3fdf))
* **models:** carry each candidate's Ollama Cloud usage level through the benchmark ([#842](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/842)) ([#864](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/864)) ([9465ff2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9465ff26e4e4570ce9d09b2220cf12bf7059c803))


### Bug Fixes

* **errors:** stop the React-toggle miss filing a third defect for one unreadable card (closes [#877](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/877)) ([#890](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/890)) ([74c81b4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/74c81b4d94f2698416663c50060712c44fd7a96e))
* **groups:** rotate past a group LEM cannot post in (closes [#858](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/858)) ([#871](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/871)) ([89c3320](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/89c3320cc35afb2cd833cc2d260fa06cb9b6330d))
* **observability:** re-ground comment sort control + expose unreadable readings ([#818](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/818)) ([#860](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/860)) ([a9424f3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a9424f3b4fe48e51521e2ac772fb8e6bcb1d86d7))

## [0.117.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.116.0...v0.117.0) (2026-08-01)


### Features

* **ui:** collapse per-post performance on the dashboard by default ([ca058b5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ca058b522e1e2c7ed170becb3aefa19981f576da)), closes [#808](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/808)
* **ui:** collapse per-post performance on the Home dashboard by default (closes [#808](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/808)) ([#870](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/870)) ([ca058b5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ca058b522e1e2c7ed170becb3aefa19981f576da))
* **ui:** prompt to refresh when /api/app-info reports a new release (closes [#754](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/754)) ([#867](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/867)) ([cf99c67](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cf99c6750c06572733165bbc9e5e60159e8bfb65))


### Bug Fixes

* **engagement:** hard-filter the [#478](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/478) reply composer so the post's main comment box can never win (closes [#886](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/886)) ([#889](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/889)) ([cf7d8e7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cf7d8e786e81ae4b0552ac564298eecce873e4cf))
* **engagement:** scope the inline comment composer to its own post card (closes [#876](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/876)) ([#884](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/884)) ([f0f97bd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f0f97bd4b16daba5ca8d29ea34cca7f814bd646c))
* **engagement:** scope the own-post reply composer to its own comment (closes [#883](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/883)) ([#887](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/887)) ([c9a87eb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c9a87eb8a5003ed2ebb98861939437a8cab4a72b))
* **errors:** stop the group-feed sort-control miss escalating as a defect (closes [#872](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/872)) ([#879](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/879)) ([84bb3df](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/84bb3df5bc3ce13a8df6afaf047afa08c2233fa8))
* **errors:** stop the optional reaction fly-out miss escalating as a defect (closes [#873](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/873)) ([#880](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/880)) ([21592f6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/21592f64600ffde3914c0b2ffdadf38912cea9f0))
* **errors:** stop the post-click reaction-state miss escalating as a defect ([b17aa53](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b17aa534c81200e3b4272473f6595919e10811f5))
* **errors:** stop the post-click reaction-state miss escalating as a defect (closes [#875](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/875)) ([#882](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/882)) ([b17aa53](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b17aa534c81200e3b4272473f6595919e10811f5))

## [0.116.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.115.0...v0.116.0) (2026-08-01)


### Features

* **content-generation:** allow regeneration of all post types with suggestions (closes [#794](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/794)) ([#857](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/857)) ([d914d22](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d914d221e955b904b88c7b892bb6690455eaf0eb))
* **groups:** make group posting a visible, per-group choice (closes [#769](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/769)) ([#856](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/856)) ([121aeef](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/121aeefd4a78496f8b3b48e7db9b30a8bca19c41))
* **marketing:** keep the YouTube OAuth token alive — weekly probe, DB storage, preflight (closes [#742](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/742)) ([#846](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/846)) ([e309aec](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e309aecde91fb219d9b63f803a388e001a1f771d))
* **observability:** group a post's whole generation chain into one $ai_trace (closes [#746](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/746)) ([#851](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/851)) ([34d93b2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/34d93b285036765c67890bd9e57d951d3b374674))


### Bug Fixes

* **ai:** guard second-wave comment against LLM responses with missing choices (closes [#768](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/768)) ([#853](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/853)) ([512016b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/512016b993eb2246eb56111d1cbaf80a2081f084))
* **analytics:** explain and widen the subset of posts the dashboard measures (closes [#809](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/809)) ([#859](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/859)) ([9ce0e95](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9ce0e95f0cff50663319fdd97733777f5c2b2334))
* **ci:** fast lane must wait for release-please, not for "a release PR exists" ([#847](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/847)) ([9aa9bdb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9aa9bdb76f9e07b3eadf049a89c5694fb31ee3a0))
* **content:** keep URLs byte-identical through sanitize_for_linkedin (closes [#823](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/823)) ([#863](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/863)) ([59a5a54](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/59a5a546443181008408b00b831ad1af54fb178a))
* **feedback:** stop a GitHub DNS blip from filing itself as a defect (closes [#767](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/767)) ([#852](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/852)) ([ec15250](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ec152509467fa01ce355563a59e595dbd9fc5c0a))
* **litellm:** use the bare catalog ids for the agent-lane Ollama tiers (closes [#844](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/844)) ([#866](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/866)) ([92a2aaf](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/92a2aafd3fa4854f956aced3ff15e9313cfcb75b))
* **security:** don't let a failed cookie write delete the user's LinkedIn password (refs [#745](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/745)) ([#848](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/848)) ([ef0587f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ef0587faac8a4005e918d97cb9e3b18775d9aa98))


### Documentation

* **deployment:** version milestones and owner-triggered 1.0.0/2.0.0 procedure (closes [#738](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/738)) ([#850](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/850)) ([14adc45](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/14adc45bc37ab0cc36bffd8ef20c2255cf1a16e6))

## [0.115.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.114.0...v0.115.0) (2026-08-01)


### Features

* **models:** refresh the Ollama Cloud roster — deepseek-v4-flash + gemma4:31b (closes [#717](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/717)) ([#843](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/843)) ([db04098](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/db04098c0c6b78f2269c7bb227d5ee7b065d69fc))
* **security:** encrypt LinkedIn secrets at rest + cookie-only default (refs [#745](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/745)) ([#807](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/807)) ([f514241](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f514241522da675970ae360af1cd08b12e687e10))


### Bug Fixes

* **agent-pipeline:** reattach unpushed branches + phase guard reads what a PR actually closes ([#832](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/832)) ([b92940d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b92940d4398e7036a2d74e29cadd6ae8606f4078))
* **deploy:** resilient worker converge + verify + early tag persist (closes [#831](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/831)) ([#840](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/840)) ([0fa4de4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0fa4de4a106881e81dffe1be3568411c8da566cc))


### Documentation

* **analytics:** SPA posthog-js does transmit — the grid result was a proxy artifact (closes [#834](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/834)) ([#839](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/839)) ([e99de15](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e99de1583ee6049be9fa19f304ceb6dc5e997664))

## [0.114.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.113.2...v0.114.0) (2026-07-31)


### Features

* **feedback:** admin panel for feedback triage (closes [#793](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/793)) ([#836](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/836)) ([ff7d5c6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ff7d5c6020686387c32e3eed8b7473edd6368c1b))


### Bug Fixes

* **dms:** separate an unapproved Catch-up backlog from an empty queue (refs [#792](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/792)) ([#833](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/833)) ([5e5bfb8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5e5bfb88629b27410fc1705c345d9aaa2239c4ca))
* **engagement:** center the comment composer before clicking it (closes [#815](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/815)) ([#838](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/838)) ([07a85ed](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/07a85ed7c9169bc05e5bb5bee1f28482d1a50b1c))
* **replies:** confirm email forwarding from a forwarded email, not just our click (closes [#813](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/813)) ([#837](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/837)) ([c6caa76](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c6caa76d9b2b9e28117ae8e3337b82b85f12d65a))

## [0.113.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.113.1...v0.113.2) (2026-07-31)


### Bug Fixes

* **build:** install ca-certificates so posthog-cli can upload source maps ([#827](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/827)) ([48832db](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/48832dba9a2ef3396b4f25b74859589c5fcdecbf))
* **newsletter:** article editor is two screens — stop gating publish on the editor screen (closes [#804](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/804)) ([#828](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/828)) ([24cf213](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/24cf21347f6d858c561a364e7a1b5ad255304e4d))


### Performance Improvements

* **ci:** cut time-to-production from ~3h to minutes — deploy reorder, working CodeQL gate, release fast lane ([#829](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/829)) ([520194c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/520194cafde27a929cb64920a5fdeb34ea81e231))

## [0.113.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.113.0...v0.113.1) (2026-07-31)


### Bug Fixes

* **dms:** make every LinkedIn Catch-up run reportable (refs [#792](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/792)) ([#825](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/825)) ([a64d231](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a64d231ffdbce25b9e78df4495bd3ff570f9e6fb))
* **scheduling:** never convert a picked time against a guessed timezone (closes [#774](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/774)) ([#819](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/819)) ([3ffc741](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3ffc7411154b1843313793aad027583a87c283b6))

## [0.113.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.112.1...v0.113.0) (2026-07-31)


### Features

* **avatar:** likeness fidelity, preview gate + usage guardrails (refs [#744](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/744)) ([#812](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/812)) ([5bda0cd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5bda0cddce66553f6b2418c7475bb28efdd5584c))


### Bug Fixes

* **observability:** escalate recurring warnings so real defects reach error tracking ([#821](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/821)) ([73a86f4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/73a86f469d5d172ef7fae860bf8e7f8f94d5ff8f))

## [0.112.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.112.0...v0.112.1) (2026-07-31)


### Bug Fixes

* **agent-pipeline:** reap abandoned agent:working claims + make the Ollama lane gauge see a broken proxy ([#810](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/810)) ([10fd70c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/10fd70c03b69247776b4b91a6f5009a6c7f8fe00))

## [0.112.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.111.1...v0.112.0) (2026-07-30)


### Features

* **linkedin:** article editor selector ladder + live validation (refs [#771](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/771)) ([#789](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/789)) ([b96cd60](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b96cd608ea1c0a06e58300638a9a38e753dd71f3))


### Bug Fixes

* **replies:** prevent duplicate replies to own posts (closes [#775](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/775)) ([#797](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/797)) ([503faad](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/503faad900115179acd0ad70a49aee4836e2bc03))

## [0.111.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.111.0...v0.111.1) (2026-07-30)


### Bug Fixes

* **db:** guard empty POST log rows for post_url lookup (closes [#800](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/800)) ([#801](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/801)) ([36f1c2a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/36f1c2a0da00d0289baec28e57b24310f0197d0d))
* **db:** guard empty POST log rows in get_post_url_from_log_for_user and get_post_message_from_log_for_user (closes [#800](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/800)) ([36f1c2a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/36f1c2a0da00d0289baec28e57b24310f0197d0d))

## [0.111.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.110.0...v0.111.0) (2026-07-30)


### Features

* **ui:** add Privacy Policy and Terms pages with footer links (closes [#772](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/772)) ([#798](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/798)) ([0a364bb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0a364bb20f4a8b966ad3f57098adf72dd0c18b8c))

## [0.110.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.109.0...v0.110.0) (2026-07-29)


### Features

* **content-generation:** add date selector for filtering posts by date range (closes [#727](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/727)) ([#795](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/795)) ([8cc08ff](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8cc08ffc062a49b5c1a4130e722b12de75975885))

## [0.109.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.108.0...v0.109.0) (2026-07-29)


### Features

* **ui:** allow regenerate-with-guidance on pending/rejected text posts (closes [#778](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/778)) ([#790](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/790)) ([4a50b8c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4a50b8ccd1c30b8bdac1a1a3fa52cf1c2d047920))

## [0.108.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.107.0...v0.108.0) (2026-07-29)


### Features

* **marketing:** affiliate program Part 1 — default-enrolled status, opt-IN promo consent, FTC disclosure gate ([#750](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/750)) ([ff61ee8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ff61ee8817e2a8b14ae2f5ed28e34f97701f0478))

## [0.107.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.106.0...v0.107.0) (2026-07-29)


### Features

* **ci:** add per-PR CodeQL quality gate (Option B) ([d0b9431](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d0b943141a8dbba1df82cd469553c291feceb3f6))
* **ci:** per-PR CodeQL quality gate (Option B prevention) ([#782](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/782)) ([d0b9431](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d0b943141a8dbba1df82cd469553c291feceb3f6))


### Bug Fixes

* **ci:** replace removed appleboy/ssh-action script_stop with set -e ([#776](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/776)) ([59c985e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/59c985e2ed72ce5ababe8e345e3db288bb978bcf))
* **ci:** use v5 input names on amannn/action-semantic-pull-request ([#783](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/783)) ([19503ed](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/19503ed7f9b203b03b213dac055d8443a79c3e96))
* **tests:** freeze time on plan-window tests so the last 1-2 days of every month don't break them ([#784](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/784)) ([ad333e9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ad333e9906abcd905bceace2fa4a4b965aa61f44))


### Documentation

* recompress CLAUDE.md under 40k chars + add size guard ([#779](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/779)) ([f0bd883](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f0bd88325088283ae8eb5357c45ce5842333e699))

## [0.106.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.105.0...v0.106.0) (2026-07-28)


### Features

* **automation:** alert on newsletter publish None path with failed step (closes [#747](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/747)) ([75f1c48](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/75f1c48c7a5652473dfe3a19640fed96658c893c))

## [0.105.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.104.0...v0.105.0) (2026-07-28)


### Features

* **content-generation:** Add rejection reason field and prompt to regenerate rejected posts (closes [#713](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/713)) ([22d676c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/22d676c7cf9445d8e28c64d80a81cd3bf6bc2d6e))
* **content-generation:** add rejection reason field and regeneration prompt (closes [#713](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/713)) ([926bd83](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/926bd834267fae0f88a3a5165c27f6733ef93087))
* **ui:** reorder Story Bank card after Publishing card (closes [#722](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/722)) ([ea55e6c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ea55e6c3016f9b3e32175f0cca4312bc497dbf06))
* **ui:** reorder Story Bank card after Publishing card on content settings (closes [#722](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/722)) ([b2faa61](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b2faa619ed8fd7a87744f43e7542256f64018a01))


### Bug Fixes

* **carousel:** make PPTX picture insertion robust to non-picture placeholders (closes [#729](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/729)) ([704baa3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/704baa38a152812ea1fc307a1f2a9e7b649a9666))
* **db:** use structured logger instead of myprint in new rejection reason functions ([44577e5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/44577e51506af0bd42c50c505e921798e3451f12))
* **ui:** allow optional guidance in regenerate mutation (fixes [#713](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/713)) ([e58975f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e58975fd15eaec13b3891c6f9b12039775da4596))

## [0.104.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.103.0...v0.104.0) (2026-07-28)


### Features

* **infra:** add Ollama-cloud agent lane aliases + LiteLLM gunicorn/resource limits ([0c32d21](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0c32d21687d3eaf6a90e81f7b559637e1054d8fd))
* **infra:** daily issue triage with impact-first rubric (closes [#748](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/748)) ([4202148](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4202148f4b0e65722f3b7c3d4cff932497586e84))
* **infra:** daily issue triage with impact-first rubric (closes [#748](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/748)) ([a39db82](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a39db8202d5ab9a4242d2c4dbbb2063fb492e1ee))
* **infra:** Ollama-cloud agent lane aliases + LiteLLM gunicorn/resource limits ([52908b3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/52908b3e411698f1967df07bff7a67d502b0208f))
* **outbound:** resolve the brand account as user 1 by convention (closes [#736](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/736)) ([2ea1f65](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2ea1f65c86516a11a234ceaeb2463204a1923d18))


### Bug Fixes

* **deps:** regenerate package-lock.json for vitest 4.1.10 + jsdom 30 ([24d3ef4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/24d3ef439d63851c6cb4dc0301a0ddba15734671))
* **observability:** attribute LLM spend to serving model, add shadow cost, vendor price map (closes [#752](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/752)) ([5af6ac7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5af6ac7985e0aaba67e5d744dd7decd376224ea2))
* **observability:** attribute LLM spend to serving model, add shadow cost, vendor price map (closes [#752](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/752)) ([8e40ae5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8e40ae5804e670a723cc03840f9a4dbde5593b31))

## [0.103.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.102.0...v0.103.0) (2026-07-27)


### Features

* **infra:** dedicated debug Grid node with published noVNC (a 9th, not a borrowed slot) ([a16069e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a16069e111483458c2ecb27b277ca06d32d20fad))
* **settings:** make the user's LinkedIn display name a required setting ([#731](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/731)) ([631da4c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/631da4c74d284b1386ae90eda8f049a1e666b965))


### Bug Fixes

* **automation:** resolve the LinkedIn message thread through a route ladder (closes [#731](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/731)) ([dae5a78](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/dae5a787da7a3a89ff8c3fea8527c72564c5f2b2))
* **automation:** resolve the LinkedIn message thread through a route ladder (closes [#731](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/731)) ([afdc3df](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/afdc3df8d44b630d64c98b7bc906260809165a4b))
* **automation:** stop the thread ladder resolving the wrong person's conversation ([56fd5a0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/56fd5a0ff25806ce5f362814ea6f6476e2b1abb3))
* **content:** one story anchor per deck + a reference-value gate on carousels (closes [#728](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/728)) ([fd7f7ac](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fd7f7acf076dabe26c157876df3d7ba0858740b4))
* **content:** rotate the carousel's story anchor and stop narrative decks passing the gate ([021569d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/021569d102a74b02db4335f596018af5df1ba4e8))
* **infra:** alias the Grid hub as selenium-chrome so pre-Grid callers keep working ([64f9433](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/64f94336c9835ad2ab61342daaa55f36c64d0b20))
* **infra:** keep the 9th Grid node out of the saturation signal, and guard the rollback direction ([d15ebdd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d15ebdd60d175b84dcd933c6616c1d5fb92811e0))
* **infra:** make the Selenium Grid the deployed topology (it was reverted by every deploy) ([e4fb8b3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e4fb8b36714566e4b4dcb4f7bdf602610af13ca0))
* **infra:** make the Selenium Grid the deployed topology (it was reverted by every deploy) ([e19b3b7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e19b3b7fc76a93c6b7fbdff39113e2b2a7edcfed))
* **infra:** restore the deploy fixes the alias commit reverted, and keep the alias ([a8bd905](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a8bd905c04bd129f82eb868e05ac1f96ba847dfe))
* **infra:** stop the topology read aborting the deploy, and drain before evicting the browser ([2f5cf77](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2f5cf772c735c4105a8b33961362f136c221b1d1))
* **outbound:** give company-page invites their own pacing budget + report session failures ([730ce58](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/730ce584868f83d5d4be7e8099fc584877ac3827))
* **outbound:** throttle company-page invites into a paced daily drip (closes [#732](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/732)) ([5240970](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/52409704d6291301acbb0450ff6100d982aa9462))
* **outbound:** throttle company-page invites into a paced daily drip (closes [#732](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/732)) ([8c614dc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8c614dc53aa856cee90297fd5e41c3a6e2ee1fe9))
* **ui:** let comma-separated settings fields accept commas and spaces (closes [#638](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/638)) ([3b39f08](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3b39f08ccad4dc977e0c1b96aae6a4886ccb3b96))
* **ui:** let the comma-separated settings fields accept commas and spaces ([0002985](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0002985625c4de980ed99832e850457ec8502f46)), closes [#638](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/638)
* **ui:** never reload an offline tab on a chunk-load failure ([d577e70](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d577e706d89150633c3c0ae57f76ec2986c5e4ce))
* **ui:** recover from stale lazy chunks after a deploy (closes [#743](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/743)) ([72fdbe2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/72fdbe299f778d984dce56c1558747e1fa3275a5))
* **ui:** recover from stale lazy chunks after a deploy (closes [#743](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/743)) ([640914d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/640914d271fdf212da01a771e5ac8f229cee0290))


### Documentation

* **workflow:** make the phase audit catch the case it cites, and say how to clear the hold ([36860ad](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/36860ad54f818051feb4b78c8f06be1fa80ca7ef))
* **workflow:** phased-work rule — an issue closes only when all its acceptance criteria are met ([62725d7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/62725d75dde1aeba168f0d6737eb1bfc3b1efbd5))
* **workflow:** phased-work rule — one phase, one issue ([dfd13e1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/dfd13e1c54990c8d80cec13eadf88f9853ad6ee2))

## [0.102.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.101.1...v0.102.0) (2026-07-27)


### Features

* **models:** benchmark cron-detected model candidates against tier contracts (closes [#721](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/721)) ([cb9a172](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cb9a1727ee2e0caeb8dddb7649faefe9362e4a2a))
* **models:** benchmark cron-detected model candidates against tier contracts (closes [#721](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/721)) ([f25907e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f25907e2ceb516ec57f3e4fc103c7021ec1dbe7f))
* **ops:** scan Ollama Cloud retirements + new models before they bite (closes [#716](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/716)) ([55e1c33](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/55e1c3386bbfb368ec4c7cbf2033fdb9caca96a4))
* **ops:** scan Ollama Cloud retirements + new models before they bite (closes [#716](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/716)) ([18731fc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/18731fc45ecda395a3726cbd31da191461e5a81e))


### Bug Fixes

* **content:** bound pull-forward on every generated post, drop stale empty reasons ([bf9fc3a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bf9fc3a09c2d58e9fb9ecda20b2173471c41a81e))
* **content:** explain an empty Generate run and pull planned posts forward ([d17e4be](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d17e4be3fbfe7b264932f9826c9e64423cbd575c)), closes [#719](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/719)
* **content:** explain an empty Generate run and pull planned posts forward (closes [#719](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/719)) ([6df057a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6df057a04c6801c32f28fc0f53d20e4fb6ae81f8))
* **feedback:** anchor the repair parse to our own provenance block ([088223a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/088223a66d558f0ddf5628c70b09a0bc1fee0e41))
* **feedback:** repair auto-filed issues whose labels never landed (closes [#718](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/718)) ([8846ab1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8846ab1f5f0a8ba00cc97e01d043b971d12dbe2d))
* **feedback:** repair auto-filed issues whose labels never landed (closes [#718](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/718)) ([a79983f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a79983f1b0954de41458795f1d8f2461417074e0))
* **infra:** warn when the appreciation-DM window drifts off its pinned midpoint ([6fe7107](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6fe7107f4b48868ae8552dfe76fcdebf6b35f772))
* **models:** keep the benchmark leaderboard and judge evidence honest ([70e81b3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/70e81b3f3ebd66188cb5a8fc4ff6d185048dc06a))
* **ops:** validate remote model names + file catalog issues from the plan already read ([23ec297](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/23ec2976383023cfaea4659788a0bbc7d626107a))


### Performance Improvements

* **infra:** open the appreciation-DM window half its width early (closes [#696](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/696)) ([4bdbfa5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4bdbfa50028077a9f4b5c3f662492329504a9277))
* **infra:** open the appreciation-DM window half its width early (closes [#696](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/696)) ([b06adc5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b06adc5d4efb4bf9ff0051797b491408acdd89e5))


### Documentation

* agent workflow playbook — issue/PR labels, Decision Comments, review economy ([075e3c0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/075e3c0cfda64156fbf391bf014337bc2d8c6e2d))
* agent workflow playbook (issue/PR labels, Decision Comments, review economy) ([b392aa0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b392aa020fc3cc9abe9de69f5d08fffc7c30d6eb))
* C1 confirmed live — API document posts render as document cards (closes [#644](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/644)) ([766b1ab](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/766b1abc4b463a2caaab4469e421b27389173c94))
* C1 confirmed live — API document posts render as document cards (closes [#644](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/644)) ([d5c1b4f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d5c1b4f548fd3ee155581e089706086e91160a92))
* correct playbook claims that silently no-op (un-park, fork PRs, model label) ([c5214a3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c5214a35338fd6a47a9816e5d7f9745f8378da21))
* remove the stale "C1 unconfirmed" passages the confirmation left behind ([e8caaa9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e8caaa92032106f51c5f6d4dc59f944b0a99aaf6))

## [0.101.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.101.0...v0.101.1) (2026-07-27)


### Bug Fixes

* **feedback:** attach labels/assignees after issue creation (closes [#598](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/598)) ([832781b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/832781b25a4e8b6057631fef968e7ddcc2514f2a))
* **feedback:** attach labels/assignees after issue creation (closes [#598](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/598)) ([a0c0a3d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a0c0a3da40af6dc2c410fbbbc8899f7d7b270da6))
* **feedback:** page on a refused triage status and pin the ENUM across ALTERs ([c43da98](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c43da98d6465e035d301cf275060e4cd0a4f8831))
* **feedback:** reject unknown triage statuses before the ENUM write (closes [#668](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/668)) ([ebc291c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ebc291c45e9609a5af84a9d66ac44fcb5edfcfe2))
* **feedback:** reject unknown triage statuses before the ENUM write (closes [#668](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/668)) ([1228ffd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1228ffdc3b2f293c1badb1d8aa1d3efeb4c6f69b))
* **ui:** Save All no longer hidden behind the feedback widget (closes [#596](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/596)) ([ac2eb85](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ac2eb85bc184e66311aab7b47e584e93faf0c3e8))

## [0.101.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.100.0...v0.101.0) (2026-07-27)


### Features

* **analytics:** PostHog Endpoints stats panel + deploy-time release annotations (closes [#654](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/654)) ([912d037](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/912d0379509c6a188cce4328257aec86b989c303))
* **analytics:** PostHog Endpoints stats panel + deploy-time release annotations (closes [#654](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/654)) ([14fd010](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/14fd0100b2eff08bd6a711f9b659a9b7f02bc042))
* **analytics:** probe the member post-stats API as a scrape replacement (closes [#645](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/645)) ([c19a695](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c19a695cb09e4c508f31680166755d004a8521eb))
* **analytics:** probe the member post-stats API as a scrape replacement (closes [#645](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/645)) ([4e6bc48](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4e6bc4889b1c448a8db5f0c41e13a252b7623391))
* **content:** configurable posting days, default Mon-Fri (closes [#581](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/581)) ([38348e4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/38348e41aff0c8c6d2fe5fa90a76c3b4d41099a8))
* **content:** configurable posting days, default Mon-Fri (closes [#581](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/581)) ([7e96835](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7e968355e6469d3ed987367aee351d8b5e87ee70))
* **engagement:** close the owned-asset CTA loop (closes [#624](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/624)) ([5537903](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5537903538840be0538454793b5124d2b684818a))
* **flags:** runtime feature flags with local evaluation + SPA bootstrap (closes [#651](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/651)) ([80495fa](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/80495faba00b9674dc09850fb278800bed75b028))
* **flags:** runtime feature flags with local evaluation + SPA bootstrap (closes [#651](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/651)) ([3778ed9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3778ed9baaee19cafc3adce2f295d29219a1f21a))
* **marketing:** UTM discipline on every outbound link + channel dashboard (closes [#658](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/658)) ([5de646d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5de646dcbe9d32bc50a47e32d9a011cff8b866a8))
* **marketing:** UTM discipline on every outbound link + conversion goals + channel dashboard (closes [#658](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/658)) ([3722662](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3722662d0735b187120d84091224a56e6500040d))
* **observability:** nightly slop/ER quality telemetry + weekly regression alerts (closes [#630](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/630)) ([c5b431a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c5b431a62989e6a091d5975a98dc479fc1d12741))
* **observability:** port cost-routing + content A/B experiments to PostHog Experiments (closes [#652](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/652)) ([cd90060](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cd90060ef3a65ec84d6d25459966a2efa3c57c30))
* **observability:** port cost-routing + content A/B experiments to PostHog Experiments (closes [#652](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/652)) ([450e728](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/450e72845b12be637a951d18b05f59d1327719f6))
* **observability:** PostHog advanced-surface spike — CDP destination + evaluation doc (closes [#655](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/655)) ([d42d39e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d42d39e9f6f7e96f6ac355c3476dbd09b6a08b9c))
* **observability:** PostHog advanced-surface spike — CDP destination, evaluation doc (closes [#655](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/655)) ([449a674](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/449a674b8ebddbdde99dda09d07a384c3936bf78))
* **observability:** slop-score & engagement-rate telemetry with regression alerts (closes [#630](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/630)) ([01bb6b1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/01bb6b19e6d8e5a00b0b372201f1ee5c6c7d8c56))
* **surveys:** NPS/CSAT via PostHog Surveys, wired into the feedback loop (closes [#653](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/653)) ([0837a10](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0837a10774ca6654ba4e0fdd2e6594fa5335ccaa))
* **surveys:** NPS/CSAT via PostHog Surveys, wired into the feedback loop (closes [#653](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/653)) ([16de7c1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/16de7c16633d6f38d6da177de6a62ba3eb20b384))


### Bug Fixes

* **analytics:** tell a DAILY-aggregation 400 apart from a version gap in the stats probe ([65e69fb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/65e69fb95619c22eed0ccbe540546425b620768e))
* **automation:** close the remaining fail paths in the profile-viewer walk ([1df1f2d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1df1f2d33885fe594a092c1516d6acd7fc949075))
* **automation:** fingerprint the lost-invite error so it reaches PostHog issues ([a53b429](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a53b4298dd84b12b0503db4aece1c3020688d926))
* **automation:** make the connection-request note best-effort (closes [#573](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/573)) ([bd47a2b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bd47a2bfe5a9cd26bcf6b86a0a7f32e8fe5b05a0))
* **automation:** make the connection-request note best-effort (closes [#573](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/573)) ([7381a39](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7381a391ab259e8260abbbad0bc130eb6f64e0c9))
* **automation:** stop an empty profile-views page failing the viewer task (closes [#572](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/572)) ([dcee64d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/dcee64d6580e7eaaeabd213bee77d4e5e3af6b8c))
* **automation:** stop an empty profile-views page failing the viewer task (closes [#572](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/572)) ([37f6c4b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/37f6c4bb506ad8cffcb3def1deec7f6923da977c))
* **automation:** stop the missing-Connect-button error paging the cron (closes [#571](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/571)) ([bd3a32b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bd3a32bc6871029e58360b3007d734462c17a200))
* **automation:** stop the missing-Connect-button error paging the cron (closes [#571](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/571)) ([2316512](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2316512a4dab97f82a67d57285ea51d710144590))
* **ci:** don't let a checkout hiccup fail an already-successful deploy ([ca529b0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ca529b06dd398650422c6d102dd05dcb92dac509))
* **experiments:** carry the routing cohort through a PostHog outage ([453a038](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/453a038adaf3f18832fed846cbb70f3e74a27f01))
* **flags:** keep GET /api/flags outside the API bearer gate ([2f0dc0b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2f0dc0b6dddef28aa25d6665186abe94edbdbda8))
* **infra:** use production's own stagger salt in the load-test model ([237f7e9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/237f7e925b58dccddaaf59e0bc7b6a49303cd1ab))
* **marketing:** close three silent-failure seams in the [#658](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/658) UTM path ([1cd40bd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1cd40bd3e75eff559255faa88702fa6dec93901e))
* **observability:** bill the quality pass's embeddings, make the ER floor reachable, stop mixing similarity scales ([b79962b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b79962b05dbed1365c8f7636fa93f0bde6977ad2))
* **observability:** use `destination` not `internal_destination` for the 429-trip CDP function ([017ccb5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/017ccb53c258858aa1917fab10e15650458f6e58))
* **surveys:** launch newly-created surveys, and never claim an unshown ask ([ef08880](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ef08880feaf661b36abe88cca50bff1b72c66c44))
* **ui:** activity feed fills its card then inner-scrolls (closes [#583](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/583)) ([469d2a1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/469d2a141c1f45662083f34f4779d5ab286dccd3))
* **ui:** Dashboard Activity Feed fills its card then inner-scrolls (closes [#583](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/583)) ([e08635f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e08635fc5fd76f9c657d6419c61ac687704e5f13))


### Performance Improvements

* **infra:** teach the load-test model the shipped golden-hour stagger, re-run (closes [#634](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/634)) ([cab88a2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cab88a20147b8397fc62f3dfdfd692f60e48dbe6))
* **infra:** teach the load-test model the shipped golden-hour stagger, re-run (closes [#634](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/634)) ([2b435a0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2b435a0aa97e5e3a7bbeaaed095ebe257136807f))


### Documentation

* `docs/surveys.md`, plus a CLAUDE.md section and `.env.example`. ([16de7c1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/16de7c16633d6f38d6da177de6a62ba3eb20b384))
* **infra:** cost/feasibility spike — hosted grids vs self-managed VPS scaling (closes [#633](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/633)) ([e7c6c6b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e7c6c6b5b12b4b5dc4249038160e3181d8bddd12))
* **infra:** cost/feasibility spike — hosted grids vs self-managed VPS scaling (closes [#633](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/633)) ([cb17474](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cb1747451d5dedb9bb51afec1a0a0585a44d6d39))
* **infra:** key the cost spike to [#634](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/634)'s measured stagger curve ([e8cdf14](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e8cdf146bfa737bc27999132a7f97df769a08297))
* **infra:** recompute Fargate/EC2 pricing against the corrected 15/28-session curve ([a7db39d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a7db39d8b8cfb638d91b943a742a63c37d6511bd))
* **infra:** rule out the hosted grid market entirely — self-managed only ([a70db9e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a70db9efa747031b166bc354e74d4d136406aafd))

## [0.100.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.99.0...v0.100.0) (2026-07-26)


### Features

* **engagement:** auto-pause automation when reach step-collapses (closes [#629](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/629)) ([275edab](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/275edab903a86301ab50e961e88541068f76a671))
* **engagement:** auto-pause automation when reach step-collapses (closes [#629](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/629)) ([027064d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/027064d5085919d30b478966ee39b07a630ed781))
* **engagement:** comment outcome tracking — replies, likes, Most-Relevant visibility (closes [#628](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/628)) ([4e54460](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4e544600515c7ac2dadddf2a75a91c262ca82e90))
* **engagement:** golden-hour report + 6-8h second-wave self-comment (closes [#622](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/622)) ([08866c1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/08866c1098ba74b402ca925d4a770992688596f8))
* **engagement:** golden-hour report + 6-8h second-wave self-comment (closes [#622](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/622)) ([9b7ca56](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9b7ca56a152c7f98209ef77aa3513c5db4b8d5a3))
* **observability:** capture $exception into PostHog error tracking (closes [#648](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/648)) ([ba38d78](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ba38d78b4c843f444317671024c4a6e327fdcec9))
* **observability:** capture $exception into PostHog error tracking (closes [#648](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/648)) ([5f7ada3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5f7ada3b246fe0f3cd13eaa8460d478ace7f690d))
* **observability:** emit $ai_generation from the LiteLLM proxy to PostHog (closes [#647](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/647)) ([979f67c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/979f67cfe080f3a5f296ebb9265087d4e627f715))
* **observability:** error-triggered + sampled session replay (closes [#649](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/649)) ([90b6e7f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/90b6e7faf244cd28b028cd9e7ba828a71f44f5a5))
* **observability:** error-triggered + sampled session replay (closes [#649](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/649)) ([f8d391b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f8d391bd0a591891ec4f1e3081ff81312572b10d))
* **observability:** KPI funnels, consolidated dashboards, threshold alerts + weekly report (closes [#650](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/650)) ([3a7606f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3a7606f81a43b41c8fdb49da512517d7f06048cb))
* **observability:** KPI funnels, Health/Growth dashboards, threshold alerts + weekly report (closes [#650](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/650)) ([605da7e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/605da7e1b6eed2fabf872e161a378b9378f38103))
* **observability:** LLM analytics — LiteLLM→PostHog native callback ($ai_generation) (closes [#647](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/647)) ([14d8326](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/14d832675773b77442ad9154b25e35afc72f6778))
* **ui:** instrument the SPA with PostHog — identify, autocapture, product events (closes [#646](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/646)) ([13adf35](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/13adf352e9d0651ff83c674bcbc130acefeebb1e))
* **ui:** instrument the SPA with PostHog — identify, autocapture, product events (closes [#646](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/646)) ([50ad82a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/50ad82aa210ca03816cff6860f2f17a6629cb0d9))


### Bug Fixes

* **brand:** stop the nightly brand sync from rewriting all 39 pref columns ([cb1184b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cb1184b11f173bc990a4e0f22185c2097cf187cf))
* **ci:** provision the integration DB schema so the [#628](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/628) tests can run ([38d7033](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/38d7033387a7a8b05bd2b72aad6af79580d562e0))
* **db:** stop partial engagement-prefs updates from wiping the row ([5b01134](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5b0113493be3748ecc28c7e8c5e060714723fda4)), closes [#639](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/639)
* **db:** stop partial engagement-prefs updates from wiping the row (closes [#639](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/639)) ([a675249](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a675249224e3cce9d3fd133f27e4c4d966eddd9d))
* **engagement:** case-fold the comment sort match and compare profile slugs exactly ([#628](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/628)) ([71983e9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/71983e90293284f450d19bdffb0450bea2fc3a45))
* **engagement:** keep measuring, and scope the resume, under a suppression pause ([ce515da](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ce515daf5ef1d6afda0d1466b05d573ed88bfd15))
* **engagement:** keep the second wave under the broker visibility timeout + report unrunnable sweeps ([5e8fd6e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5e8fd6e7af6031f2c3941f7f881b6343aad8d480))
* **observability:** make the error-triggered replay actually fire ([f4d09c6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f4d09c67e8e9b1087fbb5f5e94a185e93efabd49))
* **observability:** repair broken follower-delta tile and subscriber-less alerts ([ab40021](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ab40021dbd559a2ea91183b6262a6b4575e0ebfe))
* **ui:** report prefs_saved from a card's own Save button, mask the rest of the DM editors ([f6dc96b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f6dc96b1e260cdf5b8eba30ff626f1139a9266f3))

## [0.99.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.98.0...v0.99.0) (2026-07-26)


### Features

* **analytics:** follower & audience telemetry with daily capture (closes [#627](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/627)) ([219ebcc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/219ebccf5f82e11332584289282e393378e3e1c4))
* **analytics:** follower & audience telemetry with daily capture (closes [#627](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/627)) ([481cfec](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/481cfec890842c227ebe1f7ffd44b19a6f101dda))
* **content:** 70/20/10 content mix governor + artifact CTAs (closes [#618](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/618)) ([be6b4ed](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/be6b4ed6cb857e0838061abd4234eb5d544c601b))
* **content:** 70/20/10 content mix governor + artifact CTAs (closes [#618](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/618)) ([6bf2de3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6bf2de33e2189fef53e4804695045bb907beed6d))
* **content:** cadence reset — 2-4 posts/week on a fixed day-type calendar, sane hours (closes [#621](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/621)) ([b4484a9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b4484a902c20787cdf6dd3d112ed5a1263e5e5d6))
* **content:** cadence reset — 2-4 posts/week on a fixed day-type calendar, sane hours (closes [#621](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/621)) ([78a6c35](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/78a6c35b45e38bfb9f42fc1d1de9e5b0a5782b70))
* **content:** deterministic AI-slop lint across posts, comments, DMs, newsletter (closes [#625](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/625)) ([bfbe62f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bfbe62f2bdec6f53394d05c5c459ef06169325ae))
* **content:** deterministic AI-slop lint across posts, comments, DMs, newsletter (closes [#625](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/625)) ([dc46677](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/dc466773717f72f8bbc2887c1ed399fd576e4f58))
* **content:** save-optimized build-receipt & compendium archetypes with a no-fabrication guard (closes [#619](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/619)) ([6515688](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6515688d28265c152ee29da8d47db187208bd5d0))
* **content:** save-optimized build-receipt & compendium archetypes with a no-fabrication guard (closes [#619](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/619)) ([3b9360a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3b9360ad93c49cf9b07a2a906d61576581fb54a2))
* **content:** story bank & fact intake — human-sourced specifics as the content anchor (closes [#620](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/620)) ([9fd4f60](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9fd4f6044427466b677e0ecd30e018338cac12cf))
* **engagement:** human-pacing engine — read-time delays, schedule jitter, variable daily volumes (closes [#626](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/626)) ([a334ee5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a334ee524ecc40af588e58d336723b81af0f2054))
* **engagement:** human-pacing engine — read-time delays, schedule jitter, variable daily volumes (closes [#626](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/626)) ([dab8223](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/dab822355bd1a9ff7d508e2f38ff072adadf8907))


### Bug Fixes

* **analytics:** stop stacked LinkedIn cards handing one number to every metric ([4eabe17](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4eabe17423c73dbd53a227a8fc0c1e5d5c6ba094))
* **content:** drop two AI-slop lint false positives; lint group posts too ([704b32b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/704b32b974909ab0dacb9d748742ddce56b0a09b))
* **content:** hold the 24h post floor across planning runs ([3596db8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3596db8e18beaeb649dbf7f77d50c67bb99dac3b))
* **content:** stop the no-fabrication guard flagging stack version numbers, and keep fact-anchored archetypes off an anchorless carousel ([9691acb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9691acb1955362fd2469bfbf39ae3fd01487f601))
* **engagement:** adversarial review of the human-pacing engine ([#626](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/626)) ([6bb372a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6bb372a96261d1f88791fadbb8e36e0077cf8ea0))
* **outreach:** activate the network flywheel — clean names, skip 1st-degree, source the funnel, unblock DM nurture (closes [#623](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/623)) ([21ec617](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/21ec6178933e50d2d6141199b636e99eac75f546))
* **review:** anchor the verb-less meeting-ask patterns so they can't delete narrative ([32ee36c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/32ee36ce5f5fca8bbc8635606f942e05312bed3d))
* **review:** post-merge adversarial review of [#661](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/661)/[#662](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/662)/[#665](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/665) (closes [#670](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/670)) ([2d59e9c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2d59e9c8eca62cecb12a4df3e55e4ae945aa8edf))
* **review:** post-merge adversarial review of [#661](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/661)/[#662](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/662)/[#665](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/665) (closes [#670](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/670)) ([d8c6a0d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d8c6a0dd59493fcd9b51a9c04d4d4f8b0e1ab815))
* **review:** restore meeting-ask recall lost to the offer-verb narrowing ([60f0769](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/60f0769aa8299a9ace02beaef6806d3a32137dda))

## [0.98.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.97.1...v0.98.0) (2026-07-26)


### Features

* **engagement:** comment quality contract v2 + comment-side similarity gate (closes [#617](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/617)) ([d91a93b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d91a93bfd11717a8a903451cde49825f45724584))
* **infra:** zero-downtime blue/green deploys + 4x-daily batched releases ([e13d1b9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e13d1b9b20da325b7ef7246b15c4f177bef32d21))


### Documentation

* ground the restored marketing plan in the current repo layout ([069b912](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/069b912f290f13fea8abe78e7013a5a013e0799c))
* restore launch & marketing plan (deleted by adjacent branch cleanup) ([a68aa6d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a68aa6d078ff30ebeb2d0c5f8ef4ae3dc3003186))
* restore launch & marketing plan accidentally deleted by cost-plan branch cleanup ([23cc708](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/23cc708be556ff17a32fc18fb95640539ed46e6c))

## [0.97.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.97.0...v0.97.1) (2026-07-26)


### Bug Fixes

* **scripts:** address review threads on the live-validation probe ([9a2720b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9a2720bf200802bd172e08aa7b23602634156d3d))


### Documentation

* **format:** link the split-out follow-ups ([#644](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/644), [#645](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/645)) from the R1 note ([5ada4a4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5ada4a40ec73812a4efee94519b7183d996de0fa))
* **format:** live-validation findings — document post path + saves/impressions scraping (closes [#404](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/404)) ([12a3a6c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/12a3a6c80c1c5c832b737163e8ec729754dcb4dd))
* **format:** live-validation findings for document posts + saves/impressions (closes [#404](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/404)) ([c57f931](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c57f93153e2930fb4a3ed84e7b5cc3d826928203))

## [0.97.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.96.0...v0.97.0) (2026-07-26)


### Features

* **engagement:** target-creator roster (50/30/20) + on-topic-only commenting (closes [#616](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/616)) ([10d870f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/10d870f572805c9f0e81c714bacc072c736f7308))


### Bug Fixes

* **engagement:** address Copilot review on the roster PR ([1e82d48](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1e82d48a7564735c5b7dd01c5774e29ccb0ad856))

## [0.96.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.95.5...v0.96.0) (2026-07-26)


### Features

* **infra:** Selenium Grid horizontal path + concurrency/scale load test (closes [#556](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/556)) ([438be82](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/438be823e36d76ae25539babe18e3923fe32a952))


### Bug Fixes

* **infra:** pure-shell hub healthcheck, honest unreachable SLO rows, decision status ([d2eb724](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d2eb7244757ece320ec1e557f97a9cbc2194808f))

## [0.95.5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.95.4...v0.95.5) (2026-07-26)


### Documentation

* engagement growth analysis + Milestones 13-14 plan ([7060d86](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7060d86c15f3eed467bd26d1dfb9d375e24ab0c2))

## [0.95.4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.95.3...v0.95.4) (2026-07-26)


### Documentation

* **settings:** settings research + IA proposal for the config UX rebuild (closes [#558](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/558)) ([daa28a3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/daa28a3d09dbdfd41b9c219e60b6b36f4ffb6611))

## [0.95.3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.95.2...v0.95.3) (2026-07-26)


### Bug Fixes

* **automation:** stop paging on empty invitation manager (closes [#570](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/570)) ([ef98815](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ef988151abca8ccc5c39e01cf124f9f7da6bb033))
* **automation:** stop paging on empty invitation manager (closes [#570](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/570)) ([782b3eb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/782b3eb2d544fbe64e61da6bdffb02d61f110059))
* **scheduler:** scope the daily slot claim to its local date, gate DMs on session ([3522d30](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3522d30eaa91fa5e17b7450a5bc409b2d527c687))


### Performance Improvements

* **scheduler:** stagger the fixed-time engagement fan-outs per user (closes [#554](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/554)) ([5aea4a9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5aea4a90c6b38970bd8c8eb48748c2fb2442500e))

## [0.95.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.95.1...v0.95.2) (2026-07-26)


### Bug Fixes

* **automation:** drop dead pre-SDUI feed sort that paged 11x/24h (closes [#569](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/569)) ([8bc8834](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8bc883461b5947057c4e8161066c79be826bbb34))
* **automation:** drop dead pre-SDUI feed sort that paged 11x/24h (closes [#569](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/569)) ([c19facb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c19facbe8c419d45dc175eb512cbf76df7ff0d90))

## [0.95.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.95.0...v0.95.1) (2026-07-26)


### Performance Improvements

* **db:** MySQL connection pooling in get_db_connection (closes [#555](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/555)) ([1a77eb2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1a77eb2617fe269fc490f3d0bd64c1044c7f0b5c))
* **db:** pool MySQL connections per process in get_db_connection (closes [#555](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/555)) ([569676a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/569676a1d20fe9d7fc02b8cb30394eb2991abf59))

## [0.95.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.94.0...v0.95.0) (2026-07-26)


### Features

* **review:** surface PENDING gate reasons + remediation and let users tune the thresholds ([56465bd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/56465bdc261a7c81289f09622d5865667a930cdf)), closes [#421](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/421)
* **review:** surface PENDING gate reasons + remediation in Content Studio (closes [#421](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/421)) ([bf07237](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bf07237d30e516e9b27c64e071f306706a38cf6c))

## [0.94.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.93.1...v0.94.0) (2026-07-25)


### Features

* **infra:** raise Selenium session cap + lane concurrency for launch headroom (closes [#552](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/552)) ([0cb9489](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0cb948900a869bf17285c5b4e9a16a49b5d72dbb))


### Bug Fixes

* **capacity:** scope the prod-overlay guard to selenium-chrome + clear CodeQL alerts ([faacb3e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/faacb3e4c3483f5a3433037263d6bbb99cce29c8))

## [0.93.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.93.0...v0.93.1) (2026-07-25)


### Bug Fixes

* **video:** ambience-only, language-tagged audio for Veo renders ([#548](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/548)) ([8d8d114](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8d8d114fcea4f3e03a7ab5357238f12fc62f06ca))


### Documentation

* **avatar:** correct video_models module path in architecture table ([#548](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/548)) ([44bcf40](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/44bcf4095aa59078383bb755d827f50d59b18c6e))
* **avatar:** Phase 1 research — likeness/gender drift, video language, preview + guardrails ([#548](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/548)) ([7d64355](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d643557926ba3f39be6087189eb2b77f21a3e57))

## [0.93.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.92.2...v0.93.0) (2026-07-25)


### Features

* **infra:** dedicated se_prepost lane so pre-post commenting never queues behind the golden-hour loop (closes [#553](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/553)) ([c778c2a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c778c2ad32b1f4aced3afa2a33cae69d6d14c3b9))
* **infra:** dedicated se_prepost Selenium lane for pre-post commenting (closes [#553](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/553)) ([2bbde2f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2bbde2f16352848aacc5561e6e66bf33983de184))

## [0.92.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.92.1...v0.92.2) (2026-07-25)


### Bug Fixes

* **engagement:** clamp the pre-post feed-commenting window + per-post observability (closes [#547](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/547)) ([fe88820](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fe88820cbfd71d11572762530e407f7fe845a63b))
* **engagement:** key pre-post markers per task + address review threads ([90cc038](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/90cc03847c52d3abdd826d1215b050213e7495ae))
* **engagement:** reliable pre-post feed-commenting window + per-post observability (closes [#547](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/547)) ([be0143e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/be0143ef688e1c2fd94affff46d71750a805f5f6))

## [0.92.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.92.0...v0.92.1) (2026-07-25)


### Bug Fixes

* **scheduling:** one timezone contract from picker to executed instant (closes [#546](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/546)) ([30f34dd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/30f34dd0efc7199bb02332e7b6fc4a273af1a2de))
* **scheduling:** one timezone contract from picker to executed instant (closes [#546](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/546)) ([8511f20](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8511f20dcb7c735652a5d1b7434a552c0ce79742))
* **ui:** guard invalid DM schedule time before posting to the API ([b0480f9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b0480f9d8d0d0a7aadf9b1208aa681bb26cbd4c2))

## [0.92.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.91.3...v0.92.0) (2026-07-25)


### Features

* **content:** progress notification + status for post generation (closes [#545](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/545)) ([c273fcf](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c273fcf15f76efc90fab6aa0eb9c0d2eeb101a0d))
* **content:** progress notification + status for post generation (closes [#545](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/545)) ([3900836](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3900836010e3b804aba4139aa275b9d25dca3d5e))


### Bug Fixes

* **content:** contain post-generation failures and clear a failed dispatch ([#545](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/545)) ([27818f3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/27818f3f4d5ebee37365dbce8e36584d5de16344))

## [0.91.3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.91.2...v0.91.3) (2026-07-25)


### Bug Fixes

* **content:** bounded rolling forward buffer of ready posts (closes [#544](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/544)) ([b9e163e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b9e163eb3ad28ce36583dba7ba3948863a24517a))
* **content:** single-flight lock around the buffer top-up ([012223e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/012223eabdb4523e69003b59ad896375e0dc037e))

## [0.91.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.91.1...v0.91.2) (2026-07-25)


### Documentation

* **security:** auth/identity + at-rest encryption research & design (Phase 1 of [#568](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/568)) ([3924801](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3924801b675498982eb96289d29bbc21ddf3ee9d))
* **security:** record owner sign-off (1A 2A 3A) + correct citations/licenses ([35b9150](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/35b915030554d526140b670fe019c0541b069bd2))

## [0.91.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.91.0...v0.91.1) (2026-07-25)


### Bug Fixes

* **automation:** key feed comments on the activity URN from the card's ancestors (closes [#580](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/580)) ([a999f91](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a999f9122c9fa273bca0ac2b6d7ac4070873a36a))
* **automation:** key feed comments on the activity URN from the card's ancestors (closes [#580](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/580)) ([4d037e1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4d037e16f6e1434b11bc0ec76780d0b0eb0d87a8))

## [0.91.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.90.1...v0.91.0) (2026-07-25)


### Features

* **feedback:** auto-FAQ service — cluster recurring questions into FAQ entries (closes [#507](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/507)) ([e6ba911](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e6ba911d6a56c80d59692e262ccbc3c003ab012b))
* **feedback:** auto-FAQ service — cluster recurring questions/reviews into FAQ entries (closes [#507](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/507)) ([18550f7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/18550f75fd0cfc40912b49137f885d8f527a0007))


### Bug Fixes

* **feedback:** embed the redacted FAQ question, not the raw feedback body ([14f100b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/14f100b946575e539aea05558d681c0405fae630))

## [0.90.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.90.0...v0.90.1) (2026-07-25)


### Bug Fixes

* **logging:** set OTLP service.name so PostHog logs aren't 'unknown_service' ([10f3301](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/10f33014f9c9c57dd24040a3d11553077c0253e4))
* **logging:** set OTLP service.name so PostHog logs aren't 'unknown_service' ([158675d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/158675d89a8c0c04fc4a39f3ff55092eed86aa89))

## [0.90.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.89.0...v0.90.0) (2026-07-25)


### Features

* **ui:** auto-updating front-page FAQ backed by faq_entries (closes [#506](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/506)) ([3be3b5b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3be3b5b4f26751c7c000c75e997bd2fdc82652af))
* **ui:** auto-updating front-page FAQ backed by faq_entries (closes [#506](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/506)) ([8037d1a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8037d1a947648c74808491c4ad6bf642c22e6568))


### Bug Fixes

* **api,db:** fail-closed FAQ read and segment-boundary public-route matching ([11dce62](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/11dce628e0dfb72f32e7669fb21191f817d8e472))

## [0.89.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.88.0...v0.89.0) (2026-07-25)


### Features

* **marketing:** automated video-tutorial production pipeline (closes [#505](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/505)) ([c19c9cc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c19c9cc2797ac409ce4fed5c2e92fdd1c20ec5e0))

## [0.88.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.87.1...v0.88.0) (2026-07-25)


### Features

* **marketing:** dogfooding brand account under phase-gated outbound caps (closes [#504](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/504)) ([f8aab77](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f8aab774e1ce4e1c20f10d0967b764740d5afb97))
* **marketing:** dogfooding self-marketing — brand account under phase-gated outbound caps (closes [#504](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/504)) ([ac4d4a4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ac4d4a4c1458c120585b2aa248ff86fd40a5f91a))

## [0.87.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.87.0...v0.87.1) (2026-07-25)


### Bug Fixes

* **infra:** graceful worker shutdown + deploy maintenance mode (closes [#549](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/549)) ([7b67ff6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7b67ff67406094892a23ea4d419535a0d79ef3cb))
* **infra:** graceful worker shutdown + maintenance mode so deploys never lose in-flight Celery tasks (closes [#549](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/549)) ([08ca272](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/08ca272898727b0be4ee63f9712f798cb02abb76))
* **infra:** scale maintenance snapshot TTL + structured claim logs ([bc0aa52](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bc0aa52443857ad69d2c27a7d11d2344a2041364))

## [0.87.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.86.0...v0.87.0) (2026-07-25)


### Features

* **observability:** funnel event instrumentation for launch/marketing analytics (closes [#503](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/503)) ([4ff86ec](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4ff86ecdd225c4fe79f0fd4f0aeb6043ce1eb558))


### Bug Fixes

* **observability:** constrain explicit channels and handle user_id=0 ([1ded99a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1ded99a40e2606b71c57344b653c06c8312adf9c))

## [0.86.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.85.0...v0.86.0) (2026-07-25)


### Features

* **routing:** cost-aware model down-routing loop with A/B + auto-rollback (closes [#494](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/494)) ([975e66b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/975e66b1e74938c63eca3cfaaea5ec6e3cdecbfb))
* **routing:** cost-aware model down-routing loop with A/B + auto-rollback (closes [#494](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/494)) ([899ce4e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/899ce4e887599e456e632eb99c4f273ece36d89a))
* **routing:** ship the cost-routing loop dormant (owner decision 1A) ([63ca8f2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/63ca8f27abf4ec150306d14fb8eb54e6809666c0))


### Bug Fixes

* **routing:** address review — sha256 cohort hash, cohort clamp, quieter logs ([1e82960](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1e829600b8dfb25821016575e01888532b36abc9))


### Documentation

* **routing:** warn that the two cost-routing flags must be flipped together ([a1a0a79](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a1a0a79c16021af8cb5d5d1d87918b97bbc7d39b))
* VPS scaling & concurrency plan ([bf9dcd1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bf9dcd167dc9079ea2b995707fede46b3752e592))

## [0.85.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.84.0...v0.85.0) (2026-07-25)


### Features

* **cost:** cost_ledger table + DB helpers + media/proxy/infra cost capture (closes [#490](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/490)) ([95c3c17](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/95c3c17b4a6c55fb1cdd498150c381edf48e9d97))

## [0.84.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.83.0...v0.84.0) (2026-07-25)


### Features

* **billing:** early-adopter extended trial gated on a review (closes [#499](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/499)) ([4462cb3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4462cb3448cc9570e8f8c0d215cce5fbd406c48a))
* **billing:** early-adopter extended-trial (60d) auto-grant, gated on review submission (closes [#499](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/499)) ([e883de7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e883de79aa17c87b819b1245572ea770d5d27952))


### Bug Fixes

* **billing:** tie the early-adopter coupon to a still-live grant ([8c6b22c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8c6b22c1544056abba49ebd837fa969bc1ac0932))

## [0.83.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.82.0...v0.83.0) (2026-07-25)


### Features

* **feedback:** auto-changelog + notify reporters on shipped fixes (closes [#502](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/502)) ([e55e603](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e55e603fbb2d74f77c17dab4bf76f038881b23b0))
* **feedback:** auto-changelog + notify reporters on shipped fixes (closes [#502](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/502)) ([dd999d7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/dd999d7f2651f297f033b50abc619392ca651684))


### Bug Fixes

* **feedback:** resolve cluster-attached reports and gate early shipped acks ([db09ff0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/db09ff0a8f5778cd714fbf524d96c40eef171823))

## [0.82.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.81.0...v0.82.0) (2026-07-25)


### Features

* **feedback:** NPS/CSAT + review capture (closes [#501](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/501)) ([7446e6f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7446e6fde577b6d533e0410acbb2010db302f970))
* **feedback:** NPS/CSAT + review capture, review unlocks the extended trial (closes [#501](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/501)) ([61cca79](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/61cca79927ba3ac9c6722c71e102da9f472ff509))


### Documentation

* **feedback:** clarify survey_key ledger semantics in SurveyModal ([5141f2b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5141f2b06956d0385935e9ce3f1be1facdbfcc29))

## [0.81.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.80.0...v0.81.0) (2026-07-25)


### Features

* **onboarding:** activation checklist + stalled-user nudges (closes [#500](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/500)) ([ee0d0ca](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ee0d0ca1a3f3fd70e5af03090b40456940d2a6b2))
* **onboarding:** automated activation checklist + stalled-user nudges (closes [#500](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/500)) ([30c8bda](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/30c8bdac6326a6c93709c7007fdcae0be69b4c68))


### Bug Fixes

* **onboarding:** escape nudge email copy and use structured logging ([bb9a333](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bb9a33362737235680bf4a7cf943add87082a9d6))

## [0.80.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.79.0...v0.80.0) (2026-07-25)


### Features

* **feedback:** auto-file GitHub issues from classified feedback with dedup/clustering (closes [#498](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/498)) ([2d2e4b2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2d2e4b2bcfd7767df42a7b0b8cb4064d8610ca00))
* **feedback:** auto-file GitHub issues from classified feedback with dedup/clustering (closes [#498](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/498)) ([4668e83](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4668e83a12b506154c0f2fc5413d7841b24d64bb))


### Bug Fixes

* **feedback:** stop inflating the distinct-reporter demand signal ([54ec2dd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/54ec2dd9a1879831fb5ae2e8156a15fcecd412d1))

## [0.79.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.78.0...v0.79.0) (2026-07-25)


### Features

* **feedback:** LLM auto-classifier for captured feedback (closes [#497](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/497)) ([abeb9ce](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/abeb9cea20a94919a698c18000b329eece62beb7))
* **feedback:** LLM auto-classifier for feedback — category/severity/risk → real repo labels (closes [#497](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/497)) ([04e640d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/04e640dff866de2d9396310d630dc7eadc3183c1))

## [0.78.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.77.0...v0.78.0) (2026-07-25)


### Features

* **feedback:** in-app feedback widget + POST /feedback + feedback table (closes [#496](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/496)) ([739cca6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/739cca685edee67240443f55185488222fcf3946))
* **feedback:** in-app feedback/bug widget + POST /feedback + feedback table (closes [#496](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/496)) ([bf7385c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bf7385caa80404ff2462646f74d824a5a58aa7e6))


### Bug Fixes

* **feedback:** structured log_error on insert failure + close migration file handle ([d45a4ce](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d45a4ceaaf14347d423370a0400bf14395d9ac5e))

## [0.77.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.76.0...v0.77.0) (2026-07-25)


### Features

* **observability:** budget-threshold alerts + spend anomaly detection (closes [#493](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/493)) ([057fc40](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/057fc406cc52ae507296e0f2c3a4c0c7e260646a))
* **observability:** budget-threshold alerts + spend anomaly detection (closes [#493](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/493)) ([ef7c363](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ef7c3637909c405dfdf9b69e1e086ff859be739a))


### Bug Fixes

* **observability:** skip ledger-backed cost checks when cost_ledger is absent ([3828b82](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3828b82dbc60e619f14ec84f0e725f764b78e965))

## [0.76.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.75.0...v0.76.0) (2026-07-25)


### Features

* **observability:** PostHog cost/margin dashboards as code (closes [#492](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/492)) ([61cbeb7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/61cbeb7236a45ae65b3bb6a510bd615423d581a0))
* **observability:** PostHog cost/margin dashboards as code (closes [#492](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/492)) ([9a87747](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9a877474be6ac40c4c4a6dfc58e23e45a1e540b4))


### Bug Fixes

* **observability:** address Copilot review threads on the dashboards script ([ae8d8ef](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ae8d8efefb3bc4aa40100a00686db634a4c836b8))

## [0.75.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.74.0...v0.75.0) (2026-07-25)


### Features

* **observability:** margin/unit-economics report + cost block on the daily snapshot (closes [#491](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/491)) ([957662c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/957662cc08ba2e9cc2503ba324cef1c5bbddf0bd))
* **observability:** weekly margin report + cost/margin block on the daily snapshot (closes [#491](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/491)) ([d36080d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d36080d7d21626fe4e653ae76fe381abc160d87a))


### Bug Fixes

* **observability:** address review threads on the margin report ([4781a23](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4781a23f7eb005768fd590caa587fd968b771937))

## [0.74.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.73.1...v0.74.0) (2026-07-25)


### Features

* **outreach:** automate LinkedIn Catch-up trigger-event congratulations (closes [#482](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/482)) ([c51f4d3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c51f4d3f666266d9da1993b8d76def5893b100dd))

## [0.73.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.73.0...v0.73.1) (2026-07-25)


### Documentation

* cost, performance & margin observability plan ([c9cced7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c9cced7cc2c04e86cfc2b872800ecd99e629815b))

## [0.73.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.72.0...v0.73.0) (2026-07-25)


### Features

* **outreach:** smart connection targeting from content engagers (closes [#486](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/486)) ([5c8b47b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5c8b47b3c9e4463748f1a5203b98a5e89d443fe4))
* **outreach:** smart connection targeting from content engagers (closes [#486](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/486)) ([7366dca](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7366dca1e69a6e42906855c678c504bf1e985cc9))


### Bug Fixes

* **outreach:** guard connect-target limit=0 and env parsing ([a42d5d7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a42d5d7b59e8fd4218f953b3f8b9aa9b2ec66623))

## [0.72.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.71.0...v0.72.0) (2026-07-25)


### Features

* **outreach:** DM conversation auto-nurture (closes [#485](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/485)) ([5c7e944](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5c7e944e6d82dcca22302735d6f57fd41fc4089c))

## [0.71.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.70.0...v0.71.0) (2026-07-25)


### Features

* **observability:** per-user/per-feature LLM cost attribution (closes [#489](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/489)) ([22f5cbb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/22f5cbb77a4aaa7cad1b058565140305534d9904))
* **observability:** per-user/per-feature LLM cost attribution (closes [#489](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/489)) ([c514383](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c514383ef7cdeeff40b905700963e650c4484484))


### Bug Fixes

* **observability:** floor llm_call feature to system in track_llm_call ([8db3baa](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8db3baa53e38fa019666c868ba88c65004b7b3d3))

## [0.70.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.69.0...v0.70.0) (2026-07-25)


### Features

* **leads:** lead scoring & CRM-lite pipeline (closes [#484](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/484)) ([94f9547](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/94f9547c16b8cf3df681a2d557ca3260bf80a13f))
* **leads:** lead scoring & CRM-lite pipeline (closes [#484](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/484)) ([0c32717](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0c327173af1cdd094826cd9abbae1239d574e143))


### Bug Fixes

* **leads:** address Copilot review on the lead pipeline ([ecd8d30](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ecd8d30877654951ad6b7c056b91a3656298b5f4))

## [0.69.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.68.1...v0.69.0) (2026-07-24)


### Features

* **leads:** inbound-intent detection & hot-lead routing (closes [#483](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/483)) ([e341db9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e341db93731cd60b3c60d0d60e28380ebe44f8f7))
* **leads:** inbound-intent detection & hot-lead routing from comments/replies/DMs (closes [#483](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/483)) ([3a95af1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3a95af1c651ce43086648cd6504b0723f8478b36))

## [0.68.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.68.0...v0.68.1) (2026-07-24)


### Performance Improvements

* **tests:** make the unit lane hermetic — 49s → 20s CI unit run, same coverage (closes [#480](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/480)) ([73c85e1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/73c85e1a1da32149a8a9ef5579b7854f23313776))
* **tests:** make the unit lane hermetic and halve CI wall-clock (closes [#480](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/480)) ([b448616](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b4486161587d3e40e5d31d0f83116b840c66003d))

## [0.68.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.67.0...v0.68.0) (2026-07-24)


### Features

* **automation:** auto-follow-up on replies to our automated comments + harden reply sweep (closes [#478](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/478)) ([8680076](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/86800762581a0d4a4b5e2f296afc4fd07bc57934))
* **automation:** follow up on replies to our automated comments ([#478](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/478)) + harden reply sweep ([89eade3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/89eade3ae6a2614409d6949165a27926c9f27bfe))


### Bug Fixes

* **#478:** associate a reply to our comment via [@mention](https://github.com/mention), not just DOM nesting ([7936969](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/793696962bf079b68eaec83c18ce852be149d394))
* **#478:** comment react control is 'Open reactions menu'; add flyout Like fallback ([7e4ec6e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7e4ec6eaf4c2f5b6a4b59be6a3703255e84c9ffe))
* **#478:** hover the comment before reacting (action bar is hover-hidden/zero-size) ([56e8dd8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/56e8dd875281a5a8522bbf383591caf1d262d3ae))
* **#478:** real SDUI selectors for third-party comment threads (live-validated) ([10cbfd3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/10cbfd386582f8d3a3f63f806a93d0f76d78ef6e))
* **#478:** thread replies UNDER the comment (nearest composer), not a top-level comment ([5377b5e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5377b5e4bde5918787a7aa1b4961c59f51681a1e))
* **#478:** worker sets a tall viewport so a long post's comments lazy-render ([53b6e26](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/53b6e26347dec394e2efb7bb09ccec96af926194))

## [0.67.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.66.1...v0.67.0) (2026-07-24)


### Features

* **account:** Save All bar, unsaved-changes guard, placeholder chips ([39e7d9d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/39e7d9d36e8ab347a7214d059f1c8d16af17a545))
* **account:** ship the one-click LinkedIn extension to users ([e1fe2c0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e1fe2c0bb7d35ea8c00b5cc8d71983d21c865be6))
* **account:** ship the one-click LinkedIn extension to users ([f511dc7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f511dc75269c601d0e337acff5bfc1b60fb7ef8e))
* **ai:** add anti-ai skill reference files (for [#416](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/416) humanization) ([1b0f126](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1b0f12655fb0907950aca6afcbd01ab691eec0d5))
* **ai:** add anti-ai skill reference files (READER-mode humanization spec) ([ffcdab6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ffcdab6a58a4fce3cde80c27c539d7a42f17afaf))
* **ai:** profile synthesis generator + persistence (V48) + wire into generators ([2107efd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2107efd54e1b620a381eb1a9c80126154a5a2f1e))
* **ai:** unify newsletter blueprint/research/alignment into one shared content core ([fa3c040](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fa3c04080eb9a6d31f17c9f230b222b4e708f59d))
* **analytics:** outcome-tracked A/B variant harness (closes [#396](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/396)) ([5fdbc32](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5fdbc32d6037b41a464891ea5e56e1032a6f9bb5))
* **analytics:** outcome-tracked A/B variant harness (closes [#396](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/396)) ([f20ce66](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f20ce66f6a1b2b3773525fcf070a4459785355ce))
* **analytics:** real LLM token/cost + post-outcome PostHog events (closes [#397](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/397)) ([0cc5854](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0cc5854a31190bcaaef2e60f10fcde9bfce48848))
* **analytics:** real LLM token/cost + post-outcome PostHog events (closes [#397](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/397)) ([144250d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/144250d7f445c11a30f3978c6fb3d078bfd1dd30))
* **auth:** sliding 24h session with absolute cap so users don't re-PIN every login ([0c7b1b8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0c7b1b814b90aafee645da54431f53ec59fc0754))
* **automation:** consolidate duplicate comments to one-per-post (dry-run default) ([6db8e99](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6db8e991ef2b9b551ae3a036a7f4f317ab4d1fa9))
* **automation:** consolidate duplicate comments to one-per-post (dry-run default) ([db50a40](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/db50a401a89de78e975f18530f433fbdb4f59e77))
* **automation:** golden-hour reply amplifier for own-post comments (closes [#401](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/401)) ([f6d960d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f6d960d15fa8e2472f4b6919f147f216f011b5c0))
* **automation:** golden-hour reply amplifier for own-post comments (closes [#401](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/401)) ([9e81cab](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9e81cabce6bcd01d834fa7701b6baa86cbeae0e9))
* **automation:** post own-post seed comments via socialActions API, not Selenium ([1be855b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1be855b2d2cac78331108f153feb1570f4db520a))
* **carousel:** composite content-slide images in the posted PNG renderer ([474c20c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/474c20c6c689fda8fd21f12fe6fb4d7ecc91aef5))
* **carousel:** composite relevant images into content slides (fill white space) ([9dd5ee0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9dd5ee0db9e07ce6daf5cee8b40b1e88b36bd80e))
* **carousel:** structured deterministic image selection for content slides ([ec5eeb7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ec5eeb7c6141bf2e4c852505195952306ce6b48f))
* **ci:** autonomous milestone pipeline (Claude Max implementer, Copilot reviewer) ([414abd4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/414abd405f323b5f6e87c09e607bc922ed6fe320))
* **ci:** autonomous milestone pipeline (Claude Max implementer, Copilot reviewer) ([2f27553](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2f2755316764b61389196fa3f019ea03f0531961))
* **comments:** per-run comment angle rotation, target-post grounding kept, research gated off ([9c7d4bd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9c7d4bd44083c7fd7b20a393f0ad56718773f122))
* **comments:** recency-dominant scoring matrix + signal extraction ([2cdac4c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2cdac4cb4f1182f1b1e8ff3f1da07b10afc5cfb5))
* **content:** A1/A3 authenticity rubric + golden set (closes [#405](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/405)) ([4cfb65a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4cfb65a1062c58cb9867b49ca7e4b0219e37d2ff))
* **content:** add A1/A3 authenticity rubric + golden set (closes [#405](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/405)) ([57103c9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/57103c9c3dc6f121f296d972f994661c6ab5f970))
* **content:** add authenticity scoring gate before publish (closes [#382](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/382)) ([d7d785a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d7d785a487145c737715048da385aeffcc9398e5))
* **content:** add Topic Authority (Topic DNA) governor (closes [#384](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/384)) ([e0eb636](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e0eb636345f69fe7de0085edc3a3afc2f5779c32))
* **content:** anchor post subjects to focus topics + post-history dedup/alignment review ([304bff8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/304bff81b04e43bd0fee466fef605b60b9675b38))
* **content:** anchor trend-based post subjects to the user's focus topics ([a2fb5ff](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a2fb5ff076e6627c488f930703edcfdb557ecca3))
* **content:** authenticity scoring gate (360Brew defense) (closes [#382](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/382)) ([4cb2010](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4cb2010e8e29c4bc17b602c6f09b49406f69a4f4))
* **content:** default to no hashtags and bait-free CTAs (closes [#393](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/393)) ([b463ad2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b463ad2da3d3e5cc931dd9068b282d4b87abb0bb))
* **content:** default to no hashtags and bait-free CTAs (closes [#393](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/393)) ([0e21f62](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0e21f626ee582c845c6b1f9da20d76227acd8853))
* **content:** dwell-time-optimized content shaping (closes [#391](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/391)) ([824a841](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/824a8410a12c86e6140203e70b37eaecf2f7752d))
* **content:** dwell-time-optimized content shaping (closes [#391](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/391)) ([94f9d00](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/94f9d0015b81cb55a8ac5d24078905f75fab213b))
* **content:** humanization / anti-AI-tell rewrite pass for all AI text (closes [#416](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/416)) ([08074cc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/08074cc317e01ee162c1ee3999e10704d0450885))
* **content:** humanization / anti-AI-tell rewrite pass for all AI text (closes [#416](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/416)) ([0b54363](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0b54363cf5f9bbed2445e86b78bb31f5ba87b463))
* **content:** inject mandatory first-person proof slot into blueprints (closes [#383](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/383)) ([7624338](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/76243388f1ecf3b468d805024c49a51a11ebce28))
* **content:** inject mandatory first-person proof slot into blueprints (closes [#383](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/383)) ([e1c9944](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e1c9944d6e828871855436675dca4167080459a5))
* **content:** link-in-first-comment mechanic (closes [#392](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/392)) ([04aee3c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/04aee3c8ece43811bfdd993ef3463361afd756cc))
* **content:** link-in-first-comment mechanic (closes [#392](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/392)) ([55b964a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/55b964a38188983072824f9f147c039186d5ba79))
* **content:** performance-aware content selection — close the loop (closes [#389](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/389)) ([799656d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/799656d64586f9ec7fe79e3fb09c3ea76854bf18))
* **content:** performance-aware shape selection to close the loop (closes [#389](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/389)) ([304f44c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/304f44c909bce0417811b19fbcf09614431a1891))
* **content:** post-history dedup steering + similarity review gate ([50a89c7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/50a89c746befc7568e04bbbc9759ceccb8ae4e17))
* **content:** Topic Authority (Topic DNA) governor (closes [#384](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/384)) ([38b83fe](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/38b83fec7ff45894e06a7320d102613a44e44863))
* **content:** weave lead-magnet CTA into ~1-in-N generated posts ([3874873](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3874873bff784601d6d5eb3ad7d4e4fb0d98e0f7))
* **content:** weave lead-magnet CTA into ~1-in-N generated posts ([f54d467](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f54d467994d85bbc8df0fab275bd5b4314a3c683))
* **dashboard:** engagement-rate analytics — trends, leaderboards, per-post drill-down (closes [#395](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/395)) ([424640e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/424640e8ffc8307a2a64e2e48ad091112467b19b))
* **dashboard:** engagement-rate analytics — trends, leaderboards, per-post drill-down (closes [#395](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/395)) ([c03f849](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c03f8498a93c1f1db90f1288aa4127cfbab6e00d))
* **db:** auto-prune superseded cookies to keep sessions fresh ([29452e7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/29452e769713c505ff460e7c827c1b423f64f846))
* **db:** auto-prune superseded cookies to keep sessions fresh ([7bf93b4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7bf93b4eeacda0a7da63708e490762486649b0ff))
* **db:** content-attribution schema for post_stats (closes [#386](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/386)) ([4e1a50f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4e1a50ffd2db283de7d138c55dfc63671999eae3))
* **db:** content-attribution schema for post_stats (closes [#386](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/386)) ([3cccb81](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3cccb812ca2962ebee3b52f8a68dfe6fea6be779))
* **dm-scheduler:** schedule 1:1 DMs mirroring the post scheduler (closes [#306](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/306)) ([35d1a93](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/35d1a93c0c383d27e035cb1a1ff2e6d86c033da1))
* **dm-scheduler:** schedule 1:1 DMs, mirroring the post scheduler ([#306](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/306)) ([1805b35](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1805b35163cc7d2a132ff9dfc12f607b7f90b120))
* **dm-scheduler:** SPA DMs tab with preview/approve workflow ([#306](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/306)) ([72a2ae0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/72a2ae08c15f6e9794f86b7ce28f0aea3369e896))
* **dm:** unify DM/lead-magnet placeholder substitution engine ([9257fee](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9257fee748c2a530907b13a769526a8d89c81b31))
* **engagement:** auto seed + pin a first comment on your own post ([b4dc38d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b4dc38db62a3f828c96adc433674dbd46e760507))
* **engagement:** auto seed + pin first comment on own posts ([4dda534](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4dda5343af941be456f63aa619e5b98d3b953b7b))
* **engagement:** comment quality + recency scoring + reciprocity + no-post-day runs ([a650229](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a650229acbf1b5cf16052804864455ab1aae8c0c))
* **engagement:** daily golden-hour commenting on top of pre-post ([9819ace](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9819ace0aff7280ac8e7107c061f9b73f58fa4c5))
* **engagement:** daily golden-hour commenting on top of pre-post ([aa5fc71](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/aa5fc716dfec22503a501be36d04d85dbe14ffcb))
* **engagement:** default to substantive ≥15-word comments (closes [#394](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/394)) ([ed46287](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ed46287c70fe23cd393389e1633e4c2d7a03dcc1))
* **engagement:** default to substantive ≥15-word comments (closes [#394](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/394)) ([059c5b7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/059c5b7c77db3d23343cacccd6550462e9c857d4))
* **engagement:** honor prefs on viewer-comment path; env-tunable feed scoring ([7d67921](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d67921f10840cee3937c4066e94d7e18d641e6e))
* **engagement:** react on posts we comment on (SDUI, AI-chosen reaction) ([5de7953](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5de7953168560d2927240eb9904c6f677d280949))
* **engagement:** react on posts we comment on (SDUI, AI-chosen reaction) ([7c14270](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7c14270d26bbd52855a3e08d1701ea90aa666d9c))
* **engagement:** reciprocity loop — prioritize people who engage with us ([09bb763](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/09bb7635f7b551a239a13af4a105faf932e5c373))
* **engagement:** standalone feed commenting on no-post days ([f9e27ee](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f9e27eea27a2dda01f24686f069e794c121fcd9c))
* **engagement:** weekly profile synthesis as the voice source for generation ([50a931a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/50a931a18d393dedabd79a39c443f3314593b7c1))
* **engagement:** wire cached profile synthesis into all generation call sites + weekly refresh task ([0e2c170](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0e2c1704a44ac0fb41095f158d3c3ab3c35a7e41))
* **feed:** empty-filter fallback + reach estimate for feed commenting ([f3a36b8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f3a36b8d6ca0d2d3fedfeb89f4f2a9fc2c85f86d))
* **feed:** empty-filter fallback + reach estimate for feed commenting ([7519eba](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7519eba536cae6d7ff877e6b3d6aa32ccd82de8e))
* **growth:** roadmap P2–P7 (groups, stats, lead-magnet, hook/save, guardrails, thread-builder) ([c80a751](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c80a751576a119773c39bded63ca5b6f357d3000))
* **growth:** roadmap P2–P7 (groups, stats, lead-magnet, hook/save, guardrails, thread-builder) ([02676ac](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/02676ac64ec981b00a958991cc293f5b228b2d6e))
* **linkedin:** weekly API-version check with gated auto-bump and rollback ([6b58478](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6b58478cf35860d311be4273804b7c51aa46ed97))
* **linkedin:** weekly API-version check with gated auto-bump and rollback ([4145789](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/414578910b9d1222afc68f3512baea485f9218d1))
* **litellm:** weekly model-health check with auto-upgrade + safe deploy reload ([5a6df5e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5a6df5e914da6dc948338fd5e5bebe3a04c69630))
* **litellm:** weekly model-health check with auto-upgrade + safe deploy reload ([48bfb50](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/48bfb502b47b9281e86836c206f806275e7a1ae4))
* **newsletter:** draft-review + auto-publish scheduling workflow ([0a7251d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0a7251d5aba9ee22750e805af733520da2235fe1))
* **newsletter:** draft-review workflow + day/time scheduling ([b785a93](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b785a9350c35ea110de784be6aeac608aeaf4c93))
* **newsletter:** edition blueprint/variety system + Perplexity research grounding ([94f0ff9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/94f0ff93529644ea213ae3d05b01929d23494efe))
* **newsletter:** edition blueprint/variety system, research layer, V50 shape history ([49f5c87](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/49f5c878e058bae3549600f980ee09c47e3bac94))
* **newsletter:** humanize edition titles with a title-specific de-hype pass (closes [#439](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/439)) ([aad14dd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/aad14dd4d267070967dab0a69b5393413cdeaf62))
* **newsletter:** humanize edition titles with a title-specific de-hype pass (closes [#439](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/439)) ([d2b18f6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d2b18f6ab8b8266b8c82bf84a42bfe93bc83c2d1))
* **newsletter:** LinkedIn newsletter engine (P1 of growth roadmap) ([627305a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/627305a2a853cd9d536c47284eded41bc3971e3b))
* **newsletter:** LinkedIn newsletter engine (roadmap P1) ([cbea5a1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cbea5a1b799de54e12f4b46b95b91c5f910c6362))
* **newsletter:** multi-draft queue with days-ahead + count config ([66fc566](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/66fc5661b0d248c20ce2f579f03fb5c4afdbf23a))
* **newsletter:** multi-draft review queue on Review page + plan-ahead config ([6d08ad4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6d08ad46cb1dd9646b7f0821537ffdd4e59825ad))
* **newsletter:** multi-draft review queue with configurable count + days-ahead ([5031ec6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5031ec6183a021f28e078d72012886b7994ea41a))
* **newsletter:** persist edition subject (V49) + recent-subjects dedup history ([a1e0457](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a1e0457a2094b06ecc149d7f3f581644b02e464f))
* **newsletter:** richer, best-practice editions + subtitle field ([cc87056](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cc87056218bf5b8d4fc4029d0c2e84abbbe7bb64))
* **newsletter:** richer, best-practice editions + subtitle field ([73cbe76](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/73cbe76f6ca936c27f6c134aa4a70054a7c4e69e))
* **newsletter:** subscriber-growth tracking + opt-in invite flow (closes [#400](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/400)) ([5aeb9fb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5aeb9fb650c3840d950b1e917200864eae6e8503))
* **newsletter:** subscriber-growth tracking + opt-in invite flow (closes [#400](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/400)) ([cae2110](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cae2110e99018a47ced6a6080a90971fb89fc87e))
* **newsletter:** topic-planning phase + synthesis alignment + Re-generate w/ guidance ([642b9f0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/642b9f023771e0c7a20323ae4dfa69c96790064b))
* **newsletter:** topic-planning phase, synthesis-grounded editions, regenerate task+endpoint ([b4de4f5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b4de4f58226903b9849f8139d453ba624d35ee60))
* **newsletter:** wire blueprint + research through top-up/regenerate, UI format badges, tests ([b317f40](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b317f408374cba490181618f43fa4f2addf20302))
* **outbound:** auto-approve mode, combined invite cap, 5-min drip ([#398](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/398) review) ([680ef7e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/680ef7e8f3766b11b0a271662d437ce5f31c4bc5))
* **outbound:** human-approved proactive connection requests (closes [#398](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/398)) ([490b515](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/490b5153287a52e2d2e386c8c8597815ebd70a1d))
* **outbound:** human-approved proactive connection requests (closes [#398](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/398)) ([f14ea5d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f14ea5de4e769ae35bc90baf6865996c3d100b56))
* **outreach:** approval-gated comment→connect→DM funnel (closes [#399](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/399)) ([4390254](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/43902543788776c01d1d56248489e791912c3d88))
* **outreach:** comment→connect→DM outreach funnel, approval-gated (closes [#399](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/399)) ([2765ace](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2765aced0542135f9b9c2ce5f80df12a5f832f8a))
* **post-stats:** impression-normalized engagement-rate scoring (closes [#388](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/388)) ([6ab02e3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6ab02e3d957cc1e1a4fdc2406c7d9d07753bb1da))
* **post-stats:** impression-normalized, recency-weighted engagement scoring (closes [#388](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/388)) ([0f3765f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0f3765f4ee83eef70b3edc118252739e86b8c339))
* **poster:** publish native document/PDF posts (closes [#390](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/390)) ([8d979df](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8d979dffc814e710f3d92d9e4591a86d6aaa58e0))
* **poster:** publish native document/PDF posts (closes [#390](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/390)) ([7e75b8b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7e75b8b097808e95787858e3f1eee867088ff9a8))
* **posts:** quick delete + regenerate-with-suggestions ([8b8e2bb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8b8e2bbc66c00566d9a13158c3cf69b6aeb8d413))
* **posts:** rotate post archetypes via shared framework + research-backed trends (V51) ([c8c55f8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c8c55f8ebed3b520227882eb658848f43f4dbf92))
* **prefs:** add focus_topics + business/personal goals to engagement prefs ([099f749](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/099f7496c967cfbeec6a94910bdffb4b30829aa0))
* **rate-limit:** adaptive 429 back-off + manual automation pause (break the doom loop) ([da69066](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/da690669ed3df18696d10372e31af948bf494af4))
* **rate-limit:** adaptive 429 back-off escalation + manual automation pause ([a5e74cd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a5e74cd88d871d33b6a320ab989dc2c5ce1a0849))
* **replies:** auto-confirm Gmail forwarding to the reply address ([03e0a97](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/03e0a97aa7ec35537d3c4bb3ec1401c0bd3200fe))
* **replies:** auto-confirm Gmail forwarding to the reply address ([7d3442d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d3442dd2e96caab996b51b19b83c334ef3ea7cb))
* **replies:** comment-notification webhook → debounced reply sweep (P2) ([3d44041](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3d440416ff8a550d53a9e6d9cf2bc6148ed1b3eb))
* **replies:** event-driven + reduced reply/comment follow-up to cut LinkedIn 429s ([af66714](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/af6671403e04f279faac123d033bd53c98d2f696))
* **replies:** recent-posts sweep + reply-check config to cut 429s (P1) ([e773c36](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e773c365644324592a20077b3c4aaa6597e2cedb))
* **replies:** reply-check mode config in prefs API + Account UI (P1c) ([c048484](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c048484578c51340e52c3f2229dbc9766584991f))
* **replies:** scheduled reply-sweep beat dispatcher (P3) ([cace01d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cace01d7f5ed649877e3c3fba59f5783329f729e))
* **review:** filter posts by type + boolean keyword search + sort on Review & Edit ([a988102](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a98810282fd3ce4a45d8fee2ee944361ab6a3946))
* **review:** post-type filter + boolean keyword search + sort on Review & Edit ([d89b052](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d89b0522d6ede99706b7efbd9a7c2507c97f6473))
* **scheduler:** DEFAULT_POSTING_HOURS env override for the default post-time model ([bfb3731](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bfb3731aac3a801f238cc1d029446fa317f02da6))
* **spa:** Save Draft vs Approve & Schedule for SPA-created posts + newsletters ([c7e3d4f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c7e3d4fc84fd42292f267efa780cc363dd8c354f))
* **spa:** Save Draft vs Approve & Schedule for SPA-created posts + newsletters ([e3e6405](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e3e6405c91f2b2140a251e6e9d61254b7b10ff42))
* **stats:** capture reposts, saves & reliable impressions (closes [#387](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/387)) ([acb7cb7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/acb7cb71096fe15657cfa6ce1e3d4f3c4eb64c06))
* **stats:** capture reposts, saves, and abbreviated impressions (closes [#387](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/387)) ([0512a76](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0512a76acc1d764f29be758e0659ca072e84f771))
* **text:** normalize rogue AI typography (em dashes, smart quotes) in public output ([580e46f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/580e46ff1d14c0e5ac08ef34e765ce8e40cc6bc0))
* **text:** strip rogue AI typography (em dashes, smart quotes) from public output ([38187d4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/38187d49f1ffccd5a6f937898630664f1371f830))
* **ui:** add Content Focus & Goals card to engagement settings ([cc5af9b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cc5af9b93a933edce95931d42b6acbf0508c5675))
* **ui:** consolidate Schedule + Review into one Content Studio page ([c059d35](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c059d35847e35e12c1cd6712e282ba2a42e1af2a))
* **ui:** consolidate Schedule + Review into one Content Studio page ([168da98](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/168da9833832e398ab09ef008c633963b5beb27f))
* **ui:** copyright footer with configurable release-version text ([f2966c9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f2966c900337644c9111d57a6c2e2c564dc7789f))
* **ui:** copyright footer with configurable release-version text ([7c9867a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7c9867a3dff0e4a63d726188720e280a670e5173))
* **ui:** move newsletter draft review to Review page, add config ([1a41038](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1a41038734fc0a09eb6470ca88981b055a8511df))
* **ui:** planned-tasks card by kind + default video quality control ([0d1150a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0d1150a5111e04bc3dcdf833ce356ab897c073cd))
* **ui:** Re-generate action + Added Guidance textarea in newsletter review queue ([15f46f8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/15f46f897e79d66bc9ed36193d181727a248da26))
* **video,dashboard:** avatar-on-standard video, default_video_quality pref, upcoming planned tasks ([b2b759a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b2b759a3e7cfbecb64119aeaf6a8b4c1d672c9e4))
* **video,dashboard:** avatar-on-standard video, default_video_quality pref, upcoming Planned Tasks ([4b4be1b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4b4be1bb52ba9b0b4566236f16f14fcc94111204))


### Bug Fixes

* **ai:** ground comments in target post, block LEM self-promo, add focus steering ([9a8d1fe](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9a8d1fe88add299be10894f51db3cf563342f159))
* **ai:** harden profile-synthesis staleness check against non-datetime timestamps ([0a88cea](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0a88cea804bba6c9ca182e4f84f288bf10f8567d))
* **alignment:** drive hardcoded prompt styling from user settings (configurability audit) ([7dadb33](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7dadb33a474ab846d42f1f1c69ced97828ad45eb))
* **analytics:** address Copilot/CodeQL review on [#396](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/396) A/B harness ([3d55cb2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3d55cb2152f99b5c6be5e1d9cdbcd414bea95740))
* **analytics:** normalize 0/missing impressions to null; gate area fill on gaps ([bba31f4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bba31f41772c9ee0331cd2991d92a9151de614fe))
* **api:** let the browser extension reach /api/user/linkedin-cookie ([5e40f49](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5e40f49b2df3aabdc0a65ca4d32242c03ab01c45))
* **api:** let the browser extension reach /api/user/linkedin-cookie ([587483f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/587483f97551d3b4835004e97491c45c6dfc1a19))
* **api:** make the extension download route public (bypass bearer gate) ([a66be96](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a66be96df9a612f83af698390dc233092206c72d))
* **api:** make the LinkedIn extension download route public ([fb8b42d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fb8b42d3a03e0c14f858033599aa9e8a32a3aabc))
* **api:** remove dead AvatarActivateRequest properties ([d45faf7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d45faf731140872b698ce39e9a1ac8f1667a8c0a))
* **api:** validate PUT /dm action and reject empty updates ([19d6724](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/19d6724458e6841716b74cd1a517a1ebe9833726))
* **automation:** ground seed comments & replies on canonical post body ([2d07045](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2d07045869b9ec76cfe5f7625c49e77d8b38a7bc))
* **automation:** make golden-hour reply sweeps actually all run ([#401](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/401)) ([6e0fbc4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6e0fbc44119844f88906968a782dfb2f6c6b9fba))
* **automation:** persistent at-most-once feed comment dedup (V46 commented_posts) ([75ae240](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/75ae240b57194b5e31d3eb0d1970aea97313763e))
* **automation:** reliably react on non-own commented posts; harden SDUI react fly-out ([7d345bd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d345bdf73a553fb456802f3fc48b1b0f0994f7c))
* **automation:** seed comments on own posts were about the /posts API, not the post ([18a18f1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/18a18f15e97d1708ffebddff03bc65531f04fdb4))
* **automation:** stop duplicate feed comments — URN-stable dedup key + concurrency gate ([d91d790](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d91d790dd67cb9a1d0fe5212301c7f7869f70d29)), closes [#474](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/474)
* **automation:** stop duplicate feed comments — URN-stable dedup key + concurrency gate (closes [#474](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/474)) ([5e897d9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5e897d90748e3319857e98f9bacaa46120f43484))
* **automation:** store real post body in POST log, not a status string ([7835dec](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7835dec3dbc8cb7f30213a5c93b5fc84c6231c0e))
* **carousel:** keep content-slide body text inside the margins ([d561f30](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d561f307e91ee826e64f4177cf096d5a4a962555))
* **carousel:** keep content-slide body text inside the margins ([abf2098](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/abf2098a55275bb073d4326704e474ed69c8f460))
* **carousel:** send browser User-Agent when downloading Pexels slide images ([b8feab8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b8feab86af3d782dc897a4bccf29aa10e1115b57))
* **carousel:** send browser User-Agent when downloading Pexels slide images ([80859c7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/80859c72fad27f129a024b3996f37671f0de2b31))
* **celery:** get_aws_sqs used service_name 'elasticcache' for SQS ([78bcff8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/78bcff83e33f1f3b91a4053bb270d88f092b13c6))
* clean up coverage-found bugs + Copilot review findings (21 items) ([3674ab1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3674ab1dd89e9d10b6f152f337963cc987737f1c))
* **compose:** correct selenium-lane healthcheck node name ([26dd1ae](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/26dd1aecd810c59f98dc2425906468f4566d8266))
* **compose:** correct selenium-lane healthcheck node name (false unhealthy) ([0fb8e12](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0fb8e127a0c2fd6fdc1fa95f97971c6201de8618))
* **content:** correct perf-shape scoring scale + filter posted rows (review [#420](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/420)) ([7d10d1d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d10d1dfd2f076f9b246e5711d7babf4a82684fc))
* **content:** deterministic fallback in select_focus_topic ([e772a74](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e772a74a57b298dd3076f274196852350ced20d8))
* **content:** drive emoji/hashtag/tone prompt rules from engagement prefs ([1f265d3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1f265d35abe291f140bd2960de88687c007dc253))
* **content:** enforce LinkedIn readability + length on carousel captions ([2840cf8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2840cf8238dad8f22d4540f00eaa64bbbbc2c8b8))
* **content:** enforce LinkedIn readability + length on carousel captions ([099a5ab](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/099a5ab7fbd494be0be7112f66660e10ce800a17))
* **content:** guarantee lead-magnet comment-keyword CTA survives refinement pipeline ([09a1d87](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/09a1d87ff2f0268d81232394428002f2b29de55f))
* **content:** guarantee lead-magnet CTA survives the refinement pipeline ([a96b565](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a96b565246c4cbfce511bd3db81af5eaf72c9f46))
* **content:** index CTA repair menu by selection ordinal, not raw post_id ([c7ba539](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c7ba5394b83516239c93a8c532fd3383e8a8954b))
* **content:** persist link split only after a successful publish ([1d3e714](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1d3e714932ba46ccab7a0a6ba8e716f3891cce79))
* **content:** reflow over-long paragraphs (under-formatted posts), not just walls ([20c652a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/20c652a2b7aa30c893853ac47022890e455dea29))
* **content:** reflow OVER-LONG paragraphs, not just total walls ([baf81e2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/baf81e25ac2eab34913d9e8a0743d8c8c5a6a4ef))
* **content:** regenerate guards — unknown type skips, status validated at API ([5cd9969](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5cd9969afbaced3cc7905a44a7c2e879557198ee))
* **content:** single clean lead-magnet ask — preserve context in rewrites, strip soft paraphrase ([3e53672](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3e536729729d57ef590e1767c204e8b8c0988a28))
* **content:** single clean lead-magnet ask (preserve in rewrites + strip soft paraphrase) ([f5da84d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f5da84d30e286e309655f6293b71e6c53a97083f))
* **content:** use unrounded score for authenticity gate + mark unit tests ([2320e4b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2320e4b3ed2de1f184590165ec6ff61df4356f04))
* **dashboard:** correct stale top stats via SQL aggregates ([9eba7c2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9eba7c26c46b7ce69084e46041118e873eca6296))
* **dashboard:** correct stale/incorrect top stats via SQL aggregates ([5439af1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5439af19dffe739eb6f557a89f5d733bef1d4f2d))
* **dashboard:** link home-feed comment rows to LinkedIn comments activity ([44f1840](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/44f18406be80c910124b066f663f20a0fd147264))
* **dashboard:** link home-feed comment rows to LinkedIn comments activity ([d26d85d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d26d85d91f8970b7f88c0245165767dbc8924c8a))
* **db:** error fallbacks and set_active_avatar safety ([a00ccff](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a00ccff789966ac489e768679162736f9a75ee79))
* **db:** renumber duplicate V57 migration (unblock deploy) + version-uniqueness check ([17df6b5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/17df6b5ba039d6a21fb9fa6b2cf68afeb3b6de13))
* **db:** renumber duplicate V57 migration + add Migration Versions check ([9ee09f9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9ee09f9cd6de57647108846cc4f8431b9625d22e))
* **db:** scope post_stats attribution snapshot to user_id (tenant-safe) ([ab46de6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ab46de67befa7cdc70aff6a930931f56d7e7d9f4))
* **db:** use a timestamp version for the post_stats saves migration ([baf0c58](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/baf0c58a73f35c9a4f16d65c559e760fb793de79))
* **deploy:** pass Flyway args as a quoted array; lowercase booleans ([43e606f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/43e606fd715d4135b73b9bbe1bda29563be72f6f))
* **deploy:** persist IMAGE_TAG to .env after deploy/rollback ([57c4369](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/57c4369f029871538701bef907accc20b8976141))
* **deploy:** persist IMAGE_TAG to .env after deploy/rollback ([afe765a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/afe765a15566b84b5754f5eb7131d8d91f2cbc33))
* **deploy:** run Flyway with outOfOrder=true so timestamp migrations self-heal ([ae34330](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ae34330e6278cdfd3a800eba70ab9a4566f3cb91))
* **deploy:** run Flyway with outOfOrder=true so timestamp migrations self-heal ([87d8ba0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/87d8ba0bf3284596867864b3f0df48ce67565a00))
* **dm:** overdue approved DMs stay eligible + orphaned-DM recovery ([e251578](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e25157818b3527efcb3ba955662e092a79948b09))
* **email+auth:** trustworthy login PIN email + sliding 24h session ([755cea9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/755cea9317d6b25dc1a05e73fa50b61e6abd6eac))
* **email:** brand transactional mail as LEM + disable SendGrid click-tracking ([5499fe4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5499fe4c613d08ec4d789fac8178253f3bbf790c))
* **email:** brand transactional mail as LEM + disable SendGrid click-tracking ([ce53532](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ce53532d4a98c8e7edec8befdfee1eca71a00dc7))
* **email:** make login PIN email trustworthy (multipart text+html, aligned From, footer) to stop Gmail spam/phishing flags ([0f5299c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0f5299cd69b04bd4542310dec77517907da9b6df))
* **engagement:** 1 comment/post + reactions + topic-aligned comments (no LEM drift) ([0223d3f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0223d3fe95570e9b4a03d872ad49fd8623fd8b2b))
* **engagement:** break the LinkedIn 429 breaker doom loop ([62e3ca0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/62e3ca077a77bf0c16bd40d638f0349c32b8104b))
* **engagement:** break the LinkedIn 429 breaker doom loop ([77155cd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/77155cdf348145286dba85a81355879a00b05e46))
* **engagement:** ground group + post-stats selectors from live sweep ([7548a40](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7548a40f5a854cd201422f7b4d8ba8cc86a2a8d3))
* **engagement:** ground group + post-stats selectors from live sweep ([1d67793](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1d6779364dc85b619ca4dc365231e705eab32ae8))
* **engagement:** make feed_fallback_when_empty relax the hard gates too ([dbed71e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/dbed71e83fefe7f11fe74624c88d3da46881304c))
* **engagement:** make feed_fallback_when_empty relax the hard gates too ([1e58269](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1e58269580ddd54c719eef0260aeb84620bfb516))
* **engagement:** settings persistence + placeholder substitution + post regenerate/delete ([bd296ac](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bd296acd73d2481c904a5847262bb068a6a5a005))
* **engagement:** widen tone column so settings persist; surface save errors ([9f4ad0f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9f4ad0fda03d64752a6dc7fa7d2856bd6b6cfa23))
* **feed:** log real /feed/update/ permalinks for inline comments ([e224be7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e224be7642fb62a708bfec810b8584ffb859d819))
* **feed:** log real /feed/update/ permalinks so activity feed links work ([212b552](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/212b552f1165880efa1fd9e70a59031882ea55d0))
* **flower:** an empty FLOWER_BASIC_AUTH must mean no auth, not a lockout ([0b45939](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0b459397a728899ed9c3e7ef379336ac7e2cdaa5))
* **flower:** an empty FLOWER_BASIC_AUTH must mean no auth, not a lockout ([7d3088e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d3088e23a05711b84c1fb0f3de13fef8a9863f0))
* **flower:** require BOTH user and password before enabling basic auth ([83f5ea9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/83f5ea9c3af1f94beca06b592bf4f34a19d5ce3f))
* **lead-magnet:** exempt configured trigger word from the bait filter ([4876f16](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4876f16683bc7c3b7fe3b7632fc8ba49258b4913))
* **lead-magnet:** require message, guard invalid cadence, fix multi-word label check ([bd191f5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bd191f57de695601a878d943bd23753354394f90))
* **linkedin:** only trust conclusive probe responses; harden the orchestrator ([a5a11ab](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a5a11ab968760bb874650d1ad34a8431c4ff1e45))
* **linkedin:** self-heal a 429-after-cookie-load with a fresh login ([7a3abf2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7a3abf2b5520742479e5cbc588fe8d04886e6629))
* **linkedin:** self-heal a 429-after-cookie-load with a fresh login ([735c2bc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/735c2bc2980c6bac15885f7e9300c72a3d731384))
* **linkedin:** use a live LinkedIn API version for document posts ([99fa3a5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/99fa3a51af3419f4bc9983784a8af0ff0b967b8e))
* **litellm:** drop retired ministral-3:8b from lem-simple ([ccc4285](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ccc428505c8c6e5822b4103ea8cc2ced824d40fd))
* **litellm:** drop retired ministral-3:8b from lem-simple ([12bee63](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/12bee630cfd46feddabbaecea92db29857dae938))
* **model-check:** use the dev-venv python (openai) instead of poetry-run ([ecf5ee0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ecf5ee0f7a8613acb53dc8c3242c583e2d02ac9e))
* **newsletter:** address PR review comments ([36c88f1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/36c88f1b6364d9aaeb7e35e72e113ac890d23608))
* **newsletter:** detect digit-leading hype tokens in title slop audit ([02d6654](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/02d665402183061de48ee775542df4a853119c8c))
* **newsletter:** publish one edition per run and shift a missed-slot backlog forward ([d27d8fc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d27d8fc811cfe66a01a6cbd30dcb3417a38e43c9))
* **newsletter:** publish one per run and shift a missed-slot backlog forward ([2498983](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/249898319c309c6072be060031db64c3742f6ef7))
* **newsletter:** top up draft queue immediately when count is raised ([e884c57](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e884c571608cc26eafc90dad5fe4b1062f6ada4a))
* **outbound:** Copilot review — strict rowcount, local test import ([#398](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/398)) ([cb02952](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cb02952c2f9833430c8afc0768d05b7904039c57))
* **outreach:** Copilot review — strip target fields, retry failed/skipped stages ([b94de21](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b94de210ad92ee55b3f0b106c5b01063c5d4f59e))
* **post-stats:** rank on unrounded score; annotate _cell return (PR [#428](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/428) review) ([baafad8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/baafad82f172fa19b639cb18cd68c99ea5eb3fde))
* **poster:** address Copilot + CodeQL review on document posts ([2748084](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2748084bedab78b01cb63999069ad6398a16fc88))
* replace deprecated datetime.utcnow() with timezone-aware equivalent ([d7beabf](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d7beabfdea35b4a5183fda8611e6a7d3816abc9a))
* **replies:** forward Gmail confirmation to the user + robuster auto-confirm ([ff97b72](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ff97b72b5cb8c41caa1d5495e4097238e4a375a0))
* **replies:** forward Gmail confirmation to the user + robuster auto-confirm ([66b6a66](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/66b6a660dabe15f1b3036639b9d7b824ab549dda))
* **replies:** loop-safety guards on the reply sweep ([fcfca89](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fcfca89d3d3268f46487b8dfe52977e38676343c))
* **replies:** loop-safety guards on the reply sweep (event-driven replies on own posts) ([1c0f84e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1c0f84e5e71a62b09d1dc1cffe66d4cf6b7eb49d))
* **replies:** route reply+ mail from the single SendGrid inbound URL (PIN endpoint) ([75eaea0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/75eaea03e92f8ae3a51d4c6ef6157b615c6eeeaa))
* **replies:** route reply+ mail from the single SendGrid inbound URL (PIN endpoint) ([1433088](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1433088813990d6df2fa3938530bd7b5d1635050))
* **review:** address Copilot + CodeQL feedback on PR [#370](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/370) ([e9f79bc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e9f79bc66adde1369640dae963609e15db98674d))
* **selenium:** route auto_publish_edition to se_content (was on retired 'selenium' queue) ([7fdf7d8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7fdf7d8f01cb995065fbad232a669f4276595221))
* **selenium:** route every LinkedIn session through the user's proxy + drop the stale UA ([4659ec7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4659ec7d83964bea2f715b3c0b7bf9240181337c))
* **selenium:** route every LinkedIn session through the user's proxy + drop the stale UA ([a440be0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a440be0c58b139d1a91ec2b90a67c16b7ae9720b))
* **settings:** align input length limits across SPA, API, and DB ([8c28f52](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8c28f52a975c018ebc5de341c3c2c3b9d8d39e7c))
* **stats:** match the live post-analytics layout for reposts/saves ([#387](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/387)) ([abfadd9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/abfadd9e1a7950f7fbbf038c3fbfa14f99a48bf7))
* **time:** 12-hour newsletter publish-hour picker (never 24h) ([c0ad14c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c0ad14c32e04eb7353a1939ac1115248a94e1fa8))
* **time:** 12-hour newsletter publish-hour picker (P1 UI) ([b66d4e2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b66d4e209685833bbc7d3ec88d59709771cd3e3b))
* **time:** correct displayed times to user-local 12h + hide synthetic feed URLs ([8a62e30](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8a62e30a300d1037c6c2c5df3e5aa16d96fe5199))
* **time:** pin Celery beat to UTC + follow-ups/tz-default on UTC (P1 backend) ([655877d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/655877d131261e05d125ff4de29c0eadf29a3b65))
* **time:** pin Celery beat to UTC + standardize follow-ups/tz-default on UTC ([4622a1f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4622a1f26d4608795894984e97891bdadc318a05))
* **time:** user-local 12h times + hide synthetic feed URLs (P0) ([f4f27e0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f4f27e0c853c8e59c6eb78d4a3d7c961a9e99d06))
* **ui,deploy:** lowercase post_type on schedule + make Flower basic auth optional ([77036d8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/77036d8a74565d233bc210e0addd8edc3c6391f9))
* **ui,deploy:** lowercase post_type on schedule + optional Flower basic auth ([45736f2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/45736f27b639556dc4f47c0580ffe0a7a4c20e6e))
* **ui:** honest delete-confirmation copy + session guard on regenerate ([5889c84](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5889c8443e34bc2df2f1aafb2a8b2b71a09ccd65))
* **ui:** merge Voice&Tone + Targeting into one shared-state component ([c223158](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c22315850ef2ba522d15269c84133a71ddf9aca0))
* **ui:** pin typescript to 6.0.x (unbreak prod Docker build) ([a9faf56](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a9faf5626a44cec57ecd2ec2c60e06f8a64a8264))
* **ui:** pin typescript to 6.0.x to unbreak the Docker image build ([5bd5958](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5bd5958034939af9a3ae0705efa7cfe3b222ee80))


### Performance Improvements

* **selenium:** 3-lane workers + 4 concurrent sessions (fix automation starvation) ([5d63442](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5d6344276f1fafeadae78d67f28ef82cdfe6abb2))
* **selenium:** 3-lane workers + 4 concurrent sessions (fix automation starvation) ([0017a58](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0017a58a871f22334bc350b88771fe142fa113bb))


### Documentation

* **claude:** add Git Safety & Multi-Agent Concurrency Rules ([bf3f180](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bf3f1805f20452778777ce55456c2db78e53e6dd))
* **claude:** add Git Safety & Multi-Agent Concurrency Rules ([b173f62](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b173f62b77ec9bf50fb80db27a7a6efea6276148))
* **claude:** document production deploy flow + local hotfix fallback ([bcbbbb9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bcbbbb9b5a0d582e161fd6a20c26a6c853d0a4f7))
* document unified content core, V51, and per-type research toggles ([6e70ad0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6e70ad04f6e29a2f0d2afb66cd7d1062d198e7ca))
* **format:** address Copilot review — fix call-count wording, drop wiki links ([#406](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/406)) ([5f77721](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5f7772126f25e1eccf68392a93b3a5cfa90b47b0))
* **format:** R3 spike — document vs article/newsletter API feasibility (closes [#406](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/406)) ([38a7acc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/38a7acc9ffdd7f41fb1a66babb3a8aa3df4cfffb))
* **format:** R3 spike — document vs article/newsletter API feasibility (closes [#406](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/406)) ([16468d1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/16468d19b0b19abb5d903ddb68f06ade02d07128))
* note V48 profile synthesis migration in CLAUDE.md ([828708e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/828708e95fda3ff9efb3d025bc14f7a458aad5a3))
* refresh capabilities in CLAUDE.md, README, and copilot-instructions ([0f77d26](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0f77d267c5ca490ac660e759290485a7d8aeffed))
* refresh capabilities in CLAUDE.md, README, copilot-instructions ([747f425](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/747f42593c0cc36a905b518cb5b89bcd1ef64b4b))
* **ui:** clarify hashtag help text claim ([55b1351](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/55b1351f7a0ce937c4f13ddd859789158d233599))
* **ui:** full LinkedIn + Gmail setup steps for event-driven replies ([d55b1fb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d55b1fb9084862a4d4d33a8e03e8703c593e7311))
* **ui:** full LinkedIn + Gmail setup steps for event-driven replies ([c921911](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c921911b008a29d6740944e03978de73e9456ce6))

## [0.66.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.66.0...v0.66.1) (2026-07-24)


### Bug Fixes

* **automation:** stop duplicate feed comments — URN-stable dedup key + concurrency gate ([d91d790](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d91d790dd67cb9a1d0fe5212301c7f7869f70d29)), closes [#474](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/474)
* **automation:** stop duplicate feed comments — URN-stable dedup key + concurrency gate (closes [#474](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/474)) ([5e897d9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5e897d90748e3319857e98f9bacaa46120f43484))

## [0.66.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.65.0...v0.66.0) (2026-07-24)


### Features

* **newsletter:** subscriber-growth tracking + opt-in invite flow (closes [#400](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/400)) ([5aeb9fb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5aeb9fb650c3840d950b1e917200864eae6e8503))

## [0.65.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.64.0...v0.65.0) (2026-07-24)


### Features

* **outreach:** approval-gated comment→connect→DM funnel (closes [#399](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/399)) ([4390254](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/43902543788776c01d1d56248489e791912c3d88))
* **outreach:** comment→connect→DM outreach funnel, approval-gated (closes [#399](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/399)) ([2765ace](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2765aced0542135f9b9c2ce5f80df12a5f832f8a))


### Bug Fixes

* **outreach:** Copilot review — strip target fields, retry failed/skipped stages ([b94de21](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b94de210ad92ee55b3f0b106c5b01063c5d4f59e))

## [0.64.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.63.0...v0.64.0) (2026-07-24)


### Features

* **outbound:** auto-approve mode, combined invite cap, 5-min drip ([#398](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/398) review) ([680ef7e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/680ef7e8f3766b11b0a271662d437ce5f31c4bc5))
* **outbound:** human-approved proactive connection requests (closes [#398](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/398)) ([490b515](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/490b5153287a52e2d2e386c8c8597815ebd70a1d))
* **outbound:** human-approved proactive connection requests (closes [#398](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/398)) ([f14ea5d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f14ea5de4e769ae35bc90baf6865996c3d100b56))


### Bug Fixes

* **outbound:** Copilot review — strict rowcount, local test import ([#398](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/398)) ([cb02952](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cb02952c2f9833430c8afc0768d05b7904039c57))

## [0.63.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.62.0...v0.63.0) (2026-07-24)


### Features

* **automation:** golden-hour reply amplifier for own-post comments (closes [#401](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/401)) ([f6d960d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f6d960d15fa8e2472f4b6919f147f216f011b5c0))
* **automation:** golden-hour reply amplifier for own-post comments (closes [#401](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/401)) ([9e81cab](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9e81cabce6bcd01d834fa7701b6baa86cbeae0e9))


### Bug Fixes

* **automation:** make golden-hour reply sweeps actually all run ([#401](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/401)) ([6e0fbc4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6e0fbc44119844f88906968a782dfb2f6c6b9fba))

## [0.62.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.61.0...v0.62.0) (2026-07-24)


### Features

* **analytics:** real LLM token/cost + post-outcome PostHog events (closes [#397](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/397)) ([0cc5854](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0cc5854a31190bcaaef2e60f10fcde9bfce48848))
* **analytics:** real LLM token/cost + post-outcome PostHog events (closes [#397](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/397)) ([144250d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/144250d7f445c11a30f3978c6fb3d078bfd1dd30))

## [0.61.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.60.0...v0.61.0) (2026-07-24)


### Features

* **analytics:** outcome-tracked A/B variant harness (closes [#396](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/396)) ([5fdbc32](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5fdbc32d6037b41a464891ea5e56e1032a6f9bb5))
* **analytics:** outcome-tracked A/B variant harness (closes [#396](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/396)) ([f20ce66](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f20ce66f6a1b2b3773525fcf070a4459785355ce))


### Bug Fixes

* **analytics:** address Copilot/CodeQL review on [#396](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/396) A/B harness ([3d55cb2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3d55cb2152f99b5c6be5e1d9cdbcd414bea95740))

## [0.60.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.59.1...v0.60.0) (2026-07-24)


### Features

* **dashboard:** engagement-rate analytics — trends, leaderboards, per-post drill-down (closes [#395](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/395)) ([424640e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/424640e8ffc8307a2a64e2e48ad091112467b19b))
* **dashboard:** engagement-rate analytics — trends, leaderboards, per-post drill-down (closes [#395](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/395)) ([c03f849](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c03f8498a93c1f1db90f1288aa4127cfbab6e00d))


### Bug Fixes

* **analytics:** normalize 0/missing impressions to null; gate area fill on gaps ([bba31f4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bba31f41772c9ee0331cd2991d92a9151de614fe))

## [0.59.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.59.0...v0.59.1) (2026-07-24)


### Documentation

* **format:** address Copilot review — fix call-count wording, drop wiki links ([#406](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/406)) ([5f77721](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5f7772126f25e1eccf68392a93b3a5cfa90b47b0))
* **format:** R3 spike — document vs article/newsletter API feasibility (closes [#406](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/406)) ([38a7acc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/38a7acc9ffdd7f41fb1a66babb3a8aa3df4cfffb))
* **format:** R3 spike — document vs article/newsletter API feasibility (closes [#406](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/406)) ([16468d1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/16468d19b0b19abb5d903ddb68f06ade02d07128))

## [0.59.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.58.1...v0.59.0) (2026-07-24)


### Features

* **engagement:** default to substantive ≥15-word comments (closes [#394](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/394)) ([ed46287](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ed46287c70fe23cd393389e1633e4c2d7a03dcc1))

## [0.58.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.58.0...v0.58.1) (2026-07-24)


### Bug Fixes

* **deploy:** pass Flyway args as a quoted array; lowercase booleans ([43e606f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/43e606fd715d4135b73b9bbe1bda29563be72f6f))
* **deploy:** run Flyway with outOfOrder=true so timestamp migrations self-heal ([ae34330](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ae34330e6278cdfd3a800eba70ab9a4566f3cb91))
* **deploy:** run Flyway with outOfOrder=true so timestamp migrations self-heal ([87d8ba0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/87d8ba0bf3284596867864b3f0df48ce67565a00))

## [0.58.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.57.0...v0.58.0) (2026-07-24)


### Features

* **content:** default to no hashtags and bait-free CTAs (closes [#393](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/393)) ([b463ad2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b463ad2da3d3e5cc931dd9068b282d4b87abb0bb))
* **content:** default to no hashtags and bait-free CTAs (closes [#393](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/393)) ([0e21f62](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0e21f626ee582c845c6b1f9da20d76227acd8853))


### Documentation

* **ui:** clarify hashtag help text claim ([55b1351](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/55b1351f7a0ce937c4f13ddd859789158d233599))

## [0.57.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.56.0...v0.57.0) (2026-07-24)


### Features

* **content:** link-in-first-comment mechanic (closes [#392](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/392)) ([04aee3c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/04aee3c8ece43811bfdd993ef3463361afd756cc))

## [0.56.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.55.0...v0.56.0) (2026-07-24)


### Features

* **linkedin:** weekly API-version check with gated auto-bump and rollback ([6b58478](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6b58478cf35860d311be4273804b7c51aa46ed97))


### Bug Fixes

* **flower:** an empty FLOWER_BASIC_AUTH must mean no auth, not a lockout ([0b45939](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0b459397a728899ed9c3e7ef379336ac7e2cdaa5))
* **flower:** require BOTH user and password before enabling basic auth ([83f5ea9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/83f5ea9c3af1f94beca06b592bf4f34a19d5ce3f))
* **linkedin:** only trust conclusive probe responses; harden the orchestrator ([a5a11ab](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a5a11ab968760bb874650d1ad34a8431c4ff1e45))

## [0.55.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.54.0...v0.55.0) (2026-07-24)


### Features

* **poster:** publish native document/PDF posts (closes [#390](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/390)) ([8d979df](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8d979dffc814e710f3d92d9e4591a86d6aaa58e0))


### Bug Fixes

* **poster:** address Copilot + CodeQL review on document posts ([2748084](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2748084bedab78b01cb63999069ad6398a16fc88))

## [0.54.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.53.0...v0.54.0) (2026-07-24)


### Features

* **content:** dwell-time-optimized content shaping (closes [#391](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/391)) ([824a841](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/824a8410a12c86e6140203e70b37eaecf2f7752d))
* **content:** dwell-time-optimized content shaping (closes [#391](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/391)) ([94f9d00](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/94f9d0015b81cb55a8ac5d24078905f75fab213b))

## [0.53.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.52.0...v0.53.0) (2026-07-23)


### Features

* **newsletter:** humanize edition titles with a title-specific de-hype pass (closes [#439](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/439)) ([aad14dd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/aad14dd4d267070967dab0a69b5393413cdeaf62))
* **newsletter:** humanize edition titles with a title-specific de-hype pass (closes [#439](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/439)) ([d2b18f6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d2b18f6ab8b8266b8c82bf84a42bfe93bc83c2d1))


### Bug Fixes

* **newsletter:** detect digit-leading hype tokens in title slop audit ([02d6654](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/02d665402183061de48ee775542df4a853119c8c))

## [0.52.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.51.0...v0.52.0) (2026-07-23)


### Features

* **post-stats:** impression-normalized engagement-rate scoring (closes [#388](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/388)) ([6ab02e3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6ab02e3d957cc1e1a4fdc2406c7d9d07753bb1da))

## [0.51.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.50.2...v0.51.0) (2026-07-23)


### Features

* **stats:** capture reposts, saves & reliable impressions (closes [#387](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/387)) ([acb7cb7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/acb7cb71096fe15657cfa6ce1e3d4f3c4eb64c06))


### Bug Fixes

* **db:** use a timestamp version for the post_stats saves migration ([baf0c58](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/baf0c58a73f35c9a4f16d65c559e760fb793de79))

## [0.50.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.50.1...v0.50.2) (2026-07-23)


### Bug Fixes

* **db:** renumber duplicate V57 migration (unblock deploy) + version-uniqueness check ([17df6b5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/17df6b5ba039d6a21fb9fa6b2cf68afeb3b6de13))
* **db:** renumber duplicate V57 migration + add Migration Versions check ([9ee09f9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9ee09f9cd6de57647108846cc4f8431b9625d22e))

## [0.50.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.50.0...v0.50.1) (2026-07-23)


### Bug Fixes

* **ui:** pin typescript to 6.0.x (unbreak prod Docker build) ([a9faf56](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a9faf5626a44cec57ecd2ec2c60e06f8a64a8264))
* **ui:** pin typescript to 6.0.x to unbreak the Docker image build ([5bd5958](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5bd5958034939af9a3ae0705efa7cfe3b222ee80))

## [0.50.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.49.0...v0.50.0) (2026-07-23)


### Features

* **content:** humanization / anti-AI-tell rewrite pass for all AI text (closes [#416](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/416)) ([08074cc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/08074cc317e01ee162c1ee3999e10704d0450885))

## [0.49.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.48.0...v0.49.0) (2026-07-23)


### Features

* **db:** content-attribution schema for post_stats (closes [#386](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/386)) ([4e1a50f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4e1a50ffd2db283de7d138c55dfc63671999eae3))


### Bug Fixes

* **db:** scope post_stats attribution snapshot to user_id (tenant-safe) ([ab46de6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ab46de67befa7cdc70aff6a930931f56d7e7d9f4))

## [0.48.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.47.0...v0.48.0) (2026-07-23)


### Features

* **content:** add authenticity scoring gate before publish (closes [#382](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/382)) ([d7d785a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d7d785a487145c737715048da385aeffcc9398e5))
* **content:** authenticity scoring gate (360Brew defense) (closes [#382](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/382)) ([4cb2010](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4cb2010e8e29c4bc17b602c6f09b49406f69a4f4))

## [0.47.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.46.0...v0.47.0) (2026-07-23)


### Features

* **ai:** add anti-ai skill reference files (for [#416](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/416) humanization) ([1b0f126](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1b0f12655fb0907950aca6afcbd01ab691eec0d5))
* **ai:** add anti-ai skill reference files (READER-mode humanization spec) ([ffcdab6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ffcdab6a58a4fce3cde80c27c539d7a42f17afaf))
* **ci:** autonomous milestone pipeline (Claude Max implementer, Copilot reviewer) ([414abd4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/414abd405f323b5f6e87c09e607bc922ed6fe320))
* **ci:** autonomous milestone pipeline (Claude Max implementer, Copilot reviewer) ([2f27553](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2f2755316764b61389196fa3f019ea03f0531961))
* **content:** A1/A3 authenticity rubric + golden set (closes [#405](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/405)) ([4cfb65a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4cfb65a1062c58cb9867b49ca7e4b0219e37d2ff))
* **content:** add A1/A3 authenticity rubric + golden set (closes [#405](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/405)) ([57103c9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/57103c9c3dc6f121f296d972f994661c6ab5f970))
* **content:** add Topic Authority (Topic DNA) governor (closes [#384](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/384)) ([e0eb636](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e0eb636345f69fe7de0085edc3a3afc2f5779c32))
* **content:** inject mandatory first-person proof slot into blueprints (closes [#383](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/383)) ([7624338](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/76243388f1ecf3b468d805024c49a51a11ebce28))
* **content:** inject mandatory first-person proof slot into blueprints (closes [#383](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/383)) ([e1c9944](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e1c9944d6e828871855436675dca4167080459a5))
* **content:** performance-aware content selection — close the loop (closes [#389](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/389)) ([799656d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/799656d64586f9ec7fe79e3fb09c3ea76854bf18))
* **content:** performance-aware shape selection to close the loop (closes [#389](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/389)) ([304f44c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/304f44c909bce0417811b19fbcf09614431a1891))
* **content:** Topic Authority (Topic DNA) governor (closes [#384](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/384)) ([38b83fe](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/38b83fec7ff45894e06a7320d102613a44e44863))


### Bug Fixes

* **content:** correct perf-shape scoring scale + filter posted rows (review [#420](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/420)) ([7d10d1d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d10d1dfd2f076f9b246e5711d7babf4a82684fc))
* **content:** use unrounded score for authenticity gate + mark unit tests ([2320e4b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2320e4b3ed2de1f184590165ec6ff61df4356f04))

## [0.46.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.45.6...v0.46.0) (2026-07-23)


### Features

* **litellm:** weekly model-health check with auto-upgrade + safe deploy reload ([5a6df5e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5a6df5e914da6dc948338fd5e5bebe3a04c69630))
* **litellm:** weekly model-health check with auto-upgrade + safe deploy reload ([48bfb50](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/48bfb502b47b9281e86836c206f806275e7a1ae4))


### Bug Fixes

* **model-check:** use the dev-venv python (openai) instead of poetry-run ([ecf5ee0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ecf5ee0f7a8613acb53dc8c3242c583e2d02ac9e))
* **newsletter:** publish one edition per run and shift a missed-slot backlog forward ([d27d8fc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d27d8fc811cfe66a01a6cbd30dcb3417a38e43c9))
* **newsletter:** publish one per run and shift a missed-slot backlog forward ([2498983](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/249898319c309c6072be060031db64c3742f6ef7))

## [0.45.6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.45.5...v0.45.6) (2026-07-23)


### Bug Fixes

* **deploy:** persist IMAGE_TAG to .env after deploy/rollback ([57c4369](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/57c4369f029871538701bef907accc20b8976141))
* **deploy:** persist IMAGE_TAG to .env after deploy/rollback ([afe765a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/afe765a15566b84b5754f5eb7131d8d91f2cbc33))
* **engagement:** make feed_fallback_when_empty relax the hard gates too ([dbed71e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/dbed71e83fefe7f11fe74624c88d3da46881304c))
* **engagement:** make feed_fallback_when_empty relax the hard gates too ([1e58269](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1e58269580ddd54c719eef0260aeb84620bfb516))
* **linkedin:** self-heal a 429-after-cookie-load with a fresh login ([7a3abf2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7a3abf2b5520742479e5cbc588fe8d04886e6629))
* **linkedin:** self-heal a 429-after-cookie-load with a fresh login ([735c2bc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/735c2bc2980c6bac15885f7e9300c72a3d731384))
* **litellm:** drop retired ministral-3:8b from lem-simple ([ccc4285](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ccc428505c8c6e5822b4103ea8cc2ced824d40fd))
* **litellm:** drop retired ministral-3:8b from lem-simple ([12bee63](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/12bee630cfd46feddabbaecea92db29857dae938))

## [0.45.5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.45.4...v0.45.5) (2026-07-22)


### Bug Fixes

* **selenium:** route every LinkedIn session through the user's proxy + drop the stale UA ([4659ec7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4659ec7d83964bea2f715b3c0b7bf9240181337c))
* **selenium:** route every LinkedIn session through the user's proxy + drop the stale UA ([a440be0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a440be0c58b139d1a91ec2b90a67c16b7ae9720b))

## [0.45.4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.45.3...v0.45.4) (2026-07-22)


### Bug Fixes

* **engagement:** break the LinkedIn 429 breaker doom loop ([62e3ca0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/62e3ca077a77bf0c16bd40d638f0349c32b8104b))
* **engagement:** break the LinkedIn 429 breaker doom loop ([77155cd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/77155cdf348145286dba85a81355879a00b05e46))
* **review:** address Copilot + CodeQL feedback on PR [#370](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/370) ([e9f79bc](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e9f79bc66adde1369640dae963609e15db98674d))


### Documentation

* **claude:** document production deploy flow + local hotfix fallback ([bcbbbb9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bcbbbb9b5a0d582e161fd6a20c26a6c853d0a4f7))

## [0.45.3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.45.2...v0.45.3) (2026-07-13)


### Bug Fixes

* **replies:** loop-safety guards on the reply sweep ([fcfca89](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fcfca89d3d3268f46487b8dfe52977e38676343c))
* **replies:** loop-safety guards on the reply sweep (event-driven replies on own posts) ([1c0f84e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1c0f84e5e71a62b09d1dc1cffe66d4cf6b7eb49d))

## [0.45.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.45.1...v0.45.2) (2026-07-13)


### Bug Fixes

* **replies:** forward Gmail confirmation to the user + robuster auto-confirm ([ff97b72](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ff97b72b5cb8c41caa1d5495e4097238e4a375a0))
* **replies:** forward Gmail confirmation to the user + robuster auto-confirm ([66b6a66](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/66b6a660dabe15f1b3036639b9d7b824ab549dda))

## [0.45.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.45.0...v0.45.1) (2026-07-13)


### Bug Fixes

* **replies:** route reply+ mail from the single SendGrid inbound URL (PIN endpoint) ([75eaea0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/75eaea03e92f8ae3a51d4c6ef6157b615c6eeeaa))
* **replies:** route reply+ mail from the single SendGrid inbound URL (PIN endpoint) ([1433088](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1433088813990d6df2fa3938530bd7b5d1635050))

## [0.45.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.44.0...v0.45.0) (2026-07-13)


### Features

* **replies:** auto-confirm Gmail forwarding to the reply address ([03e0a97](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/03e0a97aa7ec35537d3c4bb3ec1401c0bd3200fe))
* **replies:** auto-confirm Gmail forwarding to the reply address ([7d3442d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d3442dd2e96caab996b51b19b83c334ef3ea7cb))

## [0.44.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.43.2...v0.44.0) (2026-07-12)


### Features

* **feed:** empty-filter fallback + reach estimate for feed commenting ([f3a36b8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f3a36b8d6ca0d2d3fedfeb89f4f2a9fc2c85f86d))
* **feed:** empty-filter fallback + reach estimate for feed commenting ([7519eba](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7519eba536cae6d7ff877e6b3d6aa32ccd82de8e))

## [0.43.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.43.1...v0.43.2) (2026-07-11)


### Bug Fixes

* **content:** enforce LinkedIn readability + length on carousel captions ([2840cf8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2840cf8238dad8f22d4540f00eaa64bbbbc2c8b8))
* **content:** enforce LinkedIn readability + length on carousel captions ([099a5ab](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/099a5ab7fbd494be0be7112f66660e10ce800a17))
* **content:** reflow over-long paragraphs (under-formatted posts), not just walls ([20c652a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/20c652a2b7aa30c893853ac47022890e455dea29))
* **content:** reflow OVER-LONG paragraphs, not just total walls ([baf81e2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/baf81e25ac2eab34913d9e8a0743d8c8c5a6a4ef))

## [0.43.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.43.0...v0.43.1) (2026-07-10)


### Documentation

* **ui:** full LinkedIn + Gmail setup steps for event-driven replies ([d55b1fb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d55b1fb9084862a4d4d33a8e03e8703c593e7311))
* **ui:** full LinkedIn + Gmail setup steps for event-driven replies ([c921911](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c921911b008a29d6740944e03978de73e9456ce6))

## [0.43.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.42.0...v0.43.0) (2026-07-10)


### Features

* **automation:** post own-post seed comments via socialActions API, not Selenium ([1be855b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1be855b2d2cac78331108f153feb1570f4db520a))
* **rate-limit:** adaptive 429 back-off + manual automation pause (break the doom loop) ([da69066](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/da690669ed3df18696d10372e31af948bf494af4))
* **rate-limit:** adaptive 429 back-off escalation + manual automation pause ([a5e74cd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a5e74cd88d871d33b6a320ab989dc2c5ce1a0849))
* **replies:** comment-notification webhook → debounced reply sweep (P2) ([3d44041](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3d440416ff8a550d53a9e6d9cf2bc6148ed1b3eb))
* **replies:** event-driven + reduced reply/comment follow-up to cut LinkedIn 429s ([af66714](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/af6671403e04f279faac123d033bd53c98d2f696))
* **replies:** recent-posts sweep + reply-check config to cut 429s (P1) ([e773c36](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e773c365644324592a20077b3c4aaa6597e2cedb))
* **replies:** reply-check mode config in prefs API + Account UI (P1c) ([c048484](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c048484578c51340e52c3f2229dbc9766584991f))
* **replies:** scheduled reply-sweep beat dispatcher (P3) ([cace01d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cace01d7f5ed649877e3c3fba59f5783329f729e))


### Bug Fixes

* **automation:** ground seed comments & replies on canonical post body ([2d07045](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2d07045869b9ec76cfe5f7625c49e77d8b38a7bc))
* **automation:** seed comments on own posts were about the /posts API, not the post ([18a18f1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/18a18f15e97d1708ffebddff03bc65531f04fdb4))
* **automation:** store real post body in POST log, not a status string ([7835dec](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7835dec3dbc8cb7f30213a5c93b5fc84c6231c0e))

## [0.42.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.41.2...v0.42.0) (2026-07-08)


### Features

* **db:** auto-prune superseded cookies to keep sessions fresh ([29452e7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/29452e769713c505ff460e7c827c1b423f64f846))
* **db:** auto-prune superseded cookies to keep sessions fresh ([7bf93b4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7bf93b4eeacda0a7da63708e490762486649b0ff))

## [0.41.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.41.1...v0.41.2) (2026-07-08)


### Bug Fixes

* **api:** let the browser extension reach /api/user/linkedin-cookie ([5e40f49](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5e40f49b2df3aabdc0a65ca4d32242c03ab01c45))
* **api:** let the browser extension reach /api/user/linkedin-cookie ([587483f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/587483f97551d3b4835004e97491c45c6dfc1a19))

## [0.41.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.41.0...v0.41.1) (2026-07-08)


### Bug Fixes

* **api:** make the extension download route public (bypass bearer gate) ([a66be96](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a66be96df9a612f83af698390dc233092206c72d))
* **api:** make the LinkedIn extension download route public ([fb8b42d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fb8b42d3a03e0c14f858033599aa9e8a32a3aabc))

## [0.41.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.40.1...v0.41.0) (2026-07-08)


### Features

* **account:** ship the one-click LinkedIn extension to users ([e1fe2c0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e1fe2c0bb7d35ea8c00b5cc8d71983d21c865be6))
* **account:** ship the one-click LinkedIn extension to users ([f511dc7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f511dc75269c601d0e337acff5bfc1b60fb7ef8e))

## [0.40.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.40.0...v0.40.1) (2026-07-08)


### Bug Fixes

* **email:** brand transactional mail as LEM + disable SendGrid click-tracking ([5499fe4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5499fe4c613d08ec4d789fac8178253f3bbf790c))
* **email:** brand transactional mail as LEM + disable SendGrid click-tracking ([ce53532](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ce53532d4a98c8e7edec8befdfee1eca71a00dc7))

## [0.40.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.39.0...v0.40.0) (2026-07-07)


### Features

* **spa:** Save Draft vs Approve & Schedule for SPA-created posts + newsletters ([c7e3d4f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c7e3d4fc84fd42292f267efa780cc363dd8c354f))
* **spa:** Save Draft vs Approve & Schedule for SPA-created posts + newsletters ([e3e6405](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e3e6405c91f2b2140a251e6e9d61254b7b10ff42))

## [0.39.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.38.1...v0.39.0) (2026-07-07)


### Features

* **review:** filter posts by type + boolean keyword search + sort on Review & Edit ([a988102](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a98810282fd3ce4a45d8fee2ee944361ab6a3946))
* **review:** post-type filter + boolean keyword search + sort on Review & Edit ([d89b052](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d89b0522d6ede99706b7efbd9a7c2507c97f6473))


### Bug Fixes

* **carousel:** keep content-slide body text inside the margins ([d561f30](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d561f307e91ee826e64f4177cf096d5a4a962555))
* **carousel:** keep content-slide body text inside the margins ([abf2098](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/abf2098a55275bb073d4326704e474ed69c8f460))
* **ui,deploy:** lowercase post_type on schedule + make Flower basic auth optional ([77036d8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/77036d8a74565d233bc210e0addd8edc3c6391f9))
* **ui,deploy:** lowercase post_type on schedule + optional Flower basic auth ([45736f2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/45736f27b639556dc4f47c0580ffe0a7a4c20e6e))

## [0.38.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.38.0...v0.38.1) (2026-07-07)


### Bug Fixes

* **carousel:** send browser User-Agent when downloading Pexels slide images ([b8feab8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b8feab86af3d782dc897a4bccf29aa10e1115b57))
* **carousel:** send browser User-Agent when downloading Pexels slide images ([80859c7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/80859c72fad27f129a024b3996f37671f0de2b31))

## [0.38.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.37.1...v0.38.0) (2026-07-07)


### Features

* **carousel:** composite content-slide images in the posted PNG renderer ([474c20c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/474c20c6c689fda8fd21f12fe6fb4d7ecc91aef5))
* **carousel:** composite relevant images into content slides (fill white space) ([9dd5ee0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9dd5ee0db9e07ce6daf5cee8b40b1e88b36bd80e))
* **ui:** planned-tasks card by kind + default video quality control ([0d1150a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0d1150a5111e04bc3dcdf833ce356ab897c073cd))
* **video,dashboard:** avatar-on-standard video, default_video_quality pref, upcoming planned tasks ([b2b759a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b2b759a3e7cfbecb64119aeaf6a8b4c1d672c9e4))
* **video,dashboard:** avatar-on-standard video, default_video_quality pref, upcoming Planned Tasks ([4b4be1b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4b4be1bb52ba9b0b4566236f16f14fcc94111204))

## [0.37.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.37.0...v0.37.1) (2026-07-07)


### Bug Fixes

* **content:** single clean lead-magnet ask — preserve context in rewrites, strip soft paraphrase ([3e53672](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3e536729729d57ef590e1767c204e8b8c0988a28))
* **content:** single clean lead-magnet ask (preserve in rewrites + strip soft paraphrase) ([f5da84d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f5da84d30e286e309655f6293b71e6c53a97083f))

## [0.37.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.36.0...v0.37.0) (2026-07-07)


### Features

* **content:** anchor post subjects to focus topics + post-history dedup/alignment review ([304bff8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/304bff81b04e43bd0fee466fef605b60b9675b38))
* **content:** anchor trend-based post subjects to the user's focus topics ([a2fb5ff](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a2fb5ff076e6627c488f930703edcfdb557ecca3))
* **content:** post-history dedup steering + similarity review gate ([50a89c7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/50a89c746befc7568e04bbbc9759ceccb8ae4e17))
* **engagement:** honor prefs on viewer-comment path; env-tunable feed scoring ([7d67921](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d67921f10840cee3937c4066e94d7e18d641e6e))
* **scheduler:** DEFAULT_POSTING_HOURS env override for the default post-time model ([bfb3731](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bfb3731aac3a801f238cc1d029446fa317f02da6))


### Bug Fixes

* **alignment:** drive hardcoded prompt styling from user settings (configurability audit) ([7dadb33](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7dadb33a474ab846d42f1f1c69ced97828ad45eb))
* **api:** remove dead AvatarActivateRequest properties ([d45faf7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d45faf731140872b698ce39e9a1ac8f1667a8c0a))
* **api:** validate PUT /dm action and reject empty updates ([19d6724](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/19d6724458e6841716b74cd1a517a1ebe9833726))
* **celery:** get_aws_sqs used service_name 'elasticcache' for SQS ([78bcff8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/78bcff83e33f1f3b91a4053bb270d88f092b13c6))
* clean up coverage-found bugs + Copilot review findings (21 items) ([3674ab1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3674ab1dd89e9d10b6f152f337963cc987737f1c))
* **content:** deterministic fallback in select_focus_topic ([e772a74](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e772a74a57b298dd3076f274196852350ced20d8))
* **content:** drive emoji/hashtag/tone prompt rules from engagement prefs ([1f265d3](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1f265d35abe291f140bd2960de88687c007dc253))
* **content:** guarantee lead-magnet comment-keyword CTA survives refinement pipeline ([09a1d87](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/09a1d87ff2f0268d81232394428002f2b29de55f))
* **content:** guarantee lead-magnet CTA survives the refinement pipeline ([a96b565](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a96b565246c4cbfce511bd3db81af5eaf72c9f46))
* **content:** index CTA repair menu by selection ordinal, not raw post_id ([c7ba539](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c7ba5394b83516239c93a8c532fd3383e8a8954b))
* **content:** regenerate guards — unknown type skips, status validated at API ([5cd9969](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5cd9969afbaced3cc7905a44a7c2e879557198ee))
* **db:** error fallbacks and set_active_avatar safety ([a00ccff](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a00ccff789966ac489e768679162736f9a75ee79))
* **dm:** overdue approved DMs stay eligible + orphaned-DM recovery ([e251578](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e25157818b3527efcb3ba955662e092a79948b09))
* **lead-magnet:** exempt configured trigger word from the bait filter ([4876f16](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4876f16683bc7c3b7fe3b7632fc8ba49258b4913))
* **lead-magnet:** require message, guard invalid cadence, fix multi-word label check ([bd191f5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bd191f57de695601a878d943bd23753354394f90))
* replace deprecated datetime.utcnow() with timezone-aware equivalent ([d7beabf](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d7beabfdea35b4a5183fda8611e6a7d3816abc9a))
* **ui:** honest delete-confirmation copy + session guard on regenerate ([5889c84](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5889c8443e34bc2df2f1aafb2a8b2b71a09ccd65))

## [0.36.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.35.0...v0.36.0) (2026-07-06)


### Features

* **content:** weave lead-magnet CTA into ~1-in-N generated posts ([3874873](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3874873bff784601d6d5eb3ad7d4e4fb0d98e0f7))

## [0.35.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.34.0...v0.35.0) (2026-07-06)


### Features

* **dm-scheduler:** schedule 1:1 DMs mirroring the post scheduler (closes [#306](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/306)) ([35d1a93](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/35d1a93c0c383d27e035cb1a1ff2e6d86c033da1))
* **dm-scheduler:** schedule 1:1 DMs, mirroring the post scheduler ([#306](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/306)) ([1805b35](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1805b35163cc7d2a132ff9dfc12f607b7f90b120))
* **dm-scheduler:** SPA DMs tab with preview/approve workflow ([#306](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/306)) ([72a2ae0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/72a2ae08c15f6e9794f86b7ce28f0aea3369e896))
* **ui:** consolidate Schedule + Review into one Content Studio page ([c059d35](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c059d35847e35e12c1cd6712e282ba2a42e1af2a))
* **ui:** consolidate Schedule + Review into one Content Studio page ([168da98](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/168da9833832e398ab09ef008c633963b5beb27f))

## [0.34.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.33.0...v0.34.0) (2026-07-06)


### Features

* **account:** Save All bar, unsaved-changes guard, placeholder chips ([39e7d9d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/39e7d9d36e8ab347a7214d059f1c8d16af17a545))
* **dm:** unify DM/lead-magnet placeholder substitution engine ([9257fee](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9257fee748c2a530907b13a769526a8d89c81b31))
* **posts:** quick delete + regenerate-with-suggestions ([8b8e2bb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8b8e2bbc66c00566d9a13158c3cf69b6aeb8d413))


### Bug Fixes

* **engagement:** settings persistence + placeholder substitution + post regenerate/delete ([bd296ac](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bd296acd73d2481c904a5847262bb068a6a5a005))
* **engagement:** widen tone column so settings persist; surface save errors ([9f4ad0f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9f4ad0fda03d64752a6dc7fa7d2856bd6b6cfa23))
* **settings:** align input length limits across SPA, API, and DB ([8c28f52](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8c28f52a975c018ebc5de341c3c2c3b9d8d39e7c))

## [0.33.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.32.0...v0.33.0) (2026-07-06)


### Features

* **ai:** unify newsletter blueprint/research/alignment into one shared content core ([fa3c040](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/fa3c04080eb9a6d31f17c9f230b222b4e708f59d))
* **comments:** per-run comment angle rotation, target-post grounding kept, research gated off ([9c7d4bd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9c7d4bd44083c7fd7b20a393f0ad56718773f122))
* **posts:** rotate post archetypes via shared framework + research-backed trends (V51) ([c8c55f8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c8c55f8ebed3b520227882eb658848f43f4dbf92))


### Documentation

* document unified content core, V51, and per-type research toggles ([6e70ad0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6e70ad04f6e29a2f0d2afb66cd7d1062d198e7ca))

## [0.32.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.31.0...v0.32.0) (2026-07-06)


### Features

* **text:** normalize rogue AI typography (em dashes, smart quotes) in public output ([580e46f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/580e46ff1d14c0e5ac08ef34e765ce8e40cc6bc0))
* **text:** strip rogue AI typography (em dashes, smart quotes) from public output ([38187d4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/38187d49f1ffccd5a6f937898630664f1371f830))

## [0.31.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.30.0...v0.31.0) (2026-07-06)


### Features

* **newsletter:** edition blueprint/variety system + Perplexity research grounding ([94f0ff9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/94f0ff93529644ea213ae3d05b01929d23494efe))
* **newsletter:** edition blueprint/variety system, research layer, V50 shape history ([49f5c87](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/49f5c878e058bae3549600f980ee09c47e3bac94))
* **newsletter:** wire blueprint + research through top-up/regenerate, UI format badges, tests ([b317f40](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b317f408374cba490181618f43fa4f2addf20302))

## [0.30.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.29.0...v0.30.0) (2026-07-06)


### Features

* **newsletter:** persist edition subject (V49) + recent-subjects dedup history ([a1e0457](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a1e0457a2094b06ecc149d7f3f581644b02e464f))
* **newsletter:** topic-planning phase + synthesis alignment + Re-generate w/ guidance ([642b9f0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/642b9f023771e0c7a20323ae4dfa69c96790064b))
* **newsletter:** topic-planning phase, synthesis-grounded editions, regenerate task+endpoint ([b4de4f5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b4de4f58226903b9849f8139d453ba624d35ee60))
* **ui:** Re-generate action + Added Guidance textarea in newsletter review queue ([15f46f8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/15f46f897e79d66bc9ed36193d181727a248da26))

## [0.29.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.28.0...v0.29.0) (2026-07-06)


### Features

* **automation:** consolidate duplicate comments to one-per-post (dry-run default) ([6db8e99](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6db8e991ef2b9b551ae3a036a7f4f317ab4d1fa9))
* **engagement:** weekly profile synthesis as the voice source for generation ([50a931a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/50a931a18d393dedabd79a39c443f3314593b7c1))


### Bug Fixes

* **ai:** harden profile-synthesis staleness check against non-datetime timestamps ([0a88cea](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0a88cea804bba6c9ca182e4f84f288bf10f8567d))

## [0.28.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.27.0...v0.28.0) (2026-07-06)


### Features

* **newsletter:** multi-draft review queue with configurable count + days-ahead ([5031ec6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5031ec6183a021f28e078d72012886b7994ea41a))
* **prefs:** add focus_topics + business/personal goals to engagement prefs ([099f749](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/099f7496c967cfbeec6a94910bdffb4b30829aa0))
* **ui:** add Content Focus & Goals card to engagement settings ([cc5af9b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cc5af9b93a933edce95931d42b6acbf0508c5675))
* **ui:** copyright footer with configurable release-version text ([f2966c9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f2966c900337644c9111d57a6c2e2c564dc7789f))
* **ui:** copyright footer with configurable release-version text ([7c9867a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7c9867a3dff0e4a63d726188720e280a670e5173))


### Bug Fixes

* **ai:** ground comments in target post, block LEM self-promo, add focus steering ([9a8d1fe](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9a8d1fe88add299be10894f51db3cf563342f159))
* **automation:** persistent at-most-once feed comment dedup (V46 commented_posts) ([75ae240](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/75ae240b57194b5e31d3eb0d1970aea97313763e))
* **automation:** reliably react on non-own commented posts; harden SDUI react fly-out ([7d345bd](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7d345bdf73a553fb456802f3fc48b1b0f0994f7c))
* **email+auth:** trustworthy login PIN email + sliding 24h session ([755cea9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/755cea9317d6b25dc1a05e73fa50b61e6abd6eac))
* **engagement:** 1 comment/post + reactions + topic-aligned comments (no LEM drift) ([0223d3f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0223d3fe95570e9b4a03d872ad49fd8623fd8b2b))
* **newsletter:** top up draft queue immediately when count is raised ([e884c57](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e884c571608cc26eafc90dad5fe4b1062f6ada4a))

## [0.27.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.26.0...v0.27.0) (2026-07-06)


### Features

* **newsletter:** multi-draft queue with days-ahead + count config ([66fc566](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/66fc5661b0d248c20ce2f579f03fb5c4afdbf23a))
* **newsletter:** multi-draft review queue on Review page + plan-ahead config ([6d08ad4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6d08ad46cb1dd9646b7f0821537ffdd4e59825ad))
* **ui:** move newsletter draft review to Review page, add config ([1a41038](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1a41038734fc0a09eb6470ca88981b055a8511df))


### Bug Fixes

* **newsletter:** address PR review comments ([36c88f1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/36c88f1b6364d9aaeb7e35e72e113ac890d23608))

## [0.26.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.25.1...v0.26.0) (2026-07-05)


### Features

* **engagement:** react on posts we comment on (SDUI, AI-chosen reaction) ([5de7953](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5de7953168560d2927240eb9904c6f677d280949))
* **engagement:** react on posts we comment on (SDUI, AI-chosen reaction) ([7c14270](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7c14270d26bbd52855a3e08d1701ea90aa666d9c))


### Bug Fixes

* **compose:** correct selenium-lane healthcheck node name ([26dd1ae](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/26dd1aecd810c59f98dc2425906468f4566d8266))
* **compose:** correct selenium-lane healthcheck node name (false unhealthy) ([0fb8e12](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0fb8e127a0c2fd6fdc1fa95f97971c6201de8618))
* **time:** 12-hour newsletter publish-hour picker (never 24h) ([c0ad14c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c0ad14c32e04eb7353a1939ac1115248a94e1fa8))
* **time:** 12-hour newsletter publish-hour picker (P1 UI) ([b66d4e2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b66d4e209685833bbc7d3ec88d59709771cd3e3b))
* **time:** correct displayed times to user-local 12h + hide synthetic feed URLs ([8a62e30](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8a62e30a300d1037c6c2c5df3e5aa16d96fe5199))
* **time:** pin Celery beat to UTC + follow-ups/tz-default on UTC (P1 backend) ([655877d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/655877d131261e05d125ff4de29c0eadf29a3b65))
* **time:** pin Celery beat to UTC + standardize follow-ups/tz-default on UTC ([4622a1f](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4622a1f26d4608795894984e97891bdadc318a05))
* **time:** user-local 12h times + hide synthetic feed URLs (P0) ([f4f27e0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f4f27e0c853c8e59c6eb78d4a3d7c961a9e99d06))


### Documentation

* **claude:** add Git Safety & Multi-Agent Concurrency Rules ([bf3f180](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/bf3f1805f20452778777ce55456c2db78e53e6dd))
* **claude:** add Git Safety & Multi-Agent Concurrency Rules ([b173f62](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b173f62b77ec9bf50fb80db27a7a6efea6276148))

## [0.25.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.25.0...v0.25.1) (2026-07-05)


### Bug Fixes

* **selenium:** route auto_publish_edition to se_content (was on retired 'selenium' queue) ([7fdf7d8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7fdf7d8f01cb995065fbad232a669f4276595221))


### Performance Improvements

* **selenium:** 3-lane workers + 4 concurrent sessions (fix automation starvation) ([5d63442](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5d6344276f1fafeadae78d67f28ef82cdfe6abb2))
* **selenium:** 3-lane workers + 4 concurrent sessions (fix automation starvation) ([0017a58](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0017a58a871f22334bc350b88771fe142fa113bb))

## [0.25.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.24.1...v0.25.0) (2026-07-05)


### Features

* **newsletter:** draft-review + auto-publish scheduling workflow ([0a7251d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0a7251d5aba9ee22750e805af733520da2235fe1))
* **newsletter:** draft-review workflow + day/time scheduling ([b785a93](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b785a9350c35ea110de784be6aeac608aeaf4c93))

## [0.24.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.24.0...v0.24.1) (2026-07-05)


### Bug Fixes

* **dashboard:** link home-feed comment rows to LinkedIn comments activity ([44f1840](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/44f18406be80c910124b066f663f20a0fd147264))
* **dashboard:** link home-feed comment rows to LinkedIn comments activity ([d26d85d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d26d85d91f8970b7f88c0245165767dbc8924c8a))

## [0.24.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.23.2...v0.24.0) (2026-07-05)


### Features

* **newsletter:** richer, best-practice editions + subtitle field ([cc87056](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cc87056218bf5b8d4fc4029d0c2e84abbbe7bb64))
* **newsletter:** richer, best-practice editions + subtitle field ([73cbe76](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/73cbe76f6ca936c27f6c134aa4a70054a7c4e69e))


### Bug Fixes

* **feed:** log real /feed/update/ permalinks for inline comments ([e224be7](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/e224be7642fb62a708bfec810b8584ffb859d819))
* **feed:** log real /feed/update/ permalinks so activity feed links work ([212b552](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/212b552f1165880efa1fd9e70a59031882ea55d0))


### Documentation

* refresh capabilities in CLAUDE.md, README, and copilot-instructions ([0f77d26](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/0f77d267c5ca490ac660e759290485a7d8aeffed))

## [0.23.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.23.1...v0.23.2) (2026-07-05)


### Bug Fixes

* **ui:** merge Voice&Tone + Targeting into one shared-state component ([c223158](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c22315850ef2ba522d15269c84133a71ddf9aca0))

## [0.23.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.23.0...v0.23.1) (2026-07-05)


### Bug Fixes

* **dashboard:** correct stale top stats via SQL aggregates ([9eba7c2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9eba7c26c46b7ce69084e46041118e873eca6296))
* **dashboard:** correct stale/incorrect top stats via SQL aggregates ([5439af1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5439af19dffe739eb6f557a89f5d733bef1d4f2d))
* **engagement:** ground group + post-stats selectors from live sweep ([7548a40](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7548a40f5a854cd201422f7b4d8ba8cc86a2a8d3))
* **engagement:** ground group + post-stats selectors from live sweep ([1d67793](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/1d6779364dc85b619ca4dc365231e705eab32ae8))

## [0.23.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.22.0...v0.23.0) (2026-07-04)


### Features

* **engagement:** auto seed + pin first comment on own posts ([4dda534](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/4dda5343af941be456f63aa619e5b98d3b953b7b))

## [0.22.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.21.0...v0.22.0) (2026-07-04)


### Features

* **growth:** roadmap P2–P7 (groups, stats, lead-magnet, hook/save, guardrails, thread-builder) ([c80a751](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/c80a751576a119773c39bded63ca5b6f357d3000))
* **growth:** roadmap P2–P7 (groups, stats, lead-magnet, hook/save, guardrails, thread-builder) ([02676ac](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/02676ac64ec981b00a958991cc293f5b228b2d6e))
* **newsletter:** LinkedIn newsletter engine (P1 of growth roadmap) ([627305a](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/627305a2a853cd9d536c47284eded41bc3971e3b))
* **newsletter:** LinkedIn newsletter engine (roadmap P1) ([cbea5a1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/cbea5a1b799de54e12f4b46b95b91c5f910c6362))

## [0.21.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.20.0...v0.21.0) (2026-07-04)


### Features

* **engagement:** daily golden-hour commenting on top of pre-post ([9819ace](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9819ace0aff7280ac8e7107c061f9b73f58fa4c5))
* **engagement:** daily golden-hour commenting on top of pre-post ([aa5fc71](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/aa5fc716dfec22503a501be36d04d85dbe14ffcb))

## [0.20.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.19.2...v0.20.0) (2026-07-04)


### Features

* **comments:** recency-dominant scoring matrix + signal extraction ([2cdac4c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2cdac4cb4f1182f1b1e8ff3f1da07b10afc5cfb5))
* **comments:** rewrite generation for engagement — short, specific, question-ending ([9b373a6](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9b373a6d0ba0ecac8804acff4b7ab34188eb4586))
* **engagement:** comment quality + recency scoring + reciprocity + no-post-day runs ([a650229](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a650229acbf1b5cf16052804864455ab1aae8c0c))
* **engagement:** reciprocity loop — prioritize people who engage with us ([09bb763](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/09bb7635f7b551a239a13af4a105faf932e5c373))
* **engagement:** standalone feed commenting on no-post days ([f9e27ee](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/f9e27eea27a2dda01f24686f069e794c121fcd9c))

## [0.19.2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.19.1...v0.19.2) (2026-07-04)


### Bug Fixes

* **automation:** actually post inline comments/replies (submit + verify were broken) ([208d5fe](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/208d5fe2ac0d09d47bb79be99ea9f766d82ad01c))
* **automation:** actually post inline comments/replies (submit was broken) ([799e946](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/799e946dedbc717cfbf21273db5594f6f11b7db6))
* **ci:** make Dependabot reconciler major-safe on main ([8dcc2a8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8dcc2a8bc7beed3e147873c8bb8ab3775cb9b001))
* **ci:** make Dependabot reconciler major-safe on main ([b9bfbcb](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/b9bfbcb9f26a78d51c7cdcfc2aaba6f151c20cd3))

## [0.19.1](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.19.0...v0.19.1) (2026-07-04)


### Bug Fixes

* resolve v0.19.0 runtime errors from celery/PostHog logs ([8b78cba](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8b78cbada8d2e8abab091b6a4701ba65bb13fbbf))
* resolve v0.19.0 runtime errors surfaced in celery/PostHog logs ([efdb4e2](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/efdb4e2401552f129cb126c329a3d987795db99a))

## [0.19.0](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/compare/v0.18.0...v0.19.0) (2026-07-04)


### Features

* **dm:** configurable voice-aligned DM templates (4b) ([5402f26](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5402f266a881baaee46d40101035074105261bd8))
* **dm:** configurable, voice-aligned DM templates (replaces hard-coded messages) ([3ae21a5](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/3ae21a5d42598370e388223d6339d82e86c9ef88))
* **dm:** multi-touch DM follow-up sequences ([09e019c](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/09e019cdfa7bf0c912305a3880f98527a0c91687))
* **dm:** multi-touch DM follow-up sequences (4c) ([7feec98](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7feec988d3b82a1c84ef03a32dd0e9d84b6293b5))
* **engagement:** configurable targeting + voice/tone (4a) ([a14ff00](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/a14ff0063d3e0619e80abfb687cc8b5c8c14267a))
* **engagement:** configurable targeting + voice/tone (engagement preferences) ([ccc6200](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ccc62003690dc30bb09245c62d31fa1d2920d775))
* **location:** city/state login-location picker + admin override ([7fc2a0d](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7fc2a0d91c7e7d4c762b72eed51175da14ab2a57))
* **location:** city/state login-location picker + admin override ([5afbf72](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/5afbf722a0494682baf1879951a5f13e3427af74))
* **ui:** engagement config cards — voice/tone, targeting, DM templates ([9bf2507](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9bf250745c6898fb81a4ae609409e5fa0a48dea2))
* **ui:** engagement config cards (voice, targeting, DM templates) ([ed2fa8e](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/ed2fa8e0a266957d82ea40f88e6f55932a605c68))


### Bug Fixes

* **dm:** reply-detection uses message-group sender, not a nonexistent class marker ([046e564](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/046e56451a561bae40a0c9e7de237fdfc7efa804))
* **login:** derive PIN Reply-To domain from configured env, not example.com ([6af92b4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/6af92b4a3805a34f1073dcd65caf5a82b5798479))
* **login:** derive PIN Reply-To domain from configured env, not example.com ([8c78efe](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8c78efe5b79f4e96e7c462a694a6430234fd956a))
* **scraper:** extract profile name from &lt;title&gt; after LinkedIn DOM change ([2a7e201](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/2a7e2016f3ef0d30e84042009be51e20e2a15208))
* **scraper:** extract profile name from &lt;title&gt; after LinkedIn DOM change ([7aa7a89](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/7aa7a8963de4e9d582afaab6aa967236612dab7e))
* **scraper:** rebuild feed commenting for LinkedIn's SDUI redesign ([9318e45](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/9318e45e62e8e3c6a4ea3beb1f2caf7e06faf84c))
* **scraper:** rebuild feed commenting for LinkedIn's SDUI redesign ([79eb92b](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/79eb92b580cd5fe25cfc290af9339a2368487029))
* **scraper:** rebuild reply-to-comments flow for LinkedIn's SDUI redesign ([8e11bb8](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/8e11bb87a31b35abfe571f85315389651d3bac0c))
* **scraper:** rebuild reply-to-comments flow for SDUI ([765a722](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/765a722c1e2581eaf2fff6cc217b9371aec7d29d))


### Documentation

* clarify SendGrid Inbound Parse "raw MIME" must be UNCHECKED ([d9dfbf9](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/d9dfbf92765a82cdc6367f4dcf9b8039ef15f775))
* clarify SendGrid Inbound Parse raw-MIME must be unchecked ([351c5e4](https://github.com/christopherqueenconsulting/linkedin_engagement_manager/commit/351c5e4c1c1ef777bc5fd10110456ddd1c6b8398))

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
