#!/usr/bin/env python3
"""AI X Curator: X の AI 関連投稿を集め、「本物度」と「熱量」で選別して feed.json を出力する。

使い方:
  python scripts/curate.py                     # 本番（環境変数 X_BEARER_TOKEN が必要）
  python scripts/curate.py --fixture tests/fixtures/sample.json   # サンプルデータで動作確認
  python scripts/curate.py --dry-run           # 取得せず、保存済みデータで再判定だけ
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from x_client import XClient, build_from_queries  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FEED = ROOT / "docs" / "data" / "feed.json"

VERDICT_ORDER = ["confirmed", "promising", "unverified", "excluded"]

# 発言者の区分（専門性）と、信頼度への加点
AUTHOR_TYPES = {
    "official":     ("公式", 25),      # 企業・研究機関・政府などの公式アカウント
    "researcher":   ("研究者", 15),    # 大学・研究所・AI企業の研究者、論文著者
    "journalist":   ("記者・メディア", 10),
    "practitioner": ("実務家", 3),     # 自分で作る・動かすエンジニア・開発者
    "commentator":  ("評論・解説", -5), # 他人の成果を論評・解説・まとめる発信者、インフルエンサー
    "individual":   ("個人", 0),
    "promoter":     ("宣伝・情報商材", -15),
}
EXPERT_TYPES = {"official", "researcher", "journalist"}
REVIEW_VERSION = 5   # 判定基準を変えたら上げる（保存済みの判定をやり直す）
AUTHOR_VERSION = 2

# 根拠（エビデンス）の種類と、話題スコアへの加点。シグナルに入れるのは evidence_ok のものだけ
EVIDENCE = {
    "official_announcement": ("公式発表", 10, True),
    "paper_or_data":         ("論文・データ", 8, True),
    "official_evaluation":   ("公式評価", 8, True),   # 公的機関・開発元・独立評価機関による正式な評価（紹介投稿も可）
    "reporting":             ("報道（取材・出典あり）", 5, True),
    "hands_on_test":         ("本人が手を動かした検証", 6, True),  # 自分で作った・動かした・測った
    "expert_analysis":       ("独自データの分析", 3, True),      # 本人が集めた・測ったデータに基づく分析
    "commentary":            ("評論・解説のみ", -20, False),     # 他人の成果や検証を論評・解説・要約しただけ
    "opinion":               ("意見・感想", -10, False),
    "rumor":                 ("噂・リーク", -10, False),
    "promo":                 ("宣伝", -20, False),
}

# ---------------------------------------------------------------- utils

def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tweet_text(t: dict) -> str:
    nt = t.get("note_tweet") or {}
    return nt.get("text") or t.get("text", "")


def tweet_urls(t: dict) -> list[str]:
    ents = (t.get("note_tweet") or {}).get("entities") or t.get("entities") or {}
    out = []
    for u in ents.get("urls", []) or []:
        url = u.get("unwound_url") or u.get("expanded_url") or u.get("url")
        if url:
            out.append(url)
    return out


def norm_url(url: str) -> str | None:
    """記事の同一性判定用にURLを正規化。X内部リンクは対象外。"""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.netloc or "").lower().removeprefix("www.").removeprefix("m.")
    if not host or host in {"x.com", "twitter.com", "t.co", "pic.x.com", "pic.twitter.com"}:
        return None
    path = re.sub(r"/+$", "", p.path or "")
    if host == "arxiv.org":  # abs/pdf・バージョン違いを同一論文として扱う
        m = re.search(r"(\d{4}\.\d{4,5})", path)
        if m:
            return f"https://arxiv.org/abs/{m.group(1)}"
    return urlunparse(("https", host, path, "", "", ""))


def domain_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def is_primary(domain: str, primary: list[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in primary)


EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿\U0001F000-\U0001F2FF]")
NUM_SEN_RE = re.compile(r"[0-9０-９一二三四五六七八九十]+選")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\.\-\+]{2,}")
KATA_RE = re.compile(r"[ァ-ヴー]{3,}")
STOP = set("""the and for with this that from you your are was have has will just new now more about into
than then they them what when how why can not all out our its it's https http www com co amp via more
one two get got make made use using used here there their just like very really also only over
""".split())


def keywords(text: str) -> set[str]:
    text = re.sub(r"https?://\S+", " ", text)
    toks = {w.lower().strip(".-") for w in WORD_RE.findall(text)}
    toks |= set(KATA_RE.findall(text))
    return {t for t in toks if t and t not in STOP and len(t) >= 3}


# ---------------------------------------------------------------- quality gates

AI_RE = None


def is_ai_related(t: dict, cfg: dict) -> bool:
    """本文かリンク先がAIに関係するか（信頼アカウントの雑談を除外するため）。"""
    global AI_RE
    if AI_RE is None:
        words = cfg.get("quality", {}).get("ai_keywords", [])
        parts = []
        for w in words:
            w = str(w)
            if re.fullmatch(r"[A-Za-z0-9\- .]+", w):
                # 英字の短い語（AI, RL など）は単語の途中に一致しないよう前後を区切る
                right = r"(?![A-Za-z])" if len(w) <= 3 else ""
                parts.append(r"(?<![A-Za-z])" + re.escape(w) + right)
            else:
                parts.append(re.escape(w))
        AI_RE = re.compile("|".join(parts), re.I) if parts else re.compile("$^")
    text = tweet_text(t)
    if AI_RE.search(text):
        return True
    return any(is_primary(domain_of(u), cfg["primary_domains"]) for u in tweet_urls(t))


def engagement(t: dict) -> float:
    m = t.get("public_metrics", {}) or {}
    return (m.get("like_count", 0) + 2 * m.get("retweet_count", 0) + 3 * m.get("quote_count", 0)
            + 0.5 * m.get("reply_count", 0) + m.get("bookmark_count", 0))


def trusted_set(cfg: dict) -> set[str]:
    tr = cfg["trusted"]
    return {h.lower() for h in tr.get("tier1", []) + tr.get("tier2", [])}


def passes_discovery_gate(t: dict, cfg: dict) -> bool:
    """キーワード探索で拾った投稿の足切り（ユーザー情報を取る前に反応数だけで判定 → 課金も節約）。"""
    q = cfg.get("quality", {})
    need = q.get("discovery_min_engagement", 15)
    if any(is_primary(domain_of(u), cfg["primary_domains"]) for u in tweet_urls(t)):
        need = min(need, 3)  # 一次情報リンク付きは基準を下げる（ゼロ反応は除外）
    return engagement(t) >= need


# ---------------------------------------------------------------- scoring

def score_tweet(t: dict, user: dict | None, cfg: dict, now: datetime) -> dict:
    text = tweet_text(t)
    low = text.lower()
    uname = (user or {}).get("username", "").lower()
    tiers = cfg["trusted"]
    pos, neg, flags = [], [], set()
    cred = 50

    tier = 0
    if uname in [h.lower() for h in tiers.get("tier1", [])]:
        tier, cred = 1, cred + 35
        pos.append("公式アカウントの発表")
    elif uname in [h.lower() for h in tiers.get("tier2", [])]:
        tier, cred = 2, cred + 20
        pos.append("信頼リストの研究者・記者")

    if (user or {}).get("verified_type") in {"business", "government"}:
        cred += 5
        pos.append("組織認証アカウント")

    a = (user or {}).get("_author") or {}
    atype = "official" if tier == 1 else a.get("type", "")
    if atype in AUTHOR_TYPES and tier == 0:
        label, bonus = AUTHOR_TYPES[atype]
        cred += bonus
        role = a.get("role") or label
        if bonus > 0:
            pos.append(f"発言者：{role}")
        elif bonus < 0:
            neg.append(f"発言者：{role}")
    if a.get("former") and tier == 0:
        cred += 5
        pos.append(f"経歴：{a['former']}")

    urls = tweet_urls(t)
    domains = [domain_of(u) for u in urls if norm_url(u)]
    primary = [d for d in domains if is_primary(d, cfg["primary_domains"])]
    if primary:
        cred += 15
        pos.append(f"一次情報リンク（{primary[0]}）")
        flags.add("primary")
    elif domains:
        cred += 3

    hits = [p for p in cfg["bait_phrases"] if p.lower() in low]
    if NUM_SEN_RE.search(text):
        hits.append("〇選")
    if hits:
        cred -= min(12 * len(hits), 36)
        neg.append("釣り表現：" + "・".join(hits[:3]))
        flags.add("bait")

    if len(EMOJI_RE.findall(text)) > 5:
        cred -= 8
        neg.append("絵文字が過剰")
    if len(re.findall(r"[#＃]\w+", text)) > 3:
        cred -= 8
        neg.append("ハッシュタグ乱用")
    if re.search(r"[!！]{3,}", text):
        cred -= 5
        neg.append("感嘆符の連打")
    letters = re.findall(r"[A-Za-z]", text)
    if len(letters) > 40 and sum(c.isupper() for c in letters) / len(letters) > 0.5:
        cred -= 5
        neg.append("大文字だらけ")

    m = t.get("public_metrics", {})
    likes, rts = m.get("like_count", 0), m.get("retweet_count", 0)
    quotes, replies = m.get("quote_count", 0), m.get("reply_count", 0)
    bookmarks = m.get("bookmark_count", 0)
    eng = likes + 2 * rts + 3 * quotes + 0.5 * replies + bookmarks

    if user:
        um = user.get("public_metrics", {})
        followers = um.get("followers_count", 0)
        following = um.get("following_count", 0)
        if user.get("created_at"):
            age_days = (now - parse_ts(user["created_at"])).days
            if age_days < 90:
                cred -= 20
                neg.append(f"作成{age_days}日の新規アカウント")
            elif age_days < 365 and tier == 0:
                cred -= 5
        if tier == 0 and followers < 500 and eng > 20 * max(followers, 1) and eng > 200:
            cred -= 15
            neg.append("フォロワー数に対し反応が不自然に多い")
            flags.add("anomaly")
        if tier == 0 and followers < 2000 and following > 3 * max(followers, 1):
            cred -= 8
            neg.append("フォロバ狙いの傾向")
        if (user.get("username") or "").lower() in [b.lower() for b in cfg.get("blocklist", [])]:
            cred = 0
            neg.append("ブロックリスト")
            flags.add("blocked")

    if likes > 50 and quotes > likes * 0.5:
        cred -= 5
        neg.append("引用が多く議論・炎上気味")
        flags.add("controversy")

    if any(p.lower() in low for p in cfg["rumor_phrases"]) and not primary and tier != 1:
        cred -= 10
        neg.append("噂・リーク（一次情報なし）")
        flags.add("rumor")

    if not is_ai_related(t, cfg):
        flags.add("off_topic")
        neg.append("AIと関係のない投稿")

    cred = max(0, min(100, cred))
    age_h = max((now - parse_ts(t["created_at"])).total_seconds() / 3600, 1.0)
    heat = max(0, min(100, round(25 * math.log10(1 + eng / age_h))))

    return {
        "cred": cred, "heat": heat, "tier": tier, "pos": pos, "neg": neg, "atype": atype,
        "flags": sorted(flags), "domains": domains, "urls": urls,
    }


# ---------------------------------------------------------------- clustering

def cluster(tweets: list[dict]) -> list[list[dict]]:
    n = len(tweets)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_id = {t["id"]: i for i, t in enumerate(tweets)}
    url_owner: dict[str, int] = {}

    for i, t in enumerate(tweets):
        for u in t["_s"]["urls"]:
            key = norm_url(u)
            if key:
                if key in url_owner:
                    union(i, url_owner[key])
                else:
                    url_owner[key] = i
        for ref in t.get("referenced_tweets", []) or []:
            if ref.get("type") == "quoted" and ref.get("id") in by_id:
                union(i, by_id[ref["id"]])

    # 文面の似ている投稿どうしの統合は、連鎖して無関係な話題までまとまるため廃止。
    # 同じ出来事の統合は、リンク・引用と、Claudeによる話題統合（merge_topics）で行う。

    groups: dict[int, list[dict]] = {}
    for i, t in enumerate(tweets):
        groups.setdefault(find(i), []).append(t)
    return list(groups.values())


def make_title(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[#＃]\w+", "", text)
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"\s+", " ", line)
    return line[:80] + ("…" if len(line) > 80 else "")


def build_cluster(group: list[dict], users: dict, cfg: dict) -> dict:
    group.sort(key=lambda t: t["_s"]["cred"] + t["_s"]["heat"] / 2, reverse=True)
    rep = group[0]
    q = cfg.get("quality", {})
    big = q.get("credible_min_followers", 10000)
    authors, credible = {}, set()
    max_followers = 0
    for t in group:
        u = users.get(t["author_id"], {})
        name = u.get("username", t["author_id"])
        a = u.get("_author") or {}
        atype = t["_s"].get("atype") or ""
        authors[name] = {"username": name, "name": u.get("name", name), "tier": t["_s"]["tier"],
                         "type": atype, "type_label": AUTHOR_TYPES.get(atype, ("", 0))[0],
                         "role": a.get("role", ""), "former": a.get("former", "")}
        fol = (u.get("public_metrics") or {}).get("followers_count", 0)
        max_followers = max(max_followers, fol)
        # 「裏付け」に数えるのは、信頼リスト・一次情報リンク付き・大きめの実績アカウントだけ
        if t["_s"]["cred"] >= 50 and (t["_s"]["tier"] or "primary" in t["_s"]["flags"] or fol >= big
                                      or t["_s"].get("atype") in EXPERT_TYPES):
            credible.add(name)

    best = max(t["_s"]["cred"] for t in group)
    heat = max(t["_s"]["heat"] for t in group)
    has_official = any(t["_s"]["tier"] == 1 for t in group)
    has_primary = any("primary" in t["_s"]["flags"] for t in group)
    rumor = any("rumor" in t["_s"]["flags"] for t in group) and not (has_official or has_primary)

    has_trusted = any(t["_s"]["tier"] for t in group)
    off_topic = all("off_topic" in t["_s"]["flags"] for t in group)
    if best < 35 or off_topic or all("blocked" in t["_s"]["flags"] for t in group):
        verdict = "excluded"
    elif has_official or (has_primary and len(credible) >= 2) or (len(credible) >= 3 and best >= 60 and not rumor):
        verdict = "confirmed"
    elif rumor or best < 50:
        verdict = "unverified"
    elif (has_trusted or len(credible) >= 2
          or any(t["_s"].get("atype") in EXPERT_TYPES and t["_s"]["cred"] >= 55 for t in group)
          or (has_primary and (max_followers >= 1000 or heat >= 10))
          or (max_followers >= big and heat >= 15)):
        verdict = "promising"
    else:
        verdict = "unverified"  # 出どころが小さい・裏付けなし

    corroboration = min(15, 5 * (len(credible) - 1)) if credible else 0
    score = min(100, round(0.6 * best + 0.4 * heat + corroboration))

    links, seen = [], set()
    for t in group:
        for u in t["_s"]["urls"]:
            k = norm_url(u)
            if k and k not in seen:
                seen.add(k)
                d = domain_of(u)
                links.append({"url": u, "domain": d, "primary": is_primary(d, cfg["primary_domains"])})
    links.sort(key=lambda x: not x["primary"])

    pos, neg = [], []
    for t in group:
        pos += [p for p in t["_s"]["pos"] if p not in pos]
        neg += [x for x in t["_s"]["neg"] if x not in neg]
    if len(credible) >= 2:
        pos.insert(0, f"独立した{len(credible)}アカウントが言及")

    posts = []
    for t in group[:8]:
        u = users.get(t["author_id"], {})
        posts.append({
            "id": t["id"],
            "url": f"https://x.com/{u.get('username', 'i')}/status/{t['id']}",
            "username": u.get("username", ""),
            "name": u.get("name", ""),
            "avatar": u.get("profile_image_url", ""),
            "text": tweet_text(t),
            "lang": t.get("lang", ""),
            "text_ja": None,
            "created_at": t["created_at"],
            "metrics": t.get("public_metrics", {}),
            "cred": t["_s"]["cred"],
            "heat": t["_s"]["heat"],
            "pos": t["_s"]["pos"],
            "neg": t["_s"]["neg"],
            "author_type": t["_s"].get("atype", ""),
            "author_role": (u.get("_author") or {}).get("role", ""),
        })

    times = [t["created_at"] for t in group]
    cid = hashlib.sha1(",".join(sorted(t["id"] for t in group)[:3]).encode()).hexdigest()[:10]
    return {
        "id": cid, "verdict": verdict, "title": make_title(tweet_text(rep)), "summary": None,
        "score": score, "credibility": best, "heat": heat,
        "first_seen": min(times), "latest": max(times),
        "authors": list(authors.values())[:10], "post_count": len(group),
        "posts": posts, "links": links[:5],
        "reasons_pos": pos[:6], "reasons_neg": neg[:6], "llm_note": None,
        "points": [], "terms": [], "evidence": None, "evidence_label": None, "evidence_note": None,
    }


# ---------------------------------------------------------------- LLM (optional)

LLM_PROMPT = """あなたはAIニュースの編集者兼ファクトチェック担当です。Xの投稿を話題ごとにまとめたクラスタを渡します。
読者はAIに関心のある日本人の社会人で、専門家ではありません。英語の投稿も日本語で説明してください。

