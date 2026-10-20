# Team plugin marketplace, with pr-review as its first plugin

Status: design approved in conversation 2026-10-20; this written spec awaits review. Implementation
plan to follow.

## Background

`pr-review` installs by cloning to exactly `~/.claude/pr-review/` and symlinking two commands into
`~/.claude/commands/`; both commands hardcode that path 29 times. It is shared with one teammate.
Other teammates have their own commands and skills and want to share them too. Claude Code's
plugin marketplaces are the native mechanism: a git repo holding `.claude-plugin/marketplace.json`
that lists plugins, each installed with `/plugin install <plugin>@<marketplace>`.

## Goals

- One team-private catalog where a teammate can publish a command or skill by opening a PR.
- Teammates install with one command and are never moved onto an author's in-progress work: each
  plugin is pinned to a tag or branch the author chooses.
- Authors and willing testers can try unreleased changes without affecting anyone else.
- `pr-review` becomes the first installable plugin and the worked example others copy.

## Non-goals

- CI for either repo. `claude plugin validate` is run by hand before merging.
- `extraKnownMarketplaces` / `enabledPlugins` in any project's `.claude/settings.json`.
- Moving teammates' existing commands into plugins - each author does that for their own.
- Converting pr-review's commands to skills, moving its scripts into a subdirectory, or setting
  `version`.
- Distribution through claude.ai organization settings.

## Part 1: the index repo, `paranext/claude-plugins`

Private repo in the `paranext` org. Read access is granted to an existing paranext org team; which
team is picked at creation time from `gh api orgs/paranext/teams`, with the operator.

```
claude-plugins/
  .claude-plugin/marketplace.json
  README.md
  CONTRIBUTING.md
```

### marketplace.json

```json
{
  "name": "paratext-team",
  "owner": { "name": "Paratext team" },
  "plugins": [
    {
      "name": "pr-review",
      "description": "Deep PR review pipeline and posting to GitHub/Reviewable",
      "source": { "source": "github", "repo": "lyonsil/pr-review", "ref": "v1.0.0" }
    }
  ]
}
```

- Every entry is an external `github` source with a `ref`. No plugin lives inside the index repo.
- `ref` is a tag (the documented default) or a branch (allowed, e.g. `stable`). The entry `name`
  must equal the plugin manifest's `name`, or install fails.
- Plugins do not set `version`, so the installed version is the commit the ref resolves to; moving
  the ref is what delivers an update.

### Releasing and trying changes

- Tag-pinned plugin: the author tags the plugin repo, then opens a PR on the index changing
  `ref`. Merging that PR is the release; everyone receives it on their next marketplace update.
- Branch-pinned plugin: the author moves the branch; no index PR. Faster, unreviewed.
- Trying unreleased work: `claude --plugin-dir <checkout>` on any branch loads the plugin for that
  session only. The installed, pinned plugin is untouched for that user and everyone else.

### README.md (for installers)

- Prerequisite: git can clone private GitHub repos non-interactively - an SSH key loaded in the
  agent, or `gh auth login` followed by `gh auth setup-git`. Claude Code never prompts for
  credentials, including during background auto-update.
- Prerequisite: membership of the org team with read access to the index.
- Setup: `/plugin marketplace add paranext/claude-plugins`, then
  `/plugin install <plugin>@paratext-team`.
- Updating: `/plugin marketplace update paratext-team`, then `/reload-plugins` or a new session.
  Auto-update is off by default for non-Anthropic marketplaces; it can be turned on under
  `/plugin` > Marketplaces.
- Catalog: a table of the plugins in `marketplace.json`, each with its one-line description and a
  link to its repo.

### CONTRIBUTING.md (for authors)

- The plugin repo must be readable by the team: public, or in the paranext org.
- Skeleton: `.claude-plugin/plugin.json` plus `commands/*.md` and/or `skills/<name>/SKILL.md` at
  the repo root. New work should prefer skills.
- `plugin.json`: `name` (kebab-case, not a reserved name), `description`, and `author`. `author`
  is a single object; list several authors as one comma-separated string in `author.name`, e.g.
  `"Matt Lyons, Ira Hopkinson"`. Do not set `version`.
- Bundled scripts are referenced as `"${CLAUDE_PLUGIN_ROOT}/<path>"`, double-quoted. Persistent
  state goes under `"${CLAUDE_PLUGIN_DATA}"`; never write into the plugin root, which is replaced
  on every update. Neither variable exists in the Bash tool's environment - they are substituted
  into the Markdown text, so only text Claude Code loads as a command, skill or agent gets them.
- Before opening a PR: `claude plugin validate <plugin-dir>`, and a real session with
  `claude --plugin-dir <plugin-dir>`.
- Releasing: tag (or move the release branch), then the index PR described above. Run
  `claude plugin validate .` on the index before merging.

## Part 2: pr-review as a plugin

The repo root is the plugin root. Scripts, tests, `DECISIONS.md` and `docs/` stay where they are.

### Manifest

New `.claude-plugin/plugin.json`:

```json
{
  "name": "pr-review",
  "description": "Deep PR review pipeline and posting to GitHub/Reviewable",
  "author": { "name": "Matt Lyons, Ira Hopkinson" },
  "repository": "https://github.com/lyonsil/pr-review"
}
```

