# Source/Compose release evidence

Release: **v0.1.0**, 2026-11-02, at
[meme-arsenal-community](https://github.com/OmarSarhan/meme-arsenal-community).
Public history starts from the selected current source in a new parentless root;
the original and development repositories remain private. The GitHub release
record supplies the actual public commit/tag and any public CI outcomes.

This page preserves dated private test evidence and its limits. Private commit
IDs and CI links are provenance, not public source history or a claim that the
new public root has already run those tests. Current confidential reporting and
its verification are recorded in [SECURITY.md](../SECURITY.md).

The release scope is source-built Compose on Linux amd64 with rootful Docker,
Compose 2.30+, PostgreSQL 16/vector, Redis 7 and Caddy private HTTPS. Telegram and
paid providers are optional. Local SQLite/mock tooling has a narrower feature
set. Preserve [0BSD, asset provenance and third-party notices](licensing.md),
including the dashboard's generated notices. No wheel, registry image or
multiplatform distribution is established by these results.

## Accepted evidence

The identities below are actual tested development heads, not a claim that a
future public root ran these tests. Private CI numbers are provenance; the
results and limits here are readable without access to those logs.

| Test scope | Tested identity | Observed result |
| --- | --- | --- |
| Fresh mock source workflow | `934440d084e2845b32c9ca653ecd1882df2daf80` | New venv/npm install, installed CLI init/sync/serve, two synthetic items, React assets/images/search/detail; one browser test passed on an isolated network. |
| Workflow and worker restart | `a47f09fc19f844608acce553d62dcb2dbd9e7883` | Backend 359 passed, 2 existing skips; Chromium 34 passed, no skips. Save/lineage convergence, PostgreSQL row locking and killed-worker stale recovery covered. |
| Restore and development-schema recovery | `c976a030681f8c1d6d50e429e3c5f923c53d2209` | Backend 359 passed, 2 skips; Chromium 34 passed. Full populated restore, installed HTTPS/owner/save/worker bytes and transactional migration failure/retry/checkpoint recovery passed. |
| Upload-first guide/readiness candidate | `4a00409868e77e568aceed5c7ba2c0f4d529e00d` | Backend 365 passed, 2 skips, 33 deselected, 2 warnings; frontend build passed; Chromium 34 passed without skips; fresh mock demo 1 passed. Community ingress/API/assets/recreation/restore/schema scenarios passed. |
| Installed private guide/UI proof | `06eeafba21f1185063d1fddbc0fb0c8c26f9aa30` | Backend 365 passed, 2 skips, 33 deselected, 2 warnings; frontend passed; Chromium 34 passed, no skips; fresh mock demo 1 passed. Interactive hash/protected-file checks, exact preflight commands and installed private HTTPS upload/save/edit/search/reload passed; existing community recovery scenarios also passed. |

The upload-first candidate's CI checkout was `4912f5c3271d2c569fe047af93aa2ba25affc995`;
its tree matched the reviewed head and accepted main
`f8d230efa784e5625e14418eb438074254916553`, tree
`cba5d678fb4808cb37c843bff47efd182bb8f600`. Standard run `41403253904` and
community run `41403253862` were inspected. The community application image was
`sha256:ec7197c2019ba60877f156414023e2ec50b2943332cb58f944b0b25f498167b4`.
The two backend skips are existing browser-import collection skips; separate
browser jobs execute that coverage. No restored-browser interaction was claimed.

Observed fresh-demo tools: Python 3.12.15, pip 25.0.1, Node 22.23.4,
npm 10.9.9, Playwright 1.64.1, Chromium 156.0.8078.73, FFmpeg 7.1.5.
`pip check` passed. These describe the fresh-demo environment, not the separately
built app's dependency inventory. Missing git affected editable-origin discovery
in `pip freeze`; checkout/tree verification established source identity separately.
Python dependency ranges and apt resolution preclude byte-identical image claims.

## Verified guide commands and installed UI

The installed UI run checked out `bb5555492e77c00f223ab557330ee351df6c5ef5`,
verified content-equal to head `06eeafba21f1185063d1fddbc0fb0c8c26f9aa30`,
tree `6cf887fcb6016a943faa2d3b80dea86d49c49c78`. Standard run `41407991648`
and community run `41407991640` passed; their logs were inspected. The app image
was `sha256:6164f757445ba9e5eed1a52849cd0da33e0718e50082f4cbf62e84c0d7fdca80`.
See the [observed environment](release-environment.txt) for resolved app packages
and tool versions. This record supersedes earlier pending guide/UI statements in
the dated verification records.

The following evidence applies to that tested source. Later evidence-document
updates and synthetic filename replacements in upload tests are separately
reviewed changes; they do not mean the earlier run executed on a later commit.
Application, build and browser-check behavior remains as tested. No local Docker,
browser installation or full-stack rehearsal was performed for this preparation.

| Community guide command/input | Observed evidence |
| --- | --- |
| Pinned Caddy interactive bcrypt command; protected hash paste | Both real password prompts observed through a PTY, password echo rejected, guide's actual hash-paste snippet executed with a temporary path, mode 0600 and exact contents verified. |
| Initializer with new external instance, HTTPS URL, image, user and hash file | Existing smoke invokes the actual initializer; offline tests cover protected modes, invalid input and refusal boundaries. Synthetic fixture user/password and reserved host replace operator inputs. |
| `config --quiet`, build app, app `--check`, `caddy validate` | Exact commands completed successfully in the same disposable fixture, alongside topology/adapt validation. |
| DB/Redis startup, migration, owner initialization, stack startup | Existing accepted smoke uses these actual commands and new storage/project. |
| Upload source slot, explicitly save, browse/find and curate | Real HTTPS UI uploaded synthetic PNG bytes, saved to the gallery, verified disabled/inactive sharing without URLs, edited the description, matched the exact item through search and rejected a nonmatching query. After reload, search repeated and the reopened editor contained the saved description. |
| Restart, private backup/restore, worker and schema recovery | Reuse accepted scenarios; no additional manual matrix. The community workflow naturally retains its existing scenarios. |

Runtime uses the existing unique disposable stack, app build and storage. Its
networks are internal and expose no host app ports; Caddy uses a fixture CA instead
of public ACME. The browser trusts only the added fixture root through its NSS
store, retains certificate/hostname verification, blocks service workers and
restricts page requests to the fixture origin. Browser packages live in CI test
tooling, not the production image. Credentials/media are synthetic. The actual
Docker/Compose, app dependency/tool, tree and image inventories were inspected.
This automation is bounded guide proof, not evidence of an independent human
walkthrough, public DNS/ACME availability or live provider/Telegram behavior.

## Recovery and remaining gates

Publication-content review covered all 65 changed/new text files between cleanup
`34707a07ceef45e741da0c8716a2016a4ab008d5` and
`f8d230efa784e5625e14418eb438074254916553`, plus the preparation delta.
All seven bundled images received bounded visual and metadata checks. The review
found incomplete backup/status guidance and personal workstation context in two
synthetic upload filenames; these were corrected. No genuine credential or real
media payload was identified. This is bounded content review, not a guarantee
against hidden data, a new legal clearance or a substitute for auditing later
destination/export changes. Private development history retains older content.

Use the [complete private backup](../deploy/community/README.md#consistent-private-maintenance-backups)
and [empty-instance exact-image restore](../deploy/community/README.md#restore-to-a-disposable-empty-instance).
Retain protected configuration/signing/owner state and the actual image identity;
drain writers first. A reserve tar alone omits PostgreSQL and external SQLite.
The proven schema path is populated development `012_public_share_codes` to
`013_explicit_recipe_grants`, with deliberate transactional failure, retry and
checkpoint restore. It is not historical released-version upgrade compatibility,
downgrade, power-loss recovery or exactly-once external generation. Generation
in recovery evidence is deterministic fixture code; live vendor quality, quota
and model availability remain unverified.

Historical remaining gates at the initial preparation checkpoint: finish acceptance of the final evidence/fixture delta;
choose the concrete public destination and
source/tag action; satisfy [confidential reporting](../SECURITY.md) with real
reporter-facing availability evidence. Preserve private development history.
Only a later authorized action may create fresh public history from approved
tracked content, validate its parentless root/ref/content boundary, and record
the final private-candidate/public-root/tag mapping. At that checkpoint, no public
repository, reporting setting, source export or tag had been created.


## Owner onboarding and documentation acceptance — 2026-11-02

OMA41's documentation and mapped installed walkthrough are accepted within the
limits below. Independent reviews of both documentation slices and the final
reader/outcome reconciliation found no actionable findings. This is later private
evidence; the earlier records above retain their original identities and results.

A fresh-context agent started at the README, followed the guides without source,
test-harness or earlier-review coaching, and froze 18 expected steps at
`c1436236f9f16b5914372a1cb94fbbcd25d21044`. Its report SHA-256 is
`7b467b1ef24e5187c0b166dfdfc8b281399fd11ecacda4713d8bd41ffb07a418`.
It found a coherent full-app mock route and distinguished contributor setup from
the limited SQLite demo. Subsequent corrections clarified host-administration
prerequisites, a PNG/JPEG under 1 MiB for first use, the original Basic password,
backup planning versus completion, and retained-work control labels. These were
corrections after the frozen observations, not discoveries credited to that reader.

The first installed run stopped before upload because a new test incorrectly
expected a visible disabled Create button in an empty workspace. The corrected
test verifies mock readiness, disabled Analyze and no Create control until sources
are selected. The entire installed run then passed anew; earlier partial results
were not combined to claim completion. Two-source Create visibility/disabled state
and recovery-selector labels are source-supported, not additional first-use trials.

### Tested identities and results

Reviewed head: `1c34fe77b4bcaae2440ac79fda580817d6a19c10`. GitHub tested merge
`b24f738621505eb29f7b72b250b20448b7eac3b4`, whose tree
`5ce422d75de8be614a4104e99a0c17a68beef987` equals that head's tree.
The built application image was
`sha256:5b6c27887feadc1e524b94cab5da20b56e75e6a0116aca49dac066556d1de2cc`.
The final evidence-only edits do not change the setup guide, executable commands,
harness or application; they do not relabel this execution as a later commit.

| Evidence input | Tested Git blob |
| --- | --- |
| README | `bf088edfae467f2593638f09be67b6da1b24a35b` |
| Community setup guide | `d3c2e621f3c9aef6d64a23623d924496ddb01c8b` |
| Installed UI script | `272ddcd990a44843fdd74013a075fc65e2f18f35` |
| Interactive hash script | `a706a58fff093359df04912f8905447ffdcbc69a` |
| Community runtime script | `214825d310329548d3c8edee4cb904704d3beae9` |

The new [community run 41486002794](https://github.com/OmarSarhan/meme-arsenal-oss/actions/runs/41486002794),
job `125416398480`, passed. The new
[general CI run 41486002771](https://github.com/OmarSarhan/meme-arsenal-oss/actions/runs/41486002771)
also passed: backend **691 passed, 2 skipped, 33 deselected**, with 26 warnings
in 200.31 seconds; Ruff passed; Chromium **34 passed** in 176.14 seconds;
fresh mock demo **1 passed** in 8.80 seconds; TypeScript/Vite build passed.
Skip reasons are not itemized in that run's final summary. The separate static
demo is not substituted for installed owner-app proof.

Observed installed tools: Docker 28.0.4, Compose 2.38.2, Python 3.12.15 and
Chromium 156.0.8078.73. These are observations, not pinned installation requirements.
All five downloaded logs and the guide/harness snapshot were hashed; independent
outcome review matched their identities and checked the relevant assertions and
phase output against the frozen reader steps.

### Reader steps reconciled with execution

| Frozen steps | Newly observed outcome and deliberate differences |
| --- | --- |
| W01–W02: prerequisites and private mock choice | Authorized CI checkout, unique temporary instance/project, generated mock defaults, no Telegram profile or live keys; actual mock health checked. Real host, mounts, capacity, domain and firewall remain operator prerequisites. |
| W03–W05: bcrypt, protected hash, initialize | Real synthetic PTY prompts and no-echo checks, exact newline/0600 hash file, new private instance. Only the hash Python snippet is literally extracted, with its destination changed; other commands are constructed argv. |
| W06–W07: configure and preflight | Compose topology, Caddy adaptation/validation, app build and app preflight succeeded. CI validates Caddy before app build/check; both independent preflights finish before provisioning/start. |
| W08–W11: dependencies, schema, owner, startup | Healthy DB/Redis, migration through `016_remix_execution`, selected durable owner, all five services ready and no published fixture ports. Compose `--wait` plus health checks substitute for the guide's `up` followed by `exec curl`. |
| W12–W14: HTTPS, upload, private save | Verified fixture HTTPS with Basic Auth; synthetic 40×40 PNG under 1 MiB uploaded in the first Remix slot, explicitly saved, and opened by exact ID in Gallery. Sharing remained disabled/inactive with null URLs. |
| W15–W17: edit, find, reload | Saved description `guideuniqueviolet`; `guidenomatch` returned no items, matching search returned the exact saved ID. Both searches repeated after reload, and the reopened editor retained the description. |
| W18: completed protection after startup | Full private backup, checksums, reserve/config/secrets/TLS contents, scratch DB comparison and exact-ID/prior-role recovery passed after UI readiness. Planning storage alone is not a completed backup. |

The installed UI completion marker was recorded at 16:58:43 UTC on 2026-11-02.
The retained drill also passed auth/Origin/proxy checks, bootstrap fault rejection,
container persistence, graceful drain, exact-image empty-instance restore,
cookie/no-cookie owner curation, ready image and saved replay, queued-worker
activation/image assertions, and development `012→013` failure/checkpoint retry.
Some assertions share an aggregate completion marker; each is not a separate
logged interaction. Intentional bootstrap/migration failures are negative tests.

### Pages reviewed and limits

All 21 tracked prose files were accounted for. Current technical coverage was
README, the community guide, development, online architecture, source ingestion,
derived-data refresh, opt-in sharing, privacy and internal extension examples.
CONTRIBUTING, SECURITY, licensing and the reserve placeholder received proportionate
routing and accuracy review. Release preparation, community verification, observed
environment, source-alpha status, assessment, plan, self-hosting and owner-identity
decisions retained their dated evidence and rationale. LICENSE was unchanged.
Static checks passed for 216 relative links/anchors, fences, 75 shell syntax blocks,
three Python snippets, named extension test targets and both architecture assertions.
All 14 original community shell blocks and the protected hash contract were preserved.

This combines an **agent-reader assessment** with a new mapped automated installed
run, not a human usability study or literal execution of every guide command.
Synthetic credentials/media, temporary storage, fixture CA, internal networks and
no host ports replace real hosting inputs. Certificate/hostname verification stayed
enabled. Public DNS/ACME, host firewall and physical mounts/capacity, live provider
credentials/cost/quality and Telegram authorization remain untested. No meaningful
AI quality, arbitrary historical upgrade/downgrade, power-loss durability or actual
operator image-archive retention is established. Retain the real images, source,
ordered recipes and private backups as documented.

At the time of this onboarding acceptance, both repositories were private and
public release was deferred. Frozen candidate
`107eb0f2a50d8adb3f39f14c02e6c722da99fae4` remains historical.
Start current owner setup at the [community guide](../deploy/community/README.md#upload-first-setup)
and source changes at the [developer guide](development.md). The
[observed environment](release-environment.txt) is a historical inventory, not
installation requirements or a lockfile. These documentation changes do not
clear publication gates or establish that private source histories can be published.

## Selected public source candidate — 2026-11-02

The selected private main is `e6c45701f41fe8448f6e3a2c84311634c9e1362c`, tree
`d850ce0450f8f0fabe1fdfebbccb3406a5b47747`. It contains the accepted provider,
ingestion, provenance, style-guidance, extension and final documentation work.
Its tree matches reviewed PR head `3217923a955b3ed1b6d4828e4f1470d3eaaabfa8`
and actual CI merge checkout `16be3a5935ad951f0c39f3331502b2453b69fa66`.

Previously observed final private CI passed all five jobs. General run
`41489257089` passed the frontend build and Ruff, 691 backend tests (2 skipped,
33 deselected), 34 Chromium cases and one fresh-install mock demo case. Community
run `41489257615` passed installed HTTPS upload/save/edit/exact-search/reload,
auth/Origin/proxy boundaries, complete backup, empty exact-image restoration,
retained owner/ready/saved/queued-worker behavior, and bounded development-schema
failure/checkpoint recovery. Its application image was
`sha256:5291da35950d3e5d1d9af9287dfc890a03ab553acc235bb7ca066c3fcdcfc4e0`.
These are reused results for the matching executable inputs, not new local tests.

The public export changes only destination, acquisition, reporting and release
documentation. It preserves application, build, configuration, migration, test,
license and bundled asset bytes and modes. Publication verification records
the complete file manifest, documentation delta and fresh-root boundary outside
the root's own contents; the public release record identifies its resulting
commit and tag. The recovery and live-service limitations above still apply.
