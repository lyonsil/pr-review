# Team Marketplace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make pr-review an installable Claude Code plugin and list it in a private team marketplace, `paratext-team`, hosted at `paranext/claude-plugins`.

**Architecture:** The pr-review repo root becomes the plugin root (one new manifest). Commands reach bundled scripts through `"${CLAUDE_PLUGIN_ROOT}"` and keep state in `"${CLAUDE_PLUGIN_DATA}"`. The refuter brief, which Claude Code never substitutes, gets the script path from `ledger_tools.py` itself. A separate index repo holds only `marketplace.json` plus docs, and each entry pins a plugin repo to a tag or branch.

**Tech Stack:** Python 3 stdlib (`unittest`), bash, git, `gh`, Claude Code 2.1.304 (`claude plugin validate`, `--plugin-dir`).

**Spec:** `docs/specs/2026-10-20-team-marketplace-design.md`

## Global Constraints

- Marketplace name `paratext-team`; owner name `Paratext team`; index repo `paranext/claude-plugins`, private.
- Plugin name `pr-review`; `author` is `{ "name": "Matt Lyons, Ira Hopkinson" }`; no `version` anywhere.
- Index entry for pr-review: `{"source": "github", "repo": "lyonsil/pr-review", "ref": "v1.0.0"}`.
- Every `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}` in Markdown is inside double quotes.
- No `~/.claude/pr-review` remains in `commands/` or `refuter-brief.tmpl.md`.
- Default branch of `lyonsil/pr-review` is `master`.
- Outward-facing steps need the operator's go-ahead at the time they happen: opening the PR, pushing the tag, creating the index repo, granting team access, changing visibility. Making the repo public additionally needs Ira's agreement first.
- Test command for this repo: `python3 -m unittest test_ledger_tools test_pr_review_anchor test_reviewable_backend test_plugin_layout && ./pr-review-wt.test.sh`

## Review Focus

1. A plugin root containing spaces (a Windows profile such as `C:/Users/First Last/`): every substituted path, and `$A` once it holds one, must stay a single shell word. Pinned by `test_plugin_variables_are_double_quoted` and `test_anchor_variable_is_quoted` (Task 2), and by `test_brief_quotes_the_worktree_script` (Task 1).
2. An existing user's remembered providers silently vanish after the switch, so `/pr-review-post` asks again or picks a different backend. Covered by the README migration step (Task 2) and the operator switch-over (Task 6).
3. Old `~/.claude/commands/pr-review*.md` symlinks shadowing the plugin and still using the old paths. Covered by the README (Task 2) and Task 6's check that `/pr-review` resolves to the plugin.
4. A teammate without non-interactive git credentials, for whom add/update fails with no prompt. Covered by the index README prerequisites (Task 4).
5. A tag cut before the plugin manifest landed, which installs a plugin with no `pr-review` name. Covered by Task 3 tagging only after merge, and Task 5's end-to-end install from the tag.

---

### Task 1: Refuter brief finds `pr-review-wt.sh` beside itself

**Files:**
- Modify: `ledger_tools.py` (constants near `TEMPLATE_PATH` ~line 510; `values` in `render_brief` ~line 613)
- Modify: `refuter-brief.tmpl.md:24-26`
- Test: `test_ledger_tools.py` (beside `test_renders_every_placeholder`)

**Interfaces:**
- Produces: module constant `WT_SCRIPT_PATH: str` in `ledger_tools`, the absolute path of `pr-review-wt.sh` in the same directory as `ledger_tools.py`, and template placeholder `{{WT_SCRIPT}}`.

- [ ] **Step 1: Write the failing tests**

```python
    def test_brief_names_the_worktree_script_beside_the_tools(self):
        text, _ = lt.render_brief(brief_ledger(), "C1", CTX, "/s/pr-review/pr13")
        wt = os.path.join(HERE, "pr-review-wt.sh")
        self.assertTrue(os.path.isabs(lt.WT_SCRIPT_PATH))
        self.assertEqual(lt.WT_SCRIPT_PATH, wt)
        self.assertTrue(os.path.exists(wt))
        self.assertIn(wt, text)
        self.assertNotIn("~/.claude/pr-review", text)

    def test_brief_quotes_the_worktree_script(self):
        text, _ = lt.render_brief(brief_ledger(), "C1", CTX, "/s/pr-review/pr13")
        wt = lt.WT_SCRIPT_PATH
        for verb in ("create", "link-deps", "remove"):
            self.assertIn(f'"{wt}" {verb}', text)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest test_ledger_tools -k worktree_script -v`
Expected: both FAIL, with `AttributeError: ... 'WT_SCRIPT_PATH'`.

- [ ] **Step 3: Implement**