各クラスタについて次を作ってください。
- title: 何が起きたかを30字以内で（誇張・煽りなし、事実だけ）
- summary: 日本語の概要を2〜3文。誰が・何を・いつ・どうしたかを、投稿に書かれている事実だけで書く
- points: 理解を深めるポイントを2〜3個（各60字以内）。
    「何が新しいのか」「なぜ重要か・誰に影響するか」「注意点・まだ分からないこと」の観点から選ぶ
- terms: 読者がつまずきそうな専門用語を最大3個。{"term":"用語","desc":"40字以内のやさしい説明"}
- verdict: confirmed / promising / unverified / excluded のいずれか
    confirmed=公式発表や一次情報で裏付けあり, promising=信頼できそうだが裏付け待ち,
    unverified=噂・誇張・出典不明, excluded=釣り・宣伝・無関係
- evidence: この話題の根拠の種類。次のどれか1つ
    official_announcement=企業・機関の公式発表そのもの / paper_or_data=論文・公開データ・ベンチマーク結果 /
    reporting=記者・メディアが自ら取材した記事や、一次情報を明示した報道 /
    hands_on_test=投稿者本人が実際に手を動かした検証。自分で作った・動かした・測った・試した結果で、
      手順・数値・コード・画面などの具体物があるもの（「〜を作った」「〜で試したら〜だった」）/
    expert_analysis=投稿者本人が集めた・測った独自データに基づく分析（他人のデータの解説は含まない）/
    official_evaluation=AIの技術的な発展に重要な「公式の評価」の発表または紹介。
      例: 政府のAI安全機関（英AISI・米CAISI・日本AISIなど）やNISTの評価報告、開発元のシステムカード・モデルカード・安全性評価、
      METR・Epoch AI・Apollo Researchなど独立評価機関の評価結果、公式ベンチマーク運営者の結果発表。
      紹介役の投稿でも、評価の出どころ（機関名・報告名）が特定できれば commentary ではなくこれにする。
      ただし製品レビュー・個人のベンチマーク感想・ランキングまとめはこれに含めない /
    commentary=他人の発表・製品・検証を論評・解説・要約・感想を述べただけのもの（official_evaluation に当たるものを除く）。
      評論家・解説系インフルエンサー・まとめ記事・「〇〇の検証を見て思うこと」・解説動画の紹介など、
      本人が手を動かしていないものは、肩書きや分かりやすさに関係なくすべてこれ /
    opinion=根拠を示さない意見・感想・予想・つぶやき（専門家・有名人でも根拠がなければこれ） /
    rumor=噂・リーク・出典不明の情報 / promo=宣伝・セミナー告知・アフィリエイト
    迷ったら: 本人が手を動かした証拠（手順・数値・成果物）が投稿から読み取れなければ hands_on_test にしない
