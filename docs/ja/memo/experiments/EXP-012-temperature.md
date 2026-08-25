# EXP-012 — サンプリング温度は腕の性能を変えるか

```yaml
experiment_id: EXP-012
date:          2026-08-26
git_commit:    44f1f07
branch:        experiment/eclss-evaluation-layer
config:        v3（乗員4）/ plant_sim / 50 step / inject_failures false / actor=llm / design=none
environment:   vLLM qwen3.5-9b @ 10.10.0.108:8000 / max_tokens 768
seed:          101（全 run 同一）
n:             temperature 0.45 で 7、temperature 0.0 で 7（+ 決定的な2腕を参照として1本ずつ）
status:        pre-registered
```

## 問い

EXP-011 で**予測していなかった観察**が出た。

```
person-steps   labeled_rule_base（決定的）  200
               llm t=0.45                185.8 ± 11.9  (n=5)
               llm t=0.0                 168.8 ± 10.7  (n=5)
               none（決定的）              164
```

**温度 0.0 の腕は、何もしない腕とほとんど変わらない。** ただし事後的に気づいた比較で n=5 だった。

**これは今後すべての実験の前提になる設定**なので、先に決着させる。

## 解析方法（結果を見る前に固定する）

- 主指標: **person-steps**（EXP-011 でノイズ床 CV 6.4%、必要 n 約7/arm）
- 検定: **Welch の t 検定、両側、α=0.05**、n=7/arm
- 効果量は平均差と 95%CI で報告する。**検定を後から選ばない**
- 副指標（判断には使わないが必ず報告する）: peak CO₂、o2_steps_below、o2_min_kg、crew_remaining、生存者数

## 予測

| # | 予測 |
|---|---|
| P1 | t=0.45 の person-steps 平均 > t=0.0 の平均（EXP-011 の観察が追試される） |
| P2 | 差は 15〜20 person-steps 程度（EXP-011 では 17.0） |
| P3 | t=0.0 の平均は no-op 腕（164）と**有意に区別できない** |
| P4 | どちらの条件も全 run で telemetry が異なる（再現しない） |
| P5 | 分散は温度で変わらない（EXP-011 の F 検定 0.82 の追試） |

## 決め方（事前に宣言する）

- **p < 0.05 かつ P1 の向き** → 今後の実験は **temperature 0.45** で回す
- **有意でない** → 温度は性能に効かないものとして、出荷既定の 0.45 を使う（変更の根拠が無いため）
- **逆向きに有意** → 0.0 を採用し、EXP-011 の観察は偶然だったと記録する

いずれの場合も**結論は記録する**。

## 結果

（測定後に追記）
