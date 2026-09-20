# qnxpi Combadge agent control layer

This standalone package runs a Pi-local daemon that controls OpenAI-hosted agent sessions. It journals approvals and workflow state in SQLite, exposes a trusted Unix socket to Combadge, and serves a loopback-only review UI. It is not yet wired into the existing Combadge application; do not install its `combadge` entry point over the main application without completing that integration.

## Setup

Install the package, then create `~/.local/state/combadge/openai.key` with mode `0600`. To enable publication, also create `github.pat` with mode `0600` and set `COMBADGE_GITHUB_OWNER`. The GitHub token must be a fine-grained personal token with repository administration and contents write access.

Run `combadge agents start`. The printed tokenized URL is intended to be opened through a tunnel:

```sh
ssh -L 8787:127.0.0.1:8787 qnxpi
```

Administrative commands are `combadge agents start|stop|status|serve`. `combadge start` also ensures the daemon is running. Defaults and all supported environment variables are defined in `combadge.config.AgentConfig`.

The Pi never executes downloaded source. Successful outputs must contain `source.zip` (or `source.tar.gz`/`source.tgz`) and `manifest.json`; archives are checked before extraction. Terminal data and artifacts are retained for 24 hours by default.

## Verification

The suite is stdlib-only:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

See [verification results](E2E-RESULTS.md) for the live happy-path run, regression
coverage, and remaining live-integration validation.

## Recovery and safety

- Start approvals reserve one of three slots in a single SQLite transaction.
  Ambiguous session-creation failures reserve their slot and are discovered by
  installation/task metadata; the daemon never blindly repeats session creation.
  If no session ever appears, an operator must investigate the reserved slot.
- A durable remote-work journal keeps cancellation/deletion retries alive after
  local task retention expires. It retains only task/session identifiers and the
  cleanup action, not the prompt, outputs, or summary. Unresolved remote work
  continues to count against capacity.
- SSE supplies activity excerpts; polling determines completion and reconciles
  missed events. Sandbox network access is explicitly disabled.
- Publication validates all candidate files before creating a repository. Common
  credential/config files are excluded and recognizable embedded private keys or
  tokens block publication. This is defense in depth, not a guarantee of detecting
  every possible secret: review source before approving publication.
- Repository creation carries a unique journaled ownership marker in its
  description. Commit inputs and timestamps are journaled before upload; recovery
  retrieves the deterministic Git object instead of constructing a new commit.
  Unrelated name collisions fail closed.
- Each successful announcement is acknowledged separately. A process crash after
  speech but before acknowledgement can still replay that one announcement;
  exactly-once audio requires receiver-side acknowledgement/idempotency.

Agent-result publication remains limited to the configured personal account.
`tools/publish_release.py` is a separate, explicit operator-only path for
publishing this package itself to an organization; its default mode is read-only.
