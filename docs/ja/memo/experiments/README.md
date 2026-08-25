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

## データの所在

生データは GitHub に入れない（フロー §10）。要約の所在:

- `~/ea-runs/2026-08-24-evidence/`（5.2 MB、866 run分の summary + 有効config + 監査レポート）— **世代 v1: 乗員4人・survival 無し**
- `~/ea-runs/2026-08-25-trunk-baseline/`（8.6 MB、48 run + 再現スクリプト）— **世代 v2: trunk d5616389、乗員50・survival 有効**

どちらも `README.md` に各ファイルがどの主張を支えるかの対応表がある。**両世代の run を同じ表に並べない**（EXP-007）。
