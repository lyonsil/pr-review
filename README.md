# pr-review

Tooling for the multi-stage PR review pattern. Two slash commands plus the
deterministic scripts they call.

- `pr-review-wt.sh`      worktree lifecycle: reap | create | remove | scratchpad | link-deps |
  remove-refuters | primary-status
- `pr-review-anchor.py`  ledger validate | reanchor | payload | preview | brief | ingest | excerpt |
  changed-lines | renumber | edit | report (shared logic in `ledger_tools.py`)
- `refuter-brief.tmpl.md`  the refuter's whole contract; `brief` renders it per cluster or item
- `commands/pr-review.md`, `commands/pr-review-post.md`  the two commands; `.claude-plugin/plugin.json`
  makes the repo a Claude Code plugin

`/pr-review-post` publishes through GitHub's review API or through Reviewable's MCP server
(`payload --provider github|reviewable`); a private repo uses GitHub unless told otherwise. A
per-repo choice, once detected or approved by the operator, is remembered in `providers.json` in
the plugin's data directory (`${CLAUDE_PLUGIN_DATA}`, which is
`~/.claude/plugins/data/pr-review-paratext-team/` when installed from the marketplace). It
survives plugin updates and is removed when the plugin is uninstalled.

Spec and plan: paranext-core `docs/superpowers/specs|plans/2026-08-28-pr-review-pipeline*.md`

## Install

pr-review is listed in the Paratext team's plugin marketplace,
[paranext/claude-plugins](https://github.com/paranext/claude-plugins) (private to the team; its
README covers the git credentials Claude Code needs). In Claude Code:

```
/plugin marketplace add paranext/claude-plugins
/plugin install pr-review@paratext-team
```

The commands are `/pr-review:pr-review` and `/pr-review:pr-review-post`; plain `/pr-review` and
`/pr-review-post` work while no other command claims those names. Updates arrive when the
marketplace entry's `ref` moves to a new tag: `/plugin marketplace update paratext-team`, then
`/reload-plugins`.

Requires: `bash`, `python3` (standard library only, no pip installs), `git`, and an authenticated
`gh` CLI (`gh auth status`). Posting through Reviewable also needs its MCP server connected;
GitHub-only use does not.

## Develop

Clone anywhere and load the checkout for one session, leaving the installed version alone:

```bash
claude --plugin-dir /path/to/pr-review
```

Such a session has its own data directory (`~/.claude/plugins/data/pr-review-inline/`), so it
starts with no remembered providers. Tests:
`python3 -m unittest test_ledger_tools test_pr_review_anchor test_reviewable_backend test_plugin_layout && ./pr-review-wt.test.sh`.
Validate the manifest with `claude plugin validate .`.

## Migrating from the symlink install

Earlier versions lived at exactly `~/.claude/pr-review/` with the commands symlinked into
`~/.claude/commands/`. **Do not update that checkout to this version while the symlinks are still
in place:** commands loaded from `~/.claude/commands/` get no plugin path substitution, so
`/pr-review` cannot find its scripts and `/pr-review-post` loses your remembered providers. Install
the plugin and finish these steps first; after that, the checkout can be updated freely.

1. Keep your remembered providers, if you have any:
   ```bash
   mkdir -p ~/.claude/plugins/data/pr-review-paratext-team
   cp ~/.claude/pr-review/providers.json ~/.claude/plugins/data/pr-review-paratext-team/
   ```
2. Remove the symlinks, which would otherwise shadow the plugin's commands with the old copies:
   ```bash
   rm ~/.claude/commands/pr-review.md ~/.claude/commands/pr-review-post.md
   ```
3. The checkout at `~/.claude/pr-review/` is no longer used by the commands; keep it as a
   development checkout or delete it.
