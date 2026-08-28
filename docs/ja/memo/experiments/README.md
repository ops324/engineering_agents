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

各ディレクトリの `README.md` に、どのファイルがどの主張を支えるかの対応表がある。

**世代の異なる run を同じ表に並べない。** v1 は乗員4・survival 無し、v2 は乗員50、v3 は乗員4・survival 有効。
次の物理修正（定格の不変条件）を当てると **v4** になり、v3 の測定はすべて取り直しになる。
