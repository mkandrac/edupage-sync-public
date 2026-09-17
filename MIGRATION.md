# Safe migration

## 1. Create a new repository

Create a **new empty Public repository**, with default branch `main`. Upload only
this reviewed source snapshot. Do not fork, import, mirror or change the visibility
of the previous private repository. Its old history, logs and artifacts must stay private.
The source ZIP excludes Git history and runtime data.

## 2. Configure privately

In **Settings > Secrets and variables > Actions > Secrets**, create:

| Secret | Value |
| --- | --- |
| `EDUPAGE_CONFIG_JSON` | Private configuration based on `config.example.json` |
| `EDUPAGE_ACCOUNT_1_USERNAME` | First account login |
| `EDUPAGE_ACCOUNT_1_PASSWORD` | First account password |
| `EDUPAGE_ACCOUNT_2_USERNAME` | Second account login, if used |
| `EDUPAGE_ACCOUNT_2_PASSWORD` | Second account password, if used |
| `GMAIL_USERNAME` | The existing state mailbox |
| `GMAIL_APP_PASSWORD` | The exact existing Gmail app password |

Add a second account object to `accounts` when needed. Preserve each **existing
account key** exactly, together with the school subdomain and display name. Replace
the configuration's `username_secret` and `password_secret` references with the
generic names above. Use the same capture cutoff as the old deployment, expressed
as a local ISO datetime without an offset. Do not deploy the example cutoff.

Keep `require_existing_state=true`. This makes a missing state an error rather than
silently starting a new history. The Gmail subject, label, encryption format, salt
and key derivation are unchanged. A different app password cannot decrypt the old
session ciphertext. Do not bootstrap merely because the repository moved.

Do not put private JSON into repository Variables, commits, issues, screenshots,
workflow inputs or comments. Only the enable/disable switches belong in Variables.
No secrets can be recovered from GitHub's old secret values: re-enter them from your
existing trusted local setup. Local Keychain entries must use the generic secret
names if environment variables are not provided.

Under **Variables**, initially set both `EDUPAGE_ENABLED` and
`EDUPAGE_KEEPALIVE_ENABLED` to `false`. Keep Actions debug logging disabled.

## 3. Switch without competing writers

1. Disable the old repository's sync and keepalive workflows. Wait for all running
   or queued jobs to finish or be cancelled. Preserve that repository as Private.
2. Confirm the latest private Gmail state exists and retains the processed-event
   history. The new and old repositories must never write state concurrently:
   GitHub concurrency groups coordinate only inside a single repository.
3. Set the new `EDUPAGE_ENABLED=true`. Under **Actions > EduPage > Run workflow**,
   choose branch `main`, mode `sync`, slot `main` and run once.
4. Verify the private RAW reports successful collection for every configured school,
   and that the new state retains previous processed IDs and valid encrypted sessions.
   A green job alone is not the complete migration check.
5. Confirm the existing parent-brief automation still reads archived MAIN RAW from
   `EduPage/System`. The format and subject remain compatible; don't duplicate the
   separate brief automation or change its recipients during this migration.
6. Test one manual `keepalive`. After success, set `EDUPAGE_KEEPALIVE_ENABLED=true`
   if you want the two-hour schedule. Monitor session survival; two hours is a starting
   interval, not a measured guarantee.

Rollback: first set the new enable switch to false and wait for its active jobs to
finish, then re-enable the old private workflows if billing and sessions permit it.
Both deployments use the same state format. Never overwrite state with an empty file.

## What the snapshot does not contain

No original commit history, workflow run logs, artifacts, personal configuration,
captured messages, passwords or session ciphertext. No production run has been
triggered by preparing this snapshot. Live collection must be checked after setup.
