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
import re
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
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


TAG_DROP = re.compile(r"(?is)<(script|style|noscript|svg|iframe)\b.*?</\1>")
TAGS = re.compile(r"(?s)<[^>]+>")


def norm(s):
    """突き合わせ用に文字をそろえる。"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    s = re.sub(r"[,、。・！!？?「」『』〈〉《》（）()\[\]【】{}~〜ー\-–—_/／\\|:：;；\"'’”“※*＊＋+=…‥､｡]", "", s)
    return s


def longest_common(needle, hay):
    n = len(needle)
    for length in range(n, 1, -1):
        for i in range(0, n - length + 1):
            if needle[i:i + length] in hay:
                return needle[i:i + length]
    return ""


def html_to_lines(html):
    body = TAG_DROP.sub(" ", html)
    body = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/td|/tr|/span)[^>]*>", "\n", body)
    body = TAGS.sub(" ", body)
    body = unicodedata.normalize("NFKC", body)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#039;", "'")):
        body = body.replace(a, b)
    return [re.sub(r"[ \t\u3000]+", " ", l).strip() for l in body.splitlines() if l.strip()]


def fetch_lp_text(url, timeout=40):
    """LPの本文。JSで描くページはChromeで描画してから取る。"""
    lines = []
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            lines = html_to_lines(r.read().decode("utf-8", "replace"))
    except Exception:
        pass
    if len(lines) >= 20:
        return lines
    for exe in CHROME_CANDIDATES:
        try:
            r = subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-sandbox",
                                "--virtual-time-budget=9000", "--dump-dom", url],
                               capture_output=True, text=True, timeout=90)
            if r.stdout:
                l2 = html_to_lines(r.stdout)
                if len(l2) > len(lines):
                    lines = l2
            break
        except FileNotFoundError:
            continue
        except Exception:
            break
    return lines


def match_copy(texts, lp_lines):
    """広告のテキストがLP本文に実在するか。クリック後に話が変わっていないかを見る。"""
    hay = norm(" ".join(lp_lines))
    if not hay:
        return None
    rows = []
    for slot in ("title", "desc", "body"):
        for t in (texts.get(slot) or []):
            for piece in re.split(r"[\n。！!？?]+", t):
                n = norm(piece)
                if len(n) < 6:
                    continue
                hit = longest_common(n, hay)
                rows.append({"slot": slot, "text": piece.strip()[:60],
                             "rate": round(len(hit) / len(n), 2)})
                break          # 1つのテキストにつき先頭の一文だけ見る
    if not rows:
        return None
    rate = sum(r["rate"] for r in rows) / len(rows)
    return {"rate": round(rate, 2), "rows": rows[:6],
            "missing": [r["text"] for r in rows if r["rate"] < 0.35][:3]}


OCR_BIN = os.path.join(HERE, "..", "tools", "jpocr")
NUM = re.compile(r"[0-9][0-9,\.]*\s*(?:円|万円|億円|%|倍|割|kWh|kW|年|ヶ月|か月|日|人|件|名|回|分|坪|畳)")
SRC_HINT = re.compile(r"(出典|参考|調査|公表|統計|資源エネルギー庁|経済産業省|総務省|国土交通省|環境省|国民生活センター|協会|※)")


def ocr_images(paths):
    """バナー画像の中の文字を読む（macOS標準のOCR）。Linuxでは動かない。"""
    paths = [p for p in dict.fromkeys(paths) if p and os.path.exists(p)]
    if not paths or not os.path.exists(OCR_BIN):
        return {}
    out = {}
    for i in range(0, len(paths), 40):
        chunk = paths[i:i + 40]
        try:
            r = subprocess.run([OCR_BIN] + chunk, capture_output=True, text=True, timeout=600)
        except Exception:
            continue
        for row in (r.stdout or "").strip().splitlines():
            try:
                d = json.loads(row)
            except Exception:
                continue
            if d.get("error"):
                continue
            lines = [l for l in d.get("lines", []) if l.get("conf", 0) >= 0.3 and l.get("text", "").strip()]
            out[d["path"]] = lines
    return out


def banner_copy(lines):
    """OCRの行から、バナーが言っている主な文言だけ取る（注記や小さい字は外す）。"""
    big = [l for l in lines if l["h"] >= 0.04 and len(l["text"].strip()) >= 3
           and not l["text"].lstrip().startswith(("※", "*", "＊"))]
    big.sort(key=lambda l: l["y"])
    return [l["text"].strip() for l in big][:8]


def lp_candidates(lines, used_text):
    """次の訴求の材料。LP本文に実在する数字の行を、出典つき・未使用を上にして返す。"""
    used = norm(used_text)
    out, seen = [], set()
    for i, l in enumerate(lines):
        nums = [m.group(0).strip() for m in NUM.finditer(l)]
        if not nums or l in seen:
            continue
        seen.add(l)
        note = ""
        if l.lstrip().startswith(("※", "*", "＊")):
            note = l
        else:
            for j in range(i + 1, min(i + 4, len(lines))):
                if lines[j].lstrip().startswith(("※", "*", "＊")) or SRC_HINT.search(lines[j]):
                    note = lines[j]
                    break
        out.append({"nums": list(dict.fromkeys(nums))[:4], "line": l[:110], "note": note[:120],
                    "src": bool(SRC_HINT.search(l) or SRC_HINT.search(note)),
                    "unused": not any(norm(n) in used for n in nums)})
    out.sort(key=lambda c: -((3 if c["src"] else 0) + (2 if c["unused"] else 0)))
    return out[:6]


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
                                     "link": r.get("管理画面", ""), "d": {}, "st": set(),
                                     "texts": r.get("広告文") or {}})
            if r.get("状態"):
                m["st"].add(r["状態"])
            if not m.get("texts", {}).get("title") and (r.get("広告文") or {}).get("title"):
                m["texts"] = r["広告文"]
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
                m["imgPath"] = m["img"] if tot >= 10000 else ""
                m["img"] = thumb_data_uri(m["img"]) if (tot >= 10000 and i < 6) else ""
            lps.append({"url": u, "cost": tot,
                        "ads": [{"name": m["name"], "sets": m["sets"], "link": m["link"],
                                 "img": m["img"], "imgPath": m.get("imgPath", ""),
                                 "st": sorted(m.get("st") or []),
                                 "texts": m.get("texts") or {},
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


def add_lp_match(accounts, workers=5):
    """表示するLPの本文を取り、広告のテキストと突き合わせる。"""
    urls = []
    for a in accounts:
        for l in a["lps"]:
            if l["cost"] >= 10000 and l["url"].startswith("http") and l["url"] not in urls:
                urls.append(l["url"])
    if not urls:
        return
    print(f"\nLPの本文を取得中… {len(urls)}本", flush=True)
    texts = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for u, lines in zip(urls, ex.map(fetch_lp_text, urls)):
            texts[u] = lines
    thin = sum(1 for u in urls if len(texts.get(u) or []) < 25)
    print(f"  取れた {sum(1 for u in urls if texts.get(u))}本 / 本文が短く照合できない {thin}本", flush=True)

    # バナー画像の文字を読む（これがアームズでやった照合の中身）
    paths = [ad["imgPath"] for a in accounts for l in a["lps"] for ad in l["ads"] if ad.get("imgPath")]
    print(f"バナーの文字を読み取り中… {len(set(paths))}枚", flush=True)
    ocr = ocr_images(paths)
    print(f"  読めた {len(ocr)}枚", flush=True)

    for a in accounts:
        for l in a["lps"]:
            lines = texts.get(l["url"]) or []
            l["lpLines"] = len(lines)
            l["lpThin"] = len(lines) < 25 or len("".join(lines)) < 400
            hay = norm(" ".join(lines))
            used = []
            for ad in l["ads"]:
                oc = ocr.get(ad.get("imgPath") or "")
                if oc:
                    ad["imgText"] = banner_copy(oc)
                    ad["small"] = [x["text"] for x in oc
                                   if x["h"] * 390 < 11 and x["h"] >= 0.03][:3]
                    used += ad["imgText"]
                if l["lpThin"] or not hay:
                    continue
                m = match_copy(ad.get("texts") or {}, lines)
                if m:
                    ad["match"] = m
                if ad.get("imgText"):
                    rows = []
                    for t in ad["imgText"]:
                        n = norm(t)
                        if len(n) < 4:
                            continue
                        hit = longest_common(n, hay)
                        rows.append({"text": t[:40], "rate": round(len(hit) / len(n), 2)})
                    if rows:
                        ad["imgMatch"] = {
                            "rate": round(sum(r["rate"] for r in rows) / len(rows), 2),
                            "rows": rows[:6],
                            "missing": [r["text"] for r in rows if r["rate"] < 0.4][:3]}
            if not l["lpThin"] and lines:
                l["cands"] = lp_candidates(lines, " ".join(used) + " " +
                                           " ".join(t for ad in l["ads"]
                                                    for t in (ad.get("texts") or {}).get("title", [])))


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
    add_lp_match(accounts)

    for a0 in accounts:
        for l0 in a0["lps"]:
            for ad0 in l0["ads"]:
                ad0.pop("imgPath", None)

    payload = {"since": str(since), "until": str(until), "period": f"{since}〜{until}",
               "ranAt": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
               "accounts": accounts}
    os.makedirs(a.out, exist_ok=True)
    tpl = open(os.path.join(HERE, "page.html"), encoding="utf-8").read()
    html = tpl.replace("__ENC__", json.dumps(encrypt(payload, pw), ensure_ascii=False))
    with open(os.path.join(a.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    # 画像なしの軽い版。重くて開けない端末のため（数字と判定はそのまま）
    import copy
    light = copy.deepcopy(payload)
    for acc0 in light["accounts"]:
        for l in acc0["lps"]:
            for ad in l["ads"]:
                ad["img"] = ""
    lhtml = tpl.replace("__ENC__", json.dumps(encrypt(light, pw), ensure_ascii=False))
    lhtml = lhtml.replace("<title>バナーの効き方</title>", "<title>バナーの効き方（軽い版）</title>")
    with open(os.path.join(a.out, "light.html"), "w", encoding="utf-8") as f:
        f.write(lhtml)
    print(f"   軽い版: {len(lhtml)/1024:.0f} KB")
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