- evidence_note: 根拠の中身を20字以内で（例「arXiv論文」「公式ブログ」「日経の取材記事」「本人の感想」）
- note: 判定理由を1文で。誰の発言か（posts の author_type / author_role）と根拠の強さを踏まえる
- translations: 日本語以外（lang が ja でない）の投稿だけ、本文の自然な日本語訳。
    意訳しすぎず、数値・固有名詞・URLはそのまま残す。日本語の投稿は含めない

厳守:
- summary には投稿にない事実（数値・日付・企業名）を足さない
- points と terms では一般的な背景知識を使ってよいが、推測は「〜とみられる」「未確認」と明記する
- 噂・リークは summary の冒頭に【未確認】と付ける
- 意見・感想は summary の冒頭に【意見】と付け、「〜と述べた」のように誰の意見かが分かる書き方にする
- 有名人や専門家の発言でも、根拠が示されていなければ evidence は opinion にする
- 他人の成果を評価・解説するだけの投稿は commentary にする（報道機関の取材記事は reporting）

結果は submit_reviews ツールで提出してください。translations は [{"post_id":"投稿id","ja":"日本語訳"}] の形です。

クラスタ:
"""


REVIEW_TOOL = {
    "name": "submit_reviews",
    "description": "各クラスタの日本語の概要・ポイント・用語・判定・翻訳を提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "points": {"type": "array", "items": {"type": "string"}},
                        "terms": {"type": "array", "items": {
                            "type": "object",
                            "properties": {"term": {"type": "string"}, "desc": {"type": "string"}},
                            "required": ["term", "desc"]}},
                        "verdict": {"type": "string",
                                    "enum": ["confirmed", "promising", "unverified", "excluded"]},
                        "note": {"type": "string"},
                        "evidence": {"type": "string", "enum": list(EVIDENCE)},
                        "evidence_note": {"type": "string"},
                        "translations": {"type": "array", "items": {
                            "type": "object",
                            "properties": {"post_id": {"type": "string"}, "ja": {"type": "string"}},
                            "required": ["post_id", "ja"]}},
                    },
                    "required": ["id", "title", "summary", "points", "verdict", "note", "evidence"],
                },
            }
        },
        "required": ["reviews"],
    },
}


TRANSLATE_TOOL = {
    "name": "submit_translations",
    "description": "各投稿の日本語訳を提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"post_id": {"type": "string"}, "ja": {"type": "string"}},
                    "required": ["post_id", "ja"],
                },
            }
        },
        "required": ["translations"],
    },
}

TRANSLATE_PROMPT = """次のXの投稿（英語など日本語以外）を、自然で読みやすい日本語に翻訳してください。
- 意訳しすぎず、数値・固有名詞・製品名・URLはそのまま残す
- 絵文字やハッシュタグは残してよい
- 全ての投稿について submit_translations ツールで提出する

