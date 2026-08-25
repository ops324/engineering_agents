# EXP-000 — 土台が古かった

```yaml
experiment_id: EXP-000
date:          2026-08-24
git_commit:    feat/separate-design-agents @ 63b6345（作業開始時点）
正しい土台:     upstream/trunk = upstream/main = d5616389
status:        invalidated（EXP-001〜006 すべてに影響）
```

## 何が起きたか

セッション開始時、ローカルにチェックアウトされていた `feat/separate-design-agents` を
そのまま土台にした。**`git fetch upstream` を実行せず、本家の現在地を確認しなかった。**

```
共通祖先                          349d1bfa
trunk にあって土台にない commit    4
土台にあって trunk にない commit   25
```

欠けていた4コミット:

```
50c8330 plant_sim occupant survival synced to operators (#54)
886d89a Document plant_sim occupant survival (#56)
371554c feat: split in-sim actors from post-run design agents (#57)
d561638 Update llm models (#58)
```

`upstream/trunk` と `upstream/main` は**同一コミット**。trunk は実験場ではなく本家そのもの。

## 影響

**最も重大**: 一日を通じて「actor 残存の概念がない／スコアカードの主軸50点が存在しない」と
繰り返し述べたが、**誤り**。本家には以下が存在する。

```
src/scenario/ssos_eclss_loop/survival.py
tests/scenario/test_ssos_eclss_loop_survival.py
docs/{ja,en}/memo/ssos_eclss_loop/occupant_survival.md
plant_sim/config.py:  survival_enabled: bool = False
```

`feat/separate-design-agents` 自体も #57（`split in-sim actors from post-run design agents`）と
同じ問題への別実装の可能性が高く、本家では既に決着している。

## 生きているもの / 要再検証

| | |
|---|---|
| 生きている | 新規4モジュール（physics_gate / reference_limits / trajectory_metrics / proposal_evaluation）。純増分なので cherry-pick 可 |
| 生きている | 質量収支の正当性（独立監査が model.py から再導出して確認） |
| **無効** | 「actor残存が無い」を前提にした設計論・優先順位 |
| 要再検証 | B4修正、「O₂がキャビン大気でない」等の指摘、EXP-003 の結論 |

## 教訓

フロー §4 Step 1 は `git switch main` → `git pull` → `git switch -c experiment/...`。
**その最初の2行を実行しなかった。**「〜が無い」は最も危険な主張で、
fetch していないだけのことがある。分析の前に本家の現在地と該当ファイルの存在を確認する。
