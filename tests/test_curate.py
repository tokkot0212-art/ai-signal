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

    def test_expert_opinion_is_not_signal(self):
        c = self.find("AGI arrives")
        self.assertEqual(c["verdict"], "unverified")
        self.assertEqual(c["evidence"], "opinion")

    def test_commentary_is_excluded(self):
        c = self.find("評価のやり方がイマイチ")
        self.assertEqual(c["verdict"], "excluded")
        self.assertEqual(c["evidence"], "commentary")

    def test_author_types_labeled(self):
        c = self.find("Introducing Orion-2")
        types = {a["username"]: a["type"] for a in c["authors"]}
        self.assertEqual(types["sample_ailab"], "official")
        self.assertEqual(types["sample_researcher"], "researcher")
        self.assertEqual(c["evidence"], "official_announcement")


class TestQualityAndArchive(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import yaml
        import curate
        self.c = curate
        self.cfg = yaml.safe_load((ROOT / "config/sources.yml").read_text(encoding="utf-8"))

    def test_discovery_gate(self):
        low = {"text": "AI tool", "public_metrics": {"like_count": 1}}
        high = {"text": "AI tool", "public_metrics": {"like_count": 20}}
        arxiv0 = {"text": "AI paper", "entities": {"urls": [{"expanded_url": "https://arxiv.org/abs/2610.1"}]},
                  "public_metrics": {}}
        self.assertFalse(self.c.passes_discovery_gate(low, self.cfg))
        self.assertTrue(self.c.passes_discovery_gate(high, self.cfg))
        self.assertFalse(self.c.passes_discovery_gate(arxiv0, self.cfg))

    def test_ai_relevance(self):
        self.assertTrue(self.c.is_ai_related({"text": "新しいLLMを公開"}, self.cfg))
        self.assertFalse(self.c.is_ai_related({"text": "今日はラーメンを食べた"}, self.cfg))
        self.assertFalse(self.c.is_ai_related({"text": "He said wait"}, self.cfg))

    def test_official_evaluation_kept(self):
        base = {"verdict": "promising", "score": 50, "reasons_pos": [], "reasons_neg": []}
        c = dict(base, reasons_pos=[], reasons_neg=[])
        self.c.apply_evidence(c, "official_evaluation", "英AISIの評価報告")
        self.assertEqual(c["verdict"], "promising")
        d = dict(base, reasons_pos=[], reasons_neg=[])
        self.c.apply_evidence(d, "commentary", "他人の検証への論評")
        self.assertEqual(d["verdict"], "excluded")

    def test_official_eval_override(self):
        self.c.CFG = self.cfg
        self.c.OFFICIAL_EVAL_RE = None
        c = {"verdict": "promising", "score": 50, "reasons_pos": [], "reasons_neg": [], "title": "t",
             "links": [], "posts": [{"id": "1", "text": "英AISIがGPT-6の想定外行動の評価を公開", "lang": "ja"}]}
        self.c.apply_review(c, {"evidence": "commentary", "evidence_note": "紹介", "verdict": "promising",
                                "summary": "s", "points": [], "terms": [], "note": "n"})
        self.assertEqual(c["evidence"], "official_evaluation")
        self.assertEqual(c["verdict"], "promising")

    def test_own_build_not_commentary(self):
        self.c.CFG = self.cfg
        self.c.OFFICIAL_EVAL_RE = None
        self.c.HANDS_ON_RE = None
        c = {"verdict": "promising", "score": 50, "reasons_pos": [], "reasons_neg": [], "title": "t", "links": [],
             "posts": [{"id": "1", "text": "23億パラメータの音声生成AIをブラウザだけで動かすツールを作った", "lang": "ja"}]}
        self.c.apply_review(c, {"evidence": "commentary", "evidence_note": "note記事への誘導", "verdict": "promising",
                                "summary": "s", "points": [], "terms": [], "note": "n"})
        self.assertEqual(c["evidence"], "hands_on_test")
        self.assertEqual(c["verdict"], "promising")

    def test_budget_is_spread_evenly(self):
        from datetime import datetime
        st = {"user_ratio": 0.3}
        now = datetime(2026, 10, 20, 7, 23, tzinfo=self.c.JST)
        p = self.c.plan_run(st, self.cfg, now)
        self.assertAlmostEqual(p["daily_base"], 20 / 31, places=3)       # 10/20〜11/20 は31日
        self.assertLessEqual(p["budget_today"], p["daily_base"] * 1.2 + 1e-9)
        self.assertEqual(p["runs_left"], 4)
        # 予算が残っていても、1日の上限を超えて駆け込みで使わない
        st2 = {"user_ratio": 0.3, "cycle_spend": {"2026-10-20": 0.0}}
        p2 = self.c.plan_run(st2, self.cfg, datetime(2026, 11, 18, 7, 23, tzinfo=self.c.JST))
        self.assertLessEqual(p2["budget_today"], p2["daily_base"] * 1.2 + 1e-9)

    def test_allocation_rotates_and_includes_discovery(self):
        st = {}
        seen_q, disc = set(), 0
        for _ in range(8):
            plan, d = self.c.allocate(20, 2, 0.7, st)
            seen_q.update(q for q, _ in plan)
            disc += d
        self.assertEqual(seen_q, {0, 1})
        self.assertGreater(disc, 0)

    def test_archive_merges_by_post_ids(self):
        from datetime import datetime, timezone
        tmp = Path(tempfile.mkdtemp())
        self.c.ARCHIVE = tmp
        def cl(cid, pids, summary=None, verdict="promising"):
            return {"id": cid, "verdict": verdict, "title": cid, "summary": summary, "points": [], "terms": [],
                    "score": 50, "credibility": 60, "heat": 10, "first_seen": "2026-10-10T01:00:00Z",
                    "latest": "2026-10-10T02:00:00Z", "authors": [{"username": "a"}], "links": [],
                    "posts": [{"id": p, "url": "u", "username": "a", "text": "t"} for p in pids],
                    "post_count": len(pids)}
        now = datetime(2026, 10, 10, 3, tzinfo=timezone.utc)
        self.c.archive([cl("A", ["1"], "要約", "confirmed"), cl("B", ["2"])], now)
        self.c.archive([cl("C", ["1", "2", "3"])], now)  # A と B が統合された
        items = json.loads((tmp / "2026-10.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["summary"], "要約")       # 以前の要約を保持
        self.assertEqual(items[0]["verdict"], "confirmed")  # 格下げしない
        self.assertEqual(set(items[0]["post_ids"]), {"1", "2", "3"})


if __name__ == "__main__":
    unittest.main()
