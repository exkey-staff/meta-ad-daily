#!/usr/bin/env python3
"""配信済みバナーの「どれが効いていて、なぜか」を機械的に出すツール。

人がやる判断（どれを勝ちと見るか、次に何を言うか）は人に残し、
手で間に合わせていた部分だけを自動にする：

  read    バナー画像の文字・位置・文字高さ％・配色を読む（macOS標準のOCR。ネットに出さない）
  lp      飛び先LPの文言・数字・注記・出典を拾って構造化する
  match   バナーの各行が LP本文に実在するか突き合わせる（勝ち負けの根拠になる事実）
  report  実績の数字を足して、勝ち負け表・次の4枚の設計枠・社内説明の下書きを出す

例:
  python3 banner_insight.py read 01.png 02.png 03.png 04.png -o banners.json
  python3 banner_insight.py lp https://example.com/lp/ -o lp.json
  python3 banner_insight.py match -b banners.json -l lp.json
  python3 banner_insight.py report -b banners.json -l lp.json \\
      --perf-row "01,16000,3" --perf-row "02,12000,0" -o report.md
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.expanduser("~/.cache/banner_insight")
OCR_BIN = os.path.join(HERE, "jpocr")
OCR_SRC = os.path.join(HERE, "jpocr.swift")

# ============================================================ 共通

def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


def norm(s):
    """突き合わせ用に文字をそろえる（全角半角・記号・空白・桁区切りを落とす）。"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[\s　]+", "", s)
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    s = re.sub(r"[,、。・！!？?「」『』〈〉《》（）()\[\]【】{}~〜ー\-–—_/／\\|:：;；\"'’”“※*＊＋+=…‥､｡]", "", s)
    return s


def longest_common(needle, hay):
    """needle の中で hay に含まれる一番長い連続部分を返す。短い語は拾わない。"""
    n = len(needle)
    if n == 0:
        return ""
    for length in range(n, 1, -1):
        for i in range(0, n - length + 1):
            part = needle[i:i + length]
            if part in hay:
                return part
    return ""