投稿:
"""


def _call_claude(key: str, model: str, prompt: str, tool: dict = None) -> list[dict]:
    """ツール呼び出しで構造化された結果を受け取る（自由文JSONの崩れを防ぐ）。"""
    tool = tool or REVIEW_TOOL
    field = tool["input_schema"]["required"][0]  # reviews / translations / authors
    body = json.dumps({
        "model": model,
        "max_tokens": 8000,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise urllib.error.URLError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    if resp.get("stop_reason") == "max_tokens":
        print("[llm] output hit max_tokens; some topics may be missing", file=sys.stderr)
    for b in resp.get("content", []):
        if b.get("type") == "tool_use" and b.get("name") == tool["name"]:
            return _normalize_reviews((b.get("input") or {}).get(field))
    return []


def _normalize_reviews(reviews) -> list[dict]:
    """モデルが配列を文字列で返すことがあるので、どちらでも受け取れるようにする。"""
    if isinstance(reviews, str):
        try:
            reviews = json.loads(reviews)
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", reviews, re.S)
            try:
                reviews = json.loads(m.group(0)) if m else []
            except json.JSONDecodeError:
                print("[llm] could not parse reviews string", file=sys.stderr)
                reviews = []
    if isinstance(reviews, dict):
        reviews = [reviews]
    if not isinstance(reviews, list):
        return []
    return [r for r in reviews if isinstance(r, dict)]


MERGE_TOOL = {
    "name": "submit_groups",
    "description": "同じ出来事を扱うクラスタIDのグループを提出する",
    "input_schema": {
        "type": "object",
        "properties": {"groups": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}},
        "required": ["groups"],
    },
}

MERGE_PROMPT = """Xの投稿を話題ごとにまとめたクラスタの一覧です。
「同じ具体的な出来事・発表・ニュース」を扱っているクラスタを見つけて、グループにしてください。
- 同じ発表についての公式投稿・報道・解説・反応は同じグループ
- 同じ会社や同じ分野というだけの別の出来事は、まとめない
- 2つ以上のクラスタを含むグループだけを提出（単独のものは書かない）
- 迷ったらまとめない
submit_groups ツールで提出してください。

