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

    def _cl(self, cid, pids, first, summary=None, verdict="promising"):
        return {"id": cid, "verdict": verdict, "title": cid, "summary": summary, "points": [], "terms": [],
                "score": 50, "credibility": 60, "heat": 10, "first_seen": first, "latest": first,
                "authors": [{"username": "a"}], "links": [],
                "posts": [{"id": p, "url": "u", "username": "a", "text": "t"} for p in pids],
                "post_count": len(pids)}

    def test_archive_by_day_and_merge(self):
        from datetime import datetime, timezone
        tmp = Path(tempfile.mkdtemp())
        self.c.ARCHIVE = tmp
        now = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
        # 10/10 23:30 JST と 10/11 09:00 JST に初出の話題
        self.c.archive([self._cl("A", ["1"], "2026-10-10T14:30:00Z", "要約", "confirmed"),
                        self._cl("B", ["2"], "2026-10-11T00:00:00Z")], now)
        idx = json.loads((tmp / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(set(idx["days"]), {"2026-10-10", "2026-10-11"})
        self.assertTrue((tmp / "days" / "2026-10-10.md").exists())
        # 翌日の続報で A と B が統合 → 最初に見つけた 10/10 にまとまる
        self.c.archive([self._cl("C", ["1", "2", "3"], "2026-10-11T00:00:00Z")], now)
        d10 = json.loads((tmp / "days" / "2026-10-10.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(len(d10), 1)
        self.assertEqual(d10[0]["summary"], "要約")
        self.assertEqual(d10[0]["verdict"], "confirmed")
        self.assertEqual(set(d10[0]["post_ids"]), {"1", "2", "3"})
        idx = json.loads((tmp / "index.json").read_text(encoding="utf-8"))
        self.assertNotIn("2026-10-11", idx["days"])

    def test_rejudged_topic_removed_from_history(self):
        from datetime import datetime, timezone
        tmp = Path(tempfile.mkdtemp())
        self.c.ARCHIVE = tmp
        now = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
        self.c.archive([self._cl("A", ["1"], "2026-10-11T00:00:00Z", "s")], now)
        op = dict(self._cl("A2", ["1"], "2026-10-11T00:00:00Z", "s", "unverified"), evidence="opinion")
        self.c.archive([op], now)
        idx = json.loads((tmp / "index.json").read_text(encoding="utf-8"))
        self.assertNotIn("2026-10-11", idx["days"])

    def test_bad_merge_is_split_again(self):
        from datetime import datetime, timezone
        tmp = Path(tempfile.mkdtemp())
        self.c.ARCHIVE = tmp
        now = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
        # 誤って統合された記録（投稿1と2）
        self.c.archive([self._cl("AB", ["1", "2"], "2026-10-10T03:00:00Z", "s", "confirmed")], now, {"1", "2"})
        # 正しく分かれた判定
        self.c.archive([self._cl("A", ["1"], "2026-10-10T03:00:00Z", "a", "confirmed"),
                        self._cl("B", ["2"], "2026-10-10T04:00:00Z", "b", "confirmed")], now, {"1", "2"})
        items = json.loads((tmp / "days" / "2026-10-10.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(sorted(tuple(e["post_ids"]) for e in items), [("1",), ("2",)])

    def test_merge_guard_blocks_different_officials(self):
        def c(cid, user, text):
            return {"id": cid, "authors": [{"username": user, "tier": 1}], "posts": [{"text": text}]}
        ids = self.c._guard_group([c("x", "OpenAIDevs", "Codex beta composer predictions"),
                                   c("y", "SakanaAILabs", "artificial life podcast Codex")])
        self.assertEqual(ids, ["x"])

    def test_migrate_monthly_archive(self):
        from datetime import datetime, timezone
        tmp = Path(tempfile.mkdtemp())
        self.c.ARCHIVE = tmp
        e = self.c._archive_entry(dict(self._cl("A", ["1"], "2026-10-09T03:00:00Z", "s"), evidence="reporting"),
                                  datetime.now(timezone.utc))
        old = self.c._archive_entry(self._cl("B", ["2"], "2026-10-09T04:00:00Z", "s"), datetime.now(timezone.utc))
        (tmp / "2026-10.json").write_text(json.dumps({"month": "2026-10", "items": [e, old]}), encoding="utf-8")
        (tmp / "index.json").write_text(json.dumps({"months": {"2026-10": {"count": 1}}}), encoding="utf-8")
        self.c.archive([], datetime(2026, 10, 11, 3, tzinfo=timezone.utc))
        idx = json.loads((tmp / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(idx["days"]["2026-10-09"]["count"], 1)
        self.assertFalse((tmp / "2026-10.json").exists())


if __name__ == "__main__":
    unittest.main()
