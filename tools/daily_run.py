#!/usr/bin/env python3
"""毎朝、前日までのMeta広告データを取って、アカウントごとの画面まで作る。

消化のあるアカウントだけを自動で見つけるので、クライアントが増えても設定は要らない。

  python3 daily_run.py --out-root ~/広告分析 --days 28

出来るもの:
  ~/広告分析/index.html            … 全アカウントの一覧（これを公開する）
  ~/広告分析/<アカウント>/index.html  … そのアカウントのLP一覧
  ~/広告分析/<アカウント>/*.html     … LPごとの詳細（重いので毎朝は公開しない）
  ~/広告分析/00_サマリー.md          … 文章の要約
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import meta_api as M                                  # noqa: E402


def slug(name, acc):
    s = re.sub(r"[^\w぀-ヿ一-鿿\-]+", "_", (name or "").strip())[:28].strip("_")
    return s or acc.replace("act_", "")


def active_accounts(tok, min_spend):
    rows = M.paged("me/adaccounts",
                   {"fields": "id,name,account_status,insights.date_preset(last_7d){spend}"}, tok)
    out = []
    for a in rows:
        ins = (a.get("insights") or {}).get("data") or []
        sp = float(ins[0].get("spend") or 0) if ins else 0.0
        if sp >= min_spend:
            out.append({"id": a["id"], "name": a.get("name", ""), "last7": sp})
    out.sort(key=lambda x: -x["last7"])
    return out


DOCTYPE_HEAD = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<style>body{margin:0;font:14px system-ui}img{max-width:100%}'
                '[hidden]{display:none!important}</style></head><body>')

INDEX = r"""<title>広告アカウント 毎朝の一覧</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&family=Roboto+Mono:wght@500;700&display=swap">
<style>
:root{--paper:#F7F8FA;--card:#FFF;--ink:#16202B;--ink-soft:#5A6675;--line:#DDE3EA;
  --accent:#14284B;--accent-soft:#E7ECF4;--mark:#F07A12;--ok:#1C7A4B;--mid:#A8700A;--bad:#BC2E24;
  --sans:"Noto Sans JP",system-ui,sans-serif;--mono:"Roboto Mono",ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#11161C;--card:#192028;--ink:#E7EDF4;--ink-soft:#97A4B4;--line:#2C3843;
  --accent:#9FBCE6;--accent-soft:#1E2A3C;--mark:#F59B4B;--ok:#59C98C;--mid:#DEAF4B;--bad:#F0766A;color-scheme:dark}}
:root[data-theme="dark"]{--paper:#11161C;--card:#192028;--ink:#E7EDF4;--ink-soft:#97A4B4;--line:#2C3843;
  --accent:#9FBCE6;--accent-soft:#1E2A3C;--mark:#F59B4B;--ok:#59C98C;--mid:#DEAF4B;--bad:#F0766A;color-scheme:dark}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.6;margin:0}
.wrap{max-width:1000px;margin:0 auto;padding-left:max(16px,env(safe-area-inset-left,0px));
  padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:28px 56px}
h1{font-size:clamp(1.3rem,1rem + 1.2vw,1.7rem);margin:0 0 4px;font-weight:700}
.meta{color:var(--ink-soft);font-size:.85rem;margin:0 0 18px}
.big{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:8px}
.big div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
.big dt{font-size:.7rem;color:var(--ink-soft);letter-spacing:.06em;margin:0}
.big dd{margin:3px 0 0;font:700 1.5rem/1.15 var(--mono);font-variant-numeric:tabular-nums}
h2{font-size:1rem;margin:30px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--line)}
.tscroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.86rem}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}
th{font-size:.7rem;color:var(--ink-soft);font-weight:500;letter-spacing:.05em;white-space:nowrap}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid color-mix(in srgb,var(--accent) 40%,transparent)}
tr.warn td{background:color-mix(in srgb,var(--bad) 7%,transparent)}
.flag.mute{background:var(--accent-soft);color:var(--ink-soft)}
.flag{display:inline-block;font:500 .68rem/1.5 var(--sans);padding:1px 7px;border-radius:4px;
  background:color-mix(in srgb,var(--bad) 15%,transparent);color:var(--bad);white-space:nowrap}
.err{color:var(--bad);font-size:.8rem}
</style>
<div class="wrap">
  <h1>広告アカウント 毎朝の一覧</h1>
  <p class="meta" id="sub"></p>
  <dl class="big" id="big"></dl>
  <h2>アカウントごと</h2>
  <div class="tscroll"><table id="tbl"></table></div>