クラスタ:
"""


def llm_merge_groups(clusters: list[dict], cfg: dict) -> list[list[str]]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    lc = cfg.get("llm", {})
    if not key or not lc.get("enabled") or len(clusters) < 2:
        return []
    items = [{"id": c["id"], "text": re.sub(r"\s+", " ", c["posts"][0]["text"])[:220],
              "links": [l["domain"] for l in c["links"][:2]]} for c in clusters[:150]]
    body = json.dumps({
        "model": lc.get("model", "claude-haiku-5-5"),
        "max_tokens": 3000,
        "tools": [MERGE_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_groups"},
        "messages": [{"role": "user", "content": MERGE_PROMPT + json.dumps(items, ensure_ascii=False)}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"[llm] merge skipped: {e}", file=sys.stderr)
        return []
    for b in resp.get("content", []):
        if b.get("type") == "tool_use":
            groups = (b.get("input") or {}).get("groups")
            if isinstance(groups, str):
                try:
                    groups = json.loads(groups)
                except json.JSONDecodeError:
                    groups = []
            valid = {c["id"] for c in clusters}
            out = [[str(i) for i in g if str(i) in valid] for g in (groups or []) if isinstance(g, list)]
            return [g for g in out if len(set(g)) >= 2]
    return []


AUTHOR_TOOL = {
    "name": "submit_authors",
    "description": "各アカウントの発言者区分を提出する",
    "input_schema": {
        "type": "object",
        "properties": {"authors": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "type": {"type": "string", "enum": list(AUTHOR_TYPES)},
                "role": {"type": "string"},
                "former": {"type": "string"},
            },
            "required": ["user_id", "type", "role"]}}},
        "required": ["authors"],
    },
}

AUTHOR_PROMPT = """Xのアカウント情報（表示名・自己紹介・フォロワー数・認証種別）から、AI分野の発言者としての区分を判定してください。
- official: 企業・研究機関・大学・政府・メディア企業などの公式アカウント
- researcher: 研究者・教授・論文著者・AI企業の研究職（自己紹介や所属で確認できる場合のみ）
- journalist: 記者・編集者・ニュースメディア
- practitioner: 自分で作る・動かすエンジニア・開発者・研究開発の実務家（自己紹介から手を動かす人と分かる場合）
- commentator: 評論家・解説者・AI系インフルエンサー・まとめ発信者・コンサルタントなど、主に他人の成果を論評・紹介する人
- individual: 上記に当てはまらない個人、所属や専門が分からない人
- promoter: 情報商材・アフィリエイト・副業勧誘・フォロワー稼ぎが目的とみられるアカウント
role: 所属や肩書きを20字以内の日本語で（例「Wharton教授」「OpenAI研究者」「ITmedia記者」）。分からなければ区分名
former: 「元Google」「元OpenAI研究者」など、自己紹介に書かれた主要な前職があれば20字以内。なければ空文字
自己紹介に書かれていない経歴を推測で足さないこと。submit_authors ツールで提出してください。

