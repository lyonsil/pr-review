import unittest, os, json, re

HERE = os.path.dirname(os.path.abspath(__file__))
MARKDOWN = ["commands/pr-review.md", "commands/pr-review-post.md", "refuter-brief.tmpl.md"]


def read(rel):  # repo-relative path -> file text (utf-8)
    with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
        return fh.read()


class TestPluginLayout(unittest.TestCase):
    def test_manifest_names_the_plugin(self):
        m = json.loads(read(os.path.join(".claude-plugin", "plugin.json")))
        self.assertEqual(m["name"], "pr-review")
        self.assertEqual(m["author"], {"name": "Matt Lyons, Ira Hopkinson"})
        self.assertEqual(m["repository"], "https://github.com/lyonsil/pr-review")
        self.assertNotIn("version", m)
        self.assertNotIn("commands", m)        # keep the default commands/ scan

    def test_no_fixed_install_path_remains(self):
        for f in MARKDOWN:
            self.assertFalse("~/.claude/pr-review" in read(f), f)

    def test_plugin_variables_are_double_quoted(self):
        # Claude Code substitutes these into the text; a plugin root with a
        # space in it must still be one shell word.
        for f in MARKDOWN:
            bad = re.findall(r'(?<!")\$\{CLAUDE_PLUGIN_(?:ROOT|DATA)\}', read(f))
            self.assertEqual(bad, [], f)

    def test_anchor_variable_is_quoted(self):
        for f in MARKDOWN:
            self.assertEqual(re.findall(r'(?<!")\$A\b', read(f)), [], f)

    def test_no_command_runs_grep_with_perl_regexes(self):
        # Git Bash's default locale makes `grep -P` fail and print nothing,
        # which reads as a clean sweep, and macOS grep has no -P. Prose may
        # name `grep -P`; only a command follows it with a quoted pattern.
        cmd = re.compile(r"""\bgrep(?:\s+-[\w-]+)*?\s+(?:-[A-Za-z]*P[A-Za-z]*|--perl-regexp)\s+['"]""")
        for f in MARKDOWN:
            self.assertEqual(cmd.findall(read(f)), [], f)

    def test_no_command_relies_on_set_e(self):
        # `set -e` does not stop a Bash tool call at a failed step, so steps
        # are chained with `&&`. Prose may quote it, but never at a line start.
        for f in MARKDOWN:
            self.assertEqual(re.findall(r"^\s*set\s+(?:-[a-z]*e|-o\s+errexit)", read(f), re.M),
                             [], f)

    def test_every_rerun_hint_names_the_plugin_command(self):
        # A leftover ~/.claude/commands/pr-review.md shadows a plain /pr-review,
        # and that copy cannot find its scripts (README, "Migrating from the symlink install").
        for f in MARKDOWN + ["pr-review-anchor.py", "ledger_tools.py"]:
            self.assertEqual(re.findall(r"re-run `?/pr-review(?![\w:-])", read(f), re.I), [], f)

    def test_providers_file_lives_in_plugin_data(self):
        post = read("commands/pr-review-post.md")
        self.assertFalse("expanduser" in post)
        self.assertEqual(post.count('"${CLAUDE_PLUGIN_DATA}/providers.json"'), 3)  # prose + 2 snippets


if __name__ == "__main__":
    unittest.main()
