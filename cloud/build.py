#!/usr/bin/env python3
"""クラウド（GitHub Actions）で毎朝動く。実績を取って、暗号化した画面を docs/ に出す。

Macに依存する処理（OCR＝バナーの文字読み取り）は入れない。ここは数字だけ。

  METAX_TOKEN と VIEW_PASSWORD を環境変数で渡す。
  python3 cloud/build.py --days 28 --out docs
"""
import argparse
import base64
import collections
import contextlib
import datetime as dt
import io
import json
import os
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


def thumb_data_uri(path, px=200, quality=62):
    """バナーを画面に埋め込める形にする。並べて見比べられる大きさで。"""
    try:
        from PIL import Image
        import io as _io
        im = Image.open(path).convert("RGB")
        im.thumbnail((px, px), Image.LANCZOS)
        b = _io.BytesIO()
        im.save(b, "JPEG", quality=quality, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()
    except Exception:
        return ""


def collect(tok, since, until, min_spend):
    accounts, out = M.paged("me/adaccounts",
        {"fields": "id,name,insights.date_preset(last_7d){spend}"}, tok), []
    for a in accounts:
        ins = (a.get("insights") or {}).get("data") or []
        if not ins or float(ins[0].get("spend") or 0) < min_spend:
            continue
        # 公開リポジトリではActionsのログも公開されるので、
        # クライアント名・LPのURL・CVの名前はログに出さない（IDだけ）
        print(f"- {a['id']}", flush=True)
        import types
        args = types.SimpleNamespace(account=a["id"], since=since, until=until,
                                     cv_action=None, no_images=False, spent_only=True, daily=True,
                                     out=os.path.join("/tmp/meta", a["id"]))
        hidden = io.StringIO()
        try:
            with contextlib.redirect_stdout(hidden):
                rows = M.cmd_insights(args)
        except SystemExit as e:
            print("   ! 取得できず", flush=True)
            out.append({"id": a["id"], "name": a.get("name", ""), "error": str(e)[:160],
                        "cost": 0, "cv": 0, "cvUnset": True, "lps": []})
            continue
        rows = [r for r in rows if (r.get("消化") or 0) > 0]
        unset = bool(rows) and all(r.get("CV未設定") for r in rows)

        # 広告×LP単位にまとめ、日別の数字を配列で持つ
        ads = collections.OrderedDict()
        for r in rows:
            lp = r.get("LP") or "(LPが取れず)"
            key = (lp, r["広告名"])
            m = ads.setdefault(key, {"lp": lp, "name": r["広告名"], "sets": [], "img": "",
                                     "link": r.get("管理画面", ""), "d": {}})
            day = m["d"].setdefault(r.get("日付") or "?", [0.0, 0.0, 0.0, 0.0])
            day[0] += r["消化"]
            day[1] += r.get("表示") or 0
            day[2] += r.get("クリック") or 0
            day[3] += 0 if r.get("CV未設定") else (r["CV"] or 0)
            if r.get("広告セット") and r["広告セット"] not in m["sets"]:
                m["sets"].append(r["広告セット"])
            if not m["img"] and r.get("画像"):
                m["img"] = r["画像"]

        bylp = collections.defaultdict(list)
        for m in ads.values():
            bylp[m["lp"]].append(m)

        lps = []
        for u, items in bylp.items():
            tot = sum(sum(v[0] for v in m["d"].values()) for m in items)
            for m in items:
                m["cost"] = sum(v[0] for v in m["d"].values())
            items.sort(key=lambda m: -m["cost"])
            for i, m in enumerate(items[:8]):
                m["img"] = thumb_data_uri(m["img"]) if (tot >= 10000 and i < 6) else ""
            lps.append({"url": u, "cost": tot,
                        "ads": [{"name": m["name"], "sets": m["sets"], "link": m["link"],
                                 "img": m["img"],
                                 "d": {k: [round(v[0]), int(v[1]), int(v[2]), int(v[3])]
                                       for k, v in sorted(m["d"].items())}}
                                for m in items[:8]]})
        lps.sort(key=lambda l: -l["cost"])

        out.append({
            "id": a["id"], "name": a.get("name", ""), "error": "",
            "cvUnset": unset,
            "cvName": next((r.get("CVの種類") for r in rows if r.get("CVの種類")), ""),
            "noTracking": (not unset and not any(r.get("CV系イベント有無") for r in rows)
                           and bool(rows)),
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

    payload = {"since": str(since), "until": str(until), "period": f"{since}〜{until}",
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
    nlp = sum(len(x["lps"]) for x in accounts)
    nimg = sum(1 for x in accounts for l in x["lps"] for ad in l["ads"] if ad["img"])
    print(f"\n{len(accounts)}アカウント / CV設定済み{len(known)}件 / 未設定{len(accounts)-len(known)}件"
          f" / LP{nlp}本 / バナー画像{nimg}枚")
    print("※ 金額とクライアント名はログに出さない（公開リポジトリのため）。画面で見る")
    print(f"→ {a.out}/index.html")


if __name__ == "__main__":
    main()
