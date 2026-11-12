# banner_insight — 配信済みバナーの「どれが効いていて、なぜか」を出す

渡されるのはたいてい「Driveのバナーのフォルダ」と「01だけCPA5,000円代、03が10,000円、あとは0件」の1行だけ。
そこから次の4枚を決めるまでに毎回やる手作業のうち、**機械でできる部分だけ**をこのツールに寄せた。

勝ち負けの理由を決めるのは人。ツールが出すのは、その理由を支える**事実**（誰でも同じ結果になるもの）。

## 何が自動になるか

| できること | 中身 |
|---|---|
| バナーの文言を全部読む | macOS標準のOCR（ネットに出さない）。文字の位置・**文字高さ%**・390px換算の実寸pxまで出す |
| 文字が沈んでいないか | 行ごとに背景色と文字色を測り、コントラスト比が3.0未満を警告 |
| LPの文言・数字・注記を拾う | FV候補、本文に実在する数字、その近くの注記・出典 |
| **バナーの文言がLPに実在するか** | 1行ずつLP本文と突き合わせて ◎○△✕。**勝ち負けの一番強い根拠**になる |
| 実績と並べる | 消化・CV・CPA・配信シェア × LP一致率。CVが少ない／消化が偏っているときは自動で注意を出す |
| 次の4枚の枠 | 訴求2×デザイン2のマトリクスと、配信の分け方（絵が同じ2枚は別の広告セット） |
| 新訴求の候補 | LP本文に実在する数字だけを、**出典つきの行を上に**並べる |
| **見る画面を作る** | バナーに枠を重ねて並べたHTML。枠の色＝LP一致。文言はその場で直せて判定もやり直す |

サムネイルで下が見切れてCTA帯を見落とす事故は、OCRが元ファイルを端まで読むので起きない。

## 使い方

**これは指示者が叩くものではない。** 指示者は「19番のバナー、効き方見て」と言うだけでよく、
下のコマンドはこちら（Claude）が回す。バナー画像はローカルに落としてから渡す（Driveのフォルダは直接読めない）。

### ふだんはこれ1本

```bash
python3 ~/skills/tools/banner_insight/banner_insight.py all ~/案件のバナーフォルダ/ \
  --lp "https://example.com/lp/" \
  --perf-row "01,52000,10" --perf-row "02,31000,0" \
  --period "11/1〜11/11" --cv-name "フォーム完了" -o ~/19番.html
```

フォルダを渡すと中の画像を拾い（`00_` で始まる一覧シートは除く）、LPを読み、
**画面（HTML）と社内説明の下書き（_レポート.md）を両方**出す。途中のJSONは要るときだけ `--keep-json`。

### 工程ごとに回したいとき

```bash
T=~/skills/tools/banner_insight

# 1. バナーを読む
python3 $T/banner_insight.py read ~/落としたバナー/*.png -o banners.json

# 2. LPを読む（JSで描くLPは、ブラウザで取った本文を --text-file で渡す）
python3 $T/banner_insight.py lp "https://example.com/lp/" -o lp.json
python3 $T/banner_insight.py lp --text-file lp.txt -o lp.json

# 3. 突き合わせ（これだけ見れば勝ち負けの理由が見える）
python3 $T/banner_insight.py match -b banners.json -l lp.json

# 4. 実績を足してレポートの下書き
python3 $T/banner_insight.py report -b banners.json -l lp.json \
  --perf-row "01,52000,10" --perf-row "02,31000,0" \
  --period "11/1〜11/11" --cv-name "フォーム完了" -o report.md
```

```bash
# 5. 見る画面（Artifactに出すならそのまま、ローカルで開くなら --standalone）
python3 $T/banner_insight.py view -b banners.json -l lp.json \
  --perf-row "01,52000,10" --perf-row "02,31000,0" \
  --period "11/1〜11/11" --cv-name "フォーム完了" -o view.html
```

画面でできること：

- バナーに**OCRの枠**を重ねて表示。枠の色が緑＝LPにそのまま載っている／黄＝一部だけ／**赤＝LPに出てこない**
- **スマホ幅(390px)** に切り替えて、実際に読めるかを見る
- 行をクリックすると、その枠が光る
- **文言はその場で直せる**（OCRが `？？？？？円` を `22223円` と読むような誤読を直すと、判定と一致率をやり直す）
- CPAが安い順・LP一致率が高い順に並べ替え
- 下に、次の訴求の候補（出典つきを上に）と、次の4枚の割り方

