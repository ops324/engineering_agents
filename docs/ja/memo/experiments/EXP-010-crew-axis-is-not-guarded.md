# EXP-010 — 提案は乗員残存を操作できる（主軸が守られていない）

```yaml
experiment_id: EXP-010
date:          2026-08-26
git_commit:    5308208（乗員4、世代 v3）
branch:        experiment/eclss-evaluation-layer
config:        plant_sim / 30 step / inject_failures true / labeled_rule_base / seed 101
result:        下記
status:        confirmed（案1で緩和。案2は未着手）
```

## 経緯

EXP-009 のテスト修正中に、**注意警報 `co2_storage_high_kg` を上げる提案**を実測していて見えた。
狙って探したものではない。

## 測定

提案は `thresholds.co2_storage_high_kg` を 2.0 → 4.76 kg（baseline peak の2倍）に上げる、それだけ。

```
                  警報値   CO2最高値   CO2ヘルス帯の内訳     乗員      死因
control（元）      2.0     2.380 kg   safe 26 / warning 34   4 → 3   co2_warning 1
treated（提案後）  4.76    2.980 kg   safe 60 / warning  0   4 → 4   なし
```

**空気は悪化している（peak 2.380 → 2.980 kg）のに、死者が減った（1名 → 0名）。**

## 機構

サバイバル判定と運用トリガーが**同じ YAML キーを読んでいる**。設計文書にも明記されている
（`occupant_survival.md`「運用トリガーとサバイバル帯は **同じ YAML キー**」）。

```
警報を上げる → ヘルス帯が safe のまま → 帯滞在カウンタが動かない → 誰も減らない
```

CO2 の帯滞在は「WARNING に2ステップ連続で `n//4`」なので、**WARNING に入らなければ発火しない**。
提案は空気を汚したまま、**死因そのものを消した**。

## 何が守られていて、何が守られていないか

`evaluate_proposal` が比較している指標を実測で列挙した:

```
steps_above / exposure_integral_kg_steps / longest_streak / peak_kg / terminal_margin_kg
```

| | 状態 |
|---|---|
| CO₂ の5指標 | **守られている。** 両腕とも baseline の凍結した物差しで採点され、判定は正しく `worse`（3指標が悪化） |
| 乗員残存 | **比較対象に入っていない。** `evaluate_proposal` は乗員を一度も見ない |
| 各 run の `crew_remaining` | **守られていない。** その run 自身の閾値で計算された数字がそのまま `summary.json` に載る |

**スコアカードの主軸50点が乗員残存**である以上、これは軽微ではない。
`summary.json` だけを読んだ人は「この提案は1人の命を救った」と読む。

## なぜ既存の防御では止まらないか

`trajectory_metrics` は「凍結した baseline の物差しで両腕を採点する」ことで物差しハザードを塞いでいる
（EXP-006、`from_frozen_baseline`）。しかし乗員減員は**採点ではなく run の中で起きる出来事**で、
その場で有効な閾値を使って計算される。**後から別の物差しで採点し直す余地がない。**

## 塞ぎ方の候補（決定していない）

1. **凍結した帯で減員を再計算する** — telemetry の CO2/O2/水系列と baseline の閾値から `survival.py` の
   帯滞在を再適用し、「baseline の物差しで数えた死者」を出す。評価層側だけで完結する
2. **サバイバル帯を運用トリガーと別キーにする** — `plant_sim.survival.bands.*` を新設し、提案の許可対象から外す。
   シナリオ設計の変更で、`occupant_survival.md` の明示的な設計判断を覆すことになる
3. **`evaluate_proposal` の比較指標に乗員を加える** — 上の1か2と併用しないと、
   「提案自身の閾値で数えた死者」を比べることになり、かえって悪い

**1 と 3 の組み合わせが最小に見えるが、決めていない。** どれもシナリオまたは評価層の設計判断。

## 塞いだもの（案1、2026-08-26）

`src/scenario/ssos_eclss_loop/survival_replay.py` を追加。**軌跡を固定したまま、凍結した帯で帯滞在を再適用する。**

検証（`~/ea-runs/2026-08-26-v3-crew4/yardstick-crew-hole/`）:

```
                その run 自身の帯で再計算   run が報告する値   凍結帯で再計算
baseline                    3 人      =        3 人
control                     3 人      =        3 人              3 人
treated                     4 人      =        4 人              3 人  ← 減員が復活
```

**自分の帯を当てれば実際の run を完全に再現する**（3/3/4 とも一致）ので、再計算の仕組み自体が信用できる。

`evaluate_proposal` の比較指標に `crew_remaining_frozen` を追加（`HIGHER_IS_BETTER`）。実際の出力:

```
verdict: worse - 0 metric(s) improved, 3 worsened
  exposure_integral_kg_steps  +4.733   worsened 1
  peak_kg                     +0.597   worsened 1
  terminal_margin_kg          -0.717   worsened 1
  crew_remaining_frozen       +0.000   improved 0   ← 「1人救った」が消えた
  control  run 報告 3 人 / 凍結帯 3 人
  treated  run 報告 4 人 / 凍結帯 3 人
```

回帰テスト `test_a_proposal_cannot_bank_the_deaths_it_deleted` で、**欺瞞（run 報告 4 > 3）と防御（凍結帯で improved 0）の両方**を固定した。

### 案1の限界（設計上のもので、バグではない）

- **反実仮想である。** 軌跡を固定して帯だけ差し替えるので、「実際にその帯で走らせた場合」とは一致しない
  （減員は CO2 生成と O2 消費を下げるフィードバックを持つ。EXP-009 で予測を外した機構と同じ）。
  **2つの腕を1つの物差しで比べるための量**であって、予測ではない
- **物理下限（`apply_capacity_drop`）は再現していない。** telemetry が step ごとの look-ahead 在庫を持たないため。
  乗員4では物理下限が一度も発火していないので今回の測定には影響しない

## 未解決

- **案2（サバイバル帯を運用トリガーと別キーにする）は未着手。** 本家の設計判断を覆すのでチームの合意が要る。案1は評価層側の緩和であって、`summary.json` の `crew_remaining` は依然として run 自身の閾値で計算されたままである
- 同じ経路が O2・水にもあるか**未確認**。機構上は同じはず（帯を外せば滞在カウンタが動かない。O2 の場合は
  `o2_storage_low_kg` を**下げる**と WARNING に入らなくなり、同時に OGS の発火も遅れて物理は悪化する）。
  ただし乗員4では O2 帯滞在による減員がそもそも起きない（EXP-009 の死因は `co2_warning` のみ、min O2 5.97 kg）ので、
  実演するには別の運用点が要る

データ: `~/ea-runs/2026-08-26-v3-crew4/yardstick-crew-hole/`（baseline + control/treated 各2組）
