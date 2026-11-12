#!/usr/bin/env python3
"""クラウド（GitHub Actions）で毎朝動く。実績を取って、暗号化した画面を docs/ に出す。

Macに依存する処理（OCR＝バナーの文字読み取り）は入れない。ここは数字だけ。

  METAX_TOKEN と VIEW_PASSWORD を環境変数で渡す。
  python3 cloud/build.py --days 28 --out docs
"""
import argparse
import base64
import collections
import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import meta_api as M                                   # noqa: E402


def encrypt(payload, password):
    """パスワードを知らないと読めない形にする。見かけのログインではない。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=250000).derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, json.dumps(payload, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"salt": b64(salt), "iv": b64(iv), "data": b64(ct), "iter": 250000}


def collect(tok, since, until, min_spend):
    accounts, out = M.paged("me/adaccounts",
        {"fields": "id,name,insights.date_preset(last_7d){spend}"}, tok), []
    for a in accounts:
        ins = (a.get("insights") or {}).get("data") or []
        if not ins or float(ins[0].get("spend") or 0) < min_spend:
            continue
        print(f"- {a.get('name','')} ({a['id']})", flush=True)
        import types
        args = types.SimpleNamespace(account=a["id"], since=since, until=until,
                                     cv_action=None, no_images=True, spent_only=True,
                                     out=os.path.join("/tmp/meta", a["id"]))
        try:
            rows = M.cmd_insights(args)
        except SystemExit as e:
            print(f"   ! {e}", flush=True)
            out.append({"id": a["id"], "name": a.get("name", ""), "error": str(e)[:160],
                        "cost": 0, "cv": 0, "cvUnset": True, "lps": []})
            continue
        rows = [r for r in rows if (r.get("消化") or 0) > 0]
        unset = bool(rows) and all(r.get("CV未設定") for r in rows)
        bylp = collections.defaultdict(lambda: {"cost": 0.0, "cv": 0.0, "imp": 0.0,
                                                "click": 0.0, "n": 0, "ads": []})
        for r in rows:
            d = bylp[r.get("LP") or "(LPが取れず)"]
            d["cost"] += r["消化"]
            d["cv"] += 0 if r.get("CV未設定") else (r["CV"] or 0)
            d["imp"] += r.get("表示") or 0
            d["click"] += r.get("クリック") or 0
            d["n"] += 1
            d["ads"].append({"name": r["広告名"], "set": r["広告セット"], "cost": r["消化"],
                             "cv": 0 if r.get("CV未設定") else (r["CV"] or 0),
                             "ctr": r.get("CTR"), "link": r.get("管理画面", "")})
        lps = []
        for u, d in sorted(bylp.items(), key=lambda kv: -kv[1]["cost"]):
            d["ads"].sort(key=lambda x: (-(x["cv"] or 0), -x["cost"]))
            lps.append({"url": u, **{k: d[k] for k in ("cost", "cv", "imp", "click", "n")},
                        "ads": d["ads"][:12]})
        out.append({
            "id": a["id"], "name": a.get("name", ""), "error": "",
            "cost": sum(r["消化"] for r in rows),
            "cv": 0 if unset else int(sum((r["CV"] or 0) for r in rows if not r.get("CV未設定"))),
            "cvUnset": unset,
            "cvName": next((r.get("CVの種類") for r in rows if r.get("CVの種類")), ""),
            "noTracking": (not unset and not any(r.get("CV系イベント有無") for r in rows)
                           and bool(rows)),
            "zeroCost": 0 if unset else sum(l["cost"] for l in lps if not l["cv"]),
            "lps": lps,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--min-spend", type=float, default=1000)
    ap.add_argument("--out", default="docs")
    a = ap.parse_args()

    tok = os.environ.get("META_ACCESS_TOKEN", "").strip()
    pw = os.environ.get("VIEW_PASSWORD", "").strip()
    if not tok:
        sys.exit("META_ACCESS_TOKEN が無い")
    if not pw:
        sys.exit("VIEW_PASSWORD が無い")

    until = dt.date.today() - dt.timedelta(days=1)
    since = until - dt.timedelta(days=a.days - 1)
    accounts = collect(tok, str(since), str(until), a.min_spend)

    payload = {"period": f"{since}〜{until}",
               "ranAt": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
               "accounts": accounts}
    os.makedirs(a.out, exist_ok=True)
    tpl = open(os.path.join(HERE, "page.html"), encoding="utf-8").read()
    html = tpl.replace("__ENC__", json.dumps(encrypt(payload, pw), ensure_ascii=False))
    with open(os.path.join(a.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(a.out, ".nojekyll"), "w") as f:
        f.write("")
    known = [x for x in accounts if not x["cvUnset"]]
    c = sum(x["cost"] for x in known)
    v = sum(x["cv"] for x in known)
    print(f"\n{len(accounts)}アカウント / 設定済み{len(known)}: 消化{c:,.0f}円 CV{v} "
          + (f"CPA{round(c/v):,}円" if v else "CPA—"))
    print(f"→ {a.out}/index.html")


if __name__ == "__main__":
    main()
