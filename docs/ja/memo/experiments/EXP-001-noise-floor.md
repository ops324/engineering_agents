# EXP-001 — LLM腕のノイズ床

```yaml
experiment_id: EXP-001
date:          2026-08-24
git_commit:    6f01b424（4 run）/ 63b63456（1 run） ※混在。差分は挙動不変と確認済み
branch:        feat/separate-design-agents
config:        ~/ea-runs/2026-08-24-evidence/scripts/noise_t00_conditions.yaml
environment:   plant_sim / vLLM qwen3.5-9b @ 10.10.0.108:8000 / temperature 0.0
seed:          101 (n=5), 103 (n=4)  ※同一 seed の反復
result:        下表
status:        confirmed
```

## 目的

同一 seed・temperature 0.0 で run が再現するか。再現するならペア設計
（同一 seed で control/treated を対にする）でノイズを打ち消せる。

## 結果

```
                     n     SD      CV      range
同一 seed 101         5   0.730   26.5%   2.01 - 3.49   (peak_co2_storage_kg)
同一 seed 103         4   0.602   23.7%   2.04 - 3.40
seed 間 (101-110)    10   0.826   28.0%   1.80 - 3.91
```

- 分岐は `messages.jsonl` index 18 = **step 1（最初の意思決定）**。累積ドリフトではない
- `labeled_rule_base` は telemetry がバイト単位で一致（決定的）。非決定性は LLM サンプリングに限局

## 結論

**同一 seed でも再現しない。** ただし「seed は何も固定していない」という強い主張は
**データが支持しない**（独立監査 EXP-006 の指摘）:

```
pooled within / between = 0.821   （当初 0.78 と述べたが ddof=0 での値）
F = 1.48 on (9,7) df,  p ≈ 0.61
90% CI [0.45, 1.57]
両群が noise_t00__r1 と r3 を共有（独立でない）
between 群は failed_calls で汚染され、比を下方に偏らせる
```

**正しい言い方**: 単一の LLM ペアでは提案の効果と run 間変動を分離できない。
ペアリングが無価値であることは立証されていない。

## 派生する制約

結果指標で条件比較する場合の必要 n（10%の効果、二標本 16σ²/δ²）:

| 指標 | n/arm | 備考 |
|---|---|---|
| peak | 117 | 95% CI [38, 1630]（σ の df=3） |
| steps_above | 68 | **退化。3 mmHg 帯では step 0 から超過しており実質 telemetry 行数** |
| exposure | 258 | 積分は打ち消さず積み上がるので極値より悪い |

データ: `~/ea-runs/2026-08-24-evidence/noise/`
