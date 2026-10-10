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

    cred = max(0, min(100, cred))
    age_h = max((now - parse_ts(t["created_at"])).total_seconds() / 3600, 1.0)
    heat = max(0, min(100, round(25 * math.log10(1 + eng / age_h))))

    return {
        "cred": cred, "heat": heat, "tier": tier, "pos": pos, "neg": neg,
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
    kws = [keywords(tweet_text(t)) for t in tweets]

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

    for i in range(n):
        for j in range(i + 1, n):
            a, b = kws[i], kws[j]
            if not a or not b:
                continue
            inter = len(a & b)
            if inter >= 3 and inter / len(a | b) >= 0.4:
                union(i, j)

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
    authors, credible = {}, set()
    for t in group:
        u = users.get(t["author_id"], {})
        name = u.get("username", t["author_id"])
        authors[name] = {"username": name, "name": u.get("name", name), "tier": t["_s"]["tier"]}
        if t["_s"]["cred"] >= 50:
            credible.add(name)

    best = max(t["_s"]["cred"] for t in group)
    heat = max(t["_s"]["heat"] for t in group)
    has_official = any(t["_s"]["tier"] == 1 for t in group)
    has_primary = any("primary" in t["_s"]["flags"] for t in group)
    rumor = any("rumor" in t["_s"]["flags"] for t in group) and not (has_official or has_primary)

    if best < 35 or all("blocked" in t["_s"]["flags"] for t in group):
        verdict = "excluded"
    elif has_official or (has_primary and len(credible) >= 2) or (len(credible) >= 3 and best >= 60 and not rumor):
        verdict = "confirmed"
    elif rumor or best < 50:
        verdict = "unverified"
    else:
        verdict = "promising"

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
        "points": [], "terms": [],
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
- note: 判定理由を1文で
- translations: 日本語以外（lang が ja でない）の投稿だけ、本文の自然な日本語訳。
    意訳しすぎず、数値・固有名詞・URLはそのまま残す。日本語の投稿は含めない

厳守:
- summary には投稿にない事実（数値・日付・企業名）を足さない
- points と terms では一般的な背景知識を使ってよいが、推測は「〜とみられる」「未確認」と明記する
- 噂・リークは summary の冒頭に【未確認】と付ける

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
                        "translations": {"type": "array", "items": {
                            "type": "object",
                            "properties": {"post_id": {"type": "string"}, "ja": {"type": "string"}},
                            "required": ["post_id", "ja"]}},
                    },
                    "required": ["id", "title", "summary", "points", "verdict", "note"],
                },
            }
        },
        "required": ["reviews"],
    },
}


def _call_claude(key: str, model: str, prompt: str) -> list[dict]:
    """ツール呼び出しで構造化された結果を受け取る（自由文JSONの崩れを防ぐ）。"""
    body = json.dumps({
        "model": model,
        "max_tokens": 8000,
        "tools": [REVIEW_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_reviews"},
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
        if b.get("type") == "tool_use" and b.get("name") == "submit_reviews":
            return _normalize_reviews((b.get("input") or {}).get("reviews"))
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
        if hit and hit.get("sig") == sig:
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
            cache[c["id"]] = {"sig": sig, "r": r}
            n += 1
    return n


# ---------------------------------------------------------------- main

def month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", help="サンプルJSON（tweets/users/now）で実行")
    ap.add_argument("--dry-run", action="store_true", help="取得せず再判定のみ")
    ap.add_argument("--out", default=str(FEED))
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config" / "sources.yml").read_text(encoding="utf-8"))
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
            tq = build_from_queries(handles, suffix)
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
            fetched += res
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

    tweets = []
    for t in store.values():
        t = dict(t)
        t["_s"] = score_tweet(t, user_cache.get(t["author_id"]), cfg, now)
        tweets.append(t)

    clusters = [build_cluster(g, user_cache, cfg) for g in cluster(tweets)]
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
            "posts": len(tweets), "clusters": len(clusters), **counts,
            "llm_reviewed": reviewed,
            "month_spend_usd": state["spend"].get(month_key(now), 0.0),
            "month_budget_usd": cfg["budget"]["monthly_usd"],
        },
        "clusters": clusters,
    }
    save_json(Path(args.out), feed)
    if not demo:
        save_json(DATA / "tweets.json", store)
        save_json(DATA / "users.json", {k: v for k, v in user_cache.items()
                                        if any(t["author_id"] == k for t in store.values())})
        save_json(DATA / "state.json", state)
        live = {c["id"] for c in clusters}
        save_json(DATA / "llm_cache.json", {k: v for k, v in llm_cache.items() if k in live})
    print(f"clusters={len(clusters)} {counts}")


if __name__ == "__main__":
    main()