アカウント:
"""


def classify_authors(user_cache: dict, author_ids: set, authors_db: dict, cfg: dict, now: datetime) -> int:
    key = os.environ.get("ANTHROPIC_API_KEY")
    lc = cfg.get("llm", {})
    if not key or not lc.get("enabled"):
        return 0
    stale = now - timedelta(days=60)
    need = [uid for uid in sorted(author_ids) if uid in user_cache and
            (uid not in authors_db or authors_db[uid].get("_v") != AUTHOR_VERSION
             or parse_ts(authors_db[uid].get("_at", "2000-01-01T00:00:00Z")) < stale)]
    n = 0
    for i in range(0, len(need), 25):
        chunk = need[i:i + 25]
        payload = []
        for uid in chunk:
            u = user_cache[uid]
            payload.append({"user_id": uid, "username": u.get("username", ""), "name": u.get("name", ""),
                            "bio": (u.get("description") or "")[:300],
                            "followers": (u.get("public_metrics") or {}).get("followers_count", 0),
                            "verified_type": u.get("verified_type", "")})
        try:
            res = _call_claude(key, lc.get("model", "claude-haiku-5-5"),
                               AUTHOR_PROMPT + json.dumps(payload, ensure_ascii=False), AUTHOR_TOOL)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            print(f"[llm] author classification skipped: {e}", file=sys.stderr)
            continue
        for r in res:
            uid = str(r.get("user_id"))
            if uid in chunk and r.get("type") in AUTHOR_TYPES:
                authors_db[uid] = {"type": r["type"], "role": (r.get("role") or "")[:24],
                                   "former": (r.get("former") or "")[:24], "_at": iso(now), "_v": AUTHOR_VERSION}
                n += 1
    if need:
        print(f"[llm] classified {n}/{len(need)} authors")
    return n


OFFICIAL_EVAL_RE = None
CFG = None  # main() で設定


def mentions_official_evaluation(c: dict, cfg: dict) -> str | None:
    """公式評価の機関名・資料名が投稿やリンクに出てくれば、その名前を返す。"""
    global OFFICIAL_EVAL_RE
    if OFFICIAL_EVAL_RE is None:
        words = cfg.get("quality", {}).get("official_eval_keywords", [])
        OFFICIAL_EVAL_RE = re.compile("|".join(re.escape(w) for w in words), re.I) if words else re.compile("$^")
    blob = " ".join([p.get("text", "") for p in c.get("posts", [])] + [l.get("url", "") for l in c.get("links", [])])
    m = OFFICIAL_EVAL_RE.search(blob)
    return m.group(0) if m else None


def apply_evidence(c: dict, ev: str, note: str | None):
    """根拠の種類で判定を補正する。意見・噂・宣伝はシグナルに入れない（格下げのみ）。"""
    if ev not in EVIDENCE:
        return
    label, bonus, ok = EVIDENCE[ev]
    c["evidence"], c["evidence_label"], c["evidence_note"] = ev, label, (note or None)
    c["score"] = max(0, min(100, c["score"] + bonus))
    if ev in ("promo", "commentary"):
        c["verdict"] = "excluded"
    elif not ok and c["verdict"] in ("confirmed", "promising"):
        c["verdict"] = "unverified"
    tag = f"根拠：{label}" + (f"（{note}）" if note else "")
    (c["reasons_pos"] if ok else c["reasons_neg"]).insert(0, tag)


def apply_review(c: dict, r: dict):
    c["title"] = (r.get("title") or c["title"]).strip()
    c["summary"] = r.get("summary")
    c["points"] = [p for p in (r.get("points") or []) if isinstance(p, str)][:3]
    c["terms"] = [t for t in (r.get("terms") or [])
                  if isinstance(t, dict) and t.get("term") and t.get("desc")][:3]
    c["llm_note"] = r.get("note")
    tr = r.get("translations") or {}
    if isinstance(tr, list):
        tr = {str(x.get("post_id")): x.get("ja") for x in tr if isinstance(x, dict)}
    if isinstance(tr, dict):
        for p in c["posts"]:
            ja = tr.get(p["id"])
            if isinstance(ja, str) and ja.strip() and p.get("lang") != "ja":
                p["text_ja"] = ja.strip()
    v = r.get("verdict")
    if v in VERDICT_ORDER:
        cur, new = VERDICT_ORDER.index(c["verdict"]), VERDICT_ORDER.index(v)
        # AIは「格下げ」は自由、「格上げ」は 未確認→有望 の1段だけ許可（証拠なしの昇格を防ぐ）
        if new > cur or (c["verdict"] == "unverified" and v == "promising"):
            c["verdict"] = v
    ev, note = r.get("evidence"), r.get("evidence_note")
    # 特例: AIの技術発展に重要な公式評価（AISI・システムカード等）の紹介は、評論扱いにしない
    if ev in ("commentary", "opinion") and CFG is not None:
        hit = mentions_official_evaluation(c, CFG)
        if hit:
            ev, note = "official_evaluation", f"{hit}の評価の紹介"
    apply_evidence(c, ev, note)


def llm_review(clusters: list[dict], cfg: dict, cache: dict) -> int:
    """日本語の概要・ポイント・用語解説を付ける。一度作った概要はキャッシュして再課金しない。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    lc = cfg.get("llm", {})
    if not key or not lc.get("enabled"):
        return 0
    targets = [c for c in clusters if c["verdict"] != "excluded"][: lc.get("max_clusters", 30)]
    todo = []
    for c in targets:
        sig = ",".join(sorted(p["id"] for p in c["posts"]))
        hit = cache.get(c["id"])
        if hit and hit.get("sig") == sig and hit.get("v") == REVIEW_VERSION:
            apply_review(c, hit["r"])  # 投稿の顔ぶれが同じなら前回の概要を再利用
        else:
            todo.append((c, sig))
    n = len(targets) - len(todo)
    batch = lc.get("batch_size", 8)
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        payload = [{
            "id": c["id"],
            "rule_verdict": c["verdict"],
            "links": [l["domain"] for l in c["links"]],
            "posts": [{"id": p["id"], "by": p["username"], "lang": p.get("lang", ""),
                       "author_type": p.get("author_type", ""), "author_role": p.get("author_role", ""),
                       "text": p["text"][:700]} for p in c["posts"][:5]],
        } for c, _ in chunk]
        try:
            results = _call_claude(key, lc.get("model", "claude-haiku-5-5"),
                                   LLM_PROMPT + json.dumps(payload, ensure_ascii=False))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            print(f"[llm] batch skipped: {e}", file=sys.stderr)
            continue
        print(f"[llm] batch {i // batch + 1}: {len(results)}/{len(chunk)} topics reviewed")
        by_id = {c["id"]: (c, sig) for c, sig in chunk}
        for r in results:
            hit = by_id.get(r.get("id"))
            if not hit:
                continue
            c, sig = hit
            apply_review(c, r)
            cache[c["id"]] = {"sig": sig, "r": r, "v": REVIEW_VERSION}
            n += 1
    # 訳は概要の上限に関係なく、ノイズ以外の英語投稿すべてに付ける
    translate_posts([c for c in clusters if c["verdict"] != "excluded"], key, lc, cache)
    return n


def translate_posts(clusters: list[dict], key: str, lc: dict, cache: dict):
    """日本語以外の投稿に日本語訳を付ける。訳は投稿IDごとに保存して再利用する。"""
    tr_cache = cache.setdefault("_translations", {})
    need = []
    for c in clusters:
        for p in c["posts"]:
            if p.get("lang") == "ja" or not p.get("text"):
                continue
            if p.get("text_ja"):
                tr_cache[p["id"]] = p["text_ja"]
            elif p["id"] in tr_cache:
                p["text_ja"] = tr_cache[p["id"]]
            else:
                need.append(p)
    for i in range(0, len(need), 10):
        chunk = need[i:i + 10]
        payload = [{"post_id": p["id"], "text": p["text"][:1200]} for p in chunk]
        try:
            res = _call_claude(key, lc.get("model", "claude-haiku-5-5"),
                               TRANSLATE_PROMPT + json.dumps(payload, ensure_ascii=False), TRANSLATE_TOOL)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            print(f"[llm] translation skipped: {e}", file=sys.stderr)
            continue
        got = {str(r.get("post_id")): r.get("ja") for r in res}
        for p in chunk:
            ja = got.get(p["id"])
            if isinstance(ja, str) and ja.strip():
                p["text_ja"] = ja.strip()
                tr_cache[p["id"]] = p["text_ja"]
        print(f"[llm] translated {sum(1 for p in chunk if p.get('text_ja'))}/{len(chunk)} posts")