Add `WT_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pr-review-wt.sh")` next to `TEMPLATE_PATH`, and `"WT_SCRIPT": WT_SCRIPT_PATH` in `render_brief`'s `values`. In the template, replace each `~/.claude/pr-review/pr-review-wt.sh` (three of them, lines 24-26) with `"{{WT_SCRIPT}}"`, keeping the surrounding backticks.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m unittest test_ledger_tools test_pr_review_anchor test_reviewable_backend -v`
Expected: all PASS. `test_renders_every_placeholder` still passes, so no placeholder is left unfilled.

- [ ] **Step 5: Commit**

```bash
git add ledger_tools.py refuter-brief.tmpl.md test_ledger_tools.py
git commit -m "Fill the refuter brief's worktree script path from the tools' own location"
```

---

### Task 2: pr-review loads as a plugin

**Files:**
- Create: `.claude-plugin/plugin.json`
- Create: `test_plugin_layout.py`
- Modify: `commands/pr-review.md` (16 script paths; 12 `$A` uses)
- Modify: `commands/pr-review-post.md` (13 script paths; 9 `$A` uses; `providers.json` at lines 140, 146, 236 and the closing `" "$REPO"` lines of both snippets)
- Modify: `README.md`, `DECISIONS.md`

**Interfaces:**
- Consumes: `{{WT_SCRIPT}}` from Task 1 (the template is already clean).
- Produces: plugin `pr-review` with commands `/pr-review:pr-review` and `/pr-review:pr-review-post`; state at `"${CLAUDE_PLUGIN_DATA}/providers.json"`.

- [ ] **Step 1: Write the failing layout tests** in `test_plugin_layout.py`, in the style of the other test files (`HERE` from `__file__`, `unittest.TestCase`, runnable with `unittest.main()`)

```python
MARKDOWN = ["commands/pr-review.md", "commands/pr-review-post.md", "refuter-brief.tmpl.md"]

def read(rel):  # repo-relative path -> file text (utf-8)
    with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
        return fh.read()

    def test_manifest_names_the_plugin(self):
        m = json.load(open(os.path.join(HERE, ".claude-plugin", "plugin.json"), encoding="utf-8"))
        self.assertEqual(m["name"], "pr-review")
        self.assertEqual(m["author"], {"name": "Matt Lyons, Ira Hopkinson"})
        self.assertEqual(m["repository"], "https://github.com/lyonsil/pr-review")
        self.assertNotIn("version", m)
        self.assertNotIn("commands", m)        # keep the default commands/ scan

    def test_no_fixed_install_path_remains(self):
        for f in MARKDOWN:
            self.assertNotIn("~/.claude/pr-review", read(f), f)

    def test_plugin_variables_are_double_quoted(self):
        # every ${CLAUDE_PLUGIN_ROOT|DATA} opens a double-quoted word: "${...}/...
        for f in MARKDOWN:
            bad = re.findall(r'(?<!")\$\{CLAUDE_PLUGIN_(?:ROOT|DATA)\}', read(f))
            self.assertEqual(bad, [], f)

    def test_anchor_variable_is_quoted(self):
        for f in MARKDOWN:
            self.assertEqual(re.findall(r'(?<!")\$A\b', read(f)), [], f)

    def test_providers_file_lives_in_plugin_data(self):
        post = read("commands/pr-review-post.md")
        self.assertNotIn("expanduser", post)
        self.assertEqual(post.count('"${CLAUDE_PLUGIN_DATA}/providers.json"'), 3)  # prose + 2 snippets
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest test_plugin_layout -v`
Expected: every test FAILS (no manifest, old paths, unquoted `$A`).

- [ ] **Step 3: Create `.claude-plugin/plugin.json`** with exactly the spec's manifest: `name`, `description` `"Deep PR review pipeline and posting to GitHub/Reviewable"`, `author`, `repository`.

- [ ] **Step 4: Rewrite the command paths**

```bash
sed -i 's#~/\.claude/pr-review/\(pr-review-wt\.sh\|pr-review-anchor\.py\)#"${CLAUDE_PLUGIN_ROOT}/\1"#g; s#python3 \$A\b#python3 "$A"#g' commands/pr-review.md commands/pr-review-post.md
```

Then read every changed line (`git diff -U0 commands/`). The quotes must not land inside an existing pair of double quotes, e.g. `WT=$("${...}/pr-review-wt.sh" create ...)` is fine, but a path already inside `"..."` must not be double-wrapped. Fix any such line by hand.

