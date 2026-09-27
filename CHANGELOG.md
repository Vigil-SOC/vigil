# Changelog

## [0.6.0](https://github.com/Vigil-SOC/vigil/compare/v0.5.0...v0.6.0) (2026-09-24)


### Features

* **agent:** GET /runs/:id/replay rebuilds what the hunt lead was shown ([#940](https://github.com/Vigil-SOC/vigil/issues/940)) ([e6c133f](https://github.com/Vigil-SOC/vigil/commit/e6c133fe0fec9d38fdf1ca78dcab873e22a97dbf))
* **agent:** register `adjudicate`, a hunt-like run kind for shadow adjudication ([#958](https://github.com/Vigil-SOC/vigil/issues/958)) ([6d4c959](https://github.com/Vigil-SOC/vigil/commit/6d4c9591a507a6d729c74554b31adfbb679ea750))
* **agents:** check_hunt_coverage tool — report in, three answers out, no hunt started ([#956](https://github.com/Vigil-SOC/vigil/issues/956)) ([5876035](https://github.com/Vigil-SOC/vigil/commit/5876035908918c733c772329b2b7ae79bdbe87ec))
* **agents:** grant the ART execute tool to the MITRE Analyst, harness-gated ([#877](https://github.com/Vigil-SOC/vigil/issues/877)) ([bbe8bb3](https://github.com/Vigil-SOC/vigil/commit/bbe8bb37a59fc16eb744dc2c794cfc1b515f3f97))
* **api:** list the intake queue and count its depth ([#919](https://github.com/Vigil-SOC/vigil/issues/919)) ([#982](https://github.com/Vigil-SOC/vigil/issues/982)) ([1a1ad3d](https://github.com/Vigil-SOC/vigil/commit/1a1ad3d9196c8240f725635bfba96ae6f7b339c4))
* **api:** proxy hunt Replay to the console and the findings MCP ([#943](https://github.com/Vigil-SOC/vigil/issues/943)) ([bc5bbf9](https://github.com/Vigil-SOC/vigil/commit/bc5bbf9dc3619544f9d244b6cc4923572b439ce9))
* **api:** versioned /api/v1 contract surface (findings, cases, approvals, workflows, runs, metrics) ([#913](https://github.com/Vigil-SOC/vigil/issues/913)) ([100d48b](https://github.com/Vigil-SOC/vigil/commit/100d48b73232f6e14e5b49f4db8458631080054c))
* **console:** check report coverage from the Run modal ([#1089](https://github.com/Vigil-SOC/vigil/issues/1089)) ([f7ef6b7](https://github.com/Vigil-SOC/vigil/commit/f7ef6b7a951f74536755918ed9480490bf5b851f))
* **console:** open a hunt move and show the digest that decision saw ([#949](https://github.com/Vigil-SOC/vigil/issues/949)) ([0697358](https://github.com/Vigil-SOC/vigil/commit/0697358f64b4474afc1a60c6b0ade427bc1ae674))
* **console:** reach a hunt run, and its replay, from the case it was run for ([#954](https://github.com/Vigil-SOC/vigil/issues/954)) ([65c2398](https://github.com/Vigil-SOC/vigil/commit/65c2398ee13ca850a08fb05adde8b48a90f61af5))
* **console:** the console says NO AUTH while the bypass is on ([#1046](https://github.com/Vigil-SOC/vigil/issues/1046)) ([be751d1](https://github.com/Vigil-SOC/vigil/commit/be751d1cc9ed483f6f46dd78ea4c9989f02b2936))
* **daemon:** a threat-intel feed hit offers the finding to intake as a detection ([#1100](https://github.com/Vigil-SOC/vigil/issues/1100)) ([fd5b85f](https://github.com/Vigil-SOC/vigil/commit/fd5b85f11763f8222a86bcdaf6cb3fd47b541d28))
* **daemon:** admission ranks by severity then age, expires on a TTL, and waits for a slot ([#922](https://github.com/Vigil-SOC/vigil/issues/922)) ([#973](https://github.com/Vigil-SOC/vigil/issues/973)) ([949abac](https://github.com/Vigil-SOC/vigil/commit/949abaccf54e5d74799e9acb6430c57e630fc21c))
* **daemon:** an admitted finding opens its case at admission ([#952](https://github.com/Vigil-SOC/vigil/issues/952)) ([c51bfa6](https://github.com/Vigil-SOC/vigil/commit/c51bfa6658fe5bc90c42e00ed4f161f65d4a0e46))
* **daemon:** mint the case in the launch transaction so a finding-run never starts without one ([#1000](https://github.com/Vigil-SOC/vigil/issues/1000)) ([#1034](https://github.com/Vigil-SOC/vigil/issues/1034)) ([8113656](https://github.com/Vigil-SOC/vigil/commit/81136568b61058f8ecda656a071aa58c9f6e5c5a))
* **daemon:** score each probe's triage against its known answer and export it ([#1055](https://github.com/Vigil-SOC/vigil/issues/1055)) ([b960323](https://github.com/Vigil-SOC/vigil/commit/b960323277b9673b70959e810749dcda630ef557))
* **daemon:** shadow adjudication run beside every admitted finding behind ORCHESTRATOR_SHADOW_ADJUDICATION ([#1071](https://github.com/Vigil-SOC/vigil/issues/1071)) ([ed83292](https://github.com/Vigil-SOC/vigil/commit/ed83292aca777c03d3e657dc038398844658976f))
* **daemon:** the intake queue is a table ([#918](https://github.com/Vigil-SOC/vigil/issues/918)) ([#971](https://github.com/Vigil-SOC/vigil/issues/971)) ([ef29d54](https://github.com/Vigil-SOC/vigil/commit/ef29d5443a42af6e3463a5d6cc42d9dabe409ed1))
* **daemon:** three known-answer probes flow through triage once a day and stop there ([#1027](https://github.com/Vigil-SOC/vigil/issues/1027)) ([00893b6](https://github.com/Vigil-SOC/vigil/commit/00893b6857d0313e249a631758f07c7a7993ab52))
* **detections:** coverage report from a red run on the ATT&CK tab ([#856](https://github.com/Vigil-SOC/vigil/issues/856)) ([0014d3e](https://github.com/Vigil-SOC/vigil/commit/0014d3e0cc3886efdc306eb72145759c86a01081))
* **detections:** lint that rejects detections keyed to a specific IP, host, user or subnet ([#840](https://github.com/Vigil-SOC/vigil/issues/840)) ([dacbaab](https://github.com/Vigil-SOC/vigil/commit/dacbaabb9c3891673745d39c868fa166d766e1cb))
* **detections:** reconstruct a red run into per-step detection verdicts ([#851](https://github.com/Vigil-SOC/vigil/issues/851)) ([71d9d8d](https://github.com/Vigil-SOC/vigil/commit/71d9d8d25bfe9dd26355415010c0f8591750bb19))
* **findings:** analyst IP exclusions hide known addresses from the findings queue ([#1141](https://github.com/Vigil-SOC/vigil/issues/1141)) ([ffa8d32](https://github.com/Vigil-SOC/vigil/commit/ffa8d32dd5edca1f2f598b44538910b6675c73f8))
* **frontend:** Health screen for spend, approvals and run outcomes; remove CostAnalyticsCard ([#1012](https://github.com/Vigil-SOC/vigil/issues/1012)) ([0232833](https://github.com/Vigil-SOC/vigil/commit/0232833c700aa565d8b8357d477e79d2dc379c00))
* **hunt:** make a threat hunt runnable from the console, and worth reading ([#700](https://github.com/Vigil-SOC/vigil/issues/700)) ([1f90b97](https://github.com/Vigil-SOC/vigil/commit/1f90b9744467f70286ebdd556906880a49c2e313))
* **intake:** intel and human-ask producers for the intake queue ([#909](https://github.com/Vigil-SOC/vigil/issues/909)) ([#1103](https://github.com/Vigil-SOC/vigil/issues/1103)) ([2694dc8](https://github.com/Vigil-SOC/vigil/commit/2694dc8d6e7d53945ce721b5c079f14758def9ba))
* **integrations:** Atomic Red Team vendor slice (Closes [#833](https://github.com/Vigil-SOC/vigil/issues/833)) ([#855](https://github.com/Vigil-SOC/vigil/issues/855)) ([6cc3464](https://github.com/Vigil-SOC/vigil/commit/6cc34646470c91156c5d11a95f213f82c16b2868))
* **ledger:** application role may only INSERT and SELECT on agent_events ([#847](https://github.com/Vigil-SOC/vigil/issues/847)) ([6da9d6a](https://github.com/Vigil-SOC/vigil/commit/6da9d6a7e855e485682fa10a60ffef0be3e32171))
* **ledger:** hash-chain agent_events on INSERT; ledger verify ([#850](https://github.com/Vigil-SOC/vigil/issues/850)) ([b25ffef](https://github.com/Vigil-SOC/vigil/commit/b25ffefef48b3507f653135cb624f8ce66def675))
* **mcp:** connect enabled servers concurrently at API boot ([#785](https://github.com/Vigil-SOC/vigil/issues/785)) ([3791578](https://github.com/Vigil-SOC/vigil/commit/3791578fefc4391a843e431b2a469c34d58d23ff))
* **mcp:** default-enable splunk-selfhosted ([#806](https://github.com/Vigil-SOC/vigil/issues/806)) ([15661ef](https://github.com/Vigil-SOC/vigil/commit/15661ef8d1c173063d554e9b00376015c7f2fd9c))
* **memory:** distil cited techniques onto Verdicts so prior hunts are queryable by T-ID ([#938](https://github.com/Vigil-SOC/vigil/issues/938)) ([90d13ea](https://github.com/Vigil-SOC/vigil/commit/90d13ea94e58914dbbaadaa653af8fb454ab17b4))
* **memory:** episodic memory — what runs saw and concluded ([#727](https://github.com/Vigil-SOC/vigil/issues/727)) ([#815](https://github.com/Vigil-SOC/vigil/issues/815)) ([24e530f](https://github.com/Vigil-SOC/vigil/commit/24e530f57bc1036f7fc65f5fea01f14e31f9da06))
* **memory:** list and export learning episodes as a fold over Distil ([#906](https://github.com/Vigil-SOC/vigil/issues/906)) ([#1016](https://github.com/Vigil-SOC/vigil/issues/1016)) ([fcde6f2](https://github.com/Vigil-SOC/vigil/commit/fcde6f24e66c030ae59af030d93b11faa78e3ee3))
* **memory:** list prior hunts by entity key and technique, in-flight via workflow_runs ([#953](https://github.com/Vigil-SOC/vigil/issues/953)) ([092faf3](https://github.com/Vigil-SOC/vigil/commit/092faf3f781546d92fe0d79284c65b7941b3440c))
* **memory:** retire MemPalace, give an investigation its Recall ([#844](https://github.com/Vigil-SOC/vigil/issues/844)) ([3357c0d](https://github.com/Vigil-SOC/vigil/commit/3357c0d3f6a05530c893f835611fed503bd3dfaa))
* **response:** reversibility and idempotency_key on ApprovalAction ([#848](https://github.com/Vigil-SOC/vigil/issues/848)) ([d9cdcb4](https://github.com/Vigil-SOC/vigil/commit/d9cdcb4a321fd013422d07db2b07fcbc86f78125))
* **skills:** add phishing-triage for a reported email's verdict and first action ([#1158](https://github.com/Vigil-SOC/vigil/issues/1158)) ([feae345](https://github.com/Vigil-SOC/vigil/commit/feae3455d987c76173fe28b8955a7ff10b5dd051))
* **skills:** grade periodic outbound traffic with beaconing-review ([#1155](https://github.com/Vigil-SOC/vigil/issues/1155)) ([9bdc842](https://github.com/Vigil-SOC/vigil/commit/9bdc84291872e546a76d549e7681fa467724bcb9))
* **skills:** ground ATT&CK technique ids in a finding's observables ([#1161](https://github.com/Vigil-SOC/vigil/issues/1161)) ([73fe752](https://github.com/Vigil-SOC/vigil/commit/73fe752216f5385f072b25ba45a2b6374d949a26))
* **skills:** judge an internal RDP session as movement or administration ([#1159](https://github.com/Vigil-SOC/vigil/issues/1159)) ([96fe466](https://github.com/Vigil-SOC/vigil/commit/96fe466b5336799f33422e683ce4e4bce96dfc0d))
* **skills:** load spec-conformant SKILL.md directories and let agents read them ([#1056](https://github.com/Vigil-SOC/vigil/issues/1056)) ([b0b4a95](https://github.com/Vigil-SOC/vigil/commit/b0b4a95fb435e03560b5562f58bb1afe85e62855))
* **skills:** port the reporter's report methodology and board-brief template into an executive-summary skill ([#1067](https://github.com/Vigil-SOC/vigil/issues/1067)) ([08ccf9b](https://github.com/Vigil-SOC/vigil/commit/08ccf9b11599bd95fc5f799f5db12c899076742f))
* **skills:** port the threat-intel enrichment methodology into an `ioc-enrichment` skill ([#1070](https://github.com/Vigil-SOC/vigil/issues/1070)) ([9bd9b04](https://github.com/Vigil-SOC/vigil/commit/9bd9b041970adaea7ab0062338dd9b0093f4c439))
* **threat-intel:** hunt proposals from recent feed indicators, still not hunts ([#905](https://github.com/Vigil-SOC/vigil/issues/905)) ([#977](https://github.com/Vigil-SOC/vigil/issues/977)) ([c07c1a3](https://github.com/Vigil-SOC/vigil/commit/c07c1a3d5eb65051ed63ae8164abcb53d7bff44d))
* **threat-intel:** parse a STIX bundle or plain-text report into entity keys and ATT&CK ids ([#944](https://github.com/Vigil-SOC/vigil/issues/944)) ([1a448dc](https://github.com/Vigil-SOC/vigil/commit/1a448dcd75bf81e23fc9368c6fdd69c1d6b267df))
* **web:** export and remember findings views ([#757](https://github.com/Vigil-SOC/vigil/issues/757)) ([9704e44](https://github.com/Vigil-SOC/vigil/commit/9704e44062906baaf88a0b6763b90001790fae77))
* **web:** Health screen shows each probe's latest verdict score ([#1129](https://github.com/Vigil-SOC/vigil/issues/1129)) ([4a28a9d](https://github.com/Vigil-SOC/vigil/commit/4a28a9d13c2f8849bc99c709d4555902aa90196a))
* **web:** Skills tab becomes a read-only list; remove Skill Builder, zip import, toggle and delete ([#937](https://github.com/Vigil-SOC/vigil/issues/937)) ([d2d2863](https://github.com/Vigil-SOC/vigil/commit/d2d2863f07491d884c543e5ead901610c25007fb))
* **workflows:** root-cause-analysis run, auto-fired on handoff for approval ([#807](https://github.com/Vigil-SOC/vigil/issues/807)) ([c1d6e15](https://github.com/Vigil-SOC/vigil/commit/c1d6e15da2122bc8cde7fc371532b9c9a8d58398))


### Bug Fixes

* agent-layer pidfiles stop the listeners; GET /api/agent-runs reports queued runs ([#1079](https://github.com/Vigil-SOC/vigil/issues/1079)) ([71336c6](https://github.com/Vigil-SOC/vigil/commit/71336c645fd8499dfeed7ac43584f10653533f9b))
* **agent-runs:** declare case_records so investigate runs can start ([#878](https://github.com/Vigil-SOC/vigil/issues/878)) ([23a7b27](https://github.com/Vigil-SOC/vigil/commit/23a7b27f506a90dd14984b57be4d2462c024d7c7))
* **agents:** a budget stop is not a visibility gap ([#859](https://github.com/Vigil-SOC/vigil/issues/859)) ([2dbe013](https://github.com/Vigil-SOC/vigil/commit/2dbe01391ba6b4e5fa3d02a729da89346b1a748a))
* **agents:** fold hunt.cost_usd from spend events, not a decision-time patch ([#1019](https://github.com/Vigil-SOC/vigil/issues/1019)) ([f485e9d](https://github.com/Vigil-SOC/vigil/commit/f485e9dd56f0a7a9614f8e5b7605250748f9e96f))
* **api:** a case with a live investigation cannot be deleted; bulk reset kills them first ([#1001](https://github.com/Vigil-SOC/vigil/issues/1001)) ([#1036](https://github.com/Vigil-SOC/vigil/issues/1036)) ([420a101](https://github.com/Vigil-SOC/vigil/commit/420a101f64d320e91e4f9705151b7a7701c78c78))
* **api:** AutoOps queued count reads the intake table ([#988](https://github.com/Vigil-SOC/vigil/issues/988)) ([86399d1](https://github.com/Vigil-SOC/vigil/commit/86399d1e70df4e11a24edb1e94706260dd89402c))
* **api:** declare the watcher notification vocabulary ([#558](https://github.com/Vigil-SOC/vigil/issues/558)) ([76c55b8](https://github.com/Vigil-SOC/vigil/commit/76c55b8be1cbf4ef92cc6ca2e1e0700510da8634))
* **api:** deleting an SLA policy answers, and stops offering an escape that never worked ([#1050](https://github.com/Vigil-SOC/vigil/issues/1050)) ([6d49fc9](https://github.com/Vigil-SOC/vigil/commit/6d49fc9f1fc960bcaa46e4e047d5f18907297cc8))
* **api:** scan-findings inserts detection rows so a scan merges and dedups ([#999](https://github.com/Vigil-SOC/vigil/issues/999)) ([#1033](https://github.com/Vigil-SOC/vigil/issues/1033)) ([cd52d0f](https://github.com/Vigil-SOC/vigil/commit/cd52d0f350a0c1b55eee00584ba3a0918d05f66c))
* **api:** the findings export writes one file, in a format it can write ([#1048](https://github.com/Vigil-SOC/vigil/issues/1048)) ([a6841e0](https://github.com/Vigil-SOC/vigil/commit/a6841e0985d9ee4e93a676e789344a3491ff1dd9))
* **bifrost:** env-placeholder guard reads Bifrost's real credential shape ([#1101](https://github.com/Vigil-SOC/vigil/issues/1101)) ([9fd150e](https://github.com/Vigil-SOC/vigil/commit/9fd150eed20c33a279d0344132f60e7d81cb3e4f))
* **bifrost:** floor a mirrored ollama row to a chat-capable model, not an embedding one ([#1088](https://github.com/Vigil-SOC/vigil/issues/1088)) ([d24ee19](https://github.com/Vigil-SOC/vigil/commit/d24ee1919ad1128e55748c722acde7a503d5185d))
* **cases:** append notes as JSONB list entries ([#745](https://github.com/Vigil-SOC/vigil/issues/745)) ([9479d62](https://github.com/Vigil-SOC/vigil/commit/9479d6298d67a230d4c9f78509655d0134b5a6d0))
* **ci:** drop the stale submodule gitlinks ([#749](https://github.com/Vigil-SOC/vigil/issues/749)) ([dd2cbf5](https://github.com/Vigil-SOC/vigil/commit/dd2cbf5c4361e69018326c10d35b4f7553502860))
* **ci:** provision nightly full-test-suite like the PR integration job ([#782](https://github.com/Vigil-SOC/vigil/issues/782)) ([730d0c7](https://github.com/Vigil-SOC/vigil/commit/730d0c7cde4443f9891167ae8727f04761725bfa))
* **ci:** start release images instead of importing their packages ([#777](https://github.com/Vigil-SOC/vigil/issues/777)) ([fba73df](https://github.com/Vigil-SOC/vigil/commit/fba73dfaa1ec0f56d0b2e2a2d4a9002d2a0d8e79))
* **compose:** bind Postgres and Redis to loopback ([#708](https://github.com/Vigil-SOC/vigil/issues/708)) ([776e6d5](https://github.com/Vigil-SOC/vigil/commit/776e6d54eaa09380d15ced15531b6b5ed04f7f66))
* **compose:** carry the run's cost on the terminal so History stops reporting $0.00 ([#1138](https://github.com/Vigil-SOC/vigil/issues/1138)) ([27045ef](https://github.com/Vigil-SOC/vigil/commit/27045ef67d6961065a2ac9ef2056aba62711cfaf))
* **compose:** pass AGENT_URL and AGENT_INTERNAL_TOKEN to soc-daemon ([#871](https://github.com/Vigil-SOC/vigil/issues/871)) ([0acdbf0](https://github.com/Vigil-SOC/vigil/commit/0acdbf02171333b3ed22d8e608c6170d564fa2fd))
* **compose:** re-apply init SQL after create_all on docker compose up ([#778](https://github.com/Vigil-SOC/vigil/issues/778)) ([660079b](https://github.com/Vigil-SOC/vigil/commit/660079bfbdbb622dadda6405b27e2d91dd179e73))
* **compose:** stop shipping pgAdmin as an unauthenticated console ([#710](https://github.com/Vigil-SOC/vigil/issues/710)) ([d6a54c2](https://github.com/Vigil-SOC/vigil/commit/d6a54c25a31428dc385effa661e2d096d8aac3bf)), closes [#707](https://github.com/Vigil-SOC/vigil/issues/707)
* **config:** report invalid startup settings cleanly ([#685](https://github.com/Vigil-SOC/vigil/issues/685)) ([f7a915b](https://github.com/Vigil-SOC/vigil/commit/f7a915b61eba547dcadf63fbb15935c303209981))
* **config:** validate Settings at llm-worker startup ([#720](https://github.com/Vigil-SOC/vigil/issues/720)) ([594d2bf](https://github.com/Vigil-SOC/vigil/commit/594d2bf7aff09eb96e181316a1156757f1b1964d))
* **connectors:** package the MCP catalog and expose self-hosted Splunk ([#1024](https://github.com/Vigil-SOC/vigil/issues/1024)) ([ad0ea75](https://github.com/Vigil-SOC/vigil/commit/ad0ea756b4d64625877ced342464e5402271a6a6))
* **console:** one rule for rendering a cost, so an unpriced model stops reading as $0.00 ([#1082](https://github.com/Vigil-SOC/vigil/issues/1082)) ([926ff32](https://github.com/Vigil-SOC/vigil/commit/926ff32fe2d0dfbbed3e615680ccff275aa132e8))
* **cost:** price by the configured provider, never by the model's name ([#1083](https://github.com/Vigil-SOC/vigil/issues/1083)) ([9212fb0](https://github.com/Vigil-SOC/vigil/commit/9212fb0443b813b6dc83ccac2de94ccf90838f45))
* **daemon:** a failed attach leaves the trigger queued instead of writing merged ([#997](https://github.com/Vigil-SOC/vigil/issues/997)) ([#1022](https://github.com/Vigil-SOC/vigil/issues/1022)) ([7f26ce6](https://github.com/Vigil-SOC/vigil/commit/7f26ce6ad0025801e9d255bf6f0e86e39d4f5b46))
* **daemon:** a finding that overlaps open work attaches to its case instead of being discarded ([#957](https://github.com/Vigil-SOC/vigil/issues/957)) ([05ba123](https://github.com/Vigil-SOC/vigil/commit/05ba123ac0817468afeed63bf57b287101434091))
* **daemon:** an unrated finding launches as unknown, not medium ([#998](https://github.com/Vigil-SOC/vigil/issues/998)) ([#1023](https://github.com/Vigil-SOC/vigil/issues/1023)) ([5f0cc8b](https://github.com/Vigil-SOC/vigil/commit/5f0cc8ba7a6a7098e54d244b4937afda78cc5f75))
* **daemon:** approve review_submitted investigations on terminal outcome ([#1160](https://github.com/Vigil-SOC/vigil/issues/1160)) ([019b435](https://github.com/Vigil-SOC/vigil/commit/019b435bf4911a3f5fe4a9a9532a1fbf4390a7b9))
* **daemon:** enqueue the run_kind the workflow definition declares ([#936](https://github.com/Vigil-SOC/vigil/issues/936)) ([98574ad](https://github.com/Vigil-SOC/vigil/commit/98574ada65e3868aa3c4266e4494d0748d3069b1)), closes [#933](https://github.com/Vigil-SOC/vigil/issues/933)
* **daemon:** expose the hourly intake pause on /status and AutoOps, and cover the ceiling's remaining cases ([#1121](https://github.com/Vigil-SOC/vigil/issues/1121)) ([4913216](https://github.com/Vigil-SOC/vigil/commit/491321670b97d1573564709a0b49e448f6508fe8))
* **daemon:** make the hourly cost kill-switch real by deriving its window from investigation rows ([#1108](https://github.com/Vigil-SOC/vigil/issues/1108)) ([6b149d1](https://github.com/Vigil-SOC/vigil/commit/6b149d18b8f6f8346e36f28d62ed68166f67360e))
* **daemon:** merged_into is always a case; overlap with a caseless run is not a merge ([#1002](https://github.com/Vigil-SOC/vigil/issues/1002)) ([#1035](https://github.com/Vigil-SOC/vigil/issues/1035)) ([63449b0](https://github.com/Vigil-SOC/vigil/commit/63449b0ceb273192d4b0fd0522c243f090dc90ef))
* **daemon:** normalise last_activity_at before the supervisor stale check ([#976](https://github.com/Vigil-SOC/vigil/issues/976)) ([0ea1ddf](https://github.com/Vigil-SOC/vigil/commit/0ea1ddf0496df93b2afff77405530887a937be74))
* **daemon:** resolve permission denied on .vigil in containerized environments ([#701](https://github.com/Vigil-SOC/vigil/issues/701)) ([003339d](https://github.com/Vigil-SOC/vigil/commit/003339d236020df46e7de1f5db340c80416d910e))
* **daemon:** stop gating the LLM worker on orchestrator.settings.enabled ([#722](https://github.com/Vigil-SOC/vigil/issues/722)) ([4de1b70](https://github.com/Vigil-SOC/vigil/commit/4de1b70f9a74262172611357245f638fa5c7e649))
* **daemon:** tell the agent to mint a case when admission left none ([#962](https://github.com/Vigil-SOC/vigil/issues/962)) ([8ef7a67](https://github.com/Vigil-SOC/vigil/commit/8ef7a67ceb3b14cbd74104b3d0659c6d4c4fdbcb))
* **daemon:** triage resolves a provider like chat does and records failures on the finding ([#965](https://github.com/Vigil-SOC/vigil/issues/965)) ([#981](https://github.com/Vigil-SOC/vigil/issues/981)) ([f41954f](https://github.com/Vigil-SOC/vigil/commit/f41954f87bf97d78d5870873c63953693e47d584))
* **dashboard:** findings without a severity or timestamp no longer break the summary and timeline ([#1140](https://github.com/Vigil-SOC/vigil/issues/1140)) ([820d516](https://github.com/Vigil-SOC/vigil/commit/820d51656e20d523b6762ca07b3178322e98f749))
* **db:** report schema drift at startup instead of silently serving ([#569](https://github.com/Vigil-SOC/vigil/issues/569)) ([73c1e9a](https://github.com/Vigil-SOC/vigil/commit/73c1e9a2239b07d143ab944c987f5efc7b3aeae9)), closes [#562](https://github.com/Vigil-SOC/vigil/issues/562)
* **db:** store notification metadata on the column, not a shadow attr ([#563](https://github.com/Vigil-SOC/vigil/issues/563)) ([cfee9d6](https://github.com/Vigil-SOC/vigil/commit/cfee9d63bed9c8e9569a4ea3ce26bc57ce45e87f))
* **decisions:** land the AI Decisions badge on the tab it counted ([#750](https://github.com/Vigil-SOC/vigil/issues/750)) ([830228e](https://github.com/Vigil-SOC/vigil/commit/830228ef3f1825761e33912de544c84cda389246))
* **defender:** store title, entities, MITRE; stop injecting limit on point-reads ([#875](https://github.com/Vigil-SOC/vigil/issues/875)) ([d72f9aa](https://github.com/Vigil-SOC/vigil/commit/d72f9aaecdf4e3f69c85936fc87500551174ba7d))
* **desktop:** stage the bifrost config from infra/ ([#706](https://github.com/Vigil-SOC/vigil/issues/706)) ([dc2059a](https://github.com/Vigil-SOC/vigil/commit/dc2059a6c25cb75ad67026a14723a8cdf6c09d1b))
* **detections:** make the ART execute trace and reconstruction agree so a real run scores ([#946](https://github.com/Vigil-SOC/vigil/issues/946)) ([3d8b2f9](https://github.com/Vigil-SOC/vigil/commit/3d8b2f98d8a1773a35a1a186acdedc329d4e8c43))
* **ingest:** normalise mitre_predictions at the /ingest boundary ([#995](https://github.com/Vigil-SOC/vigil/issues/995)) ([2599c4a](https://github.com/Vigil-SOC/vigil/commit/2599c4ac9cf4315acbb3248faf7ec5c9aad8e95e))
* **llm:** drop embedding-only models from the per-provider picker list ([#1090](https://github.com/Vigil-SOC/vigil/issues/1090)) ([45c2da4](https://github.com/Vigil-SOC/vigil/commit/45c2da41447429786bf3c9b1160c0c90b0d04905))
* **llm:** floor mirrored Bifrost gemini rows to gemini-flash-latest ([#1125](https://github.com/Vigil-SOC/vigil/issues/1125)) ([0dd3679](https://github.com/Vigil-SOC/vigil/commit/0dd3679f2155288f027fcc0081c4aa28d7b20b5b))
* **llm:** honor Ollama thinking preference ([#758](https://github.com/Vigil-SOC/vigil/issues/758)) ([906d939](https://github.com/Vigil-SOC/vigil/commit/906d939280514c9f92bdf1f5d373ecc39e70ce51))
* **llm:** mint one interaction id for the Bifrost header and the persisted row ([#1081](https://github.com/Vigil-SOC/vigil/issues/1081)) ([6c4d752](https://github.com/Vigil-SOC/vigil/commit/6c4d7525c6b6c2107c5e552d8af91a0023882e2b))
* **llm:** push custom OpenAI-compatible base_url to Bifrost ([#783](https://github.com/Vigil-SOC/vigil/issues/783)) ([6eec74d](https://github.com/Vigil-SOC/vigil/commit/6eec74d4147ba369f71f6d25da7b5a7386abd162))
* **llm:** resolve_component uses resolve_model_for_component ([#872](https://github.com/Vigil-SOC/vigil/issues/872)) ([6dc3963](https://github.com/Vigil-SOC/vigil/commit/6dc3963091e66e9530d93dc74cc19c68c9596dd1))
* **mcp:** bind runtime-enabled MCP servers in hunt resolve ([#805](https://github.com/Vigil-SOC/vigil/issues/805)) ([c8b96bc](https://github.com/Vigil-SOC/vigil/commit/c8b96bcb33fd77d9e531da8029f8fdebdbc51ea3))
* **mcp:** keep a dropped session in the chat tool list ([#812](https://github.com/Vigil-SOC/vigil/issues/812)) ([8629ccf](https://github.com/Vigil-SOC/vigil/commit/8629ccf42988305b414765e5299d0ec319dfbcfb))
* **platform:** resolve permission denied on /.vigil in containerized environments ([#695](https://github.com/Vigil-SOC/vigil/issues/695)) ([#774](https://github.com/Vigil-SOC/vigil/issues/774)) ([235e619](https://github.com/Vigil-SOC/vigil/commit/235e6198ba992084ac815742a9af8dbb628f3925))
* **release:** bump clients/desktop with each release so the app pulls matching images ([#1119](https://github.com/Vigil-SOC/vigil/issues/1119)) ([930a95a](https://github.com/Vigil-SOC/vigil/commit/930a95ad87422020f922cd6376799af5c7f87555))
* **response:** expire approvals nobody will ever answer ([#743](https://github.com/Vigil-SOC/vigil/issues/743)) ([04a7327](https://github.com/Vigil-SOC/vigil/commit/04a732730dbd7a6a0242609ae341033d38d3eb9d)), closes [#675](https://github.com/Vigil-SOC/vigil/issues/675)
* **scripts:** bind the role permissions payload through an explicit cast ([#571](https://github.com/Vigil-SOC/vigil/issues/571)) ([4122be2](https://github.com/Vigil-SOC/vigil/commit/4122be2bd1757bb6481b8c1de6517e8c6ef2ecd2)), closes [#567](https://github.com/Vigil-SOC/vigil/issues/567)
* **splunk:** search all indexes and map all-time earliest_time ([#779](https://github.com/Vigil-SOC/vigil/issues/779)) ([bc222f0](https://github.com/Vigil-SOC/vigil/commit/bc222f0c01d200ac75d809f616b8f93e3fc7260c))
* **state:** one way to locate the state directory ([#704](https://github.com/Vigil-SOC/vigil/issues/704)) ([950c1d3](https://github.com/Vigil-SOC/vigil/commit/950c1d31fcc0839e734beef5cf1965cfa4e492f7))
* stop documenting DATABASE_URL as Python's DB source of truth ([#764](https://github.com/Vigil-SOC/vigil/issues/764)) ([d3d228a](https://github.com/Vigil-SOC/vigil/commit/d3d228a9b3861088c5b3c8b0e610e2dd27bbc8b9))
* **storage:** an ARRAY(Integer) column is described as a list of ints ([#1047](https://github.com/Vigil-SOC/vigil/issues/1047)) ([ff0b430](https://github.com/Vigil-SOC/vigil/commit/ff0b430bfc8f4a31c37e61a5c764a7bbc3ea6152))
* **storage:** correct investigations.trigger_ids to List[str] ([#712](https://github.com/Vigil-SOC/vigil/issues/712)) ([f509a0a](https://github.com/Vigil-SOC/vigil/commit/f509a0a9327a546310607634c2b629aa7f0b1b3c)), closes [#554](https://github.com/Vigil-SOC/vigil/issues/554)
* **storage:** persist in-place JSONB list appends ([#709](https://github.com/Vigil-SOC/vigil/issues/709)) ([f61607d](https://github.com/Vigil-SOC/vigil/commit/f61607dfec853d248729aacff5b34050b1a68fd5))
* **timeline:** tolerate findings with no timestamp ([#1139](https://github.com/Vigil-SOC/vigil/issues/1139)) ([b7a99af](https://github.com/Vigil-SOC/vigil/commit/b7a99af6869721ecd99fdd7ad6161607e50de09d))
* **ui:** read the watcher timestamp from created_at ([#564](https://github.com/Vigil-SOC/vigil/issues/564)) ([c37d301](https://github.com/Vigil-SOC/vigil/commit/c37d30116bf4e3a81a49a1c8d9147e99d636a10d))
* **web:** don't label every Bifrost key "Disabled" ([#857](https://github.com/Vigil-SOC/vigil/issues/857)) ([bafc8cb](https://github.com/Vigil-SOC/vigil/commit/bafc8cb3df840e091ee6baf3a9b8fed3f0582ab1))
* **web:** UserMenu no longer white-screens when full_name is null ([#770](https://github.com/Vigil-SOC/vigil/issues/770)) ([b225a2b](https://github.com/Vigil-SOC/vigil/commit/b225a2b4a6f8eef1adb0383b190fb83287362f20))
* **workflows:** resolve custom agents at playbook time ([#874](https://github.com/Vigil-SOC/vigil/issues/874)) ([37eec9b](https://github.com/Vigil-SOC/vigil/commit/37eec9bc49a6efe2636a6555900f55293f74ec2d))


### Code Refactoring

* **cases:** keep literal types on lookups ([#705](https://github.com/Vigil-SOC/vigil/issues/705)) ([413b65c](https://github.com/Vigil-SOC/vigil/commit/413b65c91d0c8ae834d3a215be0721a12be2bb35))
* **integrations:** the descriptor is the registry ([#682](https://github.com/Vigil-SOC/vigil/issues/682)) ([b55f76d](https://github.com/Vigil-SOC/vigil/commit/b55f76d1327377c14fbc1e70edc2ec853b4d0302))
* replace deprecated datetime.utcnow() with naive-UTC helper ([#575](https://github.com/Vigil-SOC/vigil/issues/575)) ([d716471](https://github.com/Vigil-SOC/vigil/commit/d7164717e16289bcfcd21aac10a6576c26d14836))
* **tools:** move the MCP tool servers to httpx ([#663](https://github.com/Vigil-SOC/vigil/issues/663)) ([57f4d7a](https://github.com/Vigil-SOC/vigil/commit/57f4d7afc85aff25b3b4b5cc10c81b7678bb18ff))
* **web:** retire the redesign directory ([#711](https://github.com/Vigil-SOC/vigil/issues/711)) ([fa38c67](https://github.com/Vigil-SOC/vigil/commit/fa38c67d578524597408181288f9a928830bca9e))


### Documentation

* add SECURITY.md aligned with the published security.txt ([#723](https://github.com/Vigil-SOC/vigil/issues/723)) ([e6158b4](https://github.com/Vigil-SOC/vigil/commit/e6158b4a84b5303f4a7becdaf770ef305030bc02))
* **readme:** install path that exists, without LogLM as a requirement ([#1013](https://github.com/Vigil-SOC/vigil/issues/1013)) ([88833f0](https://github.com/Vigil-SOC/vigil/commit/88833f0209ee6552dde1ea70c677bf0fd4872b75))


### Miscellaneous Chores

* **ci:** surface refactor and chore in changelog ([#683](https://github.com/Vigil-SOC/vigil/issues/683)) ([935e1b6](https://github.com/Vigil-SOC/vigil/commit/935e1b63aad3874596d3337cd734a1dcd8f1eda9))
* **daemon:** delete auto_assign_severities — a severity list nothing reads ([#975](https://github.com/Vigil-SOC/vigil/issues/975)) ([#983](https://github.com/Vigil-SOC/vigil/issues/983)) ([c7d4db3](https://github.com/Vigil-SOC/vigil/commit/c7d4db37c56d6f739b1652df17cee77a89ceb8c8))
* **deps-dev:** bump import-linter in the python-minor-and-patch group ([#861](https://github.com/Vigil-SOC/vigil/issues/861)) ([37e264e](https://github.com/Vigil-SOC/vigil/commit/37e264eb7cf7a70255ead7b5a01316d58e7e4b1c))
* **deps-dev:** bump the python-minor-and-patch group with 2 updates ([#818](https://github.com/Vigil-SOC/vigil/issues/818)) ([f824798](https://github.com/Vigil-SOC/vigil/commit/f8247980d0c3bb129c358d5924b1e912f04fed1b))
* **deps:** bump hadolint/hadolint-action in the actions group ([#760](https://github.com/Vigil-SOC/vigil/issues/760)) ([d697766](https://github.com/Vigil-SOC/vigil/commit/d697766501975200c39eba7dbefbbb1100d20192))
* **deps:** bump the actions group across 1 directory with 15 updates ([#692](https://github.com/Vigil-SOC/vigil/issues/692)) ([2c184ca](https://github.com/Vigil-SOC/vigil/commit/2c184cacbb8289a0b9b2f06537ad8aa39b07aa34))
* **deps:** drop unused claude-agent-sdk ([#762](https://github.com/Vigil-SOC/vigil/issues/762)) ([2ab4715](https://github.com/Vigil-SOC/vigil/commit/2ab4715bf36c7ee74002417a332c8d5d5989b15a)), closes [#694](https://github.com/Vigil-SOC/vigil/issues/694)
* **deps:** regenerate requirements.lock for [#1037](https://github.com/Vigil-SOC/vigil/issues/1037)-[#1040](https://github.com/Vigil-SOC/vigil/issues/1040) bumps ([#1163](https://github.com/Vigil-SOC/vigil/issues/1163)) ([c7ff0b8](https://github.com/Vigil-SOC/vigil/commit/c7ff0b8986fd25b4af10f9de7f58561989a6397c))
* **deps:** update aiokafka requirement from &gt;=0.10.0 to &gt;=0.14.0 ([#716](https://github.com/Vigil-SOC/vigil/issues/716)) ([0771b0e](https://github.com/Vigil-SOC/vigil/commit/0771b0e5bb105028549896bbc001169ddfc427f8))
* **deps:** update arq requirement from &gt;=0.27.0 to &gt;=0.28.0 ([#677](https://github.com/Vigil-SOC/vigil/issues/677)) ([a5309d7](https://github.com/Vigil-SOC/vigil/commit/a5309d75b13677ecd715ffce14bf1e52673bcd5e))
* **deps:** update bcrypt requirement from &gt;=4.2.0 to &gt;=5.0.0 ([#865](https://github.com/Vigil-SOC/vigil/issues/865)) ([5ebe2e8](https://github.com/Vigil-SOC/vigil/commit/5ebe2e880fa931b6b27308da949478c783a8c494))
* **deps:** update boto3 requirement from &gt;=1.34.0 to &gt;=1.43.77 ([#714](https://github.com/Vigil-SOC/vigil/issues/714)) ([82d8aa7](https://github.com/Vigil-SOC/vigil/commit/82d8aa7e17f1816250776fc9f743ea9c71f45720))
* **deps:** update fastapi requirement from &gt;=0.129.0 to &gt;=0.141.1 ([#713](https://github.com/Vigil-SOC/vigil/issues/713)) ([2c825ed](https://github.com/Vigil-SOC/vigil/commit/2c825ed72a9a1425eef6a072bffb3444bb823c4b))
* **deps:** update ijson requirement from &gt;=3.2.0 to &gt;=3.5.1 ([#717](https://github.com/Vigil-SOC/vigil/issues/717)) ([710c995](https://github.com/Vigil-SOC/vigil/commit/710c995e7a957f94b881fe689c98e1150469e343))
* **deps:** update numpy requirement from &gt;=1.26.0 to &gt;=2.5.2 ([#820](https://github.com/Vigil-SOC/vigil/issues/820)) ([c9c65f0](https://github.com/Vigil-SOC/vigil/commit/c9c65f0733d901a92508941febce1642d3f2762d))
* **deps:** update numpy requirement from &gt;=2.5.2 to &gt;=2.5.3 ([#1037](https://github.com/Vigil-SOC/vigil/issues/1037)) ([b0f26b7](https://github.com/Vigil-SOC/vigil/commit/b0f26b756f240c31bac55939976f837025a501e5))
* **deps:** update opentelemetry-api requirement ([#821](https://github.com/Vigil-SOC/vigil/issues/821)) ([a3c7eaf](https://github.com/Vigil-SOC/vigil/commit/a3c7eaf2d5ae2f20fb3be4ddef74e5107b6ad30f))
* **deps:** update opentelemetry-exporter-otlp-proto-grpc requirement ([#680](https://github.com/Vigil-SOC/vigil/issues/680)) ([af3c91b](https://github.com/Vigil-SOC/vigil/commit/af3c91b186aa93129af24f7d78033b179ebbc5ad))
* **deps:** update opentelemetry-instrumentation-redis requirement ([#863](https://github.com/Vigil-SOC/vigil/issues/863)) ([4abaffa](https://github.com/Vigil-SOC/vigil/commit/4abaffa1885564fd9e7613f202f479cb7c6041db))
* **deps:** update opentelemetry-semantic-conventions-ai requirement ([#822](https://github.com/Vigil-SOC/vigil/issues/822)) ([e22e18b](https://github.com/Vigil-SOC/vigil/commit/e22e18b0e008082651ab8da09d43e909d5915fb2))
* **deps:** update psycopg2-binary requirement from &gt;=2.9.9 to &gt;=2.9.12 ([#689](https://github.com/Vigil-SOC/vigil/issues/689)) ([36ce87a](https://github.com/Vigil-SOC/vigil/commit/36ce87aaad75dd8e1dae30da32a7a84f229bb4b7))
* **deps:** update pydantic-settings requirement ([#1041](https://github.com/Vigil-SOC/vigil/issues/1041)) ([195c3ee](https://github.com/Vigil-SOC/vigil/commit/195c3eefdfca5bf2d1ef0223dc1127500378d4f9))
* **deps:** update pyjwt requirement from &gt;=2.10.0 to &gt;=2.13.0 ([#819](https://github.com/Vigil-SOC/vigil/issues/819)) ([b76b849](https://github.com/Vigil-SOC/vigil/commit/b76b8492b952057607d1ca0c753388adc2daaafd))
* **deps:** update python-multipart requirement ([#676](https://github.com/Vigil-SOC/vigil/issues/676)) ([56d6491](https://github.com/Vigil-SOC/vigil/commit/56d64918218e51b4f54ef3939344f0d392e5eb6a))
* **deps:** update pyyaml requirement from &gt;=6.0.0 to &gt;=6.0.3 ([#690](https://github.com/Vigil-SOC/vigil/issues/690)) ([44e0e29](https://github.com/Vigil-SOC/vigil/commit/44e0e296a6a577dee59ea12f7f8bb551b4d1f4b8))
* **deps:** update reportlab requirement from &gt;=4.0.0 to &gt;=5.0.0 ([#688](https://github.com/Vigil-SOC/vigil/issues/688)) ([ac641b8](https://github.com/Vigil-SOC/vigil/commit/ac641b84d9b2172dc495fecada04689747016d50))
* **deps:** update reportlab requirement from &gt;=5.0.0 to &gt;=5.0.1 ([#864](https://github.com/Vigil-SOC/vigil/issues/864)) ([980c860](https://github.com/Vigil-SOC/vigil/commit/980c8604a081bcc129107590521edc1487386bc5))
* **deps:** update requests requirement from &gt;=2.31.0 to &gt;=2.34.2 ([#678](https://github.com/Vigil-SOC/vigil/issues/678)) ([db35f2d](https://github.com/Vigil-SOC/vigil/commit/db35f2d691982541260f9695b79c94287361e012))
* **deps:** update sentry-sdk requirement from &gt;=1.40.0 to &gt;=2.69.2 ([#1040](https://github.com/Vigil-SOC/vigil/issues/1040)) ([690a98c](https://github.com/Vigil-SOC/vigil/commit/690a98c764179bca629510aa5694a8ca4c4149f6))
* **deps:** update slowapi requirement from &gt;=0.1.9 to &gt;=0.1.10 ([#715](https://github.com/Vigil-SOC/vigil/issues/715)) ([e546d7c](https://github.com/Vigil-SOC/vigil/commit/e546d7c842ba26596ed1b6fbc6bfd621de3442e7))
* **deps:** update sqlalchemy requirement from &gt;=2.0.0 to &gt;=2.0.52 ([#679](https://github.com/Vigil-SOC/vigil/issues/679)) ([bd4f8dc](https://github.com/Vigil-SOC/vigil/commit/bd4f8dcf6f6269f6de06d058cca4e1bbb7a4c555))
* **deps:** update sqlalchemy requirement from &gt;=2.0.52 to &gt;=2.0.54 ([#1038](https://github.com/Vigil-SOC/vigil/issues/1038)) ([e9ae669](https://github.com/Vigil-SOC/vigil/commit/e9ae6694f321a0054aee35f2fa637dab0f11186a))
* **deps:** update stix2 requirement from &gt;=3.0.1 to &gt;=3.0.2 ([#687](https://github.com/Vigil-SOC/vigil/issues/687)) ([c5d22fb](https://github.com/Vigil-SOC/vigil/commit/c5d22fbc1103a2b3a5f8824961efb65615d536a9))
* **deps:** update urllib3 requirement from &gt;=2.0.0 to &gt;=2.7.0 ([#862](https://github.com/Vigil-SOC/vigil/issues/862)) ([32c31b3](https://github.com/Vigil-SOC/vigil/commit/32c31b3d1a214aceacd12966856339d51afb10ed))
* **deps:** update urllib3 requirement from &gt;=2.7.0 to &gt;=2.8.0 ([#1039](https://github.com/Vigil-SOC/vigil/issues/1039)) ([e66fa91](https://github.com/Vigil-SOC/vigil/commit/e66fa91462206c8022ac17dda70ba3f0cac62f8b))
* **env:** add Docker so cloud agents can run compose ([#781](https://github.com/Vigil-SOC/vigil/issues/781)) ([0f95da9](https://github.com/Vigil-SOC/vigil/commit/0f95da9e79f2e307e70e76913c6ab69f97abe95a))
* **env:** add Helm so cloud agents can run helm template ([#790](https://github.com/Vigil-SOC/vigil/issues/790)) ([5f44aec](https://github.com/Vigil-SOC/vigil/commit/5f44aecbebbd37b7acaa8de763b20e8bab7447d9))
* **gateway:** pin the Bifrost image to v2.2.1, overridable via BIFROST_IMAGE_TAG ([#1107](https://github.com/Vigil-SOC/vigil/issues/1107)) ([e828e24](https://github.com/Vigil-SOC/vigil/commit/e828e243cda0fdf3cd19ed3b1e184c80575fc443))
* release 0.5.1 ([#763](https://github.com/Vigil-SOC/vigil/issues/763)) ([238cf3b](https://github.com/Vigil-SOC/vigil/commit/238cf3b02d49e105e25886d5a4062a5477348bbf))
* release 0.6.0 ([#817](https://github.com/Vigil-SOC/vigil/issues/817)) ([f67bf20](https://github.com/Vigil-SOC/vigil/commit/f67bf20b4b05eb62950a72e8f0ae0c0dcdc7538a))
* remove per-finding embedding storage ([#761](https://github.com/Vigil-SOC/vigil/issues/761)) ([160170b](https://github.com/Vigil-SOC/vigil/commit/160170b690ed5c5b0255eab6699d110764ae9456))
* **skills:** retire the skills table, SkillService, importer and bridge; GET /api/skills lists loaded files ([#1066](https://github.com/Vigil-SOC/vigil/issues/1066)) ([8836268](https://github.com/Vigil-SOC/vigil/commit/88362681dcf44745c8e615debcdbb429e2ba66a2))

## [0.5.0](https://github.com/Vigil-SOC/vigil/compare/v0.4.0...v0.5.0) (2026-08-14)


### Features

* **agent:** one loop — the harness, the tool bridge, and the hunt on it ([#638](https://github.com/Vigil-SOC/vigil/issues/638)) ([39b82b1](https://github.com/Vigil-SOC/vigil/commit/39b82b1c1005036719deb57d57a9ff4e9f12c547))
* **config:** typed Settings object; retire the 215 raw env reads ([#520](https://github.com/Vigil-SOC/vigil/issues/520)) ([5a6550b](https://github.com/Vigil-SOC/vigil/commit/5a6550b32aa8254e81f7eeae287b169664313784))


### Bug Fixes

* **api:** drop the DB-aligned threadpool cap ([#614](https://github.com/Vigil-SOC/vigil/issues/614)) ([3e59161](https://github.com/Vigil-SOC/vigil/commit/3e5916161138040c1e4e960ba3eae1e0fb1babcd))
* **api:** offload blocking I/O off the event loop ([#518](https://github.com/Vigil-SOC/vigil/issues/518)) ([cf2cfbb](https://github.com/Vigil-SOC/vigil/commit/cf2cfbb1eee23537f3df8724e7f7e020234c6998))
* **integrations:** hyphenated Integration IDs so cloud SIEM pollers start ([#555](https://github.com/Vigil-SOC/vigil/issues/555)) ([#583](https://github.com/Vigil-SOC/vigil/issues/583)) ([de748b6](https://github.com/Vigil-SOC/vigil/commit/de748b66b2dab7ec1ada9679ace91ada75dee52c))
* **integrations:** move to httpx, unblock the loop ([#609](https://github.com/Vigil-SOC/vigil/issues/609)) ([46e5115](https://github.com/Vigil-SOC/vigil/commit/46e5115e303d070ca6cf5611dc9792c7920b79af))
* **llm:** sync Bifrost keys via the keys API ([#640](https://github.com/Vigil-SOC/vigil/issues/640)) ([fa57c15](https://github.com/Vigil-SOC/vigil/commit/fa57c154d92b57189c8d581d70c7305dc370f978)), closes [#613](https://github.com/Vigil-SOC/vigil/issues/613)
* **tools:** make TLS verification configurable for palo_alto and misp ([#662](https://github.com/Vigil-SOC/vigil/issues/662)) ([4a0ed1d](https://github.com/Vigil-SOC/vigil/commit/4a0ed1dc84844f803565240c9dd6070d04b2dd61))

## [0.4.0](https://github.com/Vigil-SOC/vigil/compare/v0.3.0...v0.4.0) (2026-07-30)


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
* **ingest:** background upload jobs, stop dropping rows as dupes, generic parquet schema support ([#496](https://github.com/Vigil-SOC/vigil/issues/496)) ([4fc1d63](https://github.com/Vigil-SOC/vigil/commit/4fc1d63c9040efe60baf86b28875e33526d121fb))
* **llm:** omit thinking for adaptive-only models behind Bifrost ([#455](https://github.com/Vigil-SOC/vigil/issues/455)) ([44db519](https://github.com/Vigil-SOC/vigil/commit/44db519f0c2c92f32911cbc25dfe5193eaa110e6))
* **llm:** use adaptive thinking for Opus 4.7/4.8 & Fable 5 ([#454](https://github.com/Vigil-SOC/vigil/issues/454)) ([01e8af7](https://github.com/Vigil-SOC/vigil/commit/01e8af7b8da93c44ef13f48f8b9dd6174903f49e))
* **logs:** don't crash backend when logs dir isn't writable ([#382](https://github.com/Vigil-SOC/vigil/issues/382)) ([22078a4](https://github.com/Vigil-SOC/vigil/commit/22078a445515bcc39650918f66439445bfb86437)), closes [#376](https://github.com/Vigil-SOC/vigil/issues/376)
* **mcp:** pin mcp-remote to &gt;=0.1.16 to close CVE-2025-6514 ([#390](https://github.com/Vigil-SOC/vigil/issues/390)) ([b2fcaf5](https://github.com/Vigil-SOC/vigil/commit/b2fcaf58142b2956c73607d2393ed4dcb4d3e893))
* **redesign:** define missing chat-dock width helpers ([#438](https://github.com/Vigil-SOC/vigil/issues/438)) ([6f4b0df](https://github.com/Vigil-SOC/vigil/commit/6f4b0df660a9cdf6712c72e18cdc6366f38cec9e))
* **redesign:** define the missing chat-dock resize helpers ([#401](https://github.com/Vigil-SOC/vigil/issues/401)) ([#448](https://github.com/Vigil-SOC/vigil/issues/448)) ([0aa517b](https://github.com/Vigil-SOC/vigil/commit/0aa517bd794bdc304b2794f68c4dde9a318c2543))
* **redesign:** remove duplicate chat-dock helpers ([#452](https://github.com/Vigil-SOC/vigil/issues/452)) ([32e7116](https://github.com/Vigil-SOC/vigil/commit/32e71164fd962d49a2382b6c330ea4f8f977de96))
* **redesign:** scope Escape to topmost dialog layer ([#400](https://github.com/Vigil-SOC/vigil/issues/400)) ([79b4343](https://github.com/Vigil-SOC/vigil/commit/79b43436acb3608bab6dfbd580f139c16ccde326))
* repair stale setup docs and dev-bootstrap provisioning ([#375](https://github.com/Vigil-SOC/vigil/issues/375)) ([0b3bb1c](https://github.com/Vigil-SOC/vigil/commit/0b3bb1caf2bc3b8f10c6d12851a8d23842ce1220))
* replace hardcoded palace path with get_palace_path() ([#466](https://github.com/Vigil-SOC/vigil/issues/466)) ([43bfd38](https://github.com/Vigil-SOC/vigil/commit/43bfd383b812e6fe0d12756b4cd5e81362b3499e))
* **scripts:** correct daemon frontend.pid path ([#384](https://github.com/Vigil-SOC/vigil/issues/384)) ([d273d23](https://github.com/Vigil-SOC/vigil/commit/d273d23dc58f01157d05e8531a30bdce04219dd9))
* **setup:** correct wizard readiness and model assignment ([#441](https://github.com/Vigil-SOC/vigil/issues/441)) ([3bfd1fb](https://github.com/Vigil-SOC/vigil/commit/3bfd1fbbb3560116be3ab93fa6c8dc774d66e95f))

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
