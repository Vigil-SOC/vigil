# Changelog

## [0.4.0](https://github.com/Vigil-SOC/vigil/compare/v0.3.0...v0.4.0) (2026-07-24)


### Features

* **auth:** harden authentication for production readiness ([#361](https://github.com/Vigil-SOC/vigil/issues/361)) ([201173a](https://github.com/Vigil-SOC/vigil/commit/201173a90534ad4f2d237f24b178405866cbc372))
* **cases:** add guarded per-case deletion ([#402](https://github.com/Vigil-SOC/vigil/issues/402)) ([8659257](https://github.com/Vigil-SOC/vigil/commit/8659257ba970b17cd70ec62e44a4c0ea6774c578))
* **findings:** render structured source evidence ([#404](https://github.com/Vigil-SOC/vigil/issues/404)) ([ba90f1b](https://github.com/Vigil-SOC/vigil/commit/ba90f1b7545f5c65110275fd48d206560d7dbfe0))
* **llm:** native OpenAI-format agentic loop across chat, daemon, and workflows ([#357](https://github.com/Vigil-SOC/vigil/issues/357)) ([94ad791](https://github.com/Vigil-SOC/vigil/commit/94ad791d2b2be869dbd8d7407166e0a247f857a1))
* local Ollama enrichment recovery (settings + implementation) ([#435](https://github.com/Vigil-SOC/vigil/issues/435)) ([df7095d](https://github.com/Vigil-SOC/vigil/commit/df7095db526f9dd5c2b60216c4667760a986656c))
* **loglm:** page-extension host, authenticated MCP, and pgvector embeddings ([#398](https://github.com/Vigil-SOC/vigil/issues/398)) ([709da6a](https://github.com/Vigil-SOC/vigil/commit/709da6a0556f0cc336a2ee4c7c66c9bff7c38cda))
* **redesign:** make assistant dock resizable ([#401](https://github.com/Vigil-SOC/vigil/issues/401)) ([e555120](https://github.com/Vigil-SOC/vigil/commit/e5551208d54038b45dc17d819120d697e4fb18b8))
* **theme:** add Background tweak deriving full ramp from base ([#372](https://github.com/Vigil-SOC/vigil/issues/372)) ([6befe9d](https://github.com/Vigil-SOC/vigil/commit/6befe9d954e0f5b501bd58505791361f98495920))
* Vigil desktop app + first-run bootstrap + provider-URL SSRF hardening ([#397](https://github.com/Vigil-SOC/vigil/issues/397)) ([d8229e8](https://github.com/Vigil-SOC/vigil/commit/d8229e8a171b234323454953017ef055be425b9a))


### Bug Fixes

* **agents:** approval-gate vendor MCP action tools ([#399](https://github.com/Vigil-SOC/vigil/issues/399)) ([2205e94](https://github.com/Vigil-SOC/vigil/commit/2205e94f8e89fb97ac1d8aff1a271e3fd67b5b4a))
* **chat:** hide embedding models from the chat picker ([#434](https://github.com/Vigil-SOC/vigil/issues/434)) ([e3e9520](https://github.com/Vigil-SOC/vigil/commit/e3e952070046ac64a682f8562ad26c131b220325)), closes [#433](https://github.com/Vigil-SOC/vigil/issues/433)
* **chat:** self-heal stale/removed model selection ([#385](https://github.com/Vigil-SOC/vigil/issues/385)) ([4589edd](https://github.com/Vigil-SOC/vigil/commit/4589edda5cea3db7bc72dd90473f4c26b17268da))
* **chat:** show non-Anthropic models in picker ([#432](https://github.com/Vigil-SOC/vigil/issues/432)) ([d2f5339](https://github.com/Vigil-SOC/vigil/commit/d2f5339c6f6f4305274efdfc21205ba7abb9b40e)), closes [#409](https://github.com/Vigil-SOC/vigil/issues/409)
* **db:** enable pgvector extension before create_all() ([#407](https://github.com/Vigil-SOC/vigil/issues/407)) ([b02fc5e](https://github.com/Vigil-SOC/vigil/commit/b02fc5ef65222681b11738ee7cb7a39251390a84)), closes [#406](https://github.com/Vigil-SOC/vigil/issues/406)
* **frontend:** Corrects Bug: Frontend chat drawer only lists anthropic models [#409](https://github.com/Vigil-SOC/vigil/issues/409) ([#412](https://github.com/Vigil-SOC/vigil/issues/412)) ([f83c2eb](https://github.com/Vigil-SOC/vigil/commit/f83c2ebfae96ba13aefdb64c0b8314a3cf7b7a88))
* **logs:** don't crash backend when logs dir isn't writable ([#382](https://github.com/Vigil-SOC/vigil/issues/382)) ([22078a4](https://github.com/Vigil-SOC/vigil/commit/22078a445515bcc39650918f66439445bfb86437)), closes [#376](https://github.com/Vigil-SOC/vigil/issues/376)
* **mcp:** pin mcp-remote to &gt;=0.1.16 to close CVE-2025-6514 ([#390](https://github.com/Vigil-SOC/vigil/issues/390)) ([b2fcaf5](https://github.com/Vigil-SOC/vigil/commit/b2fcaf58142b2956c73607d2393ed4dcb4d3e893))
* **redesign:** define missing chat-dock width helpers ([#438](https://github.com/Vigil-SOC/vigil/issues/438)) ([6f4b0df](https://github.com/Vigil-SOC/vigil/commit/6f4b0df660a9cdf6712c72e18cdc6366f38cec9e))
* **redesign:** scope Escape to topmost dialog layer ([#400](https://github.com/Vigil-SOC/vigil/issues/400)) ([79b4343](https://github.com/Vigil-SOC/vigil/commit/79b43436acb3608bab6dfbd580f139c16ccde326))
* repair stale setup docs and dev-bootstrap provisioning ([#375](https://github.com/Vigil-SOC/vigil/issues/375)) ([0b3bb1c](https://github.com/Vigil-SOC/vigil/commit/0b3bb1caf2bc3b8f10c6d12851a8d23842ce1220))
* **scripts:** correct daemon frontend.pid path ([#384](https://github.com/Vigil-SOC/vigil/issues/384)) ([d273d23](https://github.com/Vigil-SOC/vigil/commit/d273d23dc58f01157d05e8531a30bdce04219dd9))

## [0.3.0](https://github.com/Vigil-SOC/vigil/compare/v0.2.3...v0.3.0) (2026-06-26)


### Features

* **chat:** persistent cross-device conversation history ([#368](https://github.com/Vigil-SOC/vigil/issues/368)) ([ebd7498](https://github.com/Vigil-SOC/vigil/commit/ebd74988f2d8afbc491550e7b388bb734ed48f20))
* **onboarding:** first-access setup gate + LLM provider wizard ([#350](https://github.com/Vigil-SOC/vigil/issues/350)) ([9127d9e](https://github.com/Vigil-SOC/vigil/commit/9127d9e06561ce2e0b2f67bd95b69009ec16adfd))
* **redesign:** SOC console UI preview ([#352](https://github.com/Vigil-SOC/vigil/issues/352)) ([60d883c](https://github.com/Vigil-SOC/vigil/commit/60d883c4f3428d2aa7e378d284a4be89a44adadd))


### Bug Fixes

* char db connection string correct ([#321](https://github.com/Vigil-SOC/vigil/issues/321)) ([69ef3da](https://github.com/Vigil-SOC/vigil/commit/69ef3dadcad27e3ba01696f66b2b07fdf14cf0a7))
* **chart:** add startupProbes for slow first boot ([#364](https://github.com/Vigil-SOC/vigil/issues/364)) ([3b76b87](https://github.com/Vigil-SOC/vigil/commit/3b76b87c7942609f6587ff288f299b170b76c8c4))
* **chat:** render Markdown in chat drawer + UX polish ([#346](https://github.com/Vigil-SOC/vigil/issues/346)) ([0bdd0ca](https://github.com/Vigil-SOC/vigil/commit/0bdd0ca11058f25962bda3b913f1044f130bf6e2))
* **chat:** scope streaming responses to the originating tab ([#347](https://github.com/Vigil-SOC/vigil/issues/347)) ([bc10be6](https://github.com/Vigil-SOC/vigil/commit/bc10be634d5bd8b2c49f1cfcde872c2494e7436a))
* **ci:** make integration tests gate ([#358](https://github.com/Vigil-SOC/vigil/issues/358)) ([c66ab17](https://github.com/Vigil-SOC/vigil/commit/c66ab1712b04fc76a745fd0dd495bdae261dc9f3))
* **ci:** make unit-test job gate again (drop `|| true`) ([#356](https://github.com/Vigil-SOC/vigil/issues/356)) ([6369e04](https://github.com/Vigil-SOC/vigil/commit/6369e04a8ce19e31bb13e7dd3584f0d61417a81c))
* **db:** URL-encode Postgres credentials in connection string ([#343](https://github.com/Vigil-SOC/vigil/issues/343)) ([146d4a4](https://github.com/Vigil-SOC/vigil/commit/146d4a418dff40985833af31103423f657d72211))
* **docker:** include daemon/ in backend image ([#354](https://github.com/Vigil-SOC/vigil/issues/354)) ([af6e197](https://github.com/Vigil-SOC/vigil/commit/af6e197f41f707fe49be9610f8d37aafe0d00a40))
* **docker:** restore submodule install and correct path in release images ([#335](https://github.com/Vigil-SOC/vigil/issues/335)) ([53d0cfe](https://github.com/Vigil-SOC/vigil/commit/53d0cfe02bd324bcae155c446e299212e4b15974))
* **frontend:** clear npm deprecation warnings and audit vulns ([#371](https://github.com/Vigil-SOC/vigil/issues/371)) ([89db6ca](https://github.com/Vigil-SOC/vigil/commit/89db6ca7aff440fb5b7f1aab045a1c3c1985ee8e))
* **helm:** expose POSTGRES_* env vars to pods ([#338](https://github.com/Vigil-SOC/vigil/issues/338)) ([257aa1a](https://github.com/Vigil-SOC/vigil/commit/257aa1a5853f97237fae71607fec142ddffd79fd))
* **llm-providers:** reconcile shared Bifrost key on provider delete/clear ([#360](https://github.com/Vigil-SOC/vigil/issues/360)) ([cffb4c6](https://github.com/Vigil-SOC/vigil/commit/cffb4c69cd8eae42b63e7c6b68c750347371a65e))
* **llm:** route local Ollama providers through Bifrost ([#348](https://github.com/Vigil-SOC/vigil/issues/348)) ([360da29](https://github.com/Vigil-SOC/vigil/commit/360da29383e53704d75cbbc40fc3006f940b0cee))
* **onboarding:** set-default 500, reset-setup full clear, stale dismissed redirect ([#367](https://github.com/Vigil-SOC/vigil/issues/367)) ([6732811](https://github.com/Vigil-SOC/vigil/commit/6732811b21ffe1a0286c13151288f4ed5778414f))
* provider delete failures, single-default enforcement, cascade cleanup ([#336](https://github.com/Vigil-SOC/vigil/issues/336)) ([b7d1f3e](https://github.com/Vigil-SOC/vigil/commit/b7d1f3ea562bf2942074c091a290357d1c210801))
* **settings:** recover redesign provider-delete UX and repair reset-setup ([#366](https://github.com/Vigil-SOC/vigil/issues/366)) ([0aec85a](https://github.com/Vigil-SOC/vigil/commit/0aec85adedabdd555be3dc40ca0b238ad55f5c91))

## [0.2.3](https://github.com/Vigil-SOC/vigil/compare/v0.2.2...v0.2.3) (2026-06-09)


### Bug Fixes

* **backend:** auth basics secure ([#318](https://github.com/Vigil-SOC/vigil/issues/318)) ([2a771a0](https://github.com/Vigil-SOC/vigil/commit/2a771a07a6d6e19c3c4f7b1c5f6be37806883e26))
* **daemon:** Unify Start Scripts ([#319](https://github.com/Vigil-SOC/vigil/issues/319)) ([7f4cecc](https://github.com/Vigil-SOC/vigil/commit/7f4ceccd712104d06485e4ce50e87331551c25cd))
* **frontend:** V0.2.0 commit with changes ([#316](https://github.com/Vigil-SOC/vigil/issues/316)) ([6ec5af2](https://github.com/Vigil-SOC/vigil/commit/6ec5af2489e8ee9337ae2d891abd063f1d0d3e7b))
* release files for gh ([#320](https://github.com/Vigil-SOC/vigil/issues/320)) ([976de4f](https://github.com/Vigil-SOC/vigil/commit/976de4f1a483debc476c74bb2f68d5d3f42445b4))

## [0.2.2](https://github.com/Vigil-SOC/vigil/compare/v0.2.1...v0.2.2) (2026-06-02)


### Bug Fixes

* **api:** move unauthenticated VStrike routes to authenticated_router ([#312](https://github.com/Vigil-SOC/vigil/issues/312)) ([aa78dd1](https://github.com/Vigil-SOC/vigil/commit/aa78dd149c5d398a3a6c02072cacc800a911cf54)), closes [#286](https://github.com/Vigil-SOC/vigil/issues/286)

## [0.2.1](https://github.com/Vigil-SOC/vigil/compare/v0.2.0...v0.2.1) (2026-05-29)


### Bug Fixes

* **docker:** copy mempalace into image before pip install ([#310](https://github.com/Vigil-SOC/vigil/issues/310)) ([a97c8fe](https://github.com/Vigil-SOC/vigil/commit/a97c8fe6fea8d962ba07d2edd5c8e27de9670416))

## [0.2.0](https://github.com/Vigil-SOC/vigil/compare/v0.1.2...v0.2.0) (2026-05-29)


### Bug Fixes

* **release:** check out submodules during image build ([#303](https://github.com/Vigil-SOC/vigil/issues/303)) ([e7af525](https://github.com/Vigil-SOC/vigil/commit/e7af52573dfbc3231f0d1ddc318640576788fc55))


### Miscellaneous Chores

* release 0.2.0 ([#309](https://github.com/Vigil-SOC/vigil/issues/309)) ([854e5cc](https://github.com/Vigil-SOC/vigil/commit/854e5cc7dc89900e76d9c08ae2e1a146a40b9457))

## [0.1.2](https://github.com/Vigil-SOC/vigil/compare/v0.1.0...v0.1.2) (2026-05-28)


### Features

* v0.2.0 — VStrike UI tools, chat rebrand, model registry hardening ([#301](https://github.com/Vigil-SOC/vigil/issues/301)) ([14c09aa](https://github.com/Vigil-SOC/vigil/commit/14c09aa3a6b22afb3dee17af5764ca2339037e99))


### Bug Fixes

* chat drawer works when Anthropic is configured only via the UI ([#292](https://github.com/Vigil-SOC/vigil/issues/292)) ([#293](https://github.com/Vigil-SOC/vigil/issues/293)) ([4099d89](https://github.com/Vigil-SOC/vigil/commit/4099d89466b9e8a6202a01d4ad90b89c232fb2c7))
* **release-please:** drop component prefix from tag names ([#296](https://github.com/Vigil-SOC/vigil/issues/296)) ([6f1d843](https://github.com/Vigil-SOC/vigil/commit/6f1d843008053c47b658a48ec14d8851b4acbd2e))
* replace deprecated Query(regex=) with Query(pattern=) in analytics.py ([#290](https://github.com/Vigil-SOC/vigil/issues/290)) ([945064f](https://github.com/Vigil-SOC/vigil/commit/945064fe93737cbc1440053a02611227434e333a))
* **scripts:** add auto_responder to create_workflow.py AVAILABLE_AGENTS ([#284](https://github.com/Vigil-SOC/vigil/issues/284)) ([3d4a798](https://github.com/Vigil-SOC/vigil/commit/3d4a798cee8548172e57f8d2f26847d87a819af6))
* unblock first automated release (chart, helm bundle, image tags) ([#294](https://github.com/Vigil-SOC/vigil/issues/294)) ([7952a6d](https://github.com/Vigil-SOC/vigil/commit/7952a6db809b893feb6a187be6a521aacd1677ea))


### Miscellaneous Chores

* release 0.1.2 ([#299](https://github.com/Vigil-SOC/vigil/issues/299)) ([50828c6](https://github.com/Vigil-SOC/vigil/commit/50828c67b747f8ce140ed4713b5910b49fb31169))