実績がスプレッドシートで来たらCSV/TSVに落として `--perf 実績.csv`。
列名は `名前/消化/CV/CPA/表示/クリック`（英語名でもよい）。CPAが無ければ消化÷CVで出す。

## 読むときの注意

- **LP一致率は数字の良し悪しではない。**「クリック後に話が変わっていないか」を見るだけのもの。
  CPAの差は配信量でも説明がつくが、「LPに1行も出てこない」は配信量と関係なく確認できる事実なので、社内説明ではそちらを根拠にする。
- **記号はOCRが誤読する**（`×` が `✕` や `★` になる）。文言の最終確認は目で見る。
- 「近くの注記候補」は近さで拾っただけ。**本当にその数字の注記か必ず目で確認**してからバナーに載せる。
- 役どころ（見出し／CTA・帯／注記）は位置と文字の大きさからの推定。帯が画像のときは目でも見る。


## Metaから直接取る（meta_api.py）

Driveから画像を落とす必要も、実績を手で写す必要もない。**実績・クリエイティブ画像・飛び先LPのURLが全部Metaから来る。**

```bash
T=~/skills/tools/banner_insight

# 使える広告アカウントを見る
python3 $T/meta_api.py --token-file ~/.meta_token accounts

# CVとして何が記録されているか調べる
python3 $T/meta_api.py --token-file ~/.meta_token actions \
  --account act_970150596935059 --since 2026-10-26 --until 2026-11-11

# 実績・画像・LPをまとめて取る
python3 $T/meta_api.py --token-file ~/.meta_token insights \
  --account act_970150596935059 --since 2026-10-26 --until 2026-11-11 \
  --spent-only -o ~/案件_meta/

# LP別・クリエイティブ別の優劣と気づきを出す
python3 $T/meta_api.py analyze -d ~/案件_meta/

# 取った画像とLPで、文言の照合まで一気に回す（LPごとに画面とレポートが出る）
python3 $T/banner_insight.py from-meta -d ~/案件_meta/ -o ~/案件_分析/
```

### CVの決まり方

既定では `offsite_conversion.fct_lead` などを優先順に探すが、それだと拾えないことがある。
クライアントによってはLPごとにカスタムコンバージョン（`サンクス_19` など）が作られていて、
**名前の番号がLPのURLの番号と対応している**ので、そこを自動で突き合わせる。
この名前の付け方が違うクライアントでは `--cv-action` で明示する。
何が記録されているかは `actions` コマンドで分かる。

### つまずいたところ（同じ罠を踏まないために）

- **トークン**：システムユーザーのトークンは、アプリのMarketing APIが「標準アクセス」でないと発行の承認が通らない。
  標準アクセスにはビジネス認証（数日）が要る。急ぐときはグラフAPIエクスプローラのユーザートークンでよい
  （`developers.facebook.com/tools/debug/accesstoken` で60日に延長できる）。取れるデータは同じ。
- **画像がサムネイルになる**：`image_url` が無いクリエイティブがあり、放っておくと64×64になってOCRが何も読めない。
  画像ハッシュから `/act_X/adimages` で元サイズを引き直している。
- **LPの本文が取れない**：JavaScriptで描くLPは、HTMLを取るだけではタイトルしか入らない。
  本文が20行未満ならChromeで描画して取り直す。
- **照合が成立しないLPがある**：診断クイズのようなLPは、描画しても本文が10行ほどしかない。
  バナーの文言と突き合わせる相手が無いので、`from-meta` は本文25行未満のLPを「参考値」として分けて出す。
  **こういうLPの低い一致率を「LPに出てこない」と読んではいけない。**

## 中身

- `banner_insight.py` — 本体（all / read / lp / match / report / view / from-meta）
- `meta_api.py` — Metaから実績・画像・LPを取る（accounts / actions / insights / analyze）
- `jpocr` / `jpocr.swift` — OCR。無ければ初回実行時にビルドする（`swiftc` が必要）

バナーを並べる・色を測る・拡大するのは `~/skills/meta-static-banner/scripts/banner_check.py`。
作る側（ChatGPTへの■7項目、修正指示）は `~/skills/meta-static-banner/`。
