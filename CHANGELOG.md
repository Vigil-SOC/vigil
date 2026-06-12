# Changelog

## [0.2.4](https://github.com/Vigil-SOC/vigil/compare/v0.2.3...v0.2.4) (2026-06-12)


### Bug Fixes

* char db connection string correct ([#321](https://github.com/Vigil-SOC/vigil/issues/321)) ([69ef3da](https://github.com/Vigil-SOC/vigil/commit/69ef3dadcad27e3ba01696f66b2b07fdf14cf0a7))
* **db:** URL-encode Postgres credentials in connection string ([#343](https://github.com/Vigil-SOC/vigil/issues/343)) ([146d4a4](https://github.com/Vigil-SOC/vigil/commit/146d4a418dff40985833af31103423f657d72211))
* **docker:** restore submodule install and correct path in release images ([#335](https://github.com/Vigil-SOC/vigil/issues/335)) ([53d0cfe](https://github.com/Vigil-SOC/vigil/commit/53d0cfe02bd324bcae155c446e299212e4b15974))
* **helm:** expose POSTGRES_* env vars to pods ([#338](https://github.com/Vigil-SOC/vigil/issues/338)) ([257aa1a](https://github.com/Vigil-SOC/vigil/commit/257aa1a5853f97237fae71607fec142ddffd79fd))

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
