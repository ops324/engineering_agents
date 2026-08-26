# EXP-013 — AI腕はルール腕より乗員を生かすか

```yaml
experiment_id: EXP-013
date:          2026-08-26
git_commit:    0c80d0e
branch:        experiment/eclss-evaluation-layer
config:        v3（乗員4）/ plant_sim / 50 step / inject_failures false / design=none
environment:   vLLM qwen3.5-9b @ 10.10.0.108:8000 / temperature 0.45（EXP-012 の決定）
seed:          101（全 run 同一）
n:             llm 24 本（ゲート不合格を見込んで多めに。必要 n は 18〜26）
               labeled_rule_base と none は決定的なので各1本
status:        pre-registered
```

## 問い

**運用者として、AI はルールより上手いのか。** この研究の本題である。

これまでに分かっていること:

```
labeled_rule_base（決定的）  200 person-steps
llm 21本（EXP-011+012）      平均 174.1、SD 18.3、うち 8本が 164 に着地
none（決定的）              164
```

平均だけ見れば AI は劣る。しかし **21本中8本が「何もしないのと同じ」に着地**しており、
「常に少し下手」なのか「時々まったく機能しない」なのかで意味がまるで違う。**そこを分ける。**

## 解析方法（結果を見る前に固定する）

- 主指標: **person-steps**、ゲート合格 run のみ
- 検定: ルール腕は**決定的なので定数**（200）。**1標本 t 検定・両側・α=0.05** で AI 腕の平均を 200 と比較
- **分布を必ず出す**（平均だけにしない）。164 に一致した本数を数える
- **行動側**: 適用された `air_revitalisation` の回数と person-steps の **Pearson 相関**（n と 95%CI つき）
- 164 群とそれ以外で ARS 発行回数を比較する
- 副指標として peak CO₂ / o2_steps_below / 生存者 / 拒否率を報告する（判断には使わない）

## 予測

| # | 予測 |
|---|---|
| P1 | AI 腕の平均は 200 より**有意に低い**（p<0.05） |
| P2 | 分布は二峰のまま。**164 ちょうどに着地する run が 24本中 5〜10本** |
| P3 | 164 に着地した run は **ARS 発行がゼロまたは極少**（1回以下） |
| P4 | ARS 発行回数と person-steps に**正の相関**がある（r > 0.5） |
| P5 | ゲート不合格が 24本中 **2〜7本**（EXP-012 は 3/14 = 21%） |

**P3 と P4 が当たれば、「AI が劣る理由は ARS を動かさないことだ」と言える。**
外れれば、原因は別のところにある。

## 結果

（測定後に追記）
