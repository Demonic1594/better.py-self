# claude-review-tools

Local review tooling for working on better.py-self with Claude Code. Kept off `master`
(where `.claude/` is gitignored).

- `.claude/skills/pre-pr-review/SKILL.md`: the pre-PR review workflow (local CI gates, then
  the reviewer, then a verdict block in the PR body).
- `.claude/agents/repo-reviewer.md`: the reviewer subagent, with this repository's architecture
  map and hard rules.

Restore into a clone of `master`:

```bash
git fetch origin claude-review-tools && git checkout origin/claude-review-tools -- .claude && git reset -q .claude
```
