# Changelog

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
