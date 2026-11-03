---
name: pre-pr-review
description: Review pass before opening a pull request in better.py-self. Runs the local CI gates (ruff 0.12, pyright 1.1.394, pytest, Sphinx with -W), then has the repo-reviewer subagent review the full branch diff against origin/master, iterates until it approves, and records the verdict in the PR description. Use whenever you are about to create a PR in this repository.
license: MIT
---

# Pre-PR review (better.py-self)

Every pull request in this repository gets a reviewed, approved diff **before** it is opened.
The reviewer is the `repo-reviewer` subagent (`.claude/agents/repo-reviewer.md`), which carries
the architecture map and the hard rules from `CLAUDE.md`. CI does not enforce this. It is a
convention the author keeps, which is exactly why it must not be skipped.

These files live on the `claude-review-tools` branch, not on `master`; `.claude/` is gitignored
on `master`. To restore them in a fresh clone:

```bash
git fetch origin claude-review-tools && git checkout origin/claude-review-tools -- .claude && git reset -q .claude
```

## Workflow

1. **Sync and scope the diff**

   ```bash
   git fetch -q origin master
   git diff --stat origin/master...HEAD
   git log --format='%h %an | %s' origin/master..HEAD
   ```

   If the branch has no commits ahead of `origin/master`, stop: there is nothing to review.
   Every commit must be authored as `Demonic1594 <137410379+Demonic1594@users.noreply.github.com>`
   with no Claude trailers; if not, fix the authorship first (see `CLAUDE.md`).

2. **Run the local gates first.** Cheap failures should never reach the reviewer, and these
   match CI exactly.

   ```bash
   # CI pins ruff 0.12; a newer local ruff can disagree, so use the pinned version
   python -m venv /tmp/ruff012 && /tmp/ruff012/bin/pip install -q ruff==0.12.0   # once per session
   /tmp/ruff012/bin/ruff format --check && /tmp/ruff012/bin/ruff check
   PYRIGHT_PYTHON_FORCE_VERSION=1.1.394 pyright discord          # 0 errors required
   find discord -name __pycache__ -exec rm -rf {} +              # stale .pyc after same-size edits
   python -m pytest -q                                           # about 2.5 minutes, all must pass
   (cd docs && sphinx-build -b html -D language=en -n -T -W --keep-going . /tmp/docs_out)
   ```

   Extra gates when the diff touches these areas:

   - **Caches, models, `__slots__`, parsers** (`state.py`, `member.py`, `user.py`, `guild.py`,
     `message.py`): run `python tests/memory_bench.py --members 50000` before and after
     (`git stash` for the before) and put both numbers in the PR body.
   - **Hot paths or persistence** (`gateway.py`, `state.py` parsers, `http.py` `request`,
     persistence): run `python tools/stress_bench.py` (moderate) and report any gate that fails.
   - **JSON handling**: tests must also pass with the stdlib parser, since the non-speed CI legs
     have no orjson. Run the affected tests with
     `python -c "import json,sys,discord.utils as u; u._from_json=json.loads; u._to_json=lambda o: json.dumps(o,separators=(',',':')); import pytest; sys.exit(pytest.main(['-q','<tests>']))"`.
   - **A bug fix**: confirm its regression test fails on the old code. Swap the library files in
     from `git show origin/master:<path>` (or `git stash push -- discord`), run the test, then
     restore.

   Fix everything here before step 3.

3. **Invoke the reviewer** with the Agent tool, `subagent_type: repo-reviewer`. Prompt it with:

   ```
   Pre-PR review of branch <branch name> (base: origin/master).
   Change summary: <one or two sentences: what changed and why>
   Gates run: <which of the step 2 gates ran, and their results>
   ```

   The subagent reads `git diff origin/master...HEAD` itself; do not paste the diff into the
   prompt. If `repo-reviewer` is not registered in this session (agents load at session start),
   use `subagent_type: general-purpose` with `model: haiku`, and give it the body of
   `.claude/agents/repo-reviewer.md` followed by the prompt above.

4. **Handle the verdict**

   - `REQUEST_CHANGES`: **verify each blocker against the code first.** The reviewer runs on a
     small model and can be wrong. Fix every real blocker, re-run the affected gates from
     step 2, then re-run step 3. For a blocker that does not hold, say why in the next review
     prompt so the reviewer can re-check it. Repeat until `APPROVE`. Never open the PR while a
     real blocker is outstanding.
   - `APPROVE`: proceed. Apply the cheap, safe suggestions. For any you skip, say why in the PR
     body.

5. **Record the verdict in the PR description.** The PR body carries the reviewer's final
   report verbatim inside this block, so a reader can see the review actually happened:

   ```markdown
   <!-- pre-pr-review: approved <HEAD sha> -->
   ## Pre-PR review

   <paste the reviewer's final report here>
   ```

   If a later push changes the branch materially (any code change, not only a typo in docs),
   re-run this skill and update the block. `approved <sha>` must name the commit that was
   actually reviewed.

6. **Then follow the normal workflow** from `CLAUDE.md`: open the PR, subscribe to it, merge
   with a merge commit pinned to the tested head SHA once CI is green, and restart the working
   branch from the updated `master`.

## Rules

- Do not skip, summarize away, or forge the reviewer's report. The block must be the
  subagent's actual output for the current HEAD. Nothing checks this, which is why it matters.
- If no reviewer can run at all, say so in the PR body. Do not open the PR silently.
- Release PRs that only bump the version and changelog still get a review. They are quick,
  and the reviewer checks the `versionadded` tags.