# ---------------------------------------------------------------- main

# ---------------------------------------------------------------- archive (history)

ARCHIVE = ROOT / "docs" / "archive"
JST = timezone(timedelta(hours=9))


def archive(clusters: list[dict], now: datetime):
    """「シグナル」（確認済み・有望）を月ごとのファイルに貯める。同じ話題は最新の内容で上書き。"""
    picks = [c for c in clusters if c["verdict"] in ("confirmed", "promising")]
    if not picks:
        return
    by_month: dict[str, list[dict]] = {}
    for c in picks:
        mk = parse_ts(c["first_seen"]).astimezone(JST).strftime("%Y-%m")
        by_month.setdefault(mk, []).append(c)
    index = load_json(ARCHIVE / "index.json", {"months": {}})
    for mk, items in by_month.items():
        path = ARCHIVE / f"{mk}.json"
        data = load_json(path, {"month": mk, "items": []})
        entries = data["items"]
        post_owner = {pid: i for i, e in enumerate(entries) for pid in e.get("post_ids", [])}
        for c in items:
            p0 = c["posts"][0]
            entry = {
                "id": c["id"], "verdict": c["verdict"], "title": c["title"],
                "summary": c.get("summary"), "points": c.get("points", []), "terms": c.get("terms", []),
                "score": c["score"], "credibility": c["credibility"], "heat": c["heat"],
                "first_seen": c["first_seen"], "latest": c["latest"],
                "authors": [a["username"] for a in c["authors"]][:6],
                "author_labels": [{"username": a["username"], "label": a.get("role") or a.get("type_label", ""),
                                   "type": a.get("type", ""), "former": a.get("former", "")}
                                  for a in c["authors"]][:6],
                "evidence": c.get("evidence"), "evidence_label": c.get("evidence_label"),
                "evidence_note": c.get("evidence_note"),
                "links": c["links"][:3],
                "post": {"url": p0["url"], "username": p0["username"], "text": p0["text"][:500],
                         "text_ja": p0.get("text_ja")},
                "post_ids": [p["id"] for p in c["posts"]],
                "post_count": c["post_count"],
                "archived_at": iso(now),
            }
            hits = sorted({post_owner[pid] for pid in entry["post_ids"] if pid in post_owner})
            if hits:
                keep = hits[0]
                old = entries[keep]
                # 以前の要約しかない場合は残す／確認済みは格下げしない
                if not entry["summary"] and old.get("summary"):
                    for k in ("title", "summary", "points", "terms", "evidence", "evidence_label", "evidence_note"):
                        entry[k] = old.get(k)
                if old.get("verdict") == "confirmed":
                    entry["verdict"] = "confirmed"
                entry["first_seen"] = min(old.get("first_seen", entry["first_seen"]), entry["first_seen"])
                entry["post_ids"] = list(dict.fromkeys(old.get("post_ids", []) + entry["post_ids"]))
                entry["post_count"] = max(old.get("post_count", 0), entry["post_count"])
                entries[keep] = entry
                for extra in reversed(hits[1:]):  # 統合された話題の重複を消す
                    entries.pop(extra)
                post_owner = {pid: i for i, e in enumerate(entries) for pid in e.get("post_ids", [])}
            else:
                entries.append(entry)
                for pid in entry["post_ids"]:
                    post_owner[pid] = len(entries) - 1
        entries.sort(key=lambda e: e["first_seen"], reverse=True)
        save_json(path, data)
        index["months"][mk] = {"count": len(entries),
                               "confirmed": sum(e["verdict"] == "confirmed" for e in entries)}
    index["updated_at"] = iso(now)
    save_json(ARCHIVE / "index.json", index)


