# Standalone control-layer verification

The full live task-to-GitHub happy path now passes. Broader recovery and
integration gaps remain as listed below.
The existing Combadge application was not modified.

## Fix verification (2026-09-20)

The nine failures listed in the historical follow-up below are fixed. The suite
now contains 46 tests, including 11 additional recovery/protocol checks. All 46
pass on the development host and QNX Pi. Publication crash tests now reopen the
SQLite database and construct a fresh publisher between attempts. Git commit
hashing was independently compared against `git hash-object`.

Implemented transactional start reservations, uncertain-creation discovery,
durable cancellation/deletion retries (including after retention expiry), safe
publication recovery using ownership markers and deterministic commit objects,
credential filtering/content checks, bounded artifact/manifest downloads, and
per-notification acknowledgements. Completed tasks now use validated manifest
summaries. SSE monitoring is wired into the daemon, with polling and deadlines
continuing even when session listing fails. Events are bounded to 200 excerpts
per task.

The OpenAI Docs check also led to explicitly disabling sandbox network access
instead of relying on defaults. References: [session creation schema](https://developers.openai.com/api/reference/typescript/resources/beta/subresources/agents/subresources/sessions/methods/create)
and [GitHub commit creation](https://docs.github.com/en/rest/git/commits#create-a-commit).

The isolated Pi daemon was restarted successfully with the patched component;
the original Combadge application remains untouched. No new billed hosted task
was created during this fix verification. The previous live happy-path test is
not a post-fix live crash/recovery test: those checks still use fake providers.
Actual Combadge voice integration and a new live active-turn restart/publication
smoke test remain follow-up validation, not claims made by this suite.

See README recovery notes for conservative handling of creation ambiguity and
the remaining speech/acknowledgement crash window.

Final Pi verification: all 46 tests passed again after deployment; daemon
stop/start passed and authenticated loopback task listing returned HTTP 200
with five retained tasks. The previous installation's source/tests are backed
up at `/data/home/qnxuser/combadge-agents-backup.Fd0pHD`.

Organization release to the requested private destination
`Combadge-HTN/combadge-agent-control` was attempted using the Pi-only credential.
GitHub rejected repository creation with HTTP 403: "You need admin access to the
organization before adding a repository to it." No successful release is claimed.
This describes the Pi-credential attempt only. The private repository was
subsequently created with the development machine's authenticated GitHub CLI;
this source snapshot is being released through that local Git workflow.
The operator release journal is retained under
`/data/home/qnxuser/.local/state/combadge-release` for a safe retry after access
is granted.

## Failure-path follow-up (2026-09-20)

Added `tests/test_failure_paths.py`: 17 test methods covering recovery,
simultaneous approvals, cleanup failures, publication interruption, artifact
safety, notifications, and retention. The complete suite runs 35 tests on both
the development host and QNX Pi, with the same 9 failing checks on each
(including two publication-phase subtests).
The original 18 tests still pass. These are intentional regression tests for
unresolved defects, not expected-failure annotations. No production fixes were
made in this follow-up.

| Failing check | Observed behavior |
| --- | --- |
| Lost session-create response | Remote session exists but recovery does not attach it to the failed local task. |
| Concurrent start confirmations | Four simultaneous approvals bypass the three-task cap. |
| Failed timeout cancellation | Local task becomes terminal; cancellation is not retried after restart. |
| Failed cloud deletion | Terminal session cleanup is not retried after restart. |
| Credential-file exclusion | `.env.production` and `id_rsa` remain eligible for publication (test files contain dummy values only). |
| Artifact download bound | Download uses an unbounded read before archive limits are enforced. |
| Interrupted notification delivery | A successfully spoken notification repeats when a later notification fails. |
| Crash after remote repository creation | Retry fails with a name collision before recovering the publication job. |
| Crash after remote commit creation | Retry submits another commit creation instead of recovering the first result. |

Passing new checks include attached-session restart recovery, recovery after
creation but before local session attachment, sequential capacity enforcement,
independent task cancellation and steering, expired start/cancel/publish
approvals, malformed-output rejection, archive size/count limits, the retention
boundary, collision protection, and publication replay after blob, tree, and
ref writes. Baseline authentication, CSRF, single-use approvals, traversal and
symlink rejection, and happy-path publication tests also pass.

Scope: failures are injected using fake providers; restart tests reopen SQLite,
and publication tests interrupt between a remote mutation and journal update.
They do not kill a live daemon during a billed task. Retention uses an injected
clock rather than a 24-hour wall-clock wait. No new cloud task or GitHub repository
was created in this follow-up. Real Live-session integration, SSE consumption,
useful final-summary capture, exhaustive malformed-manifest validation, and a
live active-turn crash/restart remain unverified or incomplete.

The historical sections below describe earlier runs; their statements about
unverified failure paths are superseded by this follow-up. The happy-path
publication fix did not close the newly reproduced crash windows.

## Full live publication follow-up

- New hosted task: CDX-3S5P.
- Private repository: https://github.com/combadgehtn/combadge-e2e-cdx-3s5p
- Verified branch commit: 64ce4fce816104e6e5d417942634394380525d54.
- Files: README.md, hello.py, test_hello.py. Each GitHub blob was downloaded and
  compared byte-for-byte with the captured source; the entire path set matched.
- Hosted manifest reports one passing unittest on the freshly extracted ZIP.
- Publication ran through the daemon's prepare/confirm workflow and GitHub REST
  publisher. No generated source was executed on the Pi.
- Publisher now initializes a branch, creates the source commit with that branch
  as parent, and advances it without force. Existing repository collisions fail
  instead of adopting unrelated repositories. Saved commit SHAs are reused.
- All 18 automated tests pass after the change; the former failure reproducer
  now verifies successful bootstrap and a no-op retry after completion.
- Reproducible opt-in script: tests/live_publish_smoke.py. Each explicit run
  creates a new billed hosted task and a private repository, which it retains.
- Evidence on Pi: ~/.local/state/combadge-e2e/full-e2e-result.json.

The sections below describe the initial verification before this follow-up.

## Environment and results

- Isolated QNX 8 Pi installation: `/data/home/qnxuser/combadge-agents-e2e`.
- Isolated state: `/data/home/qnxuser/.local/state/combadge-e2e`; UI port 18787.
- 18 deterministic tests pass on the development host and QNX Python 3.14.
  These include three real HTTP/Unix-socket integration tests and one HTTP
  GitHub contract failure reproducer. The GitHub reproducer passes by asserting
  that publication fails; it is not evidence of successful publication.
- HTTP authentication, cookie flags, CSRF rejection, artifact downloads, and
  confirmations shared between HTTP and Unix socket verified with local servers.
- Live daemon startup, stop/start, retained task recovery, authenticated HTTP
  artifact retrieval, and byte-for-byte manifest download verified on the Pi.
- Live task CDX-PA2T completed and downloaded an archive containing hello.py and
  a manifest reporting a successful hosted execution. Source was inspected as
  text only; generated code was not executed on the Pi.
- Live steering and cancellation requests were accepted. CDX-2YSW completed
  despite the cancellation request; this does not prove cancellation interrupted
  execution. Its completed result and cloud deletion were confirmed.
- CDX-YE9Z, CDX-PA2T, and CDX-2YSW cloud sessions returned 404 after cleanup.
  The old CDX-Z39L cancellation test left an idle session, which was identified
  by its task metadata and deleted during the resumed verification.
- No GitHub repository was created or published during these tests.

## Corrections made during testing

- Increased readiness wait from 5 to 60 seconds after slow startup validation.
- Bind HTTP before unlinking the Unix socket, preventing a second daemon on the
  same port from disconnecting the running daemon's socket.
- Require outputs under `/workspace/outputs` and accept artifact `path` metadata.
- Keep cancellation in `cancelling` until a provider turn result is reconciled.
- Close SQLite test connections during teardown.

## Remaining integration blockers and limits

1. Publication creates an empty repository (`auto_init=False`), then attempts to
   create its first branch through Git refs. The HTTP fake reproduces GitHub's
   documented empty-repository rejection. A supported bootstrap is needed before
   a live publication smoke test.
2. Publication recovery rebuilds commits instead of resuming every durable
   phase. Repository-create failure can adopt an existing same-name repository
   based only on visibility. Collision protection and crash replay need tests.
3. Timed-out tasks bypass further reconciliation; failed cloud deletion is logged
   but never retried. Durable cleanup reconciliation is still needed.
4. The daemon does not consume SSE: a parser/client exists, but monitoring only
   polls. Activity excerpts therefore remain sparse.
5. The live task's terminal summary was the generic `Agent task completed.` even
   though its manifest contained a useful summary. Final-output capture needs
   correction.
6. Live restart was tested with terminal tasks, not a running cloud turn. Full
   crash injection and concurrency-race coverage remain unverified.

The earlier claim that the entire implementation was complete was too strong.
These results establish a working task/artifact path, not production readiness.
