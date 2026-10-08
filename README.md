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
optional keepalive at minute 17 every hour. Timezone-aware schedules handle
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

The worker cannot be launched directly. Collection first tries the saved session.
If EduPage rejects it, the collector logs in with the password and can complete an
email second factor automatically. Keepalive cannot revive an expired session.

## Automatic email 2FA — rollout in progress

**Fresh email 2FA verified for both configured accounts on 2026-10-08**, in
[run 37820819016](https://github.com/mkandrac/edupage-sync-public/actions/runs/37820819016).
Both reported `email_2fa_finish:ok` and `auth_check:email_verified`; saving the new
sessions also succeeded. **Keepalive stays enabled until a subsequent data
collection is verified.** Session restore alone is not evidence of fresh 2FA.

Live check on 2026-10-08 (run `37818992962`): both configured accounts delivered
their email codes, but both polls timed out before code submission. Polling now
reselects the read-only All Mail view and verifies UIDVALIDITY on every iteration,
retaining the original pre-request UID checkpoint. The subsequent live check above passed email retrieval, code submission and
fresh login for both accounts with this change. Keepalive remains enabled while
the subsequent collection check is pending.

The collector uses `edupage-api==0.13.1` to request the email code after EduPage's
countdown, reads it through Gmail IMAP, and submits it in the same login session.
It uses the existing `GMAIL_USERNAME` and `GMAIL_APP_PASSWORD` secrets. EduPage's
2FA destination must match that mailbox; missing or different destinations stop
the attempt. No new secret is required for accounts using that mailbox.

- Schools are processed sequentially under the workflow's existing concurrency lock.
- Each school requests at most one successfully sent email per run. Countdown
  rejections are retried at most twice, within a 210-second overall budget.
- Only new mailbox UIDs after the request checkpoint are considered. The recipient,
  message date, EduPage sender domain and Gmail DMARC result must match. Codes must
  have a recognized Slovak/Czech/English label; expired, ambiguous, unexpected or
  unrecognized messages are rejected. The real Slovak email template was verified in the live check above.
- One unambiguous code is submitted once. CAPTCHA, unavailable email delivery or
  a required app-only confirmation still need intervention; this is not a bypass.
- Messages are read without marking them seen. Codes and message bodies are never
  written to state, RAW or public logs. Only allowlisted status codes are logged.
- The normal RAW/processed-event history is preserved. The separate brief automation
  and calendar integration are not changed by this authentication update.

### Verify independently of keepalive

Run the workflow manually on `main` with **mode `auth-check`**, or run
`python run.py auth-check` locally with the same private configuration. This mode
ignores saved sessions and performs fresh logins for all configured accounts. It
saves successful sessions without collecting messages, sending RAW or advancing
processed-event IDs. It may trigger an EduPage app notification before requesting
the email alternative; no manual confirmation is expected for the email path.

Interpret public diagnostics:

| Status | Meaning |
| --- | --- |
| `auth_check:email_verified` | Fresh login completed using an emailed code. |
| `auth_check:password_only` | Fresh login succeeded without 2FA; the email path was not exercised. |
| `auth_check:ok` plus `state_save:ok` | All configured accounts authenticated and state was saved. |
| `email_2fa_finish:recipient_unverified` | EduPage did not identify the configured Gmail destination. |
| `email_2fa_wait:mail_unmatched` | New EduPage mail arrived but did not satisfy validation/parsing rules. |
| `email_2fa_finish:timeout` | No acceptable code arrived within the time budget. |

After the email path has succeeded for every applicable account and a subsequent
normal sync has sent RAW, set repository variable `EDUPAGE_KEEPALIVE_ENABLED=false`.
The four collection schedules continue unchanged; they can renew authentication
on demand. Keepalive remains available for rollback. Do not regard GitHub's
scheduled start time as a guaranteed deadline; retain a buffer before the brief.

Upstream implementation and live-test notes: [email 2FA support](https://github.com/EdupageAPI/edupage-api/pull/118).

Run offline tests with `python -m unittest discover -v`.
