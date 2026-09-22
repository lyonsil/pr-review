import unittest, importlib.util, os, sys, json, tempfile, io, contextlib
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ledger_tools as lt

spec = importlib.util.spec_from_file_location("anchor", os.path.join(HERE, "pr-review-anchor.py"))
anchor = importlib.util.module_from_spec(spec); spec.loader.exec_module(anchor)


def tmpdir():
    return tempfile.mkdtemp()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class TestPrimitives(unittest.TestCase):
    def test_anchor_reexports_the_moved_primitives(self):
        self.assertIs(anchor.parse_changed_lines, lt.parse_changed_lines)
        self.assertIs(anchor.fix_slot, lt.fix_slot)
        self.assertEqual(anchor.SEVERITIES, lt.SEVERITIES)

    def test_write_ledger_is_atomic_and_keeps_a_backup(self):
        d = tmpdir(); p = os.path.join(d, "ledger.json")
        write(p, '{"schema": 2, "items": []}')
        lt.write_ledger(p, {"schema": 2, "items": [1]}, "ingest")
        self.assertEqual(json.load(open(p))["items"], [1])
        self.assertEqual(json.load(open(p + ".bak-ingest"))["items"], [])
        self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")], [])

    def test_load_json_names_the_file_and_rejects_a_non_object_root(self):
        with self.assertRaisesRegex(lt.LedgerError, "ledger file not found: /nope"):
            lt.load_json("/nope")
        p = write(os.path.join(tmpdir(), "l.json"), "[1]")
        with self.assertRaisesRegex(lt.LedgerError, "root must be a JSON object"):
            lt.load_json(p)

    def test_read_lines_errors_past_eof_and_on_a_missing_file(self):
        d = tmpdir(); write(os.path.join(d, "a.ts"), "one\ntwo\n")
        self.assertEqual(lt.read_lines(d, "a.ts", 1, 2), ["one", "two"])
        with self.assertRaisesRegex(lt.LedgerError, "beyond end of file"):
            lt.read_lines(d, "a.ts", 2, 3)
        with self.assertRaisesRegex(lt.LedgerError, "not found"):
            lt.read_lines(d, "gone.ts", 1, 1)
        with self.assertRaisesRegex(lt.LedgerError, "line_end 1 is before line 2"):
            lt.read_lines(d, "a.ts", 2, 1)


class TestSchema(unittest.TestCase):
    def test_schema_1_is_rejected_with_a_rerun_instruction(self):
        errs = anchor.validate({"schema": 1, "items": [], "clusters": []})
        self.assertTrue(any("schema 1" in e and "re-run /pr-review" in e for e in errs), errs)

    def test_missing_schema_is_rejected(self):
        errs = anchor.validate({"items": [], "clusters": []})
        self.assertTrue(any("schema" in e for e in errs), errs)