- [ ] **Step 5: Move `providers.json` to plugin data** in `commands/pr-review-post.md`

  - Line 140 prose: `` `~/.claude/pr-review/providers.json` `` → `` `"${CLAUDE_PLUGIN_DATA}/providers.json"` ``.
  - Read snippet: `path = sys.argv[2]`; its closing line becomes `" "$REPO" "${CLAUDE_PLUGIN_DATA}/providers.json"`.
  - Write snippet: `path = sys.argv[3]`; its closing line becomes `" "$REPO" "<provider>" "${CLAUDE_PLUGIN_DATA}/providers.json"`.
  - The prose introducing the write snippet names the new third argument.

- [ ] **Step 6: Run the layout tests and the full suite**

Run: the Global Constraints test command.
Expected: all PASS, and `pr-review-wt.test.sh` reports no failures.

- [ ] **Step 7: Validate the plugin**

Run: `claude plugin validate .`
Expected: `Validation passed`, or `passed with warnings` where the only warning is the missing `version`, which is intended.

- [ ] **Step 8: Check substitution in a real session**

Run: `claude -p --plugin-dir . 'Without running anything, quote verbatim the first bash code block of the /pr-review:pr-review-post command and the closing line of its provider lookup snippet.'`
Expected: the output shows absolute paths under this repo for the script, and `~/.claude/plugins/data/<id>/providers.json` for the data file, with no literal `${CLAUDE_PLUGIN_`. If the literal survives, stop and report: the spec's premise is wrong.

- [ ] **Step 9: Update README.md and DECISIONS.md**

  - README: the file list drops the "symlinked into ~/.claude/commands/" line and the "edit these files, not the links" paragraph.
  - README: the `providers.json` paragraph names `"${CLAUDE_PLUGIN_DATA}/providers.json"` (on disk, `~/.claude/plugins/data/pr-review-paratext-team/`).
  - README: Install becomes three parts.
    - *Install:* link to the index README. `/plugin marketplace add paranext/claude-plugins`, then `/plugin install pr-review@paratext-team`.
    - *Develop:* `claude --plugin-dir <checkout>`; such a session has its own data directory.
    - *Migrating from the symlink install:* copy `~/.claude/pr-review/providers.json` into the data directory, `rm ~/.claude/commands/pr-review.md ~/.claude/commands/pr-review-post.md`. The checkout can then live anywhere.
  - README: the requirements list is unchanged.
  - DECISIONS.md, new Structural bullet after the existing install bullet:
    - It records: "Installed as a Claude Code plugin from the `paratext-team` marketplace; commands reach scripts via `${CLAUDE_PLUGIN_ROOT}`, the refuter brief via `WT_SCRIPT_PATH`, and `providers.json` lives in `${CLAUDE_PLUGIN_DATA}`."
    - Its *If wrong* line: the template placeholder and two snippet arguments are the only coupling points.

- [ ] **Step 10: Commit**

```bash
git add .claude-plugin/plugin.json test_plugin_layout.py commands/ README.md DECISIONS.md
git commit -m "Package pr-review as a Claude Code plugin"
```

---

### Task 3: Merge and tag `v1.0.0` (operator-gated)

- [ ] **Step 1:** Ask the operator before pushing. Then `git push -u origin team-marketplace` and `gh pr create --base master`. The title is "Package pr-review as a plugin for the paratext-team marketplace". The body summarizes Tasks 1-2, links the spec, and ends with the attribution line.
- [ ] **Step 2:** The operator merges. Then `git switch master && git pull`, and confirm `.claude-plugin/plugin.json` exists on `master`.
- [ ] **Step 3:** Ask the operator, then `git tag -a v1.0.0 -m "First plugin release" && git push origin v1.0.0`.
  Expected: `git ls-remote origin refs/tags/v1.0.0` prints one line.

---

### Task 4: Create the index repo (operator-gated)

**Files (new repo, checked out at a location the operator picks; default `~/claude-plugins`):**
- Create: `.claude-plugin/marketplace.json`, `README.md`, `CONTRIBUTING.md`

- [ ] **Step 1:** Run `gh api orgs/paranext/teams --jq '.[] | "\(.slug)  \(.name)"'`, show the list, and ask the operator which team gets read access. Confirm creation.
- [ ] **Step 2:** `gh repo create paranext/claude-plugins --private --clone --description "Claude Code plugins shared by the Paratext team"`.
  If org permissions refuse it, stop and tell the operator which org admin action is needed.
- [ ] **Step 3:** Write `marketplace.json` exactly as in the spec's Part 1.
- [ ] **Step 4:** Write `README.md` and `CONTRIBUTING.md` with the bullet content of spec Part 1's "README.md" and "CONTRIBUTING.md" sections.
  - CONTRIBUTING gets a copy-paste `plugin.json` skeleton using the multi-author `author.name` form.
  - CONTRIBUTING gets a tag-release example: `git tag -a v1.1.0 -m ... && git push origin v1.1.0`, then the index PR changing `ref`.
  - README's catalog table has the one pr-review row.
