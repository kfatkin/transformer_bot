# transformer_bot

## Repository owner workflow preference

The repository owner permits committing and pushing changes directly to `main`;
pull requests are not required for this repository. This preference is specific
to `transformer_bot` and does not change the workflow for neighboring repositories.

## Working practices

- Preserve existing local changes and never discard or overwrite unrelated work.
- Fetch the remote and incorporate its latest changes before publishing.
- Review the exact files being committed; do not accidentally include unrelated
  uncommitted application work.
- Run targeted tests, lint, and type checks for code changes. Validate Docker
  startup with environment variables when changing application configuration.
- Keep secrets, local environments, runtime databases, market-data CSVs, and
  caches out of Git. Keep intentional synthetic samples clearly labeled.
- This is an SPX/SPXW paper-evaluation application. Never submit broker orders.

## Calendar protection

- New calendars use a bot-monitored 30% loss trigger based on natural liquidation
  credit versus opening debit before fees, not midpoint prices or broker stop orders.
- Reject a proposed entry already at or beyond that loss; persist its quote audit
  without recording simulated fills.
- Preserve each trade's recorded policy. Legacy trades without a policy field retain
  their original 20% threshold; never rewrite closed outcomes to match a new policy.

## Controlled manual retests

- Preserve the one-normal-entry-per-session cap. Only the separately confirmed
  manual-retest endpoint can create an additional test, after today's normal entry
  and with no active SPX position from any session.
- Keep all quote, market-hour, study-window, configuration, and 30% entry guards.
- Retain the request UUID/reason on uncertain responses; never generate a new ID
  merely to retry. Successful retries must return the existing trade without fills.
- Label retests in persisted metadata, CSV notes, and the dashboard; they are not
  scheduled study observations. Aggregate paper statistics include these tests.
- Back up SQLite before a schema upgrade. Preserve original payloads/audits/settings
  and keep the table migration transactional, including rollback of its DDL.
