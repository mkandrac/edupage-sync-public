# Public development, private operation

The public repository is the canonical source for collector code, fixes, tests,
email-2FA support and documentation. A private runtime can execute a reviewed public
commit without maintaining a divergent copy of the collector.

## Deployment model

Keep a workflow in the private repository. In its checkout step use:

```yaml
- uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
  with:
    repository: mkandrac/edupage-sync-public
    ref: cd57214e42137d02649b101d5ce265dece002659
    persist-credentials: false
```

This checks out the public code into the private job. Credentials come from the
**private repository's** secrets. Pin a full commit SHA, review upstream changes,
then update that pin after offline checks. Do not use a moving public main ref
with production credentials. Develop general fixes in public first; never copy
private configuration or state back upstream.

The separate parent-brief/calendar service is not installed by this repository.
It continues reading the same Gmail RAW and STATE messages.

## Required configuration

Use the seven secret names consumed by the current public workflow:
- EDUPAGE_CONFIG_JSON
- EDUPAGE_ACCOUNT_1_USERNAME
- EDUPAGE_ACCOUNT_1_PASSWORD
- EDUPAGE_ACCOUNT_2_USERNAME
- EDUPAGE_ACCOUNT_2_PASSWORD
- GMAIL_USERNAME
- GMAIL_APP_PASSWORD

Use the same account keys and Gmail credentials as the working deployment.
Preserve encrypted state and processed-event IDs. GitHub cannot return saved
secret values: provision them from your trusted local source, never from logs or
committed files. Older school-specific secret names are not sufficient for the
current generic collector.

Keep workflow permissions at contents: read. Keep the existing output-suppression
wrapper and credential-free offline checks even when running privately.

## Safe cutover

1. Prepare the private workflow with both activation variables unset/false.
   Retire all older private collector, probe and keepalive workflows.
2. Provision the seven private secrets. Check the selected runner's current
   included-minute accounting and account-wide usage in GitHub billing.
3. Pause public collection and wait for all running jobs to finish. Repository
   concurrency locks do NOT coordinate between public and private repositories.
4. Enable private manual execution only. Run auth-check on main, without
   approving the app notification, and verify fresh email authentication for
   every account plus successful state save.
5. Run sync with slot=main. Verify the new RAW has status ok for every school,
   correct account/child mapping, and successful state persistence. A green
   workflow alone is insufficient evidence.
6. Enable the private schedule only after verification. Leave the public
   collector disabled; the public source and community documentation stay live.
7. Verify the next scheduled collection and downstream brief.

For the maintainer's private runtime these controls are:
EDUPAGE_PRIVATE_ENABLED=true permits manual execution;
EDUPAGE_PRIVATE_SCHEDULE_ENABLED=true additionally permits the three daily
collections (09:00 morning, 12:00 noon, 18:00 main, Europe/Bratislava).
Both default to disabled. It has no keepalive or automatic fourth retry run.
Existing public users retain their own deployment and controls.

Example cutover commands (run only after configuration and migration merge):

```bash
gh variable set EDUPAGE_ENABLED --repo mkandrac/edupage-sync-public --body false
# Wait for existing public jobs to finish before continuing.
gh variable set EDUPAGE_PRIVATE_ENABLED --repo mkandrac/edupage-sync --body true
gh variable set EDUPAGE_PRIVATE_SCHEDULE_ENABLED --repo mkandrac/edupage-sync --body false
gh workflow run edupage.yml --repo mkandrac/edupage-sync --ref main -f mode=auth-check
# Inspect the completed run and private state before starting sync.
gh workflow run edupage.yml --repo mkandrac/edupage-sync --ref main -f mode=sync -f slot=main
# Only after both-account RAW verification:
gh variable set EDUPAGE_PRIVATE_SCHEDULE_ENABLED --repo mkandrac/edupage-sync --body true
```

Rollback: set both private activation variables false, wait for in-flight private
jobs to finish, then set public EDUPAGE_ENABLED=true. Never reset Gmail state as a
routine migration step.

## Scheduling and external triggers

Private visibility does not guarantee timely cron delivery. Moving the runtime
does not fix GitHub schedule delays. An external scheduler can invoke the existing
workflow_dispatch endpoint with mode=sync and the intended slot. This is an
optional integration, not an already provisioned service.

Use a fine-grained token restricted to the runtime repository with Actions write
permission; keep it in the scheduler's secret store. Disable the private schedule
variable when external scheduling becomes authoritative. Keep manual execution
enabled, serialize requests and avoid blind retries: dispatch acceptance does not
prove successful collection. Check the resulting run and RAW coverage.

Retain the public project as the source of fixes and publish deployment changes
here so other users can operate either public or private runtimes.
