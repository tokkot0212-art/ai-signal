"""X API v2 の最小クライアント（標準ライブラリのみ）。

課金を抑えるため:
  - 検索では expansions を使わず投稿だけを取得（ユーザー情報の重複課金を避ける）
  - 著者情報はキャッシュに無い人だけ /2/users でまとめて取得
  - since_id で前回以降の新着だけを取得
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.x.com/2"
TWEET_FIELDS = "created_at,author_id,public_metrics,entities,lang,note_tweet,referenced_tweets"
USER_FIELDS = "created_at,public_metrics,verified,verified_type,description,profile_image_url,name,username"


class XClient:
    def __init__(self, bearer_token: str):
        self.token = bearer_token
        self.posts_read = 0
        self.users_read = 0

    def _get(self, path: str, params: dict) -> dict:
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "ai-x-curator/1.0",
        })
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:300]
                if e.code == 429 and attempt < 2:
                    reset = int(e.headers.get("x-rate-limit-reset", time.time() + 60))
                    time.sleep(min(max(reset - time.time(), 5), 120))
                    continue
                raise RuntimeError(f"X API {e.code}: {body}") from e
        raise RuntimeError("X API: retries exhausted")

    def search_recent(self, query: str, max_results: int, since_id: str | None = None) -> list[dict]:
        if max_results < 10:
            return []
        params = {
            "query": query,
            "max_results": min(max_results, 100),
            "tweet.fields": TWEET_FIELDS,
            "sort_order": "relevancy",
        }
        if since_id:
            params["since_id"] = since_id
        data = self._get("/tweets/search/recent", params)
        tweets = data.get("data", [])
        self.posts_read += len(tweets)
        return tweets

    def lookup_users(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            data = self._get("/users", {"ids": ",".join(chunk), "user.fields": USER_FIELDS})
            users = data.get("data", [])
            self.users_read += len(users)
            out.extend(users)
        return out


def build_from_queries(handles: list[str], suffix: str, max_len: int = 500) -> list[str]:
    """信頼アカウントを from:a OR from:b ... のクエリに分割（クエリ長制限対策）。"""
    queries, cur = [], []
    for h in handles:
        trial = "(" + " OR ".join(f"from:{x}" for x in cur + [h]) + ") " + suffix
        if len(trial) > max_len and cur:
            queries.append("(" + " OR ".join(f"from:{x}" for x in cur) + ") " + suffix)
            cur = [h]
        else:
            cur.append(h)
    if cur:
        queries.append("(" + " OR ".join(f"from:{x}" for x in cur) + ") " + suffix)
    return queries
