# UTGen — C Unit-Test Vector Generator

Static analysis of C functions with clang.cindex that auto-generates row-aligned
test vectors — statement / branch / decision / MC/DC coverage, boundary tests and
fault injection — for the TESSY pipeline (consumed via tessy_parse.py).

## Documents

- Spec: `docs/superpowers/specs/2026-07-21-utgen-design.md`
- Implementation plan: `docs/superpowers/plans/2026-07-21-utgen-implementation.md`
- Progress log: `docs/superpowers/plans/PROGRESS.md`

## Status

Under development, one task per run (see progress log).
Full install/usage documentation arrives with Task 17.
