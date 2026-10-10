"""サンプルデータで判定ロジックが期待どおりか確認する。  python -m unittest discover tests"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestCurate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = Path(tempfile.mkdtemp()) / "feed.json"
        subprocess.run([sys.executable, str(ROOT / "scripts/curate.py"),
                        "--fixture", str(ROOT / "tests/fixtures/sample.json"), "--out", str(out)],
                       check=True, capture_output=True)
        cls.feed = json.loads(out.read_text(encoding="utf-8"))

    def find(self, word):
        return next(c for c in self.feed["clusters"] if any(word in p["text"] for p in c["posts"]))

    def test_official_release_confirmed(self):
        c = self.find("Orion-2")
        self.assertEqual(c["verdict"], "confirmed")
        self.assertEqual(c["post_count"], 3)

    def test_arxiv_versions_merge_and_confirm(self):
        c = self.find("world model")
        self.assertEqual(c["post_count"], 2)
        self.assertEqual(c["verdict"], "confirmed")

    def test_bait_excluded(self):
        for w in ("保存推奨", "THIS CHANGES EVERYTHING", "神ツール"):
            self.assertEqual(self.find(w)["verdict"], "excluded", w)

    def test_rumor_unverified(self):
        self.assertEqual(self.find("LEAK")["verdict"], "unverified")

    def test_single_news_promising(self):
        self.assertEqual(self.find("ガイドライン")["verdict"], "promising")


if __name__ == "__main__":
    unittest.main()
