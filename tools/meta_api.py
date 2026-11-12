#!/usr/bin/env python3
"""Meta広告（Marketing API）から、分析に要るものを丸ごと取ってくる。

取るもの：
  - 広告別の実績（消化・表示・クリック・CV・CPA）
  - 広告セット／キャンペーンの所属
  - クリエイティブの画像そのもの（→ そのままOCRに回せる）
  - 飛び先LPのURL（→ LPごとにまとめられる）

トークンは環境変数 META_ACCESS_TOKEN か --token-file から読む。
画面にもファイルにもトークンは書き出さない。

使い方:
  export META_ACCESS_TOKEN="（システムユーザーのトークン）"
  python3 meta_api.py insights --account act_1234567890 \\
      --since 2026-10-26 --until 2026-11-11 -o ~/19番_meta/
  python3 meta_api.py accounts      # 使える広告アカウントを一覧する
  python3 meta_api.py actions --account act_... --since ... --until ...  # CVの種類を調べる
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://graph.facebook.com"
# 新しい順に試す。使えないバージョンは自動で次へ送る
VERSIONS = ["v27.0", "v26.0", "v25.0", "v24.0", "v23.0", "v22.0", "v21.0"]
_version = None

INSIGHT_FIELDS = ("ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,"
                  "spend,impressions,clicks,ctr,cpc,cpm,frequency,reach,"
                  "actions,cost_per_action_type")
AD_FIELDS = ("id,name,status,effective_status,adset{id,name},campaign{id,name},"
             "creative{id,name,image_url,image_hash,thumbnail_url,object_story_spec,asset_feed_spec,"
             "effective_object_story_id,instagram_permalink_url}")

# CVとして数えたい順（先に見つかったものを使う）
CV_PREFER = ["offsite_conversion.fct_lead", "lead", "onsite_conversion.lead_grouped",
             "offsite_conversion.fct_complete_registration", "complete_registration",
             "offsite_conversion.fct_submit_application", "submit_application",
             "offsite_conversion.fct_contact", "contact", "onsite_web_lead"]


def token(args):
    t = os.environ.get("META_ACCESS_TOKEN", "").strip()
    if not t and getattr(args, "token_file", None):
        with open(os.path.expanduser(args.token_file), encoding="utf-8") as f:
            t = f.read().strip()
    if not t:
        sys.exit("トークンが無い。環境変数 META_ACCESS_TOKEN に入れるか --token-file で渡す")
    return t


def hide(s, tok):
    return s.replace(tok, "（トークン）") if tok else s


def call(path, params, tok, tries=3):
    """Graph API を叩く。使えないAPIバージョンは自動で乗り換える。"""
    global _version
    versions = ([_version] if _version else []) + [v for v in VERSIONS if v != _version]
    last = None
    for v in versions:
        q = {k: v for k, v in params.items() if v is not None}
        q["access_token"] = tok
        url = f"{BASE}/{v}/{path.lstrip('/')}?" + urllib.parse.urlencode(q)
        for attempt in range(tries):
            try:
                with urllib.request.urlopen(url, timeout=90) as r:
                    _version = v
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                try:
                    err = json.loads(body).get("error", {})
                except Exception:
                    err = {"message": body[:300]}
                msg = err.get("message", "")
                code = err.get("code")
                if "version" in msg.lower() or code == 2635:
                    last = msg
                    break                      # このバージョンは使えない → 次へ
                if code in (4, 17, 32, 613) or e.code == 429:
                    time.sleep(5 * (attempt + 1))   # 回数制限 → 少し待って再試行
                    last = msg
                    continue
                sys.exit(f"Meta APIがエラーを返した: {hide(msg, tok)}")
            except Exception as e:
                last = str(e)
                time.sleep(2 * (attempt + 1))
    sys.exit(f"Meta APIに繋がらない: {hide(str(last), tok)}")


def paged(path, params, tok, limit_pages=50):
    out, after, page = [], None, 0
    while page < limit_pages:
        q = dict(params)
        q["limit"] = params.get("limit", 200)
        if after:
            q["after"] = after
        res = call(path, q, tok)
        out += res.get("data", [])
        cur = (res.get("paging") or {}).get("cursors") or {}
        after = cur.get("after")
        if not after or not res.get("data"):
            break
        page += 1
    return out


# ---------------------------------------------------------------- 取り出し

CV_LIKE = ("offsite_conversion.", "lead", "onsite_conversion.lead",
           "complete_registration", "submit_application", "purchase", "contact")


def has_cv_event(actions):
    """CVらしいイベントが1件でも記録されているか。計測が入っているかの判断に使う。"""
    for a in (actions or []):
        t = a.get("action_type", "")
        if any(t.startswith(k) for k in CV_LIKE) and float(a.get("value") or 0) > 0:
            return True
    return False


def pick_cv(actions, prefer=None):
    """actions から、CVとして数えるものを拾う。prefer は文字列でもリストでもよい。

    リストを渡すと全部足す（出張買取・宅配買取・無料査定のように種類が分かれている場合）。
    """
    by = {a.get("action_type"): float(a.get("value") or 0) for a in (actions or [])}
    if isinstance(prefer, (list, tuple)):
        total = sum(by.get(t, 0) for t in prefer)
        return (list(prefer), total) if prefer else (None, None)
    if not actions:
        return None, None
    order = ([prefer] if prefer else []) + CV_PREFER
    for t in order:
        if t in by:
            return t, by[t]
    return None, None


def lp_url(creative):
    """クリエイティブの飛び先LPを取り出す。置き場所が何通りかある。"""
    if not creative:
        return ""
    spec = creative.get("object_story_spec") or {}
    for key in ("link_data", "video_data", "template_data"):
        d = spec.get(key) or {}
        for f in ("link", "caption"):
            v = d.get(f)
            if isinstance(v, str) and v.startswith("http"):
                return v
        cta = (d.get("call_to_action") or {}).get("value") or {}
        if isinstance(cta.get("link"), str) and cta["link"].startswith("http"):
            return cta["link"]
    afs = creative.get("asset_feed_spec") or {}
    for u in (afs.get("link_urls") or []):
        for f in ("website_url", "display_url"):
            if isinstance(u.get(f), str) and u[f].startswith("http"):
                return u[f]
    return ""


def image_hashes(creative):
    """クリエイティブに入っている画像ハッシュを集める。元サイズの取り直しに使う。"""
    out = []
    if not creative:
        return out
    if creative.get("image_hash"):
        out.append(creative["image_hash"])
    spec = creative.get("object_story_spec") or {}
    for key in ("link_data", "video_data", "template_data"):
        d = spec.get(key) or {}
        if d.get("image_hash"):
            out.append(d["image_hash"])
    afs = creative.get("asset_feed_spec") or {}
    for im in (afs.get("images") or []):
        if im.get("hash"):
            out.append(im["hash"])
    return out


def resolve_hashes(account, hashes, tok):
    """画像ハッシュ → 元サイズのURL。サムネイルしか無いクリエイティブを救う。"""
    out = {}
    hs = [h for h in dict.fromkeys(hashes) if h]
    for i in range(0, len(hs), 30):
        chunk = hs[i:i + 30]
        try:
            res = call(f"{account}/adimages",
                       {"hashes": json.dumps(chunk), "fields": "hash,url,width,height"}, tok)
        except SystemExit:
            continue
        for im in res.get("data", []):
            if im.get("url"):
                out[im["hash"]] = im["url"]
    return out


def image_url(creative):
    if not creative:
        return ""
    for f in ("image_url", "thumbnail_url"):
        if creative.get(f):
            return creative[f]
    afs = creative.get("asset_feed_spec") or {}
    for im in (afs.get("images") or []):
        if im.get("url"):
            return im["url"]
    return ""


def strip_params(u):
    """計測パラメータを落として、LPごとにまとめられる形にする。"""
    if not u:
        return ""
    p = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/") or "/", "", ""))


def safe_name(s, fallback="ad"):
    s = re.sub(r"[^\w぀-ヿ一-鿿\-]+", "_", (s or "").strip())
    return (s[:60] or fallback).strip("_")


# ---------------------------------------------------------------- コマンド

def cmd_accounts(args):
    tok = token(args)
    rows = paged("me/adaccounts", {"fields": "id,name,account_status,currency"}, tok)
    if not rows:
        print("使える広告アカウントが無い。システムユーザーにアカウントが割り当てられているか確認する")
    for a in rows:
        state = "有効" if a.get("account_status") == 1 else f"状態{a.get('account_status')}"
        print(f"  {a['id']}  {a.get('name','')}  ({state} / {a.get('currency','')})")
    return rows


def cmd_actions(args):
    """CVとして何を数えるか決めるために、出てくる action_type を並べる。"""
    tok = token(args)
    rows = paged(f"{args.account}/insights",
                 {"level": "ad", "fields": "actions",
                  "time_range": json.dumps({"since": args.since, "until": args.until})}, tok)
    seen = {}
    for r in rows:
        for a in (r.get("actions") or []):
            seen[a["action_type"]] = seen.get(a["action_type"], 0) + float(a.get("value") or 0)
    if not seen:
        print("この期間に発生したアクションが無い")
    chosen = next((t for t in CV_PREFER if t in seen), None)
    for t, v in sorted(seen.items(), key=lambda kv: -kv[1]):
        print(f"  {v:10,.0f}  {t}" + ("  ←既定ではこれをCVに使う" if t == chosen else ""))
    print("\n--cv-action で明示できる。指定が無ければ上の優先順で自動に選ぶ")
    return seen


def custom_conversions(account, tok, with_rules=False):
    """カスタムコンバージョンの id→名前。LPごとのCVを当てるのに使う。"""
    try:
        cc = paged(f"{account}/customconversions",
                   {"fields": "id,name,rule" if with_rules else "id,name"}, tok)
    except SystemExit:
        return {}
    if with_rules:
        return {c["id"]: c for c in cc}
    return {c["id"]: c.get("name", "") for c in cc}


def counts_all_pageviews(rule):
    """「event=PageView ＋ URLはドメインだけ」のルールは、サイト全体の閲覧を数えてしまう。

    サンクスページのつもりで設定されていることがあるので、CVに選ぶ前に警告する。
    （「サンクス合計」という名前でも中身がこれだった例がある。90件に見えて実際は5件だった）
    """
    if isinstance(rule, str):
        try:
            rule = json.loads(rule)
        except Exception:
            return False, ""
    s = json.dumps(rule, ensure_ascii=False)
    if '"PageView"' not in s:
        return False, ""
    urls = re.findall(r'"i_contains":\s*"([^"]+)"', s)
    if not urls:
        return True, "URLの絞り込みが無い"
    for u in urls:
        tail = re.sub(r"^https?://", "", u.replace("\\/", "/")).rstrip("/")
        if "/" not in tail:
            return True, f"URLがドメインだけ（{tail}）"
    return False, ""


# CVとして数えてよい名前／数えてはいけない名前
CV_GOOD = [r"サンクス合計", r"CV合計", r"サンクス", r"予約完了", r"申[込こ]み?完了",
           r"問[いヽ]?合[わ]?せ完了", r"送信完了", r"完了", r"成約", r"申[込こ]", r"予約"]
CV_BAD = re.compile(r"(mCV|ｍCV|クリック|画面|入力|遷移|スクロール|滞在|閲覧|PV|マイクロ|LINE友[達だ]ち?追加)")
CV_OVERRIDE = os.environ.get("META_CV_CONFIG") or os.path.expanduser("~/.config/meta_cv.json")


def load_overrides():
    """アカウントごとに「何をCVと数えるか」を人が決めた設定。

    クライアント名が入るので、クラウドで回すときはファイルではなく
    環境変数 META_CV_JSON（＝GitHubのシークレット）から読む。
    """
    raw = os.environ.get("META_CV_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            print("! META_CV_JSON が読めない（JSONの形を確認する）", file=sys.stderr)
            return {}
    try:
        with open(CV_OVERRIDE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def cv_by_name(ccmap):
    """カスタムコンバージョンの名前から、本命のCVを選ぶ。

    マイクロCV（mCV・クリック・入力画面）は除く。選べなければ None を返して
    「未設定」として扱う。0件と表示するより、分からないと言うほうが安全。
    """
    cands = [(cid, n) for cid, n in ccmap.items() if n and not CV_BAD.search(n)]
    for pat in CV_GOOD:
        hit = [(cid, n) for cid, n in cands if re.search(pat, n)]
        if len(hit) == 1:
            return f"offsite_conversion.custom.{hit[0][0]}", hit[0][1]
        if len(hit) > 1:
            # 同じ強さの候補が複数あるときは決め打ちしない
            return None, "候補が複数: " + " / ".join(n for _, n in hit[:4])
    return None, ""


def cv_for_lp(lp, ccmap):
    """LPのURL先頭の番号（/19_... ）と同じ番号を持つカスタムCVを探す。

    このクライアントは「サンクス_19」のようにLP番号で名前を付けているので、
    LPごとに何をCVとして数えるかを自動で決められる。
    """
    m = re.search(r"/(?:[a-z]+/)?(\d{1,2})[_-]", urllib.parse.urlsplit(lp or "").path)
    if not m:
        return None, None
    num = int(m.group(1))
    for cid, name in ccmap.items():
        n = re.search(r"(\d{1,2})", name or "")
        if n and int(n.group(1)) == num and ("サンクス" in name or "thanks" in name.lower()):
            return f"offsite_conversion.custom.{cid}", name
    return None, None


def cmd_insights(args):
    tok = token(args)
    os.makedirs(args.out, exist_ok=True)
    img_dir = os.path.join(args.out, "画像")
    os.makedirs(img_dir, exist_ok=True)

    tr = json.dumps({"since": args.since, "until": args.until})
    print(f"実績を取得中… {args.account} {args.since}〜{args.until}")
    q = {"level": "ad", "fields": INSIGHT_FIELDS, "time_range": tr}
    if getattr(args, "daily", False):
        q["time_increment"] = 1
    if args.spent_only:
        q["filtering"] = json.dumps([{"field": "spend", "operator": "GREATER_THAN", "value": 0}])
    ins = paged(f"{args.account}/insights", q, tok)
    ins = [r for r in ins if r]
    print(f"  広告 {len(ins)}件")

    print("クリエイティブを取得中…")
    ads = paged(f"{args.account}/ads", {"fields": AD_FIELDS}, tok)
    admap = {a["id"]: a for a in ads}
    print(f"  広告の定義 {len(ads)}件")

    # サムネイルしか無いクリエイティブのために、画像ハッシュから元サイズを引く
    allh = []
    for a in ads:
        allh += image_hashes(a.get("creative") or {})
    hashmap = resolve_hashes(args.account, allh, tok) if allh else {}
    if hashmap:
        print(f"  元サイズの画像 {len(hashmap)}件を解決")

    ccmap = {} if args.cv_action else custom_conversions(args.account, tok)
    conf = load_overrides().get(args.account)
    ov = bool(conf)                      # 設定ファイルに行があるか（空リスト＝わからない も含む）
    acc_cv, acc_cv_name = (None, "")
    if args.cv_action:
        acc_cv, acc_cv_name = args.cv_action, args.cv_action
    elif conf:
        acts = conf.get("actions") or []
        if acts:
            acc_cv = list(acts)
            acc_cv_name = " ＋ ".join(ccmap.get(a.rsplit(".", 1)[-1], a) for a in acts)
        else:
            acc_cv, acc_cv_name = None, "（わからない、として設定済み）"
    elif ccmap:
        acc_cv, acc_cv_name = cv_by_name(ccmap)
    if acc_cv:
        print(f"CVとして数えるもの: {acc_cv_name}")
        ids = [a.rsplit(".", 1)[-1] for a in (acc_cv if isinstance(acc_cv, list) else [acc_cv])
               if isinstance(a, str) and a.startswith("offsite_conversion.custom.")]
        if ids:
            full = custom_conversions(args.account, tok, with_rules=True)
            for cid in ids:
                c = full.get(cid) or {}
                bad, why = counts_all_pageviews(c.get("rule"))
                if bad:
                    print(f"  ★ 「{c.get('name','')}」はサイト全体のPageViewを数えるルール（{why}）。"
                          "サンクスページではないので、CVとしては多すぎる数字が出る")
    elif ccmap:
        print(f"! CVをどれにするか決められない（{acc_cv_name or 'サンクス/完了らしい名前が無い'}）。"
              f"\n  カスタムCV: " + " / ".join(list(ccmap.values())[:8])
              + f"\n  {CV_OVERRIDE} に \"{args.account}\": \"<名前>\" を書くと確定する")

    rows = []
    for r in ins:
        ad = admap.get(r.get("ad_id"), {})
        cre = ad.get("creative") or {}
        lp = strip_params(lp_url(cre))
        # LP番号で紐付く命名（サンクス_19 など）があればLPごとに、無ければアカウント共通で
        want, cv_name = cv_for_lp(lp, ccmap) if (ccmap and not args.cv_action) else (None, "")
        if not want:
            want, cv_name = acc_cv, acc_cv_name
        cv_type, cv = pick_cv(r.get("actions"), want) if want else (None, None)
        cv_unknown = want is None
        spend = float(r.get("spend") or 0)
        rows.append({
            "日付": r.get("date_start", ""),
            "広告名": r.get("ad_name") or ad.get("name", ""),
            "広告ID": r.get("ad_id"),
            "広告セット": r.get("adset_name", ""),
            "広告セットID": r.get("adset_id", ""),
            "キャンペーン": r.get("campaign_name", ""),
            "LP": lp,
            "LP_元のURL": lp_url(cre),
            "消化": round(spend),
            "表示": int(float(r.get("impressions") or 0)),
            "クリック": int(float(r.get("clicks") or 0)),
            "CTR": round(float(r.get("ctr") or 0), 2),
            "CV": "" if cv_unknown else int(cv or 0),
            "CV未設定": cv_unknown,
            "CVの種類": cv_name or (", ".join(cv_type) if isinstance(cv_type, list) else (cv_type or "")),
            "CV系イベント有無": has_cv_event(r.get("actions")),
            "CPA": round(spend / cv) if (cv and not cv_unknown) else "",
            "状態": ad.get("effective_status", ""),
            "画像URL": next((hashmap[h] for h in image_hashes(cre) if h in hashmap),
                           image_url(cre)),
            "管理画面": ("https://adsmanager.facebook.com/adsmanager/manage/ads?act="
                     + args.account.replace("act_", "")
                     + "&selected_ad_ids=" + str(r.get("ad_id"))),
        })

    rows.sort(key=lambda x: (x["LP"], -(x["消化"] or 0)))

    # 画像を落とす（OCRにそのまま回せるように広告名でファイル名を付ける）
    if not args.no_images:
        print("画像を取得中…")
        got = 0
        for r in rows:
            if not r["画像URL"]:
                continue
            name = safe_name(r["広告名"], r["広告ID"]) + ".jpg"
            path = os.path.join(img_dir, name)
            if os.path.exists(path):
                r["画像"] = path
                got += 1
                continue
            try:
                req = urllib.request.Request(r["画像URL"], headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as f:
                    f.write(resp.read())
                r["画像"] = path
                got += 1
            except Exception as e:
                print(f"  ! {r['広告名']} の画像が落とせない: {e}")
                r["画像"] = ""
        print(f"  {got}枚 → {img_dir}")

    cols = ["日付", "広告名", "広告セット", "キャンペーン", "LP", "消化", "表示", "クリック", "CTR",
            "CV", "CPA", "CVの種類", "CV未設定", "CV系イベント有無", "状態", "画像", "広告ID", "広告セットID",
            "管理画面", "画像URL", "LP_元のURL"]
    csv_path = os.path.join(args.out, "実績.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    with open(os.path.join(args.out, "実績.json"), "w", encoding="utf-8") as f:
        json.dump({"account": args.account, "since": args.since, "until": args.until,
                   "rows": rows}, f, ensure_ascii=False, indent=2)
    print(f"→ {csv_path}")

    # LPごとのまとまりを見せる
    bylp = {}
    for r in rows:
        d = bylp.setdefault(r["LP"] or "(LPが取れず)", {"消化": 0, "CV": 0, "枚": 0})
        d["消化"] += r["消化"] or 0
        d["CV"] += (r["CV"] or 0) if not r.get("CV未設定") else 0
        d["枚"] += 1
    print("\nLPごと:")
    for lp, d in sorted(bylp.items(), key=lambda kv: -kv[1]["消化"]):
        cpa = f"{round(d['消化']/d['CV']):,}円" if d["CV"] else "—"
        print(f"  {d['枚']:>2}枚  消化{d['消化']:>9,}円  CV{d['CV']:>4}  CPA{cpa:>10}  {lp}")
    return rows


def cmd_analyze(args):
    """取ってきた実績を、LPごと・広告セットごとに割って、気づきを立てる。"""
    import collections
    d = json.load(open(os.path.join(args.dir, "実績.json"), encoding="utf-8"))
    rows = [r for r in d["rows"] if (r.get("消化") or 0) > 0]
    if not rows:
        sys.exit("消化のある広告が無い")

    tot_cost = sum(r["消化"] for r in rows)
    tot_cv = sum(r["CV"] or 0 for r in rows)
    ctrs = sorted(r["CTR"] for r in rows if r.get("CTR"))
    ctr_hi = ctrs[int(len(ctrs) * 0.75)] if ctrs else 0

    L = []
    L.append(f"# クリエイティブ分析 — {d['since']}〜{d['until']}")
    L.append("")
    L.append(f"- 広告アカウント: {d['account']}")
    L.append(f"- 消化 {tot_cost:,}円 / CV {tot_cv}件 / "
             + (f"CPA {round(tot_cost/tot_cv):,}円" if tot_cv else "CPA —"))
    L.append("")

    bylp = collections.defaultdict(list)
    for r in rows:
        bylp[r["LP"] or "(LPが取れず)"].append(r)

    # ---- LPごとの比較 ----
    L.append("## LPごと")
    L.append("")
    L.append("| LP | 枚数 | 消化 | CV | CPA |")
    L.append("|---|---:|---:|---:|---:|")
    lp_sorted = sorted(bylp.items(), key=lambda kv: -sum(r["消化"] for r in kv[1]))
    for lp, rs in lp_sorted:
        cost = sum(r["消化"] for r in rs)
        cv = sum(r["CV"] or 0 for r in rs)
        L.append(f"| {lp} | {len(rs)} | {cost:,}円 | {cv} | "
                 + (f"{round(cost/cv):,}円 |" if cv else "— |"))
    L.append("")

    notes = []
    # ---- LPごとのクリエイティブ ----
    for lp, rs in lp_sorted:
        cost = sum(r["消化"] for r in rs)
        cv = sum(r["CV"] or 0 for r in rs)
        if cost < args.min_cost:
            continue
        L.append(f"### {lp}")
        L.append("")
        L.append(f"消化 {cost:,}円 / CV {cv}件 / "
                 + (f"CPA {round(cost/cv):,}円" if cv else "CPA —"))
        L.append("")
        L.append("| | クリエイティブ | 広告セット | 消化 | CTR | CV | CPA |")
        L.append("|---|---|---|---:|---:|---:|---:|")
        rs.sort(key=lambda r: (-(r["CV"] or 0), -(r["消化"] or 0)))
        for r in rs:
            mark = "★" if r["CV"] else ""
            cpa = f"{r['CPA']:,}円" if r["CPA"] else "—"
            L.append(f"| {mark} | {r['広告名']} | {r['広告セット']} | {r['消化']:,}円 | "
                     f"{r['CTR']}% | {r['CV']} | {cpa} |")
        L.append("")

        # 気づきを立てる
        for r in rs:
            if r["CV"] == 0 and r.get("CTR", 0) >= ctr_hi and r["消化"] >= args.min_cost / 2:
                notes.append(f"**{r['広告名']}**（{lp}）はCTR {r['CTR']}%と高いのにCV0。"
                             f"消化{r['消化']:,}円。クリックは取れているのでクリック後で落ちている。"
                             "バナーの文言がLPに出てこない可能性を先に確かめる")
        if cv == 0 and cost >= args.min_cost * 2:
            notes.append(f"**{lp}** は{len(rs)}枚で消化{cost:,}円を使ってCV0。"
                         "クリエイティブ個別ではなくLP側を疑う")
        shares = [r["消化"] / cost for r in rs]
        if len(rs) > 1 and max(shares) > 0.6:
            top = max(rs, key=lambda r: r["消化"])
            notes.append(f"**{lp}** は消化が{top['広告名']}に{max(shares)*100:.0f}%寄っている。"
                         "ほかの枚のCPAは判断材料にならない")

    # ---- 同じ絵が複数セットに入っている ----
    byname = collections.defaultdict(list)
    for r in rows:
        byname[r["広告名"]].append(r)
    dup = {n: rs for n, rs in byname.items()
           if len({r["広告セットID"] for r in rs}) > 1}
    if dup:
        L.append("## 同じクリエイティブが複数の広告セットに入っている")
        L.append("")
        L.append("同じ絵なので、差はセット側（ターゲティング・配信先）の要因。クリエイティブの優劣とは読まない。")
        L.append("")
        L.append("| クリエイティブ | 広告セット | 消化 | CTR | CV |")
        L.append("|---|---|---:|---:|---:|")
        for n, rs in sorted(dup.items(), key=lambda kv: -sum(r["消化"] for r in kv[1]))[:12]:
            for r in sorted(rs, key=lambda r: -r["消化"]):
                L.append(f"| {n} | {r['広告セット']} | {r['消化']:,}円 | {r['CTR']}% | {r['CV']} |")
        L.append("")

    if notes:
        L.append("## 気づき")
        L.append("")
        for n in dict.fromkeys(notes):
            L.append(f"- {n}")
        L.append("")

    L.append("## 次に確かめること")
    L.append("")
    L.append("- CTRが高くCVが出ていないクリエイティブは、`banner_insight.py` で**バナーの文言がLP本文に実在するか**を照合する（画像とLPのURLはこのフォルダに揃っている）")
    L.append("- CV件数が1桁のLPは、CPAの差を配信量の偏りと切り分けてから読む")
    L.append("")

    text = "\n".join(L)
    out = args.out or os.path.join(args.dir, "分析.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"→ {out}")
    print(f"\n全体: 消化 {tot_cost:,}円 / CV {tot_cv}件 / "
          + (f"CPA {round(tot_cost/tot_cv):,}円" if tot_cv else "CPA —"))
    if notes:
        print(f"\n気づき {len(dict.fromkeys(notes))}件:")
        for n in list(dict.fromkeys(notes))[:8]:
            print("  - " + re.sub(r"\*\*", "", n))


def main():
    ap = argparse.ArgumentParser(description="Meta広告から実績・クリエイティブ・LPを取ってくる")
    ap.add_argument("--token-file", help="トークンを書いたファイル（既定は環境変数 META_ACCESS_TOKEN）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("accounts", help="使える広告アカウントを一覧する")
    p.set_defaults(func=cmd_accounts)

    for name, fn, helptext in (("actions", cmd_actions, "CVの種類を調べる"),
                               ("insights", cmd_insights, "実績・クリエイティブ・LPを取る")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--account", required=True, help="act_1234567890")
        p.add_argument("--since", required=True, help="2026-10-26")
        p.add_argument("--until", required=True, help="2026-11-11")
        if name == "insights":
            p.add_argument("-o", "--out", required=True, help="出力先フォルダ")
            p.add_argument("--cv-action", help="CVとして数える action_type")
            p.add_argument("--no-images", action="store_true")
            p.add_argument("--spent-only", action="store_true", help="消化のある広告だけ")
        p.set_defaults(func=fn)

    p = sub.add_parser("analyze", help="取った実績をLPごと・広告セットごとに割って気づきを立てる")
    p.add_argument("-d", "--dir", required=True, help="insights の出力フォルダ")
    p.add_argument("--min-cost", type=int, default=5000, help="この消化額未満のLPは詳細を出さない")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_analyze)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