</div>
<script id="payload" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('payload').textContent);
const yen=v=>v==null?'—':Math.round(v).toLocaleString('ja-JP')+'円';
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
document.getElementById('sub').textContent=D.period+' ・ 更新 '+D.ranAt+' ・ '+D.accounts.length+'アカウント';
const t=D.accounts.filter(x=>!x.cvUnset).reduce((a,x)=>({cost:a.cost+x.cost,cv:a.cv+x.cv}),{cost:0,cv:0});
const unsetCost=D.accounts.filter(x=>x.cvUnset).reduce((a,x)=>a+x.cost,0);
document.getElementById('big').innerHTML=
  [['消化',yen(t.cost)],['CV',t.cv+'<small> 件</small>'],['CPA',t.cv?yen(t.cost/t.cv):'—']]
  .map(([k,v])=>'<div><dt>'+k+'</dt><dd>'+v+'</dd></div>').join('')
  + (unsetCost?'<div><dt>CV未設定</dt><dd>'+yen(unsetCost)+'<small> の消化</small></dd></div>':'');
document.getElementById('tbl').innerHTML=
 '<thead><tr><th>アカウント</th><th>消化</th><th>CV</th><th>CPA</th><th>気になるところ</th></tr></thead><tbody>'
 + D.accounts.map(a=>'<tr'+((a.zeroCost>0||a.noTracking)&&!a.cvUnset?' class="warn"':'')+'>'
   + '<td>'+(a.page?'<a href="'+esc(a.page)+'">'+esc(a.name)+'</a>':esc(a.name))
   + (a.error?'<div class="err">'+esc(a.error)+'</div>':'')+'</td>'
   + '<td class="n">'+yen(a.cost)+'</td><td class="n">'+a.cv+'</td>'
   + '<td class="n">'+(a.cv?yen(a.cost/a.cv):'—')+'</td>'
   + '<td>'+(a.cvUnset?'<span class="flag mute">CVの定義が未設定</span>'
       :(a.noTracking?'<span class="flag">CVイベントが1件も無い。計測を疑う</span>'
       :(a.zeroCost>0?'<span class="flag">CV0のLPに'+yen(a.zeroCost)+'</span>':'')))+'</td></tr>').join('')
 + '</tbody>';
