# Gemini Code Assist Review Style Guide

This guide expands Gemini Code Assist's default GitHub review behavior. It does
not replace the default categories. Continue reviewing for correctness,
efficiency, maintainability, and security even when repo-specific rules add
more context.

## Default Review Categories To Preserve

- **Correctness**: logic bugs, edge cases, state regressions, invalid control
  flow
- **Efficiency**: obvious performance regressions, wasted work, pathological
  API or I/O usage
- **Maintainability**: brittle abstractions, unsafe complexity, duplication that
  creates future defects
- **Security**: secret exposure, unsafe deserialization, injection risks,
  missing validation, auth gaps

## Review Priorities

- Prioritize changed behavior over unchanged surroundings.
- Watch provider/API boundaries closely; do not assume model calls are safe,
  cheap, or authorized.
- Prefer findings about experiment isolation, held-out test discipline, resume
  safety, and missing verification.
- Keep style feedback low-noise unless it materially affects future defects.
- Leave one comment per root cause instead of repeating the same concern.

## Output Format

- Start with a short summary of overall risk and review confidence.
- Use inline comments for actionable findings when file and line targets exist.
- Explain why the issue matters and how to fix it.

## Noise Control

- Avoid formatter-only feedback when tooling already enforces it.
- Avoid duplicating linter rules or opening duplicate comments for the same risk.
- Keep repo-specific addenda here instead of bloating `GEMINI.md`.