`commands/` is found by the default scan; the manifest does not set `commands`. Installed, the
commands are `/pr-review:pr-review` and `/pr-review:pr-review-post`; the bare `/pr-review` and
`/pr-review-post` still resolve while nothing else claims those names.

### Script paths in the commands

Every `~/.claude/pr-review/<script>` in `commands/pr-review.md` (16) and
`commands/pr-review-post.md` (13) becomes `"${CLAUDE_PLUGIN_ROOT}/<script>"`, always in double
quotes - including in assignments such as `A="${CLAUDE_PLUGIN_ROOT}/pr-review-anchor.py"`. Claude
Code substitutes the path into the text before the model reads it, with forward slashes on Windows;
the quotes keep a path containing spaces one word. After the change, no
`~/.claude/pr-review` remains in `commands/`.

### The refuter brief template

`refuter-brief.tmpl.md` reaches refuter subagents through `pr-review-anchor.py brief`, not through
Claude Code, so `${CLAUDE_PLUGIN_ROOT}` would arrive unexpanded. Its three
`~/.claude/pr-review/pr-review-wt.sh` references become a `{{WT_SCRIPT}}` placeholder that
`render_brief` in `ledger_tools.py` fills with the absolute path of `pr-review-wt.sh` beside
itself, the same way `TEMPLATE_PATH` already locates the template. This works installed, under
`--plugin-dir`, and from a plain checkout. Rendered paths are double-quoted in the template.

### providers.json

Both inline Python snippets in `commands/pr-review-post.md` (read, and atomic write) change
`os.path.expanduser('~/.claude/pr-review/providers.json')` to the substituted
`"${CLAUDE_PLUGIN_DATA}/providers.json"`, passed into the snippet as a quoted string. The prose at
step 3 of provider selection names the new location. `CLAUDE_PLUGIN_DATA` survives plugin updates
and is deleted on uninstall from the last scope unless `--keep-data` is passed. A `--plugin-dir`
session gets its own data directory, so a development session starts with no remembered
providers; that is acceptable.

`.gitignore` keeps its `providers.json` line so an old checkout's file is never committed.

### README and DECISIONS

- README: the Install section is replaced by marketplace install (pointing at the index README),
  development via `--plugin-dir`, and a one-time migration for existing users: copy
  `~/.claude/pr-review/providers.json` into the plugin's data directory, then delete the two
  `~/.claude/commands/pr-review*.md` symlinks so they do not shadow the plugin. Requirements
  (bash, python3, git, authenticated `gh`, Reviewable MCP for Reviewable posting) are unchanged.
  The file-list line about the symlinks is updated.
- DECISIONS.md: a new Structural entry records that the plugin replaces the fixed-path install,
  noting the template placeholder and data-dir choices and their cost if wrong. The original
  entry about `~/.claude/pr-review` stays as history.

### Tests

- Existing suites pass: `python3 -m unittest test_ledger_tools test_pr_review_anchor
  test_reviewable_backend` and `./pr-review-wt.test.sh`.
- New test in `test_ledger_tools.py`: a rendered brief contains an absolute path to an existing
  `pr-review-wt.sh` and no `~/.claude/pr-review` and no unfilled `{{WT_SCRIPT}}`.
- New check (test or grep in the plan): no `~/.claude/pr-review` in `commands/` or the template.
- `claude plugin validate .` passes on both repos.
- End to end: in a fresh session with the marketplace added and `pr-review@paratext-team`
  installed from the tag, `/pr-review` resolves, `"${CLAUDE_PLUGIN_ROOT}/pr-review-wt.sh" reap`
  runs from the cache path, and the provider-default snippet reads and writes
  `providers.json` under `~/.claude/plugins/data/`.
- Windows: Ira runs the end-to-end check on Windows before the team is told to install (rollout
  step 5).

## Part 3: making `lyonsil/pr-review` public

Public visibility removes the need for per-person read access to pr-review. Control stays with
collaborators (today: `lyonsil` admin, `irahopkinson` write).

- Pull requests: Settings > Features > Pull requests set to "Collaborators only" (GitHub,
  February 2026). Outsiders can view and comment on PRs but not open them. Issues are left as
  they are.
- Ruleset on `master`: block deletion and force-push. No required PR reviews (direct pushes to
  `master` stay allowed for collaborators, as today).
- Ruleset on tags matching `v*`: block deletion and update, so a released tag cannot move under
  installers. Admin bypass allowed.
- Pre-publication check, already done for secrets: no tokens or keys in the 77 commits and
  `providers.json` was never committed. Still to do: skim `DECISIONS.md` and `docs/` for
  internal-only content. Going public also exposes both authors' `@sil.org` commit emails and
  PRs #1 and #2 with their discussion.
- Ira's agreement is a precondition. The operator gives an explicit go-ahead immediately before
  the visibility change; approval of this spec is not that go-ahead.

## Rollout

1. pr-review conversion on branch `team-marketplace`, PR, merge, tag `v1.0.0`.
2. Create `paranext/claude-plugins` (private) with the files above; operator confirms before
   creation and picks the org team.
3. Ira's Windows check and agreement to go public; then make `lyonsil/pr-review` public and apply
   the PR setting and rulesets.
4. Operator switches over: migrate `providers.json`, remove the symlinks, add the marketplace,
   install `pr-review@paratext-team`.
5. Announce to the team with the index README link.
