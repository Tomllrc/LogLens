# Decisions

Judgment calls made while building LogLens, with the reason for each. Newest last.

## D1. The generic parser is built in Phase 1

The brief listed "the generic parser's timestamp/level/component extraction" as done, but
the repository stopped at step 5b (YAML text formats): nothing extracted fields from free
text. Phase 1 needs it (the pipeline falls back to it on low confidence), so it is built
first, in `loglens/parsers/generic.py`.