- [ ] **Step 5:** `claude plugin validate .`
  Expected: passes. Remote sources are not fetched, so this checks only the index.
- [ ] **Step 6:** Commit "Add the paratext-team marketplace with pr-review" and push to `main`.
- [ ] **Step 7:** `gh api -X PUT orgs/paranext/teams/<slug>/repos/paranext/claude-plugins -f permission=pull`
  Expected: `gh api orgs/paranext/teams/<slug>/repos/paranext/claude-plugins` returns the repo with `pull` permission.

---

### Task 5: End-to-end install from the tag

The operator's own machine is used; the symlinks stay in place for now. Plugin commands are namespaced, so the checks below use `/pr-review:...`.

- [ ] **Step 1:** `claude plugin marketplace add paranext/claude-plugins`, then `claude plugin install pr-review@paratext-team`.
  Expected: install succeeds. `claude plugin list` shows `pr-review@paratext-team`, with its cache path under `~/.claude/plugins/cache/paratext-team/pr-review/`.
- [ ] **Step 2:** Run `"<cache path>/pr-review-wt.sh" reap` and `python3 "<cache path>/pr-review-anchor.py" --help`.
  Expected: the same clean output as the README sanity check.
- [ ] **Step 3:** `claude -p 'Without running anything, quote the provider lookup command from /pr-review:pr-review-post verbatim.'`
  Expected: the path shown is under `~/.claude/plugins/data/` and the script path is under the cache.
- [ ] **Step 4:** Ask Ira to repeat Steps 1-2 on Windows (Git Bash), and to say whether they agree to `lyonsil/pr-review` going public. Record both answers here before Task 7.

---

### Task 6: Operator switch-over

- [ ] **Step 1:** Copy `~/.claude/pr-review/providers.json` into the data directory Task 5 Step 3 printed, if the file exists. Verify with `python3 -m json.tool` on the copy.
- [ ] **Step 2:** `rm ~/.claude/commands/pr-review.md ~/.claude/commands/pr-review-post.md`, after `ls -l` confirms both are symlinks into this repo.
- [ ] **Step 3:** In a new session, `/pr-review` appears in the slash-command list as the plugin's command.
  Expected: the description matches and the source is `pr-review@paratext-team`.

---

### Task 7: Make `lyonsil/pr-review` public (gated on Ira's agreement and the operator's explicit go-ahead)

- [ ] **Step 1:** Skim `DECISIONS.md` and `docs/` for internal-only content (non-public repo names beyond paranext-core and scripture-editors, people's names, hostnames). Report findings to the operator; change nothing without approval.
- [ ] **Step 2:** Get the operator's explicit go-ahead in this same moment, restating that commit emails and PRs #1-2 become public.
- [ ] **Step 3:** Restrict PRs before going public.
  - Run `gh api -X PATCH repos/lyonsil/pr-review -f pull_request_creation_policy=collaborators_only`.
  - Read it back with `gh api repos/lyonsil/pr-review --jq .pull_request_creation_policy`.
  - If the value is refused, have the operator set Settings > General > Features > Pull requests > "Collaborators only" in the UI, then read it back.
- [ ] **Step 4:** Create a branch ruleset with `gh api -X POST repos/lyonsil/pr-review/rulesets --input -`.
  - `name` "protect master", `target` "branch", `enforcement` "active".
  - `conditions.ref_name.include` `["~DEFAULT_BRANCH"]`, `exclude` `[]`.
  - `rules` `[{"type":"deletion"},{"type":"non_fast_forward"}]`.
  - `bypass_actors` `[{"actor_id":5,"actor_type":"RepositoryRole","bypass_mode":"always"}]` (admin).
- [ ] **Step 5:** Create a tag ruleset the same way.
  - `name` "freeze release tags", `target` "tag".
  - `include` `["refs/tags/v*"]`.
  - `rules` `[{"type":"deletion"},{"type":"update"}]`.
  - Same admin bypass.
- [ ] **Step 6:** `gh repo edit lyonsil/pr-review --visibility public --accept-visibility-change-consequences`.
  Expected: `gh api repos/lyonsil/pr-review --jq .visibility` prints `public`, and `gh api repos/lyonsil/pr-review/rulesets --jq '.[].name'` lists both rulesets.
- [ ] **Step 7:** Draft (do not send) a short team announcement.
  - It links the index README and says to run `/plugin marketplace add paranext/claude-plugins` and `/plugin install pr-review@paratext-team`.
  - It invites teammates to add their own plugins per CONTRIBUTING.
  - Hand it to the operator.