def cache_path(kind, key):
    import hashlib
    d = os.path.join(CACHE, kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def cache_get(kind, key, max_age_days=None):
    """毎朝回すときに、同じLPを描画し直したり同じ画像をOCRし直したりしないため。"""
    import time
    p = cache_path(kind, key)
    if not os.path.exists(p):
        return None
    if max_age_days and (time.time() - os.path.getmtime(p)) > max_age_days * 86400:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_put(kind, key, value):
    try:
        with open(cache_path(kind, key), "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
    except Exception:
        pass


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print(f"→ {path}")


# ============================================================ read（バナーを読む）

def ensure_ocr():
    if os.path.exists(OCR_BIN):
        return True
    if not os.path.exists(OCR_SRC):
        warn("OCRの元ファイル jpocr.swift が無い")
        return False
    print("  OCRを初回ビルド中…")
    r = subprocess.run(["swiftc", "-O", "-o", OCR_BIN, OCR_SRC], capture_output=True, text=True)
    if r.returncode != 0:
        warn("OCRのビルドに失敗した: " + r.stderr.strip().splitlines()[-1:][0] if r.stderr else "不明")
        return False
    return True


def guess_role(line, lines):
    """行の役どころを当てる。あくまで目安（最終判断は目で見る）。"""
    y, h, t = line["y"], line["h"], line["text"]
    big = max(l["h"] for l in lines) if lines else h
    if h <= 0.030 or t.lstrip().startswith(("※", "*", "＊")):
        return "注記"
    if y >= 0.60 and len(t) <= 18 and h >= 0.035:
        return "CTA・帯"
    if h >= big * 0.75:
        return "見出し（最大）"
    if y <= 0.35:
        return "見出し周り"
    return "本文・補足"


def _lum(c):
    def f(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def contrast_ratio(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 2)


def line_colors(im, line):
    """行の枠から背景色と文字色を推定し、コントラスト比を出す。

    枠の中で一番多い色を背景、そこから一番離れた色（1%以上occupy）を文字と見る。
    帯の文字が沈んでいないかを数字で見るため。
    """
    W, H = im.size
    x0 = max(0, int((line["x"] - 0.01) * W)); x1 = min(W, int((line["x"] + line["w"] + 0.01) * W))
    y0 = max(0, int((line["y"] - 0.02) * H)); y1 = min(H, int((line["y"] + line["h"] + 0.02) * H))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    box = im.crop((x0, y0, x1, y1))
    px = [(r // 16 * 16, g // 16 * 16, b // 16 * 16) for r, g, b in box.getdata()]
    if not px:
        return None
    cnt = Counter(px)
    total = sum(cnt.values())
    bg = cnt.most_common(1)[0][0]
    fg, best = None, -1.0
    for c, n in cnt.most_common(24):
        if n / total < 0.01:
            break
        d = abs(_lum(c) - _lum(bg))
        if d > best:
            best, fg = d, c
    if fg is None:
        return None
    return {"背景": "#%02X%02X%02X" % bg, "文字": "#%02X%02X%02X" % fg,
            "コントラスト": contrast_ratio(bg, fg)}


def dominant_colors(path):
    """上・中・下の帯ごとの最頻色。LPの実配色と見比べる用。"""
    try:
        from PIL import Image
    except ImportError:
        return {}
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return {}
    W, H = im.size
    out = {}
    for label, (y0, y1) in (("上部0-20%", (0.0, 0.20)), ("中央40-60%", (0.40, 0.60)),
                            ("下部75-95%", (0.75, 0.95))):
        box = im.crop((0, int(H * y0), W, max(int(H * y1), int(H * y0) + 1)))
        box = box.resize((min(W, 160), max(1, box.size[1] * 160 // max(W, 1))))
        q = [(r // 24 * 24, g // 24 * 24, b // 24 * 24) for r, g, b in box.getdata()]
        if not q:
            continue
        (r, g, b), _ = Counter(q).most_common(1)[0]
        out[label] = "#%02X%02X%02X" % (r, g, b)
    return out


def cmd_read(args):
    if not ensure_ocr():
        sys.exit(1)
    paths = [p for p in args.images if os.path.exists(p)]
    missing = [p for p in args.images if not os.path.exists(p)]
    for p in missing:
        warn(f"見つからない: {p}")
    if not paths:
        sys.exit("読める画像が無い")

    def img_key(p):
        st = os.stat(p)
        return f"{os.path.abspath(p)}|{st.st_size}|{int(st.st_mtime)}"

    cached, todo = {}, []
    for p in paths:
        c = None if getattr(args, "no_cache", False) else cache_get("ocr", img_key(p))
        if c:
            cached[p] = c
        else:
            todo.append(p)

    out_rows = []
    if todo:
        r = subprocess.run([OCR_BIN] + todo, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("OCRが失敗した: " + r.stderr.strip())
        for row in r.stdout.strip().splitlines():
            d = json.loads(row)
            if not d.get("error"):
                cache_put("ocr", img_key(d["path"]), d)
            out_rows.append(row)
    for p in paths:
        if p in cached:
            out_rows.append(json.dumps(cached[p], ensure_ascii=False))

    banners = []
    for row in out_rows:
        d = json.loads(row)
        if d.get("error"):
            warn(f"{d['path']}: {d['error']}")
            continue
        lines = [l for l in d["lines"] if l["conf"] >= args.min_conf and l["text"].strip()]
        for l in lines:
            l["role"] = guess_role(l, lines)
            l["高さ%"] = round(l["h"] * 100, 1)
            l["390px換算"] = round(l["h"] * 390, 1)   # スマホ幅での文字の高さ(px)
        try:
            from PIL import Image as _Im
            _im = _Im.open(d["path"]).convert("RGB")
            for l in lines:
                c = line_colors(_im, l)
                if c:
                    l.update(c)
        except Exception:
            pass
        banners.append({
            "name": args.name_from(d["path"]) if hasattr(args, "name_from") else
                    os.path.splitext(os.path.basename(d["path"]))[0],
            "path": d["path"],
            "size": [d["width"], d["height"]],
            "lines": lines,
            "colors": dominant_colors(d["path"]),
        })

    for b in banners:
        print(f"\n■ {b['name']}  {b['size'][0]}x{b['size'][1]}  "
              + "  ".join(f"{k}{v}" for k, v in b["colors"].items()))
        for l in b["lines"]:
            flag = ""
            if l["390px換算"] < 11 and l["role"] != "注記":
                flag += "  ← 390pxで読みにくい"
            if l.get("コントラスト") is not None and l["コントラスト"] < 3.0:
                flag += f"  ← 背景に沈んでいる(コントラスト{l['コントラスト']})"
            col = f" {l.get('文字','')}on{l.get('背景','')}" if l.get("背景") else ""
            print(f"   y{l['y']*100:5.1f}%  字高{l['高さ%']:4.1f}%({l['390px換算']:4.1f}px)  "
                  f"[{l['role']}]{col} {l['text']}{flag}")
        if not any(l["role"] == "CTA・帯" for l in b["lines"]):
            print("   ! 下側にCTAらしい行が見つからない（帯が画像なら目でも確認する）")

    if args.out:
        save_json(banners, args.out)
    return banners


# ============================================================ lp（LPを読む）

TAG_DROP = re.compile(r"(?is)<(script|style|noscript|svg|iframe)\b.*?</\1>")
TAGS = re.compile(r"(?s)<[^>]+>")
NUM = re.compile(r"[0-9０-９][0-9０-９,，\.]*\s*(?:円|万円|億円|%|％|倍|割|kWh|kW|年|ヶ月|か月|ヵ月|ケ月|日|人|件|名|回|分|坪|畳)")
SRC_HINT = re.compile(r"(出典|参考|調査|公表|統計|資源エネルギー庁|経済産業省|総務省|国土交通省|環境省|省|庁|協会|※|より)")


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def render_html(url, wait_ms=9000):
    """JavaScriptで描くLPは、Chromeで実際に描画してからHTMLを取る。"""
    if not os.path.exists(CHROME):
        return ""
    try:
        r = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-sandbox",
             f"--virtual-time-budget={wait_ms}", "--dump-dom", url],
            capture_output=True, text=True, timeout=90)
        return r.stdout or ""
    except Exception:
        return ""


def fetch_text(url):
    import requests
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    res = requests.get(url, headers={"User-Agent": ua}, timeout=30)
    res.encoding = res.apparent_encoding or "utf-8"
    html = res.text
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if m:
        title = re.sub(r"\s+", " ", TAGS.sub("", m.group(1))).strip()
    heads = []
    for m in re.finditer(r"(?is)<h([1-3])[^>]*>(.*?)</h\1>", html):
        t = re.sub(r"\s+", " ", TAGS.sub(" ", m.group(2))).strip()
        if t:
            heads.append(f"h{m.group(1)}: {t}")
    body = TAG_DROP.sub(" ", html)
    body = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/td|/tr)[^>]*>", "\n", body)
    body = TAGS.sub(" ", body)
    body = unicodedata.normalize("NFKC", body)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#039;", "'")):
        body = body.replace(a, b)
    lines = [re.sub(r"[ \t　]+", " ", l).strip() for l in body.splitlines()]
    return title, heads, [l for l in lines if l]


def lines_from_html(html):
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if m:
        title = re.sub(r"\s+", " ", TAGS.sub("", m.group(1))).strip()
    heads = []
    for m in re.finditer(r"(?is)<h([1-3])[^>]*>(.*?)</h\1>", html):
        t = re.sub(r"\s+", " ", TAGS.sub(" ", m.group(2))).strip()
        if t:
            heads.append(f"h{m.group(1)}: {t}")
    body = TAG_DROP.sub(" ", html)
    body = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/td|/tr|/span)[^>]*>", "\n", body)
    body = TAGS.sub(" ", body)
    body = unicodedata.normalize("NFKC", body)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#039;", "'")):
        body = body.replace(a, b)
    lines = [re.sub(r"[ \t\u3000]+", " ", l).strip() for l in body.splitlines()]
    return title, heads, [l for l in lines if l]


def cmd_lp(args):
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            raw = f.read()
        title, heads = "", []
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        src = args.text_file
    else:
        title, heads, lines = fetch_text(args.url)
        src = args.url
        # 本文がほとんど取れない＝JavaScriptで描くLP。Chromeで描画してから取り直す
        if len(lines) < 20 and not getattr(args, "no_render", False):
            html = render_html(args.url)
            if html:
                t2, h2, l2 = lines_from_html(html)
                if len(l2) > len(lines):
                    title, heads, lines = (t2 or title), (h2 or heads), l2
                    print(f"  （JavaScriptで描くLPのため、Chromeで描画して取り直した: {len(lines)}行）")

    numbers = []
    for i, l in enumerate(lines):
        for m in NUM.finditer(l):
            note = ""
            if l.lstrip().startswith(("※", "*", "＊")):
                note = l
            else:
                for j in range(i + 1, min(i + 4, len(lines))):
                    if lines[j].lstrip().startswith(("※", "*", "＊")) or SRC_HINT.search(lines[j]):
                        note = lines[j]
                        break
            numbers.append({"値": m.group(0).strip(), "行": l[:120], "近くの注記候補": note[:160]})
    notes = [l for l in lines if l.lstrip().startswith(("※", "*", "＊"))]
    fv = (heads[:4] or []) + lines[:12]

    lp = {"source": src, "title": title, "headings": heads[:20], "fv候補": fv,
          "numbers": numbers, "notes": notes, "lines": lines}
    if not args.text_file:
        cache_put("lp", src, lp)

    print(f"\n■ LP: {src}")
    print(f"  タイトル: {title or '(取れず)'}")
    print(f"  行数 {len(lines)} / 数字 {len(numbers)}件 / 注記 {len(notes)}件")
    print("\n-- FV候補（ここからバナーの意図を取る）--")
    for l in fv[:10]:
        print("   " + l[:110])
    print("\n-- 本文に実在する数字（次の訴求はここからだけ取る）--")
    seen = set()
    for n in numbers:
        if n["値"] in seen:
            continue
        seen.add(n["値"])
        print(f"   {n['値']:>12}  | {n['行'][:70]}")
        if n["近くの注記候補"]:
            print(f"   {'':>12}  └ {n['近くの注記候補'][:90]}")
    if args.out:
        save_json(lp, args.out)
    return lp


# ============================================================ match（照合）

def lp_is_thin(lp):
    """本文がほとんど無いLP（診断クイズなど）では、文言の照合は成立しない。"""
    body = "".join(lp.get("lines", []))
    return len(lp.get("lines", [])) < 25 or len(body) < 400


def match_banners(banners, lp, skip_roles=("注記",)):
    hay = norm(" ".join(lp["lines"]))
    lp_norm_lines = [(norm(l), l) for l in lp["lines"]]
    result = []
    for b in banners:
        rows, scored = [], []
        for l in b["lines"]:
            if l.get("role") in skip_roles:
                continue
            n = norm(l["text"])
            if len(n) < 3:
                continue
            hit = longest_common(n, hay)
            score = len(hit) / len(n) if n else 0.0
            mark = "◎" if score >= 0.9 else "○" if score >= 0.6 else "△" if score >= 0.35 else "✕"
            lp_line = ""
            if hit and score >= 0.5 and (len(hit) >= 5 or score >= 0.9):
                for nl, orig in lp_norm_lines:
                    if hit in nl:
                        lp_line = orig[:110]
                        break
            rows.append({"text": l["text"], "role": l.get("role", ""), "高さ%": l.get("高さ%"),
                         "一致": mark, "率": round(score, 2), "一致部分": hit, "LPの行": lp_line,
                         "コントラスト": l.get("コントラスト")})
            scored.append((score, l.get("高さ%") or 0))
        # 大きい文字ほど重く見る（バナーの主役がLPに無いのが一番危ない）
        wsum = sum(h for _, h in scored) or 1
        weighted = sum(s * h for s, h in scored) / wsum
        result.append({"name": b["name"], "rows": rows,
                       "一致率_文字の大きさ重み": round(weighted, 2),
                       "LPに無い行": [r["text"] for r in rows if r["一致"] in ("✕", "△")]})
    # 全枚に出る文言は、枚ごとの差の説明にはならない
    if len(result) > 1:
        from collections import Counter as _C
        c = _C()
        for r in result:
            for t in {norm(x["text"]) for x in r["rows"]}:
                c[t] += 1
        for r in result:
            for row in r["rows"]:
                row["全枚共通"] = c[norm(row["text"])] == len(result)
        for r in result:
            r["LPに無い行"] = [row["text"] for row in r["rows"]
                             if row["一致"] in ("✕", "△") and not row.get("全枚共通")]
    return result


def cmd_match(args):
    banners, lp = load_json(args.banners), load_json(args.lp)
    res = match_banners(banners, lp)
    for r in res:
        print(f"\n■ {r['name']}   LP一致率(文字の大きさ重み) {r['一致率_文字の大きさ重み']:.2f}")
        for row in r["rows"]:
            print(f"   {row['一致']} {row['率']:.2f}  字高{row['高さ%']:>4}%  {row['text']}")
            if row["LPの行"]:
                print(f"        └ LP: {row['LPの行']}")
            elif row["一致部分"] and row["率"] < 0.9:
                print(f"        └ LPにあるのは「{row['一致部分']}」までで、この言い方は無い")
        if r["LPに無い行"]:
            print("   ! LPに出てこない／言い換わっている行: " + " / ".join(r["LPに無い行"]))
    if args.out:
        save_json(res, args.out)
    return res


# ============================================================ report

def candidate_lines(lp, banners):
    """次の訴求の材料になりそうなLPの行を、出典つき・未使用を上にして返す。"""
    byline = {}
    for n in lp.get("numbers", []):
        line = n["行"]
        if line == lp.get("title", "") or (lp.get("lines") and line == lp["lines"][0]):
            continue   # タイトル行は現行訴求なので候補にしない
        d = byline.setdefault(line, {"値": [], "注記": n["近くの注記候補"]})
        if n["値"] not in d["値"]:
            d["値"].append(n["値"])
    banner_text = norm(" ".join(l["text"] for b in banners for l in b["lines"]))
    SRC_STRONG = re.compile(r"(経済産業省|資源エネルギー庁|総務省|国土交通省|環境省|国民生活センター|出典|公表値|統計|調査結果)")

    def has_src(line, note):
        return bool(SRC_STRONG.search(line) or SRC_STRONG.search(note or ""))

    out = []
    for line, d in byline.items():
        out.append({"値": d["値"], "行": line, "注記": d["注記"],
                    "出典あり": has_src(line, d["注記"]),
                    "未使用": not any(norm(v) in banner_text for v in d["値"])})
    # 出典がある行、既存バナーで使っていない行を上に
    out.sort(key=lambda c: -((3 if c["出典あり"] else 0) + (2 if c["未使用"] else 0)))
    return out


def parse_perf(args):
    if getattr(args, "_perf", None):
        return list(args._perf)
    rows = []
    if getattr(args, "perf", None):
        with open(args.perf, encoding="utf-8-sig", newline="") as f:
            sample = f.read(2048)
            f.seek(0)
            delim = "\t" if "\t" in sample.splitlines()[0] else ","
            for d in csv.DictReader(f, delimiter=delim):
                rows.append({(k or "").strip(): (v or "").strip() for k, v in d.items()})
    for s in (getattr(args, "perf_row", None) or []):
        parts = [p.strip() for p in s.split(",")]
        keys = ["name", "cost", "cv", "imp", "click"]
        rows.append({k: v for k, v in zip(keys, parts) if v != ""})

    ALIAS = {"name": ("name", "名前", "バナー", "クリエイティブ", "広告名", "広告"),
             "cost": ("cost", "消化", "費用", "金額", "消化金額", "spend"),
             "cv": ("cv", "cvs", "獲得", "件数", "コンバージョン", "conv"),
             "cpa": ("cpa",), "imp": ("imp", "impressions", "表示", "インプレッション"),
             "click": ("click", "clicks", "クリック")}

    def pick(row, key):
        for a in ALIAS[key]:
            for k, v in row.items():
                if norm(k) == norm(a):
                    return v
        return ""

    def num(v):
        v = re.sub(r"[^\d\.\-]", "", str(v or ""))
        try:
            return float(v) if v not in ("", "-", ".") else None
        except ValueError:
            return None

    out = []
    for row in rows:
        r = {"name": pick(row, "name") or "(無名)"}
        for k in ("cost", "cv", "cpa", "imp", "click"):
            r[k] = num(pick(row, k))
        if r["cpa"] is None and r["cost"] and r["cv"]:
            r["cpa"] = r["cost"] / r["cv"]
        out.append(r)
    return out


def perf_by_banner(banners, perf):
    """実績の行名をバナー名に寄せる。"""
    pmap = {}
    for p in perf:
        key = p["name"]
        hit = next((b["name"] for b in banners
                    if norm(key) in norm(b["name"]) or norm(b["name"]) in norm(key)), key)
        pmap[hit] = p
    total = sum(p["cost"] or 0 for p in perf) or None
    for p in pmap.values():
        p["share"] = (p["cost"] / total * 100) if (p.get("cost") and total) else None
    return pmap, total


def fmt(v, unit="", dash="—"):
    if v is None:
        return dash
    if unit == "円":
        return f"{round(v):,}円"
    if unit == "%":
        return f"{v:.1f}%"
    return f"{round(v):,}" if float(v).is_integer() else f"{v:,.1f}"


def cmd_report(args):
    banners = getattr(args, "_banners", None) or load_json(args.banners)
    lp = getattr(args, "_lp", None) or load_json(args.lp)
    match = match_banners(banners, lp)
    mmap = {m["name"]: m for m in match}
    perf = parse_perf(args)
    pmap, total_cost = perf_by_banner(banners, perf)
    total_cv = sum(p["cv"] or 0 for p in perf if p["cv"] is not None)

    L = []
    L.append(f"# バナー分析 — {lp.get('title') or lp.get('source')}")
    L.append("")
    L.append(f"- 飛び先LP: {lp.get('source')}")
    L.append(f"- 対象バナー: {len(banners)}枚")
    L.append(f"- 集計期間: {args.period or '〔要記入：いつからいつまでの数字か〕'}")
    L.append(f"- 計測対象のCV: {args.cv_name or '〔要記入：何をCVとして数えているか〕'}")
    L.append("")

    L.append("## 1. 実績とLP一致（事実）")
    L.append("")
    L.append("| バナー | 消化 | CV | CPA | 配信シェア | LP一致率 | LPに出てこない主な文言 |")
    L.append("|---|---:|---:|---:|---:|---:|---|")
    for b in banners:
        p = pmap.get(b["name"], {})
        m = mmap.get(b["name"], {})
        miss = " / ".join(m.get("LPに無い行", [])[:3]) or "—"
        L.append(f"| {b['name']} | {fmt(p.get('cost'),'円')} | {fmt(p.get('cv'))} | "
                 f"{fmt(p.get('cpa'),'円')} | {fmt(p.get('share'),'%')} | "
                 f"{round(m['一致率_文字の大きさ重み']*100) if m.get('一致率_文字の大きさ重み') is not None else '—'}% | {miss} |")
    L.append("")
    L.append("※ LP一致率は、バナーの各行が LP本文に実在するかを文字の大きさで重みづけした値（100%＝全文がLPにある）。"
             "100%＝バナーの文言が全部LPに載っている。数字の良し悪しではなく「クリック後に話が変わっていないか」を見るためのもの。"
             "全枚に共通する文言は、枚ごとの差の説明にならないので「出てこない文言」から外してある。")
    L.append("")

    faint = []
    for b in banners:
        for l in b["lines"]:
            if l.get("コントラスト") is not None and l["コントラスト"] < 3.0:
                faint.append(f"{b['name']}「{l['text'][:24]}」（{l.get('文字')} on {l.get('背景')} / コントラスト{l['コントラスト']}）")
    if faint:
        L.append("### 背景に沈んでいる文字（コントラスト3.0未満）")
        for s2 in faint:
            L.append(f"- {s2}")
        L.append("")

    small = []
    for b in banners:
        for l in b["lines"]:
            if l.get("role") != "注記" and l.get("390px換算", 99) < 11:
                small.append(f"{b['name']}「{l['text'][:24]}」（字高{l['高さ%']}%）")
    if small:
        L.append("### 390pxで読みにくい文字")
        for s2 in small:
            L.append(f"- {s2}")
        L.append("")

    L.append("## 2. 勝ち負けの理由")
    L.append("")
    cands = sorted([b for b in banners if pmap.get(b["name"], {}).get("cpa")],
                   key=lambda b: pmap[b["name"]]["cpa"])
    if cands:
        win = cands[0]["name"]
        lose = [b["name"] for b in banners if (pmap.get(b["name"], {}).get("cv") == 0)]
        L.append(f"- 一番安いのは **{win}**（CPA {fmt(pmap[win]['cpa'],'円')}）。"
                 f"LP一致率は {round(mmap[win]['一致率_文字の大きさ重み']*100)}%。")
        for n in lose:
            miss = " / ".join(mmap[n]["LPに無い行"][:3]) or "なし（別の理由を探す）"
            L.append(f"- 獲得0件 {n}：LPに出てこない文言 → {miss}")
    if total_cv and total_cv < 10:
        L.append(f"- ! CV合計が{int(total_cv)}件しかない。CPAの差は配信量の偏りでも説明がつくので、"
                 "数字だけを理由にしない。**LPに出てこない文言があるかどうか**は配信量と無関係に確認できる事実なので、そちらを根拠にする。")
    shares = [p["share"] for p in pmap.values() if p.get("share")]
    if shares and max(shares) > 50:
        L.append(f"- ! 消化が1枚に偏っている（最大 {max(shares):.0f}%）。CPA比較はこの偏りを添えて説明する。")
    L.append("- 〔要記入：上の事実から言える勝ち負けの理由を1〜2行で〕")
    L.append("")

    L.append("## 3. 次の4枚（訴求2 × デザイン2）")
    L.append("")
    base = cands[0]["name"] if cands else "〔勝ちバナー〕"
    L.append(f"基準＝{base}（効いている構成）。変える要素を1つずつにして、何が効いたか切り分ける。")
    L.append("")
    L.append("| # | 訴求 | デザイン | 狙い |")
    L.append("|---|---|---|---|")
    L.append(f"| 1 | 勝ち訴求（現行） | {base} と同じ | 基準。再現するか |")
    L.append(f"| 2 | 新訴求A | {base} と同じ | 訴求だけの差を見る |")
    L.append("| 3 | 勝ち訴求（現行） | 新デザイン | デザインだけの差を見る |")
    L.append("| 4 | 新訴求A | 新デザイン | 両方変えた上限値 |")
    L.append("")
    L.append("**配信**：1と2（絵が同じ）、3と4（絵が同じ）を**別の広告セット**に分ける。"
             "同じセットに入れるとMetaが配信を偏らせて切り分けにならない。")
    L.append("")
    L.append("### 新訴求の候補（LP本文に実在する数字だけ）")
    L.append("")
    for i, c in enumerate(candidate_lines(lp, banners)):
        if i >= 4:
            break
        tags = ("【出典あり】" if c["出典あり"] else "") + ("【未使用】" if c["未使用"] else "")
        L.append(f"- {tags}**{' / '.join(c['値'])}** — {c['行'][:90]}")
        if c["注記"]:
            L.append(f"  - 近くの注記候補（この数字の注記か目で確認して載せる）: {c['注記'][:120]}")
    L.append("")
    L.append("※ ここに出ていない数字はバナーに載せない（クリック後に裏づけが無くなる）。")
    L.append("")

    L.append("## 4. 社内説明の下書き")
    L.append("")
    L.append("**結論**：〔要記入〕")
    L.append("")
    L.append(f"**既存の数字**（{args.period or '〔期間〕'}）：")
    for b in banners:
        p = pmap.get(b["name"], {})
        if p:
            L.append(f"- {b['name']}：消化 {fmt(p.get('cost'),'円')} / CV {fmt(p.get('cv'))} / CPA {fmt(p.get('cpa'),'円')}")
    L.append("")
    L.append("**勝ち負けの理由**：〔2.から転記〕")
    L.append("")
    L.append("**追加分の設計と出典**：〔3.から転記。数字には必ずLPの出典を添える〕")
    L.append("")
    L.append("**配信の分け方**：絵が同じ2枚を別の広告セットに分ける。")
    L.append("")

    text = "\n".join(L)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"→ {args.out}")
    else:
        print(text)


# ============================================================ view（見る画面）

PAGE = r"""<title>バナー効き方ビューア</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&family=Roboto+Mono:wght@500;700&display=swap">
<style>
/* 画像を主役にした2カラム。狭い画面では画像の上に表が回り込む */
:root{
  --paper:#F7F8FA; --card:#FFFFFF; --ink:#16202B; --ink-soft:#5A6675; --line:#DDE3EA;
  --accent:#14284B; --accent-soft:#E7ECF4; --mark:#F07A12;
  --ok:#1C7A4B; --mid:#A8700A; --bad:#BC2E24;
  --sans:"Noto Sans JP",system-ui,-apple-system,"Hiragino Kaku Gothic ProN",sans-serif;
  --mono:"Roboto Mono",ui-monospace,SFMono-Regular,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#11161C; --card:#192028; --ink:#E7EDF4; --ink-soft:#97A4B4; --line:#2C3843;
  --accent:#9FBCE6; --accent-soft:#1E2A3C; --mark:#F59B4B;
  --ok:#59C98C; --mid:#DEAF4B; --bad:#F0766A; color-scheme:dark;
}}
:root[data-theme="dark"]{
  --paper:#11161C; --card:#192028; --ink:#E7EDF4; --ink-soft:#97A4B4; --line:#2C3843;
  --accent:#9FBCE6; --accent-soft:#1E2A3C; --mark:#F59B4B;
  --ok:#59C98C; --mid:#DEAF4B; --bad:#F0766A; color-scheme:dark;
}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.6;margin:0}
.wrap{max-width:1180px;margin:0 auto;
  padding-left:max(16px,env(safe-area-inset-left,0px));
  padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:28px 64px}
h1{font-size:clamp(1.25rem,1rem + 1.2vw,1.7rem);font-weight:700;margin:0 0 6px;text-wrap:balance}
h2{font-size:1.05rem;font-weight:700;margin:40px 0 14px;padding-bottom:7px;border-bottom:2px solid var(--line)}
a{color:var(--accent)}
.meta{color:var(--ink-soft);font-size:.85rem;margin:0 0 4px}
.lead{margin:14px 0 0;padding:12px 14px;background:var(--accent-soft);border-radius:8px;font-size:.85rem;color:var(--ink)}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:22px 0 6px;
  position:sticky;top:env(safe-area-inset-top,0px);z-index:5;
  background:var(--paper);padding-block:10px}
.btn{font:500 .8rem/1 var(--sans);color:var(--ink);background:var(--card);
  border:1px solid var(--line);border-radius:999px;padding:8px 14px;cursor:pointer}
.btn[aria-pressed="true"]{background:var(--accent);color:var(--card);border-color:var(--accent)}
.btn:focus-visible{outline:2px solid var(--mark);outline-offset:2px}
.sel{font:500 .8rem var(--sans);background:var(--card);color:var(--ink);
  border:1px solid var(--line);border-radius:999px;padding:8px 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:16px;margin-top:16px;display:grid;gap:18px;grid-template-columns:minmax(0,340px) minmax(0,1fr)}
@media (max-width:820px){.card{grid-template-columns:minmax(0,1fr)}}
.card.base{border-color:var(--mark);box-shadow:0 0 0 1px var(--mark) inset}
.shot{position:relative;width:100%;align-self:start}
.shot.phone{width:390px;max-width:100%}
.shot img{display:block;width:100%;border-radius:6px;border:1px solid var(--line)}
.box{position:absolute;border:2px solid var(--ok);border-radius:2px;pointer-events:none;
  box-shadow:0 0 0 1px rgba(0,0,0,.35)}
.box.mid{border-color:var(--mid);border-width:3px;background:rgba(200,140,20,.20)}
.box.bad{border-color:var(--bad);border-width:4px;background:rgba(210,40,30,.26);
  box-shadow:0 0 0 1px rgba(0,0,0,.45),0 0 10px rgba(210,40,30,.5)}
.box.on{border-width:3px;background:rgba(240,122,18,.22);border-color:var(--mark)}
.shot:not(.boxes) .box{display:none}
.head{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:baseline}
.name{font-weight:700;font-size:1rem}
.tag{font:500 .7rem/1 var(--sans);padding:4px 8px;border-radius:5px;background:var(--accent-soft);color:var(--ink)}
.kpis{display:flex;flex-wrap:wrap;gap:14px 22px;margin:12px 0 14px}
.kpi{min-width:72px}
.kpi dt{font-size:.68rem;color:var(--ink-soft);letter-spacing:.06em}
.kpi dd{margin:2px 0 0;font:700 1.15rem/1.2 var(--mono);font-variant-numeric:tabular-nums}
.kpi dd small{font:500 .7rem var(--sans);color:var(--ink-soft)}
.rate{font-family:var(--mono);font-weight:700}
.rate.hi{color:var(--ok)} .rate.mi{color:var(--mid)} .rate.lo{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:.82rem}
.tscroll{overflow-x:auto}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.68rem;color:var(--ink-soft);letter-spacing:.05em;font-weight:500;white-space:nowrap}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
tr.row{cursor:pointer}
tr.row:hover{background:var(--accent-soft)}
tr.row.on{background:var(--accent-soft);box-shadow:inset 3px 0 0 var(--mark)}
tr.note td,tr.skip td{color:var(--ink-soft)}
.m{font-weight:700;font-family:var(--mono)}
.m.ok{color:var(--ok)} .m.mid{color:var(--mid)} .m.bad{color:var(--bad)}
.txt{min-width:9rem;outline:none}
.txt:focus{background:var(--accent-soft);border-radius:3px}
.lpline{display:block;color:var(--ink-soft);font-size:.74rem;margin-top:3px}
.flags{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.flag{font:500 .72rem/1.35 var(--sans);padding:5px 9px;border-radius:5px}
.flag.bad{background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad)}
.flag.mid{background:color-mix(in srgb,var(--mid) 18%,transparent);color:var(--mid)}
.flag.calm{background:var(--accent-soft);color:var(--ink-soft)}
ul.cand{list-style:none;padding:0;margin:0;display:grid;gap:8px}
ul.cand li{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--line);
  border-radius:8px;padding:10px 12px;font-size:.84rem}
ul.cand li.src{border-left-color:var(--mark)}
ul.cand b{font-family:var(--mono);font-size:.95rem}
ul.cand .src-note{display:block;color:var(--ink-soft);font-size:.74rem;margin-top:4px}
.hint{color:var(--ink-soft);font-size:.78rem;margin-top:8px}
</style>

<div class="wrap">
  <h1 id="ttl"></h1>
  <p class="meta" id="sub"></p>
  <p class="lead">枠の色は、その文言が<strong>飛び先LPの本文に実在するか</strong>。緑＝そのまま載っている／黄＝一部だけ／赤＝LPに出てこない。
  赤が勝ち負けを分けた行。文言はクリックで直せる（OCRの誤読を直すと判定もやり直す）。</p>

  <div class="bar">
    <button class="btn" id="tBox" aria-pressed="true">OCRの枠</button>
    <button class="btn" id="tPhone" aria-pressed="false">スマホ幅(390px)</button>
    <button class="btn" id="tNote" aria-pressed="true">注記も見る</button>
    <select class="sel" id="sort">
      <option value="file">ファイル順</option>
      <option value="cpa">CPAが安い順</option>
      <option value="rate">LP一致率が高い順</option>
    </select>
  </div>

  <div id="cards"></div>

  <h2>次の訴求の候補（LP本文に実在する数字だけ）</h2>
  <ul class="cand" id="cand"></ul>
  <p class="hint">ここに出ていない数字はバナーに載せない。クリック後に裏づけが無くなる。
  「近くの注記候補」は距離で拾っただけなので、その数字の注記か目で確認してから載せる。</p>

  <h2>次の4枚の割り方</h2>
  <div class="tscroll"><table id="matrix"></table></div>
  <p class="hint">絵が同じ2枚（1と2、3と4）は<strong>別の広告セット</strong>に分ける。同じセットだとMetaが配信を偏らせて切り分けにならない。</p>
</div>

<script id="payload" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('payload').textContent);

const PUNCT = /[,、。・!?「」『』〈〉《》()\[\]【】{}~〜ー\-–—_/／\\|:：;；"'’”“※*＊+=…‥､｡]/g;
function norm(s){
  return (s||'').normalize('NFKC').toLowerCase()
    .replace(/[\s　]+/g,'').replace(/(\d),(?=\d)/g,'$1').replace(PUNCT,'');
}
function longestCommon(needle, hay){
  const n = needle.length;
  for(let len=n; len>1; len--)
    for(let i=0; i+len<=n; i++){
      const part = needle.slice(i,i+len);
      if(hay.includes(part)) return part;
    }
  return '';
}
const HAY = norm(D.lp.lines.join(' '));
const LPN = D.lp.lines.map(l => [norm(l), l]);

function judge(text){
  const n = norm(text);
  if(n.length < 3) return {skip:true, mark:'—', score:null, cls:'', lp:''};
  const hit = longestCommon(n, HAY);
  const score = hit.length / n.length;
  const mark = score>=0.9 ? '◎' : score>=0.6 ? '○' : score>=0.35 ? '△' : '✕';
  const cls  = score>=0.6 ? 'ok' : score>=0.35 ? 'mid' : 'bad';
  let lp = '';
  if(hit && score>=0.5 && (hit.length>=5 || score>=0.9))
    for(const [nl, orig] of LPN) if(nl.includes(hit)){ lp = orig; break; }
  else if(hit && score<0.9) lp = 'LPにあるのは「'+hit+'」までで、この言い方は無い';
  return {skip:false, mark, score, cls, lp, boxCls: score>=0.6?'':score>=0.35?'mid':'bad'};
}
function rateOf(b){
  let w=0, sum=0;
  for(const l of b.lines){
    if(l.role === '注記') continue;
    const j = judge(l.text);
    if(j.skip) continue;
    w += l.hpct; sum += j.score * l.hpct;
  }
  return w ? sum/w : null;
}
const yen = v => v==null ? '—' : Math.round(v).toLocaleString('ja-JP')+'円';
const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

document.getElementById('ttl').textContent = D.lp.title || 'バナー効き方ビューア';
document.getElementById('sub').innerHTML =
  '飛び先 <a href="'+esc(D.lp.source)+'" target="_blank" rel="noopener">'+esc(D.lp.source)+'</a>'
  + ' ・ ' + D.banners.length + '枚 ・ 期間 ' + esc(D.period || '未記入')
  + ' ・ CV ' + esc(D.cv_name || '未記入');

let showBoxes = true, phone = false, showNotes = true;

function cardHTML(b){
  const rate = rateOf(b);
  const rcls = rate==null ? '' : rate>=0.8 ? 'hi' : rate>=0.6 ? 'mi' : 'lo';
  const p = b.perf || {};
  const rows = b.lines.map((l,i) => {
    if(l.role === '注記' && !showNotes) return '';
    const j = judge(l.text);
    const faint = l.contrast!=null && l.contrast<3.0;
    const small = l.role!=='注記' && l.px390<11;
    return '<tr class="row'+(l.role==='注記'?' note':'')+(j.skip?' skip':'')+'" data-b="'+b.i+'" data-l="'+i+'">'
      + '<td class="m '+j.cls+'">'+j.mark+'</td>'
      + '<td><span class="txt" contenteditable="plaintext-only" data-b="'+b.i+'" data-l="'+i+'">'
      + esc(l.text)+'</span>'
      + (j.lp ? '<span class="lpline">'+esc(j.lp)+'</span>' : '')
      + (l.common ? '<span class="lpline">全枚共通。枚ごとの差の説明にはならない</span>' : '')
      + (j.skip ? '<span class="lpline">短すぎるので照合しない（漫画のセリフなど）</span>' : '')
      + '</td>'
      + '<td class="n">'+l.hpct.toFixed(1)+'%</td>'
      + '<td class="n'+(small?' m bad':'')+'">'+l.px390.toFixed(0)+'px</td>'
      + '<td class="n'+(faint?' m bad':'')+'">'+(l.contrast!=null?l.contrast.toFixed(1):'—')+'</td>'
      + '<td>'+esc(l.role)+'</td></tr>';
  }).join('');
  const boxes = b.lines.map((l,i) => {
    const j = judge(l.text);
    return '<span class="box '+(j.boxCls||'')+'" data-b="'+b.i+'" data-l="'+i+'" style="left:'
      + (l.x*100)+'%;top:'+(l.y*100)+'%;width:'+(l.w*100)+'%;height:'+(l.h*100)+'%"></span>';
  }).join('');
  const flags = [];
  const miss = b.lines.filter(l => l.role!=='注記' && !l.common && judge(l.text).score!=null && judge(l.text).score<0.6);
  if(miss.length) flags.push('<span class="flag bad">LPに出てこない：'
    + miss.map(l=>esc(l.text)).join(' / ')+'</span>');
  for(const l of b.lines){
    if(l.contrast!=null && l.contrast<3.0)
      flags.push('<span class="flag mid">「'+esc(l.text)+'」が背景に沈んでいる（コントラスト'+l.contrast.toFixed(2)+'）</span>');
    if(l.role!=='注記' && l.px390<11)
      flags.push('<span class="flag mid">「'+esc(l.text)+'」は390pxで'+l.px390.toFixed(0)+'px。読みにくい</span>');
  }
  if(!b.lines.some(l=>l.role==='CTA・帯')) flags.push('<span class="flag calm">下側にCTAらしい行が見つからない。帯が画像なら目でも見る</span>');
  if(!flags.length) flags.push('<span class="flag calm">機械で見つかる引っかかりは無し</span>');

  return '<section class="card'+(b.base?' base':'')+'">'
   + '<div class="shot'+(showBoxes?' boxes':'')+(phone?' phone':'')+'">'
   + '<img src="'+b.img+'" alt="'+esc(b.name)+'">'+boxes+'</div>'
   + '<div><div class="head"><span class="name">'+esc(b.name)+'</span>'
   + (b.base?'<span class="tag" style="background:var(--mark);color:#fff">基準（一番安い）</span>':'')
   + '</div>'
   + '<dl class="kpis">'
   + '<div class="kpi"><dt>LP一致率</dt><dd class="rate '+rcls+'">'+(rate==null?'—':Math.round(rate*100)+'<small>%</small>')+'</dd></div>'
   + '<div class="kpi"><dt>CPA</dt><dd>'+yen(p.cpa)+'</dd></div>'
   + '<div class="kpi"><dt>CV</dt><dd>'+(p.cv==null?'—':p.cv)+'<small> 件</small></dd></div>'
   + '<div class="kpi"><dt>消化</dt><dd>'+yen(p.cost)+'</dd></div>'
   + '<div class="kpi"><dt>配信シェア</dt><dd>'+(p.share==null?'—':p.share.toFixed(1)+'<small>%</small>')+'</dd></div>'
   + '</dl>'
   + '<div class="flags">'+flags.join('')+'</div>'
   + '<div class="tscroll"><table><thead><tr><th>LP</th><th>文言（クリックで直せる）</th>'
   + '<th>字高</th><th>390px</th><th>コントラスト</th><th>役どころ</th></tr></thead>'
   + '<tbody>'+rows+'</tbody></table></div></div></section>';
}

function render(){
  const mode = document.getElementById('sort').value;
  const list = D.banners.slice();
  if(mode==='cpa') list.sort((a,c)=>((a.perf&&a.perf.cpa)??Infinity)-((c.perf&&c.perf.cpa)??Infinity));
  if(mode==='rate') list.sort((a,c)=>(rateOf(c)??-1)-(rateOf(a)??-1));
  document.getElementById('cards').innerHTML = list.map(cardHTML).join('');
}

document.getElementById('cand').innerHTML = D.candidates.map(c =>
  '<li class="'+(c.src?'src':'')+'"><b>'+esc(c.nums.join(' / '))+'</b>'
  + (c.src?' <span class="tag">出典あり</span>':'') + (c.unused?' <span class="tag">未使用</span>':'')
  + '<br>'+esc(c.line)
  + (c.note ? '<span class="src-note">近くの注記候補：'+esc(c.note)+'</span>' : '')
  + '</li>').join('');

const base = D.banners.find(b=>b.base);
const bn = base ? base.name : '勝ちバナー';
document.getElementById('matrix').innerHTML =
  '<thead><tr><th>#</th><th>訴求</th><th>デザイン</th><th>何が分かるか</th></tr></thead><tbody>'
  + [['1','勝ち訴求（現行）',esc(bn)+' と同じ','基準。もう一度出るか'],
     ['2','新訴求A',esc(bn)+' と同じ','訴求だけの差'],
     ['3','勝ち訴求（現行）','新デザイン','デザインだけの差'],
     ['4','新訴求A','新デザイン','両方変えた上限']]
    .map(r=>'<tr><td class="n">'+r[0]+'</td><td>'+r[1]+'</td><td>'+r[2]+'</td><td>'+r[3]+'</td></tr>').join('')
  + '</tbody>';

function toggle(id, set){
  const el = document.getElementById(id);
  el.addEventListener('click', () => {
    const on = el.getAttribute('aria-pressed') !== 'true';
    el.setAttribute('aria-pressed', on);
    set(on); render();
  });
}
toggle('tBox', v => showBoxes = v);
toggle('tPhone', v => phone = v);
toggle('tNote', v => showNotes = v);
document.getElementById('sort').addEventListener('change', render);

const cards = document.getElementById('cards');
cards.addEventListener('click', e => {
  const tr = e.target.closest('tr.row');
  if(!tr || e.target.classList.contains('txt')) return;
  const was = tr.classList.contains('on');
  cards.querySelectorAll('tr.row.on,.box.on').forEach(n => n.classList.remove('on'));
  if(was) return;
  tr.classList.add('on');
  const box = cards.querySelector('.box[data-b="'+tr.dataset.b+'"][data-l="'+tr.dataset.l+'"]');
  if(box) box.classList.add('on');
});
cards.addEventListener('blur', e => {
  if(!e.target.classList || !e.target.classList.contains('txt')) return;
  const b = D.banners.find(x => x.i == e.target.dataset.b);
  const l = b && b.lines[e.target.dataset.l];
  if(!l) return;
  const t = e.target.textContent.trim();
  if(t && t !== l.text){ l.text = t; render(); }
}, true);

render();
</script>
"""


def cmd_view(args):
    from PIL import Image
    import base64
    import io

    banners = getattr(args, "_banners", None) or load_json(args.banners)
    lp = getattr(args, "_lp", None) or load_json(args.lp)
    match = {m["name"]: m for m in match_banners(banners, lp)}
    pmap, _ = perf_by_banner(banners, parse_perf(args))

    cheapest, best = None, None
    for b in banners:
        cpa = (pmap.get(b["name"]) or {}).get("cpa")
        if cpa and (best is None or cpa < best):
            best, cheapest = cpa, b["name"]

    out = []
    for i, b in enumerate(banners):
        im = Image.open(b["path"]).convert("RGB")
        if im.width > args.img_width:
            im = im.resize((args.img_width, round(im.height * args.img_width / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=args.quality, optimize=True)
        common = {r["text"]: r.get("全枚共通", False) for r in match.get(b["name"], {}).get("rows", [])}
        out.append({
            "i": i, "name": b["name"], "base": b["name"] == cheapest,
            "img": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
            "perf": pmap.get(b["name"]),
            "lines": [{"text": l["text"], "x": l["x"], "y": l["y"], "w": l["w"], "h": l["h"],
                       "hpct": l["高さ%"], "px390": l["390px換算"], "role": l.get("role", ""),
                       "contrast": l.get("コントラスト"), "common": common.get(l["text"], False)}
                      for l in b["lines"]],
        })

    data = {
        "lp": {"title": lp.get("title"), "source": lp.get("source"), "lines": lp.get("lines", [])},
        "period": args.period, "cv_name": args.cv_name, "banners": out,
        "candidates": [{"nums": c["値"], "line": c["行"], "note": c["注記"],
                        "src": c["出典あり"], "unused": c["未使用"]}
                       for c in candidate_lines(lp, banners)[:4]],
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = PAGE.replace("__DATA__", payload)
    if args.standalone:
        html = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<style>body{margin:0;font:14px system-ui}img{max-width:100%}'
                '[hidden]{display:none!important}</style></head><body>'
                + html + "</body></html>")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"→ {args.out}  ({len(html)/1024/1024:.2f} MB / バナー{len(out)}枚)")


def cmd_all(args):
    """フォルダとLPのURLだけで、画面とレポートまで一息に作る。"""
    import glob as _glob
    paths = []
    for src in args.images:
        if os.path.isdir(src):
            for ext in ("png", "jpg", "jpeg", "webp"):
                paths += _glob.glob(os.path.join(src, f"*.{ext}"))
                paths += _glob.glob(os.path.join(src, f"*.{ext.upper()}"))
        else:
            paths.append(src)
    # 一覧シートやチェック用の画像は分析対象から外す
    paths = sorted(p for p in paths
                   if not os.path.basename(p).startswith(("00_", "._"))
                   and "一覧" not in os.path.basename(p))
    if not paths:
        sys.exit("画像が見つからない")
    print(f"バナー {len(paths)}枚：" + " / ".join(os.path.basename(p) for p in paths))

    args.images = paths
    args.min_conf = 0.3
    args.out = None
    banners = cmd_read(args)

    args.url, args.text_file = args.lp_url, args.lp_text
    lp = cmd_lp(args)

    args._banners, args._lp = banners, lp
    stem = args.o
    args.out = stem if stem.endswith(".html") else stem + ".html"
    cmd_view(args)
    args.out = (args.out[:-5] if args.out.endswith(".html") else args.out) + "_レポート.md"
    cmd_report(args)
    if args.keep_json:
        save_json(banners, args.out[:-4] + "_banners.json")
        save_json(lp, args.out[:-4] + "_lp.json")


INDEX_PAGE = r"""<title>広告アカウント 効き方一覧</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&family=Roboto+Mono:wght@500;700&display=swap">
<style>
/* 上に全体の数字、下にLPの一覧。LPから各画面へ飛ぶ */
:root{
  --paper:#F7F8FA; --card:#FFFFFF; --ink:#16202B; --ink-soft:#5A6675; --line:#DDE3EA;
  --accent:#14284B; --accent-soft:#E7ECF4; --mark:#F07A12;
  --ok:#1C7A4B; --mid:#A8700A; --bad:#BC2E24;
  --sans:"Noto Sans JP",system-ui,-apple-system,sans-serif;
  --mono:"Roboto Mono",ui-monospace,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#11161C; --card:#192028; --ink:#E7EDF4; --ink-soft:#97A4B4; --line:#2C3843;
  --accent:#9FBCE6; --accent-soft:#1E2A3C; --mark:#F59B4B;
  --ok:#59C98C; --mid:#DEAF4B; --bad:#F0766A; color-scheme:dark;
}}
:root[data-theme="dark"]{
  --paper:#11161C; --card:#192028; --ink:#E7EDF4; --ink-soft:#97A4B4; --line:#2C3843;
  --accent:#9FBCE6; --accent-soft:#1E2A3C; --mark:#F59B4B;
  --ok:#59C98C; --mid:#DEAF4B; --bad:#F0766A; color-scheme:dark;
}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.6;margin:0}
.wrap{max-width:1040px;margin:0 auto;
  padding-left:max(16px,env(safe-area-inset-left,0px));
  padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:28px 64px}
h1{font-size:clamp(1.3rem,1rem + 1.3vw,1.8rem);font-weight:700;margin:0 0 4px;text-wrap:balance}
h2{font-size:1.02rem;font-weight:700;margin:36px 0 12px;padding-bottom:7px;border-bottom:2px solid var(--line)}
.meta{color:var(--ink-soft);font-size:.85rem;margin:0}
.big{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:20px}
.big div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.big dt{font-size:.7rem;color:var(--ink-soft);letter-spacing:.06em;margin:0}
.big dd{margin:4px 0 0;font:700 1.6rem/1.15 var(--mono);font-variant-numeric:tabular-nums}
.big dd small{font:500 .72rem var(--sans);color:var(--ink-soft)}
.vs{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:4px}
@media (max-width:560px){.vs{grid-template-columns:1fr}}
.vs div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.vs .hl{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok) inset}
.vs dt{font-size:.78rem;color:var(--ink-soft);margin:0}
.vs dd{margin:4px 0 0;font:700 1.5rem/1.15 var(--mono)}
.vs .sub{font:500 .76rem var(--sans);color:var(--ink-soft);margin-top:4px}
.alert{margin-top:14px;background:color-mix(in srgb,var(--bad) 10%,transparent);
  border:1px solid color-mix(in srgb,var(--bad) 35%,transparent);border-radius:10px;padding:14px 16px}
.alert b{color:var(--bad)}
.tscroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-size:.7rem;color:var(--ink-soft);letter-spacing:.05em;font-weight:500;white-space:nowrap}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
tr.zero td{background:color-mix(in srgb,var(--bad) 7%,transparent)}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid color-mix(in srgb,var(--accent) 40%,transparent)}
a:hover{border-bottom-color:var(--accent)}
.pill{display:inline-block;font:500 .68rem/1.5 var(--sans);padding:1px 7px;border-radius:4px;
  background:var(--accent-soft);color:var(--ink-soft);white-space:nowrap}
.pill.no{background:color-mix(in srgb,var(--mid) 18%,transparent);color:var(--mid)}
.thumbs{display:flex;gap:4px}
.thumbs img{width:34px;height:34px;object-fit:cover;border-radius:4px;border:1px solid var(--line)}
.hint{color:var(--ink-soft);font-size:.78rem;margin-top:10px}
</style>
<div class="wrap">
  <h1 id="ttl"></h1>
  <p class="meta" id="sub"></p>
  <dl class="big" id="big"></dl>
  <h2>インスタントフォーム と LP経由</h2>
  <dl class="vs" id="vs"></dl>
  <div id="alert"></div>
  <h2>LPごと</h2>
  <div class="tscroll"><table id="tbl"></table></div>
  <p class="hint">「文言照合」は、バナーの文言が飛び先LPの本文に実在するかを見る方法。
  診断クイズのように本文がほとんど無いLPでは成立しないので、そのLPは「使えない」と出している。</p>
</div>
<script id="payload" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('payload').textContent);
const yen = v => v==null ? '—' : Math.round(v).toLocaleString('ja-JP')+'円';
const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
document.getElementById('ttl').textContent = D.title;
document.getElementById('sub').textContent = D.period + ' ・ ' + D.account + ' ・ LP ' + D.lps.length + '本';
document.getElementById('big').innerHTML =
  [['消化', yen(D.total.cost)], ['CV', D.total.cv + '<small> 件</small>'],
   ['CPA', D.total.cv ? yen(D.total.cost/D.total.cv) : '—']]
  .map(([k,v]) => '<div><dt>'+k+'</dt><dd>'+v+'</dd></div>').join('');
const f = D.form, l = D.vialp;
document.getElementById('vs').innerHTML =
  '<div class="hl"><dt>インスタントフォーム</dt><dd>'+(f.cv?yen(f.cost/f.cv):'—')
  + '</dd><div class="sub">消化'+yen(f.cost)+' ・ CV'+f.cv+'件</div></div>'
  + '<div><dt>LP経由</dt><dd>'+(l.cv?yen(l.cost/l.cv):'—')
  + '</dd><div class="sub">消化'+yen(l.cost)+' ・ CV'+l.cv+'件</div></div>';
if (D.zero.cost > 0) {
  document.getElementById('alert').innerHTML =
    '<div class="alert">CVが1件も出ていないLPが <b>'+D.zero.n+'本</b>。'
    + 'そこに <b>'+yen(D.zero.cost)+'</b> 使っている（LP経由の'+Math.round(D.zero.cost/l.cost*100)+'%）。</div>';
}
document.getElementById('tbl').innerHTML =
  '<thead><tr><th></th><th>LP</th><th>枚</th><th>消化</th><th>CV</th><th>CPA</th><th>文言照合</th></tr></thead><tbody>'
  + D.lps.map(p =>
    '<tr'+(p.cv?'':' class="zero"')+'>'
    + '<td><div class="thumbs">'+(p.thumbs||[]).map(t=>'<img src="'+t+'" alt="">').join('')+'</div></td>'
    + '<td>'+(p.page ? '<a href="'+esc(p.page)+'">'+esc(p.name)+'</a>' : esc(p.name))+'</td>'
    + '<td class="n">'+p.n+'</td><td class="n">'+yen(p.cost)+'</td>'
    + '<td class="n">'+p.cv+'</td><td class="n">'+(p.cv?yen(p.cost/p.cv):'—')+'</td>'
    + '<td>'+(p.page ? (p.thin ? '<span class="pill no">使えない（本文'+p.lines+'行）</span>'
                               : '<span class="pill">使える</span>') : '<span class="pill no">—</span>')+'</td></tr>').join('')
  + '</tbody>';
</script>
"""


def write_index(args, data, done, thin_map, line_map):
    """アカウント全体の一覧画面。各LPの画面へのリンクつき。"""
    import base64
    import collections
    import io
    from PIL import Image

    rows = [r for r in data["rows"] if (r.get("消化") or 0) > 0]
    bylp = collections.defaultdict(lambda: {"cost": 0.0, "cv": 0.0, "n": 0, "imgs": []})
    for r in rows:
        d = bylp[r.get("LP") or "(LPが取れず)"]
        d["cost"] += r["消化"]
        d["cv"] += r["CV"] or 0
        d["n"] += 1
        if r.get("画像") and r["画像"] not in d["imgs"]:
            d["imgs"].append(r["画像"])

    form = bylp.get("http://fb.me/", {"cost": 0.0, "cv": 0.0})
    tot_cost = sum(v["cost"] for v in bylp.values())
    tot_cv = sum(v["cv"] for v in bylp.values())
    pages = {u: slug for u, slug, _, _ in done}

    def thumb(path, px=72):
        try:
            im = Image.open(path).convert("RGB")
            im.thumbnail((px, px), Image.LANCZOS)
            b = io.BytesIO()
            im.save(b, "JPEG", quality=70)
            return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()
        except Exception:
            return ""

    lps = []
    for u, d in sorted(bylp.items(), key=lambda kv: -kv[1]["cost"]):
        if u == "http://fb.me/":
            continue
        slug = pages.get(u)
        lps.append({
            "name": re.sub(r"^https?://[^/]+/", "", u) or u,
            "page": (slug + ".html") if slug else "",
            "cost": d["cost"], "cv": int(d["cv"]), "n": d["n"],
            "thin": bool(thin_map.get(u)), "lines": line_map.get(u, 0),
            "thumbs": [t for t in (thumb(p) for p in d["imgs"][:3]) if t],
        })

    payload = {
        "title": "広告アカウント 効き方一覧",
        "account": data["account"], "period": f"{data['since']}〜{data['until']}",
        "total": {"cost": tot_cost, "cv": int(tot_cv)},
        "form": {"cost": form["cost"], "cv": int(form["cv"])},
        "vialp": {"cost": tot_cost - form["cost"], "cv": int(tot_cv - form["cv"])},
        "zero": {"n": sum(1 for p in lps if not p["cv"]),
                 "cost": sum(p["cost"] for p in lps if not p["cv"])},
        "lps": lps,
    }
    html = INDEX_PAGE.replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    out = os.path.join(args.out, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"→ {out}")


def cmd_from_meta(args):
    """Metaから取ったフォルダを丸ごと受けて、LPごとに照合まで回す。

    人が用意するものは何もない。画像もLPのURLも実績も、すべてMetaから来ている。
    """
    import collections
    import contextlib
    import io
    import types

    data = json.load(open(os.path.join(args.dir, "実績.json"), encoding="utf-8"))
    img_dir = os.path.join(args.dir, "画像")
    have = {os.path.splitext(f)[0]: os.path.join(img_dir, f)
            for f in (os.listdir(img_dir) if os.path.isdir(img_dir) else [])}

    def find_image(r):
        """実績.jsonに画像のパスが無くても、フォルダの中から広告名で探す。"""
        if r.get("画像") and os.path.exists(r["画像"]):
            return r["画像"]
        key = re.sub(r"[^\w\u3040-\u30FF\u4E00-\u9FFF\-]+", "_", (r.get("広告名") or "").strip())[:60].strip("_")
        if key in have:
            return have[key]
        n = norm(r.get("広告名") or "")
        for k, path in have.items():
            if n and norm(k) == n:
                return path
        return ""

    rows = []
    for r in data["rows"]:
        if (r.get("消化") or 0) <= 0:
            continue
        r["画像"] = find_image(r)
        if r["画像"]:
            rows.append(r)
    if not rows:
        sys.exit(f"画像が見つからない。{img_dir} に画像があるか確認する")
    thin_lps = []
    os.makedirs(args.out, exist_ok=True)

    bylp = collections.defaultdict(list)
    for r in rows:
        if r.get("LP", "").startswith("http") and "fb.me" not in r["LP"]:
            bylp[r["LP"]].append(r)

    summary, done = [], []
    thin_map, line_map = {}, {}
    for lp_url, rs in sorted(bylp.items(), key=lambda kv: -sum(r["消化"] for r in kv[1])):
        cost = sum(r["消化"] for r in rs)
        cv = sum(r["CV"] or 0 for r in rs)
        if cost < args.min_cost:
            continue
        slug = re.sub(r"[^\w\-]+", "_", lp_url.split("//", 1)[-1].split("/", 1)[-1])[:50] or "lp"
        print(f"\n■ {lp_url}  {len(rs)}枚 / 消化{cost:,}円 / CV{cv}")

        quiet = io.StringIO()
        try:
            with contextlib.redirect_stdout(quiet):
                a = types.SimpleNamespace(images=[r["画像"] for r in rs], min_conf=0.3, out=None)
                banners = cmd_read(a)
                b = types.SimpleNamespace(url=lp_url, text_file=None, out=None)
                lp = cmd_lp(b)
        except SystemExit as e:
            print(f"   ! 読めなかった: {e}")
            continue
        except Exception as e:
            print(f"   ! 読めなかった: {e}")
            continue

        # 画像のファイル名は広告名から作られているので、実績と突き合わせられる
        # 同じ絵が複数の広告セットに入っていることがあるので、LPの中では合算する
        agg = collections.OrderedDict()
        for r in rs:
            key = os.path.splitext(os.path.basename(r["画像"]))[0]
            a = agg.setdefault(key, {"name": key, "cost": 0.0, "cv": 0.0,
                                     "imp": 0.0, "click": 0.0, "sets": set()})
            a["cost"] += float(r["消化"] or 0)
            a["cv"] += float(r["CV"] or 0)
            a["imp"] += float(r.get("表示") or 0)
            a["click"] += float(r.get("クリック") or 0)
            a["sets"].add(r.get("広告セット", ""))
        perf = []
        for a in agg.values():
            a["cpa"] = a["cost"] / a["cv"] if a["cv"] else None
            a.pop("sets", None)
            perf.append(a)

        v = types.SimpleNamespace(
            banners=None, lp=None, _banners=banners, _lp=lp, _perf=perf,
            perf=None, perf_row=None, period=f"{data['since']}〜{data['until']}",
            cv_name=(rs[0].get("CVの種類") or ""), img_width=args.img_width, quality=80,
            standalone=args.standalone, out=os.path.join(args.out, slug + ".html"))
        with contextlib.redirect_stdout(quiet):
            cmd_view(v)
            v.out = os.path.join(args.out, slug + "_レポート.md")
            cmd_report(v)
        done.append((lp_url, slug, cost, cv))

        thin = lp_is_thin(lp)
        thin_map[lp_url] = thin
        line_map[lp_url] = len(lp.get("lines", []))
        if thin:
            thin_lps.append((lp_url, len(lp.get("lines", []))))
            print(f"   ※ このLPは本文が{len(lp.get('lines', []))}行しかない"
                  "（診断クイズなど）。文言の照合は成立しないので参考値として扱う")
        # まとめ用に、CVが出ていない枚のLP一致を拾う
        m = {x["name"]: x for x in match_banners(banners, lp)}
        for a in sorted(perf, key=lambda x: -x["cost"]):
            mm = m.get(a["name"])
            if not mm:
                continue
            rate = mm["一致率_文字の大きさ重み"]
            miss = mm["LPに無い行"]
            ctr = (a["click"] / a["imp"] * 100) if a["imp"] else 0
            print(f"   {'★' if a['cv'] else ' '} {a['name'][:28]:<28} "
                  f"消化{a['cost']:>7,.0f} CV{a['cv']:>3.0f} CTR{ctr:>5.2f}%  LP一致{rate*100:>3.0f}%"
                  + (f"  ← LPに無い: {' / '.join(miss[:2])}" if miss else ""))
            if thin:
                continue
            if not a["cv"] and a["cost"] >= args.min_cost / 2 and miss:
                summary.append({"lp": lp_url, "ad": a["name"], "cost": a["cost"],
                                "ctr": round(ctr, 2), "rate": rate, "miss": miss[:3]})

    # ---- まとめ ----
    L = [f"# クリエイティブの傾向 — {data['since']}〜{data['until']}", "",
         f"広告アカウント {data['account']} / LP {len(done)}本", ""]
    L.append("各バナーの文言が、飛び先LPの本文に実在するかを1行ずつ照合した。")
    L.append("CPAの差は配信量でも説明がつくが、「LPに出てこない」は配信量と関係なく確認できる事実なので、")
    L.append("クリック後に話が変わっているかどうかの判断に使える。")
    L.append("")
    if summary:
        L.append("## 獲得が無く、かつ文言がLPに出てこないもの")
        L.append("")
        L.append("クリックは取れているのにCVが出ていない。バナーで言っていることがLPに書かれていない。")
        L.append("")
        L.append("| クリエイティブ | LP | 消化 | CTR | LP一致率 | LPに出てこない文言 |")
        L.append("|---|---|---:|---:|---:|---|")
        for s in sorted(summary, key=lambda x: -x["cost"]):
            L.append(f"| {s['ad']} | {s['lp'].rsplit('/',1)[-1]} | {s['cost']:,.0f}円 | {s['ctr']}% | "
                     f"{round(s['rate']*100)}% | {' / '.join(s['miss'])} |")
        L.append("")
    if thin_lps:
        L.append("## 照合が成立しないLP")
        L.append("")
        L.append("本文がほとんど無いLP（診断クイズなど）。バナーの文言と突き合わせる相手が無いので、")
        L.append("一致率は参考値にとどめ、上の表からは外してある。")
        L.append("")
        for u, n in thin_lps:
            L.append(f"- {u} — 本文{n}行")
        L.append("")
    L.append("## LPごとの画面")
    L.append("")
    for lp_url, slug, cost, cv in done:
        L.append(f"- [{lp_url}]({slug}.html) — 消化{cost:,}円 / CV{cv}")
    L.append("")
    out = os.path.join(args.out, "00_まとめ.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    write_index(args, data, done, thin_map, line_map)
    print(f"\n→ {args.out} に LP{len(done)}本ぶんの画面とレポート、00_まとめ.md")


def main():
    ap = argparse.ArgumentParser(description="配信済みバナーの効き方を機械的に分析する")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("read", help="バナー画像の文字・位置・配色を読む")
    p.add_argument("images", nargs="+")
    p.add_argument("-o", "--out")
    p.add_argument("--min-conf", type=float, default=0.3)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("lp", help="飛び先LPの文言・数字・注記を拾う")
    p.add_argument("url", nargs="?")
    p.add_argument("--text-file", help="ブラウザで取った本文テキストを使う（JSで描くLPはこちら）")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_lp)

    p = sub.add_parser("match", help="バナーの文言がLPに実在するか突き合わせる")
    p.add_argument("-b", "--banners", required=True)
    p.add_argument("-l", "--lp", required=True)
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_match)

    p = sub.add_parser("report", help="実績を足して分析レポートの下書きを出す")
    p.add_argument("-b", "--banners", required=True)
    p.add_argument("-l", "--lp", required=True)
    p.add_argument("--perf", help="実績CSV/TSV（列名は 名前/消化/CV/CPA/表示/クリック など）")
    p.add_argument("--perf-row", action="append", help='"名前,消化,CV[,表示,クリック]" を枚数分')
    p.add_argument("--period", help="集計期間（例: 11/1〜11/11）")
    p.add_argument("--cv-name", help="CVの定義（例: フォーム完了）")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("view", help="バナーを並べて見る画面（HTML）を作る")
    p.add_argument("-b", "--banners", required=True)
    p.add_argument("-l", "--lp", required=True)
    p.add_argument("--perf")
    p.add_argument("--perf-row", action="append")
    p.add_argument("--period")
    p.add_argument("--cv-name")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--img-width", type=int, default=760, help="埋め込む画像の横幅(px)")
    p.add_argument("--quality", type=int, default=80)
    p.add_argument("--standalone", action="store_true",
                   help="ローカルで開く用に doctype を付ける（Artifactに出すときは付けない）")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("all", help="フォルダとLPのURLだけで、画面とレポートまで一息に作る")
    p.add_argument("images", nargs="+", help="バナーのフォルダ、または画像ファイル")
    p.add_argument("--lp", dest="lp_url", help="飛び先LPのURL")
    p.add_argument("--lp-text", dest="lp_text", help="JSで描くLPは本文のテキストファイル")
    p.add_argument("--perf")
    p.add_argument("--perf-row", action="append")
    p.add_argument("--period")
    p.add_argument("--cv-name")
    p.add_argument("-o", required=True, help="出力するHTML（同じ名前で _レポート.md も出る）")
    p.add_argument("--img-width", type=int, default=760)
    p.add_argument("--quality", type=int, default=80)
    p.add_argument("--standalone", action="store_true")
    p.add_argument("--keep-json", action="store_true", help="途中のJSONも残す")
    p.set_defaults(func=cmd_all)

    p = sub.add_parser("from-meta", help="Metaから取ったフォルダを受けて、LPごとに照合まで回す")
    p.add_argument("-d", "--dir", required=True, help="meta_api.py insights の出力フォルダ")
    p.add_argument("-o", "--out", required=True, help="出力先フォルダ")
    p.add_argument("--min-cost", type=int, default=5000)
    p.add_argument("--img-width", type=int, default=700)
    p.add_argument("--standalone", action="store_true")
    p.set_defaults(func=cmd_from_meta)

    args = ap.parse_args()
    if args.cmd == "all" and not args.lp_url and not args.lp_text:
        ap.error("--lp（LPのURL）か --lp-text が必要")
    if args.cmd == "lp" and not args.url and not args.text_file:
        ap.error("URL か --text-file のどちらかが必要")
    args.func(args)


if __name__ == "__main__":
    main()
