"""注目するAIサービス一覧（config/services.yml）を使った判定。すべてローカル処理で追加費用なし。

- match_services: 投稿の本文・リンク先からサービス名を見つける（タグ付け）
- detect_events:  資金調達・買収・提携・新機能などの「動き」を見分ける
- official_by_profile: プロフィールのリンク先が公式サイト＋名前に会社名 → 公式アカウントとみなす
- build_queries:  探索の検索語（サービス名×発表・調達・買収）を作る
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Catalog:
    def __init__(self, data: dict | None):
        data = data or {}
        self.categories: dict = data.get("categories", {}) or {}
        self.services: list[dict] = [s for s in (data.get("services") or []) if s.get("name")]
        self.event_query: str = (data.get("event_query") or "").strip()
        self._svc_re = []
        for s in self.services:
            parts = []
            for a in s.get("aliases") or [s["name"]]:
                a = str(a)
                # 英数字の名前は単語の途中に一致させない（"Qodo" が "Qodor" に当たらない等）。大文字小文字は区別する
                left = r"(?<![A-Za-z0-9])" if re.match(r"[A-Za-z0-9]", a) else ""
                right = r"(?![A-Za-z0-9])" if re.search(r"[A-Za-z0-9]$", a) else ""
                parts.append(left + re.escape(a) + right)
            self._svc_re.append(re.compile("|".join(parts)))
        self._dom_owners: dict[str, int] = {}
        for s_ in self.services:
            for d in {x.lower() for x in s_.get("domains") or []}:
                self._dom_owners[d] = self._dom_owners.get(d, 0) + 1
        self._events = []
        for key, ev in (data.get("events") or {}).items():
            words = [str(w) for w in ev.get("words") or []]
            if words:
                rx = re.compile("|".join(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])"
                                         if re.fullmatch(r"[A-Za-z .\-]+", w) else re.escape(w)
                                         for w in words), re.I)
                self._events.append((key, ev.get("label", key), rx))

    # ---- タグ付け
    def match_services(self, texts: list[str], domains: list[str] = ()) -> list[dict]:
        text = "\n".join(t for t in texts if t)
        out = []
        for s, rx in zip(self.services, self._svc_re):
            hit = bool(rx.search(text))
            if not hit and domains:
                # リンク先でのタグ付けは、そのサービス専用のドメインだけ（openai.com などは複数製品で共有のため使わない）
                own = [x for x in s.get("domains") or [] if self._dom_owners.get(x.lower()) == 1]
                hit = any(_dom_match(d, own) for d in domains)
            if hit:
                out.append({"name": s["name"], "company": s.get("company", ""),
                            "category": s.get("category", ""),
                            "category_label": self.categories.get(s.get("category", ""), ""),
                            "tier": s.get("tier", "")})
        return out

    def detect_events(self, texts: list[str]) -> list[str]:
        text = "\n".join(t for t in texts if t)
        return [label for _, label, rx in self._events if rx.search(text)]

    # ---- 一次情報・公式アカウント
    def domains(self) -> list[str]:
        return sorted({d.lower() for s in self.services for d in s.get("domains") or []})

    def official_by_profile(self, user: dict | None) -> dict | None:
        """プロフィールのリンク先が公式サイトで、ユーザー名に会社名・サービス名が入っていれば公式とみなす。
        なりすまし対策として、組織認証かフォロワー1,000人以上を条件にする。"""
        if not user:
            return None
        fol = (user.get("public_metrics") or {}).get("followers_count", 0)
        if user.get("verified_type") not in ("business", "government") and fol < 1000:
            return None
        doms = [_domain(u) for u in profile_urls(user)]
        if not doms:
            return None
        # 社員の個人アカウント（表示名に社名を入れがち）を公式と誤認しないよう、ユーザー名（@の後ろ）だけで照合する
        handle = _squash(user.get("username", ""))
        for s in self.services:
            if not any(_dom_match(d, s.get("domains") or []) for d in doms):
                continue
            keys = {_squash(s.get("company", "")), _squash(s["name"])} - {""}
            if any(k in handle for k in keys):
                return {"service": s["name"], "company": s.get("company", ""), "domain": doms[0]}
        return None

    # ---- 探索の検索語
    def build_queries(self, extra_terms: list[str] = (), suffix: str = "", max_len: int = 480) -> list[str]:
        terms = [str(s["query"]).strip() for s in self.services if s.get("query")]
        terms += [str(t).strip() for t in extra_terms if str(t).strip()]  # 自分で追いかけたい語（X検索の書き方のまま）
        ev = self.event_query
        tail = " ".join(x for x in (ev, suffix) if x)
        queries, cur = [], []

        def q(items):
            def wrap(x):
                quoted = x.startswith('"') and x.endswith('"') and x.count('"') == 2
                return x if quoted or " " not in x else f"({x})"
            return "(" + " OR ".join(wrap(x) for x in items) + ") " + tail

        for t in terms:
            if cur and len(q(cur + [t])) > max_len:
                queries.append(q(cur))
                cur = []
            cur.append(t)
        if cur:
            queries.append(q(cur))
        return queries

    def mentions_any(self, text: str) -> bool:
        return any(rx.search(text) for rx in self._svc_re)


def load_catalog(path: Path | None = None) -> Catalog:
    p = path or ROOT / "config" / "services.yml"
    if not p.exists():
        return Catalog({})
    return Catalog(yaml.safe_load(p.read_text(encoding="utf-8")))


def profile_urls(user: dict) -> list[str]:
    urls = []
    for u in ((user.get("entities") or {}).get("url") or {}).get("urls") or []:
        if u.get("expanded_url"):
            urls.append(u["expanded_url"])
    if not urls and user.get("url") and "t.co/" not in user["url"]:
        urls.append(user["url"])
    return urls


def _domain(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _dom_match(d: str, doms: list[str]) -> bool:
    d = d.lower()
    return any(d == x.lower() or d.endswith("." + x.lower()) for x in doms)


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9ぁ-んァ-ヴ一-龥]", "", (s or "").lower())
