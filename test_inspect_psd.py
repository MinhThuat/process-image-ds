"""Regression checks: python3 -m unittest discover -s studio -p test_inspect_psd.py."""
import base64
import math
from pathlib import Path
import tempfile
import unittest

from psd_tools import PSDImage
from psd_tools.psd.descriptor import Bool, Descriptor
from inspect_psd import PSDEncoder, audit_tree, build_report, inspect_layer, transform_details, main
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO


class InspectPSDTests(unittest.TestCase):
    def test_unknown_field_and_later_runs_are_never_silent(self):
        data = {"StyleRun": [{"FontSize": 12}, {"FutureTextSetting": 99}],
                "ParagraphRun": [{"Justification": 0}, {"FutureParagraph": 7}]}
        rows = list(audit_tree(PSDEncoder().encode(data), "text"))
        for suffix in ("StyleRun[1]/FutureTextSetting", "ParagraphRun[1]/FutureParagraph"):
            row = next(r for r in rows if r["path"].endswith(suffix))
            self.assertEqual(row["status"], "CHƯA XÁC MINH")

    def test_disabled_descriptor_retains_every_parameter(self):
        descriptor = Descriptor(name="Hidden", classID=b"FutureFX")
        descriptor[b"enab"] = Bool(False)
        descriptor[b"future"] = Bool(True)
        rows = list(audit_tree(PSDEncoder().encode(descriptor), "fx"))
        row = next(r for r in rows if '"future"' in r["path"] and r["path"].endswith("/value"))
        self.assertEqual(row["status"], "ĐANG TẮT")
        self.assertIs(row["value"], True)

    def test_master_disabled_inherits_and_binary_still_warns(self):
        parent = Descriptor()
        parent[b"masterFXSwitch"] = Bool(False)
        child = Descriptor()
        child[b"enab"] = Bool(True)
        child[b"blob"] = bytes(range(256))
        parent[b"child"] = child
        rows = list(audit_tree(PSDEncoder(True).encode(parent), "fx"))
        enabled = next(r for r in rows if '"enab"' in r["path"] and r["path"].endswith("/value"))
        self.assertEqual(enabled["status"], "ĐANG TẮT")
        blob = next(r for r in rows if r["path"].endswith(':"blob"]'))
        self.assertEqual(blob["status"], "KHÔNG GIẢI MÃ")
        self.assertNotIn("base64", blob["value"])

    def test_cli_strict_and_source_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "small.psd"
            PSDImage.new("RGB", (16, 16)).save(path)
            original = path.read_bytes()
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main([str(path), "--summary", "--strict"]), 2)
                with self.assertRaises(SystemExit):
                    main([str(path), "-o", str(path)])
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(path.with_suffix(".inspect.audit.txt").exists())

    def test_starjedi_fill_and_non_present_effects(self):
        fixture = Path(__file__).resolve().parents[1] / "magnet_tooltool/3 mẫu magnet/MLT312503BN01/Template-40oz.psd"
        if not fixture.exists():
            self.skipTest("Local StarJedi fixture not available")
        report = build_report(fixture)
        layer = next(l for l in report["layers"] if l["name"] == "Jill")
        self.assertEqual(layer["fill_opacity"], 0)
        rows = report["audit"]
        self.assertTrue(any('"dropShadowMulti"' in r["path"] and r["status"] == "ĐANG TẮT" for r in rows))
        self.assertTrue(any('"Leading"' in r["path"] and r["status"] == "CHƯA XÁC MINH" for r in rows))
        self.assertTrue(any(r["path"].startswith("raw_psd/image_resources") for r in rows))

    def test_transform_rotation_reflection_and_singular(self):
        angle = math.radians(37)
        a, b = 2 * math.cos(angle), 2 * math.sin(angle)
        result = transform_details((a, b, -3 * math.sin(angle), 3 * math.cos(angle), 12, 34))
        self.assertAlmostEqual(result["rotation_degrees"], 37)
        self.assertAlmostEqual(result["skew_degrees"], 0)
        self.assertEqual(result["translation"], [12, 34])
        self.assertTrue(transform_details((-1, 0, 0, 1, 0, 0))["reflected"])
        singular = transform_details((0, 0, 0, 1, 0, 0))
        self.assertTrue(singular["singular"])
        self.assertIsNone(singular["rotation_degrees"])

    def test_descriptor_preserves_class_name_and_unknown_disabled_effect(self):
        descriptor = Descriptor(name="Future effect", classID=b"FutureFX")
        descriptor[b"enab"] = Bool(False)
        encoded = PSDEncoder().encode(descriptor)["fields"]
        self.assertEqual(encoded["name"], "Future effect")
        self.assertEqual(encoded["classID"]["ascii"], "FutureFX")
        entry = encoded["_items"]["_entries"][0]
        self.assertEqual(entry["key"]["ascii"], "enab")
        self.assertIs(entry["value"]["fields"]["value"], False)

    def test_binary_payload_is_exact_or_explicitly_summarized(self):
        data = bytes(range(256))
        summary_encoder = PSDEncoder()
        summary = summary_encoder.encode(data, "payload")
        self.assertTrue(summary["payload_omitted"])
        self.assertEqual(summary_encoder.binary_summaries[0]["path"], "payload")
        complete = PSDEncoder(True).encode(data)
        self.assertEqual(base64.b64decode(complete["base64"]), data)
        self.assertEqual(summary["sha256"], complete["sha256"])

    def test_unknown_types_report_issue(self):
        encoder = PSDEncoder()
        encoder.encode(object(), "unknown")
        self.assertEqual(encoder.issues[0]["path"], "unknown")

    def test_full_binary_report_on_small_psd(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "small.psd"
            PSDImage.new("RGB", (16, 16)).save(path)
            report = build_report(path, include_binary=True)
        self.assertFalse(report["issues"])
        self.assertFalse(report["binary_payloads_summarized"])
        self.assertIn("image_resources", report["raw_psd"]["fields"])
        self.assertIn("image_data", report["raw_psd"]["fields"])

    def test_real_psd_all_effects_disabled_effect_and_extra_style_run(self):
        fixture = Path(__file__).resolve().parents[1] / "magnet_tooltool/EFFECT.psd"
        if not fixture.exists():
            self.skipTest("Local EFFECT.psd fixture not available")
        psd = PSDImage.open(fixture)
        layer = next(l for l in psd.descendants() if l.kind == "type")
        effects = list(layer.effects)
        self.assertEqual(len(effects), 10)
        effects[0].descriptor[b"enab"] = Bool(False)
        runs = layer.engine_dict["StyleRun"]["RunArray"]
        # Mutate in memory only; verify no first-run filtering in exported data.
        runs.append(runs[0])
        encoder = PSDEncoder()
        result = inspect_layer(layer, "1", encoder)
        self.assertEqual(len(result["effects"]), 10)
        self.assertIs(result["effects"][0]["properties"]["enabled"], False)
        def engine_items(value):
            return {entry["key"]["fields"]["value"]: entry["value"]
                    for entry in value["fields"]["_items"]["_entries"]}

        engine = engine_items(result["engine_dict"])
        style = engine_items(engine["StyleRun"])
        self.assertEqual(len(style["RunArray"]["fields"]["_items"]), len(runs))
        self.assertFalse(encoder.issues)


if __name__ == "__main__":
    unittest.main()
