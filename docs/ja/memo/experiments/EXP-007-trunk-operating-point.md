# EXP-007 — trunk の運用点（土台を差し替えて測り直す）

```yaml
experiment_id: EXP-007
date:          2026-08-25
git_commit:    d5616389（upstream/trunk = upstream/main、worktree は clean）
branch:        detached worktree @ d5616389 → experiment/eclss-evaluation-layer
config:        scenario.yaml 既定 + --set thresholds.co2_storage_high_kg / thresholds.o2_storage_low_kg / inject_failures
environment:   plant_sim / labeled_rule_base（決定的、0.1秒/run）
seed:          101
result:        48 run。下表
status:        confirmed
```

## 目的

EXP-000 で土台が古いと判明した。本家の現行コードで運用点を測り直し、EXP-001〜006 の
どの結論が生き残るかを決める。**「〜が無い」と述べる前に測る。**

## 土台の差は survival.py だけではなかった

| | 古い土台 `feat/*` | trunk d5616389 |
|---|---|---|
| 乗員 | **4人**（`scenario.yaml` に `plant_sim` ブロックが無く `PlantSimConfig` 既定 `crew_size=4`） | **50人**（`scenario.yaml` が正本） |
| 初期O2 / `o2_storage_low_kg` | 2.98 / **0.45** kg | 8.0 / **6.0** kg |
| `o2_storage_critical_kg` | 未指定（fallback `low*0.75`） | 1.0 kg |
| `co2_storage_high_kg` / `critical` | 1.5 / 2.2 kg | 2.0 / **8.0** kg |
| 初期水 | 51 L | 80 L |
| occupant survival | 存在しない（#54 以前） | `plant_sim.survival.enabled: true` |

一日分の実験は「乗員4人の居住区」を測っていた。

## 測定

格子: actor-mode 2 × `co2_high` 4値 × `o2_low` 3値 × `inject_failures` 2値 = **48 run**、50 step。

```
                     person-steps        絶滅step   crew_remaining
none               637 - 670  (mean 652.8)   25-26        0
labeled_rule_base  756 - 1050 (mean 866.9)   21-43        0
```

**1. 48/48 で全滅。** `crew_remaining` は 48 run すべて 0。終端の残存人数は定数。

**2. person-steps（`crew_alive` の積分）には分解能がある。** `none` の幅は 637–670（±2.5%）に対し、
ルール腕は 756–1050。腕の差（652.8 → 866.9、+32.8%）は `none` 内のばらつきの数倍。
**ルール腕が no-op 腕に勝つことを、このシナリオで初めて実測した。**

**3. O2 律速。効く設計レバーは `o2_low` だけ。**

```
o2_low   3.0 → 808.8      co2_high  1.5 → 946.3
         6.0 → 972.0                2.0 → 941.7
         7.0 → 1037.0               3.0 → 937.7
         （単調、+28%）             4.0 → 931.3   （ほぼ不動）
```

死因内訳（48 run 合計）も O2 側が重い。ルール腕: o2_critical 308 + o2_warning 238 + o2_physics 139 = **685**
対 co2_warning 309 + co2_critical 206 = 515。OGS は step 4 で発火する。

**4. `inject_failures` は `none` 腕で完全に無効**（24組が値までペアで一致）。ルール腕でのみ約10%削る
（796.0 → 723.6）。**故障注入は環境の摂動ではなく「運用者を無効化するレバー」。**

**5. 容量が最初から足りない。** ARS 0.25 kg/action ≒ 17人分、OGS 0.1285 kg O2/action ≒ 11人分に対し乗員 50。
step 1 で既に帯を割り、**SAFE な定常状態が存在しない**。

## 何が引っくり返ったか

| 前回の結論 | 実測に照らすと |
|---|---|
| actor 残存の概念が無い | **誤り**（EXP-000 で既に訂正）。実装済み・有効 |
| 主軸「残存50点」が定数になる | **結論は生き残るが理由が正反対。** 古い土台は誰も死なない、trunk は全員死ぬ。どちらも終端指標の飽和 |
| O₂軸が回路から切断、実質CO₂の1軸問題（EXP-003） | `o2_low 0.45` の産物。**trunk では O₂ が主軸**。EXP-003 の 2・3・5 は無効 |
| B4: ARS が定格の4.3倍（EXP-004） | crew 4 での話。crew 50 では 0.25 kg/action 対 生成 0.722 kg/step で**既に3倍不足**。A案を当てると全腕が即死し分解能が消える |
| 388 m³ で運用警報が基準の手前に来る（EXP-005） | 閾値 1.5/2.2 に合わせた選定。trunk の 2.0/8.0 では critical = 8.6 mmHg で基準の2.9倍、実測 peak は 8.4–11.8 mmHg で**全 run 基準違反**＝ppCO₂ 指標も飽和。388/50 = 7.8 m³/人 |

## 付随して見えたもの

- **`ea gate` は 50 step の run を `steps=100` と数える。** EXP-006 が `trajectory_metrics` で直した
  「telemetry 行 vs step」の二重計数が gate 側に残っている（`ea score` は 50 と正しく数える）
- **`capacity_bounds` が ARS を定格の 6.00 倍と報告**（10倍超のみ不合格なので pass）。上の二重計数の
  影響を受けている可能性があり、未解決の「真の上限」問題と B4 の両方に直結する
- **run 成果物が commit を名乗れない。** trunk の `summary.json` に `code_version` は無い
  （書き込む `c9fccac` は古い枝にあり未 cherry-pick）。この実験の SHA 対応は本メモと
  `~/ea-runs/.../README.md` という外部記録に依存する。フロー最重要ルール（SHA × Config × Result）を
  成果物側で担保するには `c9fccac` を載せる必要がある（5ファイル、+97/-1）
- 物差しハザードのテストが literal `9.0` に依存していた。乗員50では軌跡が 9 kg を自力で越えるため
  実演が成立しなくなり、baseline run 自身の peak から導出する形に変更した（`2be14ad`）

## 結論

**評価層の主軸・難易度・容積のすべてが、乗員4人の運用点に合わせて選ばれていた。**
指標は終端値ではなく軌跡（person-steps / 絶滅step）を使う。B4 と 388 m³ の適用は、
判別可能な運用点を選び直したあとでなければ意味を持たない。

運用点の選び直しは `simulation.*` と `plant_sim.crew.*` に触れるので decision 89/90 の第4層
（人間のみ・事前登録・世代を切る）。この実験は測っただけで、何も変えていない。

データ: `~/ea-runs/2026-08-25-trunk-baseline/`（48 run + 既定条件2 run + 再現スクリプト、8.6 MB）
