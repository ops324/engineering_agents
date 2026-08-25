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
| [EXP-003](EXP-003-rule-arm-characterisation.md) | ルール腕の特性把握 | 要再検証 |
| [EXP-004](EXP-004-b4-operation-quantum.md) | B4 時間量子の選択肢比較 | confirmed |
| [EXP-005](EXP-005-habitat-volume.md) | 居住区容積の選定 | confirmed |
| [EXP-006](EXP-006-independent-audit.md) | 独立監査 | confirmed |

## データの所在

生データは GitHub に入れない（フロー §10）。要約は `~/ea-runs/2026-08-24-evidence/`（5.2 MB、866 run分の summary + 有効config + 監査レポート）。
`README.md` に各ファイルがどの主張を支えるかの対応表がある。