def month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", help="サンプルJSON（tweets/users/now）で実行")
    ap.add_argument("--dry-run", action="store_true", help="取得せず再判定のみ")
    ap.add_argument("--out", default=str(FEED))
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config" / "sources.yml").read_text(encoding="utf-8"))
    global CFG
    CFG = cfg
    state = load_json(DATA / "state.json", {"since": {}, "spend": {}})
    user_cache = load_json(DATA / "users.json", {})
    store = load_json(DATA / "tweets.json", {})
    now = datetime.now(timezone.utc)
    demo = False
    demo_llm = {}
    note = None

    if args.fixture:
        fx = load_json(Path(args.fixture), {})
        now = parse_ts(fx["now"])
        demo = True
        demo_llm = fx.get("demo_llm", {})
        for k in ("tier1", "tier2"):
            cfg["trusted"][k] = cfg["trusted"].get(k, []) + fx.get("extra_trusted", {}).get(k, [])
        store = {t["id"]: t for t in fx["tweets"]}
        user_cache = {u["id"]: {**u, "_fetched": iso(now)} for u in fx["users"]}
    elif not args.dry_run:
        token = os.environ.get("X_BEARER_TOKEN")
        if not token:
            sys.exit("X_BEARER_TOKEN が未設定です（GitHub の Settings → Secrets に登録してください）")
        b, f = cfg["budget"], cfg["fetch"]
        mk = month_key(now)
        spent = state["spend"].get(mk, 0.0)
        est = (f["trusted_max_results"] + f["discovery_max_results"]) * (b["post_read_usd"] + 0.3 * b["user_read_usd"])
        if spent + est > b["monthly_usd"]:
            note = f"今月の予算上限（${b['monthly_usd']}）に近いため取得をスキップしました"
            print(note)
        else:
            client = XClient(token)
            suffix = "-is:retweet -is:reply"
            handles = cfg["trusted"].get("tier1", []) + cfg["trusted"].get("tier2", [])
            tq = build_from_queries(handles, suffix, max_len=f.get("trusted_query_len", 330))
            per = max(10, f["trusted_max_results"] // max(len(tq), 1))
            fetched = []
            for i, q in enumerate(tq):
                key = f"trusted{i}"
                res = client.search_recent(q, per, state["since"].get(key))
                if res:
                    state["since"][key] = max(res, key=lambda t: int(t["id"]))["id"]
                fetched += res
            q = re.sub(r"\s+", " ", cfg["discovery_query"]).strip()
            res = client.search_recent(q, f["discovery_max_results"], state["since"].get("discovery"))
            if res:
                state["since"]["discovery"] = max(res, key=lambda t: int(t["id"]))["id"]
            kept = [t for t in res if passes_discovery_gate(t, cfg)]
            print(f"discovery: {len(res)} fetched, {len(kept)} passed the gate")
            fetched += kept
            for t in fetched:
                store[t["id"]] = t  # 同じ投稿は最新の反応数で上書き

            stale = now - timedelta(days=f["user_cache_days"])
            need = sorted({t["author_id"] for t in store.values()
                           if t["author_id"] not in user_cache
                           or parse_ts(user_cache[t["author_id"]]["_fetched"]) < stale})
            for u in client.lookup_users(need):
                user_cache[u["id"]] = {**u, "_fetched": iso(now)}
            cost = client.posts_read * b["post_read_usd"] + client.users_read * b["user_read_usd"]
            state["spend"][mk] = round(spent + cost, 4)
            print(f"fetched posts={client.posts_read} users={client.users_read} cost=${cost:.3f} month=${state['spend'][mk]:.2f}")

    # 古い投稿を捨てる
    cutoff = now - timedelta(hours=cfg["fetch"]["keep_hours"])
    store = {k: t for k, t in store.items() if parse_ts(t["created_at"]) >= cutoff}

    # 既に保存されている投稿にも同じ足切りを適用（以前の取得分の掃除）
    trusted = trusted_set(cfg)
    def _keep(t):
        uname = (user_cache.get(t["author_id"], {}) or {}).get("username", "").lower()
        return uname in trusted or passes_discovery_gate(t, cfg)
    dropped = sum(1 for t in store.values() if not _keep(t))
    store = {k: t for k, t in store.items() if _keep(t)}

    authors_db = load_json(DATA / "authors.json", {})
    if demo:
        authors_db = {k: v for k, v in fx.get("demo_authors", {}).items()}
    else:
        try:
            classify_authors(user_cache, {t["author_id"] for t in store.values()}, authors_db, cfg, now)
        except Exception as e:
            print(f"[llm] author classification failed: {e!r}", file=sys.stderr)
    for uid, a in authors_db.items():
        if uid in user_cache:
            user_cache[uid] = {**user_cache[uid], "_author": a}

    tweets = []
    for t in store.values():
        t = dict(t)
        t["_s"] = score_tweet(t, user_cache.get(t["author_id"]), cfg, now)
        tweets.append(t)

    groups = cluster(tweets)
    clusters = [build_cluster(list(g), user_cache, cfg) for g in groups]
    if not demo:
        try:
            raw_by_id = {c["id"]: g for c, g in zip(clusters, groups)}
            cand = sorted([c for c in clusters if c["verdict"] != "excluded"], key=lambda c: -c["score"])
            merges = llm_merge_groups(cand, cfg)
            if merges:
                used, merged = set(), []
                for g in merges:
                    ids = [i for i in dict.fromkeys(g) if i not in used]
                    if len(ids) < 2:
                        continue
                    used.update(ids)
                    merged.append([t for i in ids for t in raw_by_id[i]])
                groups = [g for c, g in zip(clusters, groups) if c["id"] not in used] + merged
                clusters = [build_cluster(list(g), user_cache, cfg) for g in groups]
                print(f"[llm] merged {len(used)} clusters into {len(merged)} topics")
        except Exception as e:
            print(f"[llm] merge failed: {e!r}", file=sys.stderr)
    clusters.sort(key=lambda c: (VERDICT_ORDER.index(c["verdict"]) == 3, -c["score"]))
    llm_cache = load_json(DATA / "llm_cache.json", {})
    if demo:  # サンプルでは事前に用意した概要を表示
        reviewed = 0
        for c in clusters:
            for p in c["posts"]:
                if p["id"] in demo_llm:
                    apply_review(c, demo_llm[p["id"]])
                    reviewed += 1
                    break
    else:
        try:
            reviewed = llm_review(clusters, cfg, llm_cache)
        except Exception as e:  # 概要づくりが失敗しても、取得したニュースは必ず保存する
            print(f"[llm] review failed, continuing without summaries: {e!r}", file=sys.stderr)
            reviewed = sum(1 for c in clusters if c.get("summary"))
    clusters.sort(key=lambda c: (VERDICT_ORDER.index(c["verdict"]) == 3, -c["score"]))

    counts = {v: sum(c["verdict"] == v for c in clusters) for v in VERDICT_ORDER}
    feed = {
        "generated_at": iso(now),
        "demo": demo,
        "note": note,
        "stats": {
            "posts": len(tweets), "clusters": len(clusters), **counts, "dropped_low_quality": dropped,
            "llm_reviewed": reviewed,
            "month_spend_usd": state["spend"].get(month_key(now), 0.0),
            "month_budget_usd": cfg["budget"]["monthly_usd"],
        },
        "clusters": clusters,
    }
    save_json(Path(args.out), feed)
    if not demo:
        try:
            archive(clusters, now)
        except Exception as e:
            print(f"[archive] failed: {e!r}", file=sys.stderr)
        save_json(DATA / "tweets.json", store)
        save_json(DATA / "users.json", {k: {kk: vv for kk, vv in v.items() if kk != "_author"}
                                        for k, v in user_cache.items()
                                        if any(t["author_id"] == k for t in store.values())})
        save_json(DATA / "state.json", state)
        save_json(DATA / "authors.json", authors_db)
        live = {c["id"] for c in clusters}
        pruned = {k: v for k, v in llm_cache.items() if k in live}
        live_posts = set(store.keys())
        pruned["_translations"] = {k: v for k, v in llm_cache.get("_translations", {}).items() if k in live_posts}
        save_json(DATA / "llm_cache.json", pruned)
    print(f"clusters={len(clusters)} {counts}")


if __name__ == "__main__":
    main()
