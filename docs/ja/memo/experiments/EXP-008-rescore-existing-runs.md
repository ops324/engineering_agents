# EXP-008 — 既存 run を新しい評価層で採点し直す

```yaml
experiment_id: EXP-008
date:          2026-08-25
git_commit:    a509f8c（v2 スイープ実行時）/ ae4913b（本メモ執筆時）
branch:        experiment/eclss-evaluation-layer（trunk d5616389 + 評価層）
config:        v1 = ~/ea-runs/2026-08-24-evidence/  /  v2 = EXP-007 と同一の48条件
environment:   plant_sim / labeled_rule_base（決定的）
seed:          101
result:        下記
status:        confirmed
```

## 目的

S2。評価層を trunk に載せ直したので、**既存 run がその層で読めるか**を確かめる。
物理は回し直さない（換算のみ）はずだった。

## 1. 861 run は採点できない（データが無い）

866 run のうち **telemetry.jsonl が残っているのは 5 run だけ**。アーカイブは意図的に
「telemetry は捨て、summary + 有効 config に畳む」方針で作られている（1 run 155 KB × 866 ≒ 130 MB）。

`ea gate`（毎ステップの収支閉合）も `steps_above` / `exposure` も **per-step 系列を要求する**。
summary から復元できるのは peak と終端値だけ。**form が弱いのではなく、材料が無い。**

| 指標 | 必要なもの | 861 run で可能か |
|---|---|---|
| 収支ゲート | telemetry | 不可 |
| `steps_above` / `longest` / `exposure` | telemetry | 不可 |
| peak の ppCO₂ 換算 | summary | **可** |

**教訓: 物差しを後から変える前提なら、telemetry を捨ててはいけない。** 実際にそれが起きた。

## 2. 軌跡が残る 5 run — 新しい層は v1 世代を読める

```
ea gate --all   →  5/5 pass, full form, coverage full
```

full form で通ったのは、これらが `63b6345`（累積 ledger）以後の run だから。

```
ea score                peak        steps_above (3 mmHg)
labeled_1              2.05 mmHg        0
labeled_2              2.05 mmHg        0     ← labeled_1 と完全一致（決定的）
llm_full_50            2.96 mmHg        0     ← 余裕 +0.036 kg
det_a                  3.74 mmHg       12
det_b                  3.99 mmHg       16
```

**v1 の運用点では物差しが効いている。** 0 と 12/16 に分かれる。

## 3. 866 run の peak 換算（できる範囲の一括再採点）

`reference_limits`（388 m³）で peak を ppCO₂ に換算:

```
合計 866 run   3 mmHg 超過 236 (27.3%)   min 1.40 / median 2.45 / max 4.45 mmHg

sweeps/charmap_before_B4.json      270 run    0 超過   （B4 修正前）
sweeps/after_A_after_B4.json       270 run   80 超過   （B4 修正後）
sweeps/failure_patterns.json       180 run  120 超過
sweeps/failure_window.json          50 run   16 超過
b4/option_comparison.json           20 run    9 超過
```

## 4. v2 を評価層ブランチで回し直す

EXP-007 と同一の48条件を、このブランチで再実行（`~/ea-runs/2026-08-25-v2-fullform/`）。

**物理は 48/48 で完全一致。** peak・crew_remaining・死因内訳・person-steps・min_o2・final_co2 の
すべてが trunk 単体の run と一致した。**評価層は物理を変えていない。**

```
                       gate の form        coverage
trunk 単体で回した run   retroactive 48     partial 48
このブランチの run       full 48            full 48
```

**同じ軌跡なのに form が違う。** 差は telemetry に累積 ledger 15項目が出るかどうかだけ
（`63b6345`）。S1 の衝突解決が何を守ったかの実証。

## 5. v2 では ppCO₂ 指標が退化する

```
peak ppCO2      8.39 – 11.84 mmHg（中央値 11.14）   48/48 が 3 mmHg 超過
steps_above     全 run 47（50 step 中）  ← min = median = max。完全な定数
exposure        183.5 – 327.5 kg*steps  ← レンジを持つのはこれだけ
```

EXP-001 が予告した「`steps_above` は 3 mmHg 帯では step 0 から超過して実質 telemetry 行数」という
**退化が、v2 の運用点で実際に起きている**。v1 では 27.3% 超過で効いていた同じ物差しが、v2 では
全 run 100% 超過・step 数まで定数になる。**物差しが間違ったのではなく、運用点が物差しの下から動いた。**

## 6. `capacity_bounds` 6.00x の内訳

二重計数ではなかった。1 step あたりの ARS 除去量を実測:

```
通常 step        0.2500 kg = 定格の 4.00x   ← ars_operation_seconds 4800 / step_seconds 1200
最大（step 26）  0.3750 kg = 定格の 6.00x   ← 上記 × エスカレーション時の goal scale 1.5
```

理論値と一致（4.50 kg/day × 4800 s = 0.25 kg、×1.5 = 0.375 kg）。
gate は goal scale を意図的に上限に含めない設計なので、6.00x はそのまま報告される。

## 7. 副産物として直したもの

- **`ea gate` の `steps` が telemetry 行数だった**（50 step の run を 100 と表示）。判定には未使用だが
  修正し、`readings` を別フィールドに分離。schema 0.1.0 → 0.2.0（`ae4913b`）
- **run が commit を名乗れなかった。** `code_version` を書く `c9fccac` は未 cherry-pick だった。
  出所記録の部分だけを取り込み（`7abb7aa`）、clean → `dirty:false` / 汚れたツリー → `dirty:true` を実測確認。
  `llm_usage` は **`2639bfa`（未 cherry-pick）が無いと無言で何も記録しない**ので意図的に落とした

## 結論

新しい評価層は v1 世代の run をそのまま読める（5/5 full form）。ただし**採点し直せるのは
telemetry を残した run だけ**で、861 run は永久に peak 換算しかできない。

指標の選択は運用点に従属する。**v1 では ppCO₂ が効き、v2 では退化する。**
v2 で分解能を持つのは `exposure` と、EXP-007 で見た person-steps。

## 未解決

- `llm_usage` を summary に入れるなら `2639bfa` の cherry-pick が先
- v1 アーカイブへの `physics_gate.json` 書き込み（`--write`）は未実施。公表済み主張の裏付けなので保留中
- S3（NASA 帯を跨ぐ故障条件の地図）は**現運用点では作れない**。全 run が step 1 から跨ぎっぱなし

データ: `~/ea-runs/2026-08-25-v2-fullform/`（48 run、11 MB）、`~/ea-runs/2026-08-24-evidence/`（v1）
