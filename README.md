# EduPage private collector on a public runner

Generic collector with private configuration. It sends broad RAW data and encrypted
session state to the configured Gmail mailbox. Choosing relevant items and sending a
parent brief is a separate automation; this project does not configure that automation.

## Privacy boundary

- No family configuration, credentials, state, session cookies or captured data belong in Git.
- `run.py` discards **all worker stdout and stderr**, including dependency tracebacks
  and direct file-descriptor writes. Public logs contain only fixed job status messages.
- Workflows upload no artifacts, caches or captured data. The temporary runner may
  contain private output during execution; it is discarded with the runner.
- Secrets are supplied only to the collection step. Checkout, installation and offline
  tests run without account credentials. Checkout does not persist its token.
- Only scheduled runs and manual runs from `main` can access the collector. There are
  no pull-request triggers. Review code and dependency changes before merging to main;
  code running on main can use the repository secrets.
- Failure details may appear in private Gmail RAW, never in the public worker log.
  A failure before RAW creation has only a generic public status.

See [MIGRATION.md](MIGRATION.md) for the private-to-public transfer.

## Schedule and startup

Default schedules use Europe/Bratislava: MORNING RAW at 09:00, NOON RAW at 12:00,
EARLY RAW at 16:00, MAIN RAW at 18:30,
optional keepalive at minute 17 every two hours. Timezone-aware schedules handle
daylight-saving changes, but dispatch can still be delayed by GitHub.

Every slot captures broad incremental RAW, including a zero-item status email.
The separate morning/noon brief sends only explicitly important or urgent messages.
The early brief considers all RAW collected that day, so homework captured earlier
is not lost. The main brief merges all daily RAW and repeats relevant items even if
already sent in a morning or early brief: it is the complete daily reference.

**All jobs are disabled by default** until repository variable `EDUPAGE_ENABLED`
is exactly `true`. Scheduled keepalive additionally requires
`EDUPAGE_KEEPALIVE_ENABLED=true`. A manual keepalive can be tested before enabling
its schedule. Setting `EDUPAGE_ENABLED=false` stops new collector jobs, but does
not cancel an already running job.

Standard `macos-15` runners in public repositories are eligible for free Actions
usage; larger runners have different billing. Public scheduled workflows can be
disabled after 60 days without repository activity. Monitor the last successful
RAW/state rather than assuming schedules guarantee delivery.

- [GitHub runner documentation](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [Scheduled workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Timezone support](https://github.blog/changelog/2026-03-19-github-actions-late-march-2026-updates/)

## Local commands

Use Python 3.11. Install `requirements.txt`, provide private environment variables,
then run `python run.py sync`, `python run.py keepalive`, or on your trusted Mac,
`python run.py bootstrap --account YOUR_EXISTING_ACCOUNT_KEY`.

The worker cannot be launched directly. No new interactive OTP flow is implemented:
bootstrap still fails if EduPage requires an unsupported second factor. Keepalive
cannot revive an expired session. This migration does not fix authentication issues.

Run offline tests with `python -m unittest discover -v`.
