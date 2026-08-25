# EXP-009 — 乗員を装置の定格に合わせる（世代 v3）

```yaml
experiment_id: EXP-009
date:          2026-08-26
git_commit:    （設定変更コミット。結果は同一 SHA で取得）
branch:        experiment/eclss-evaluation-layer
config:        plant_sim.crew.size 50 → 4、agents.yaml actor.team.count 50 → 4。他は未変更
environment:   plant_sim / labeled_rule_base（決定的）
seed:          101
status:        pre-registered（予測を先に記録。結果は下段に追記）
```

## 変更の理由

trunk の運用点（乗員50）は、**選ばれた難易度設定ではなく噛み合っていない状態**だった（EXP-007）。

```
ARS 4.50 kg CO2/day ÷ 1.04 kg/人/day = 4.33 人分   ← 装置は4人用
OGS 9.25 kg O2/day  ÷ 0.84 kg/人/day = 11.0 人分
容積 388 m³ ÷ 4人 = 97 m³/人（ISS は 388 m³ に6人 ≒ 65 m³/人）
```

**`scenario.yaml` の CO₂ 除去装置は定格が 4.33 人分**で、そこに50人を入れていた。結果は 48/48 の全滅で、
どの腕も区別できなかった。人数を装置と容積に合わせる。

**代案 B（装置と容積を50人に合わせる: ARS 約52 kg/day、容積 約3,200 m³）は採らない。** 変更範囲が大きく、
「ISS の12倍規模」という根拠を別途要する。

## 乗員50 の出所（履歴から確認できること）

- `50c8330`（#54, survival 導入）が `scenario.yaml` に `plant_sim` ブロックを新設し、**そのとき crew 50 と
  現行閾値（co2 2.0/8.0、o2_low 6.0、初期 O2 8.0 kg、水 80 L）が同時に入った**
- その直前の `349d1bf`（#53）は actor team を **4 → 10** に変えたコミットで、同じ差分に
  `Baseline 4-crew inventories` と書かれている
- **なぜ 50 なのかは、コミットメッセージにも docs にも書かれていない。** docs は「いまの `ssos_eclss_loop`
  既定（乗員 50）」と現状として記述し、`50→38→19` を減員の計算例に使うのみ

理由が記録に無いので、**この変更はチームに確認すること**。乗員数の感度を見る専用ツールが本家にある
（`python3 -m tools.plant_sim_sensitivity_app`、port 8502、N スイープあり。survival はオフ）。

## 何を変え、何を変えないか

| | |
|---|---|
| 変える | `plant_sim.crew.size` 50 → 4、`agents.yaml actor.team.count` 50 → 4（この2つは同一人物なので `scenario_run.py:206` が一致を強制する） |
| 変えない | 閾値、初期在庫、装置定格、容積 388 m³、`ars_operation_seconds`（B4）、`discourse_window` |

容積 388 m³ は据え置く。4人なら 97 m³/人 で実在の居住区として筋が通り、選定当時の
「ISS USOS を参考に」という note とも整合する（EXP-005 の選定根拠自体は EXP-007 で無効化済み）。

## 予測（結果を見る前に記録）

1 step = 1200 s、乗員4人での計算:

```
CO2 生成   4 × 1.04 × 1200/86400 = 0.0578 kg/step
O2 消費    4 × 0.84 × 1200/86400 = 0.0467 kg/step
水消費     4 × 2.28 × 1200/86400 = 0.1267 L/step
ARS 1回    0.25 kg   = CO2 生成 4.33 step 分
OGS 1回    0.1285 kg = O2 消費 2.75 step 分
```

| # | 予測 | 根拠 |
|---|---|---|
| P1 | `none` 腕は乗員を失うが**全滅しない**（残存 1〜3） | ARS 無しで CO2 は step 12 に 2.0 kg を越え帯に居座る → `n//4` = 1名。O2 は step 43 に 6.0 kg を割り WARNING 反復 |
| P2 | `labeled_rule_base` 腕（故障なし）は**乗員4を維持**（損失ゼロ） | ARS 1回で 4.33 step 分を除去でき、2 step 連続 WARNING に至らない。OGS も同様 |
| P3 | 故障注入ありのルール腕は**乗員を失う**（1〜2名） | ARS 停止 step 10–20 の間に CO2 が帯に居座る |
| P4 | CO2 CRITICAL (8.0 kg) にも O2 CRITICAL (1.0 kg) にも**到達しない** | ARS 皆無でも 50 step で 1.3 + 2.89 = 4.19 kg。O2 は 5.67 kg 残 |
| P5 | 水は**不動**（50 L を割らない） | 消費 0.127 L/step、初期 80 L、WRS 回収あり |
| P6 | peak ppCO₂ は **3 mmHg を跨ぐ帯**に入る（none 腕 約4.5、ルール腕 約2.2 mmHg） | 388 m³ で 4.19 kg = 4.5 mmHg、2.0 kg = 2.15 mmHg |

**P2 と P3 が両方当たれば、シナリオは「操作の良し悪し」と「故障の影響」を区別できる。**
P2 が外れて誰も死なない場合は v1 と同じ「易しすぎ」に戻ったことになり、難易度は故障注入で上げる。

## 世代

これは **v3**。v1（乗員4・survival 無し）とも v2（乗員50・survival 有効）とも**同じ表に並べない**。

## 結果

（測定後に追記）
