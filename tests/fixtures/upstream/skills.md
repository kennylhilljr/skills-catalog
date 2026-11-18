# Extend Claude with skills

> Fixture page for the upstream watch tests, shaped like the Claude Code skills docs.

## Choose where skills load

| Location | Path | Loads in |
| :- | :- | :- |
| Personal | `~/.claude/skills/<skill-name>/SKILL.md` | All your projects |
| Project | `.claude/skills/<skill-name>/SKILL.md` | This repository only |
| Plugin | `<plugin>/skills/<skill-name>/SKILL.md` | Wherever the [plugin](https://example.com/plugins?utm_source=docs) is enabled |

## Configure skills

### Frontmatter reference

```yaml
---
name: my-skill
# not a heading: inside a fence
---
```

| Field | Required | Description |
| :- | :- | :- |
| `name` | No | Command name shown in the `/` menu. |
| `description` | Recommended | What the skill does and when to use it. |
| `when_to_use` | No | Extra trigger phrases. |
| `allowed-tools` | No | Tools usable without asking. |
| `disable-model-invocation` | No | Set to `true` to prevent automatic loading. |

#### How a skill gets its command name

The folder name, unless `name` is set.

### Types of skill content

Not part of the frontmatter section.

---

[Edit this page](https://example.com/edit/skills.md)

Last updated on November 17, 2026
