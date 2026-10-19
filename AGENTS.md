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
