# Security

## Reporting

Email the maintainer via the contact in README.md. Do not open a public issue
for security reports.

## History rewrite disclosure

On 2026-11-04 the repository history was rewritten to remove content that
should not have been committed. The rewrite was force-pushed; all reachable
refs (branches, tags) on the default branch point only at the rewritten
history. A fresh clone of this repository contains no pre-rewrite content.

GitHub's object store may continue to serve pre-rewrite objects to callers
that possess their object SHAs until GitHub's garbage collector runs. A
garbage-collection request has been filed with GitHub Support (ticket
reference on file with the maintainer). A credential-pattern scan of the
rewritten reachable history and of the known orphaned objects found no keys,
tokens, or passwords; no personal email addresses are present in the
rewritten reachable history.

If you obtained a copy of this repository while it was briefly public on
2026-11-04 and believe you have pre-rewrite content, please discard it and
notify the maintainer.
