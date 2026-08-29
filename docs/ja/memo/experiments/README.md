# 実験記録

`GPUシミュレーションを含む実験開発フロー` §7・§9 に従い、実験ごとに
**Commit SHA × Config × Result × 結論** を記録する。

MkDocs のビルド対象外（`mkdocs.yml` の `exclude_docs`）。`e2e_records/` と同じ扱い。

## 書式

```yaml
experiment_id: EXP-00N
date:          YYYY-MM-DD
git_commit:    <SHA>          # dirty なら必ずその旨を書く
branch:        <branch>
config:        <path または要点>
environment:   <backend / model / GPU>
seed:          <seed>
result:        <測定値>
conclusion:    <何が分かったか>
status:        confirmed | rejected | superseded | invalidated
```

`status` の意味:

| | |
|---|---|
| `confirmed` | 結論が有効 |
| `rejected` | 仮説を試して棄却した（**負の結果も必ず残す**） |
| `superseded` | より良い測定に置き換えられた |
| `invalidated` | 前提が崩れて結論が無効になった |

## 一覧

| ID | 内容 | status |
|---|---|---|
| [EXP-000](EXP-000-stale-base.md) | 土台が古かった | invalidated（他の実験への影響） |
| [EXP-001](EXP-001-noise-floor.md) | LLM腕のノイズ床 | confirmed |
| [EXP-002](EXP-002-determinism.md) | 直列化による決定性 | **rejected** |
| [EXP-003](EXP-003-rule-arm-characterisation.md) | ルール腕の特性把握 | **invalidated**（EXP-007） |
| [EXP-004](EXP-004-b4-operation-quantum.md) | B4 時間量子の選択肢比較 | **invalidated**（採用判断のみ。測定は有効） |
| [EXP-005](EXP-005-habitat-volume.md) | 居住区容積の選定 | **invalidated**（選定根拠のみ。一次資料は有効） |
| [EXP-006](EXP-006-independent-audit.md) | 独立監査 | confirmed |
| [EXP-007](EXP-007-trunk-operating-point.md) | trunk の運用点を測り直す | confirmed |
| [EXP-008](EXP-008-rescore-existing-runs.md) | 既存 run の再採点 | confirmed |
| [EXP-009](EXP-009-crew-four.md) | 乗員を装置定格に合わせる（v3） | confirmed（予測6件中5件的中） |
| [EXP-010](EXP-010-crew-axis-is-not-guarded.md) | 提案が乗員残存を操作できる | confirmed（案1+案2で封鎖） |
| [EXP-011](EXP-011-noise-floor-v3.md) | ノイズ床の測り直し（v3） | confirmed（予測5件中2件的中） |
| [EXP-012](EXP-012-temperature.md) | サンプリング温度は性能を変えるか | **rejected**（仮説は追試されず） |
| [EXP-013](EXP-013-llm-vs-rule.md) | AI腕 vs ルール腕 | **confirmed**（ルール腕が有意に優位） |
| [EXP-014](EXP-014-llm-vs-rule-v4.md) | AI腕 vs ルール腕（v4・定格の不変条件） | **confirmed**（同じ結論） |
| [EXP-015](EXP-015-llm-vs-rule-v5.md) | AI腕 vs ルール腕（v5・O₂ がキャビン大気） | **confirmed**（3世代目・同じ結論） |
| [EXP-016](EXP-016-scoring-resolution.md) | 判断の分解能はどの軸にあるか（再採点のみ） | **retracted**（独立監査で撤回） |
| [EXP-017](EXP-017-no-tradeoff.md) | この運用点には代償が無い | **confirmed（負の結果）**（判断を置く場所が無い） |
| [EXP-018](EXP-018-scarcity-without-value.md) | 希少性だけでは判断は生まれない（案1） | **confirmed（負の結果）**（案1 は退化。案3 が前提） |
| [EXP-019](EXP-019-failure-has-no-ambiguity.md) | 故障には期限があるが曖昧さが無い（案4） | **confirmed（負の結果）**（案4 も退化。同じ壁に3回目） |
| [EXP-020](EXP-020-o2-resolution-is-discarded.md) | O₂ の分解能を採点式が捨てている（再採点のみ） | **partially retracted**（EXP-021。観察は正しく、直し方が誤り） |
| [EXP-021](EXP-021-the-guard-broke-a-third-time.md) | 独立監査：EXP-020 の撤回と、ガードの破れ3件 | **confirmed**（★ガードが3度目に破られた。乗員2→4・+24.33点） |
| [EXP-022](EXP-022-the-guard-had-four-more-windows.md) | 独立監査2巡目：帯の前提の訂正と、ガードの破れ4件 | **confirmed**（★`--steps` が最も安い穴。何もしない腕が 2 step で 86.1/90） |
| [EXP-023](EXP-023-the-rule-i-wrote-fired.md) | 独立監査3巡目：O₂ の帯の変更を自分の撤回条件で止めた | **rejected（提案を撤回）**（帯は 91.31 のまま。飽和は免疫でもあった） |
| [EXP-024](EXP-024-the-loop-nobody-ran.md) | 誰も回していなかった提案評価ループを回した | **partially retracted**（EXP-028。現象は実在するが原因の帰属が誤り） |
| [EXP-025](EXP-025-llm-designer.md) | LLM designer の実験設計（第1版・第2版） | **retracted**（2版とも。第2版は独立監査3体が全員反対） |
| [EXP-026](EXP-026-the-metric-a-constant-string-wins.md) | 主指標が判断を測っていなかった（定数1行が満点） | **confirmed**（★測る前に「判断ゼロの入力」を通す規律の出所） |
| [EXP-027](EXP-027-the-designer-layer-degenerates-too.md) | 設計の層にも内点が無い。定数がルールを27本ぶん上回る | **confirmed（負の結果）**（同じ壁に4回目） |
| [EXP-028](EXP-028-the-loop-is-closed.md) | 案3 は成立しない。ARS に水のコストが無く、ループが閉じている | **retracted**（EXP-029。格子・飽和・探索空間の3点で誤り） |
| [EXP-029](EXP-029-the-audit-broke-exp-028.md) | 独立監査が EXP-028 を壊した | **partially retracted**（EXP-030。摘発は有効、「案3 は成立する」は撤回） |
| [EXP-030](EXP-030-the-window-was-cutting-the-ending.md) | 案3 は成立しない。定数が勝ち、50 step が結末を切っていた | **confirmed**（★**生存者数は打ち切りカウント** — 評価層全体に効く） |
| [EXP-031](EXP-031-the-second-headline-was-the-window.md) | 本題の第一の見出しは無傷、第二の見出しは窓 50 の産物 | **confirmed**（★「LLM ≈ no-op」は W=15 で**反転**。公表値も step 887 までの数字） |
| [EXP-032](EXP-032-we-moved-the-window-and-moved-it-back.md) | 観測窓を 72 にして戻した。分解能は増えなかった | **reverted**（★設定は 50 のまま。**request_o2 が窓より重い交絡**と判明） |
| [EXP-033](EXP-033-the-trap-and-what-it-did-not-explain.md) | `request_o2` は罠。だが除くと LLM は no-op に**勝つ** | **confirmed**（★初稿の見出しが監査で**反転**。EXP-015 の README と整合） |
| [EXP-034](EXP-034-early-was-not-the-advantage.md) | 「早さ」は利点ではなかった。効くのは密度で、定数で買える | **confirmed**（★初稿の「早さに効果」は監査で否定。**既存記録の再発見が今日3回目**） |
| [EXP-035](EXP-035-does-the-s2-gate-catch-a-rediscovery.md) | S2 ゲートは再発見を止めたか。**機械が新たに止めたのは1回**、見逃した1回は監査でしか止まらなかった件 | **corrected**（★主要な結論2つを監査で撤回。**事前登録の撤回条件が2つとも到達不能だった**） |
| [EXP-036](EXP-036-what-the-audit-missed.md) | 監査は何を見逃したか。**「7回中7回」はスコープ内の分子**。失敗様式は3つとも監査を使う側の手順にあった | **corrected**（★3つの数字が全部、著者が宣言した偏りの向きに誤っていた） |
| [EXP-037](EXP-037-the-window-question-has-a-cheap-answer.md) | 窓の問題には安い答えがあった — **結論は窓を倍にして生き残ったときだけ採用する** | **confirmed**（★著者の予測は外れ。ルール腕は 8倍の窓で 0.34点しか動かない） |

