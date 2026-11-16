## Change

What user problem does this solve? Link the issue and state what stays out of scope.

## Evidence

Paste commands and actual results. Mark skipped checks and explain why.

- Python tests (include Hermes commit):
- Node syntax/help and `npm pack --dry-run`:
- Live checks, only if explicitly opted in (model, CLI version, trials), or “not run”:

## Regression and failure paths

Which test fails before this fix? What happens on discovery, process, or model-selection failure?

## Compatibility and security

Describe effects on macOS/Linux, Python/Node minimums, executable overrides, and existing model names.
Does this touch credentials, workspace permissions, or tool approvals? Explain the boundary and tests; do not weaken the deny list.

## Documentation

Link updated usage/contributor docs, or explain why none need changes. Remove secrets and private prompts from all evidence.