</script>
"""


def main():
    ap = argparse.ArgumentParser(description="毎朝のMeta広告データ取得と画面づくり")
    ap.add_argument("--out-root", default=os.path.expanduser("~/広告分析"))
    ap.add_argument("--token-file", default=os.path.expanduser("~/.meta_token"))
    ap.add_argument("--days", type=int, default=28, help="前日からさかのぼる日数")
    ap.add_argument("--min-spend", type=float, default=1000, help="直近7日の消化がこれ未満のアカウントは飛ばす")
    ap.add_argument("--only", action="append", help="このアカウントだけ（act_...）")
    ap.add_argument("--img-width", type=int, default=520)
    ap.add_argument("--rebuild-only", action="store_true",
                    help="取得し直さず、手元のデータから画面と要約だけ作り直す")
    args = ap.parse_args()

    tok = M.token(args)
    until = dt.date.today() - dt.timedelta(days=1)
    since = until - dt.timedelta(days=args.days - 1)
    os.makedirs(args.out_root, exist_ok=True)

    accounts = active_accounts(tok, args.min_spend)
    if args.only:
        accounts = [a for a in accounts if a["id"] in args.only]
    print(f"対象 {len(accounts)}アカウント / 期間 {since}〜{until}\n")

    results, lines = [], []
    for i, a in enumerate(accounts, 1):
        sl = slug(a["name"], a["id"])
        meta_dir = os.path.join(args.out_root, sl, "meta")
        out_dir = os.path.join(args.out_root, sl)
        rec = {"name": a["name"] or a["id"], "id": a["id"], "cost": 0, "cv": 0,
               "zeroCost": 0, "noTracking": False, "cvUnset": False, "page": "", "error": ""}
        print(f"[{i}/{len(accounts)}] {a['name']} ({a['id']})")
        try:
            if args.rebuild_only:
                raise StopIteration
            subprocess.run(
                [sys.executable, os.path.join(HERE, "meta_api.py"),
                 "--token-file", args.token_file, "insights", "--account", a["id"],
                 "--since", str(since), "--until", str(until), "--spent-only",
                 "-o", meta_dir],
                check=True, capture_output=True, text=True, timeout=1800)
            subprocess.run(
                [sys.executable, os.path.join(HERE, "banner_insight.py"), "from-meta",
                 "-d", meta_dir, "-o", out_dir, "--img-width", str(args.img_width),
                 "--standalone"],
                check=True, capture_output=True, text=True, timeout=3600)
        except StopIteration:
            pass
        except subprocess.CalledProcessError as e:
            rec["error"] = (e.stderr or e.stdout or "")[-180:].strip()
            print("   ! " + rec["error"][:120])
        except Exception as e:
            rec["error"] = str(e)[:180]
            print("   ! " + rec["error"][:120])

        js = os.path.join(meta_dir, "実績.json")
        if os.path.exists(js):
            d = json.load(open(js, encoding="utf-8"))
            rows = [r for r in d["rows"] if (r.get("消化") or 0) > 0]
            rec["cost"] = sum(r["消化"] for r in rows)
            # 「わからない」のアカウントは CV0 ではなく未設定として扱う
            rec["cvUnset"] = bool(rows) and all(r.get("CV未設定") for r in rows)
            rec["cv"] = 0 if rec["cvUnset"] else int(sum((r["CV"] or 0) for r in rows
                                                         if not r.get("CV未設定")))
            bylp = {}
            for r in rows:
                k = r.get("LP") or "?"
                v = bylp.setdefault(k, [0, 0])
                v[0] += r["消化"]
                v[1] += r["CV"] or 0
            rec["zeroCost"] = 0 if rec["cvUnset"] else sum(c for c, cv in bylp.values() if not cv)
            # CVらしいイベントが1件も無い＝計測が入っていない疑い
            rec["noTracking"] = (not rec["cvUnset"] and rec["cv"] == 0 and rec["cost"] > 0
                                 and not any(r.get("CV系イベント有無") for r in rows))
        acc_index = os.path.join(out_dir, "index.html")
        if os.path.exists(acc_index):
            # 公開時はリンク先ページとして配信されるので doctype が要る
            txt = open(acc_index, encoding="utf-8").read()
            if not txt.lstrip().lower().startswith("<!doctype"):
                open(acc_index, "w", encoding="utf-8").write(DOCTYPE_HEAD + txt + "</body></html>")
            rec["page"] = f"{sl}/index.html"
        results.append(rec)
        cpa = f"{round(rec['cost']/rec['cv']):,}円" if rec["cv"] else "—"
        cvtxt = "CVの定義が未設定" if rec["cvUnset"] else f"CV{rec['cv']} / CPA{cpa}"
        print(f"   消化{rec['cost']:,}円 {cvtxt}")
        lines.append(f"- **{rec['name']}** 消化{rec['cost']:,}円 / {cvtxt}"
                     + (" / **CVイベントが1件も記録されていない。計測を疑う**" if rec["noTracking"] else "")
                     + (f" / CV0のLPに{rec['zeroCost']:,}円" if rec["zeroCost"] and not rec["noTracking"] else "")
                     + (f" / 取得できず: {rec['error'][:80]}" if rec["error"] else ""))

    ran = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = {"period": f"{since}〜{until}", "ranAt": ran, "accounts": results}
    with open(os.path.join(args.out_root, "index.html"), "w", encoding="utf-8") as f:
        f.write(INDEX.replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")))
    known = [r for r in results if not r["cvUnset"]]
    tot_c = sum(r["cost"] for r in known)
    tot_v = sum(r["cv"] for r in known)
    unset_c = sum(r["cost"] for r in results if r["cvUnset"])
    with open(os.path.join(args.out_root, "00_サマリー.md"), "w", encoding="utf-8") as f:
        f.write(f"# 毎朝の広告まとめ {ran}\n\n期間 {since}〜{until} / {len(results)}アカウント\n\n"
                f"CVを設定済みの{len(known)}アカウント：消化{tot_c:,}円 / CV{tot_v} / "
                + (f"CPA{round(tot_c/tot_v):,}円" if tot_v else "CPA—") + "\n\n"
                + (f"※ ほか{len(results)-len(known)}アカウント（消化{unset_c:,}円）はCVの定義が未設定のため、上の集計に入れていない。\n\n"
                   if unset_c else "")
                + "\n".join(lines) + "\n")
    print(f"\n→ {args.out_root}/index.html と 00_サマリー.md")
    print(f"全体 消化{tot_c:,}円 / CV{tot_v} / " + (f"CPA{round(tot_c/tot_v):,}円" if tot_v else "CPA—"))


if __name__ == "__main__":
    main()