## セッション記録と設計方針

| | 内容 |
|---|---|
| [SESSION-2026-08-29.md](SESSION-2026-08-29.md) | **1日分の全体像** — 検証7件・結果・組み込み3件・訂正6件・繰り返した失敗5種 |
| [flow-engineering-design.md](../flow-engineering-design.md) | **次の方向**（草案・未監査）— 流れは決定的に、判断に LLM。グラフDB と再帰的改善 |
| [REPORT-2026-08-29-team.md](REPORT-2026-08-29-team.md) | チーム向け報告。**判断5件**（うち判断4 は `request_o2` の実バグ） |

## データの所在

生データは GitHub に入れない（フロー §10）。要約の所在（合計 54 MB）:

| ディレクトリ | 世代 | 内容 |
|---|---|---|
| `~/ea-runs/2026-08-24-evidence/` | **v1**（乗員4・survival 無し・旧閾値） | 866 run の summary + 監査3件。**telemetry は5 run 分しか無い**（EXP-008） |
| `~/ea-runs/2026-08-25-trunk-baseline/` | **v2**（trunk d5616389・乗員50） | 48 run。全滅で判別不能を示す（EXP-007） |
| `~/ea-runs/2026-08-25-v2-fullform/` | v2 | 同48条件を評価層ブランチで再実行。full form ゲート（EXP-008） |
| `~/ea-runs/2026-08-26-v3-crew4/` | **v3**（乗員4・survival 有効） | 4 run + 物差しハザードの証拠（EXP-009 / EXP-010） |
| `~/ea-runs/2026-08-26-v3-noise/` | v3 | LLM 10 run。ノイズ床（EXP-011） |
| `~/ea-runs/2026-08-26-v3-temperature/` | v3 | LLM 14 run + 参照2。温度の検証（EXP-012） |
| `~/ea-runs/2026-08-26-v3-llm-vs-rule/` | v3 | LLM 24 run + 参照2。**本題の比較**（EXP-013） |
| `~/ea-runs/2026-08-27-v4-llm-vs-rule/` | **v4**（定格の不変条件） | LLM 24 run + 参照2（EXP-014） |
| `~/ea-runs/2026-08-28-v5-llm-vs-rule/` | **v5**（O₂ がキャビン大気・水が [V2 6109]） | LLM 24 run + 参照2（EXP-015 / EXP-016） |
| `~/ea-runs/2026-08-29-exp018-plan1/` | v5 | 決定的 5 run。案1 の退化（EXP-018）。`exp018.sh` で再生成可 |
| `~/ea-runs/2026-08-29-exp019-failure-deadline/` | v5 | 決定的 61 run。案4 の退化と故障の期限（EXP-019）。`exp019.sh` で再生成可 |
| `~/ea-runs/2026-08-29-exp028-water-tightening/` | v5（**全 run が運用点を変えた probe**） | 決定的 789 run の **digest のみ**（生 run 282 MB は保存せず）。案3 の退化（EXP-028）。`exp028.sh` で再生成可 |
| `~/ea-runs/2026-08-29-exp029-audit/` | v5（同上・probe） | 決定的 411 run の **digest のみ**。監査の指摘を著者が再現（EXP-029）。`exp029.sh` で再生成可 |
| `~/ea-runs/2026-08-29-plan3-probe/` | v5（同上・probe） | 決定的 1,651 run の **digest のみ**＋事前登録。案3 の測り直しと監査の再現（EXP-030） |

各ディレクトリの `README.md` に、どのファイルがどの主張を支えるかの対応表がある。

**世代の異なる run を同じ表に並べない。** v1 は乗員4・survival 無し、v2 は乗員50、v3 は乗員4・survival 有効。
次の物理修正（定格の不変条件）を当てると **v4** になり、v3 の測定はすべて取り直しになる。
