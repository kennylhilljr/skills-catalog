---
name: zz-canary
description: "Freshness bot canary that holds exactly three seeded faults and is delivered to no consumer. Use when the prompt is the literal token zz-canary-self-test-7f3a9c and nothing else."
metadata:
  catalog-version: "1.0.0"
  catalog-owner: "kennylhilljr"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-consumers: "none"
  catalog-last-verified: "2025-01-01"
  catalog-max-age-days: "30"
---

# zz-canary

## Purpose

This held skill proves that the nightly freshness checks still work (SPEC FRS-18, DESIGN 7.6). Every run must report exactly the three faults below, no more and no fewer. Do not fix them, and do not add links, model IDs, commands or fenced shell blocks: any change to this file changes what the canary expects.

## Seeded faults

1. Dead link: [a page that is never created](https://github.com/kennylhilljr/skills-catalog/blob/main/.held/zz-canary/never-created.md).
2. Retired model ID: claude-3-opus-20240229.
3. Past max age: `catalog-last-verified` is 2025-01-01 and `catalog-max-age-days` is 30.

`tools/catalog.py` leaves this skill out of `catalog.json` and no consumer links it (FRS-19).

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.0.0 | 2026-11-18 | Created in skills-catalog at `3f1d52f` as the freshness canary with three seeded faults (FRS-18) |
