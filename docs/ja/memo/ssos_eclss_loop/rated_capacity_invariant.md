# 定格を超えて処理させない — 設計と、その実装

2026-08-27。**実装済み。** EXP-004（B4 時間量子）と EXP-012（注文が能力を倍にする）が
**同じ1つの欠陥の2つの症状**であることが分かったので、まとめて直した。
下の「現状の診断」は修正前の姿であり、記録として残してある。

## 現状の診断（コードで確認済み）

```python
# ARS
operation_capacity = per_interval(c.ars_capacity_kg_day, c.ars_operation_seconds)  # 4800 s
scale = goal_co2_mass_kg / c.ars_reference_goal_co2_kg                             # 上限なし
max_removable = operation_capacity * scale
```

| 系統 | 時間量子 | 注文による倍率 | 1 step 複数回の合算上限 | 判定 |
|---|---|---|---|---|
| ARS | **4800 s**（step の4倍） | **あり・上限なし** | **なし** | 欠陥3つ |
| OGS | 1200 s（step と同じ） | なし（`input_water_mass` は量で、能力で飽和） | なし | ほぼ健全 |
| WRS | `wrs_operation_seconds` は**未使用** | なし | なし | **定格そのものが無い**（1操作 10 L のバッチ上限のみ） |

`operation_seconds <= step_seconds` を検査する不変条件は**どこにも無い**。

## 実測されている帰結

```
ルール腕        ARS 定格の 4.00倍   ← 時間量子のみ
AI腕（最悪）    ARS 定格の 14.38倍  = 4.00 × 1.798（goal 3.236/1.80）× 2（同一 step に2体）
```

**出荷時の設定では、どちらの腕も定格超えの装置を使っている。** 物理ゲートの上限が10倍なので
ルール腕は通過し、AI の14倍だけが弾かれていた（EXP-012 では 3/14 が不合格）。

スコアカードの物理整合性ゲートは「**装置能力上限内**」を必須項目に挙げている。
文言を厳密に読めば、**ルール腕の run も検証無効になりうる**。

## 提案する不変条件

> **1 step の経過時間の中で、どの系統も定格を超えて処理しない。**

```python
# ARS の場合
elapsed = min(c.ars_operation_seconds, c.step_seconds)   # 量子は step を超えられない
rated_this_step = per_interval(c.ars_capacity_kg_day, c.step_seconds)
allowance = max(0.0, rated_this_step - s.co2_removed_this_step)  # 同一 step の合算
max_removable = min(per_interval(c.ars_capacity_kg_day, elapsed) * scale, allowance)
```

**注文（goal）は残す。** 「もっと急いで」を表現する手段としては妥当で、
ただし**能力の天井を買えなくなる**。現実の機械と同じ「いくら注文しても定格まで」の挙動になる。

`co2_removed_this_step` は `advance_step` でリセットする状態を1つ増やす。
同じ形を OGS（`o2_generated_this_step`）にも入れる。**対称にする方が、あとで読む人に優しい。**

### なぜ「注文を目標値に変える」案を採らないか

`initial_co2_mass` を「この値まで下げる目標」と解釈し直す案も考えたが、採らない。
**ルール腕のエスカレーション（critical で goal を 1.5倍）が逆向きになる**ためである
（目標を上げる＝下げ幅が減る）。ポリシー側の書き換えを誘発し、変更が広がる。

## 直したあとどうなるか（算術。実測ではない）

```
乗員4の CO₂ 生成      0.0578 kg/step
ARS の定格（1 step）   0.0625 kg/step
余裕                  1.082倍   ← 毎ステップ回して、やっと 8%
```

**「一度サボると取り返せない」運用点**になる。判断が意味を持つ領域である。
EXP-012 で見つけた「ARS を一度も動かさない run」（21本中8本）は、修正後は壊滅するはずである。

O₂ 側は OGS が 2.75倍の余裕を持つので、当面ゆるいまま。

## 波及

| | |
|---|---|
| **全 run が比較不能**（世代 v4） | ノイズ床・EXP-009・EXP-011・EXP-012・EXP-013 をすべて取り直す |
| EXP-004 の A案（4800→1200）は**不要になる** | 不変条件が構造的に同じ効果を出す |
| 物理ゲートの `capacity_bounds` は**二度と発火しなくなるはず** | 発火したら実装のバグ。良い自己検査になる |
| スコアカードの C軸「要求量の妥当性」は**残す** | 定格超えの注文は依然として判断ミス。ただし害はなくなる |

## 決定（2026-08-27、実装済み）

3点とも決めて実装した。**この枝の選択であり、チームの基準ではない。**

### 1. WRS の定格 — `wrs_capacity_l_day: 13.5` を新設した

**引用値ではなく導出値である。** 経緯を残す。

裏取りで確認できたのは UPA の側だけだった。NASA の ICES 論文2本（[ICES-2023-097]、[ICES-2026-425]）に
同一の文がある:

> The UPA was designed to process a nominal load of 9 kg/day (19.8 lbs/day) of wastewater
> consisting of urine and flush water. **This is the expected quantity for a 6-crew load on ISS.**

**乗員6人想定の値**であって、4人ではない。そして 9.0 は、この枝の `urine_kg_day_person: 1.50`
× 6人 と**完全に一致する**。つまりサイジングの流儀が確認できた —
「**その箱に入ってくるもの全部の、6人ぶん**」。

`run_wrs` は UPA と WPA を1つの箱にまとめており、尿と**グレイ水の両方**が入る。同じ流儀を当てると:

```
(尿 1.50 + グレイ 0.75) × 6人 = 13.5 L/day
```

**WPA の設計処理量を明記した NASA 資料は見つからなかった。** 出てくるのは運転点（給水 9.1 → 製品水 7.7 kg/day）と
実績値（2022〜23年で約20 L/day）ばかりで、定格ではない。当初 13.6 という数字を挙げたが**裏が取れず撤回した**。
13.5 は上の導出であって、どこかの表から引いた値ではない。

乗員4での余裕は 1.50倍（必要 9.0 L/day）。ARS の 1.082倍と OGS の 2.753倍の間に入る。

なお **`max_feed_l_per_operation: 10.0` は到達不能になった**（定格 0.1875 L/step の 53倍）。
無害だが、これは「効かない定数」であり、今回直した欠陥と同じ形をしている。**要判断として残す。**

### 2. `ars_operation_seconds: 4800` — 残した。ただし clamp を記録する

実害は不変条件が消す。**1200 に変えなかったのは、そうすると `min()` が既定設定で一度も発動せず、
壊れても気づけない死んだコードになるからである。** 4800 のままなら、既定設定そのものが clamp の常時試験になる。
感度解析のスライダー（600〜7200s）も意味を保ち、EXP-004 の「4 step 占有するコミットメント」への入口も残る。

代償は「読んだ人が4倍動くと誤解する」こと。これを潰すため、**黙って clamp せず telemetry に出す**:

```python
"elapsed_seconds": 1200,              # 設定は 4800
"limited_by": "rated_step_capacity",
```

設定の嘘が、毎 step 測定値として現れる。

### 3. 適用時期 — 即時

EXP-013 は完走済みで、v3 の基準点は telemetry つきで `~/ea-runs/` に保存されている。待つ理由が残っていなかった。

[ICES-2023-097]: https://ntrs.nasa.gov/api/citations/20230006217/downloads/ICES%202023-097%20Status%20of%20ISS%20Water%20Management%20and%20Recovery.pdf
[ICES-2026-425]: https://ntrs.nasa.gov/api/citations/20260004140/downloads/2026%20Status%20of%20ISS%20Water%20FINAL.pdf

## 検証結果（1・2 は済み）

1. ✅ **不変条件のテスト** — 同一 step に ARS を6回発行しても合算 1.00x。1000倍の注文も 1.00x。
   `advance_step` で定格が戻る。ARS・WRS の両方でテスト済み
2. ✅ **物理ゲート** — 修正後の run で `capacity_bounds` が ars 1.0x で pass
3. ⬜ **ルール腕が生き残れるか** — 下に予測を記す。**未測定**
4. ⬜ **AI腕の二峰性** — 未測定

```
テスト: 605 passed / 5 skipped / 0 failed
        変更前のベースラインは 601 passed / 5 skipped / 0 failed（+4 は新規テスト）
        ※ STATE-2026-08-27 の「587 passed / 14 failed」は古い記述だった
```

### 波及して直したもの

**スコアカード C軸（要求量の妥当性）が WRS を見逃すようになっていた。** `_per_operation_capacity` は
WRS の上限に `max_feed_l_per_operation`（10 L）を使っていたが、不変条件後の真の1操作能力は 0.1875 L。
ルール腕が要求する 2.0 L は**真の能力の 10.7倍**なのに「within capacity」と判定されていた。
プラント側で塞いだ穴を、スコアカードの中で開け直すことになるので、step で頭打ちにするよう直した。

**`test_a_proposal_that_buys_super_rated_capacity_is_refused` は前提が消えた。** このテストは
`initial_co2_mass: 1800` が物理ゲートで**拒否される**ことを固定していた。不変条件が能力そのものを消したので、
ゲートには捕まえるものが無く、run は普通に評価される。テストは新しい契約に書き直した —
ゲートは通り、`capacity_bounds` は 1.0x を報告し、**C軸は依然として対照腕より低く採点する**
（害は消えたが、判断ミスは判断ミスのまま）。

## 予測 — ルール腕は生き残るか（測定前に記す。手順ルール5）

**この枝のルール腕は効果量を自前計算していない**ので、不変条件による見積もり誤りの交絡は無い
（`_ars_effect_kg` を持つのは upstream の c7f3d89 以降。この枝は未取り込み）。
一方で**ラッチの構造が、余裕 1.082倍と噛み合わない**:

- WARNING 帯（CO₂ ≥ 2.0）は**一撃のみ**（`ars_invoked`）。再武装は「CO₂ が発行時の値以上に戻ったら」
- CRITICAL 帯（≥ 8.0）は毎 step 発行し、goal を 1.5倍に escalate する。
  **不変条件により、この 1.5倍はもう効かない**（定格で頭打ち）。ルール腕の唯一の増強手段が消えた

生成 0.057778/step に対し ARS 定格 0.062500/step。毎 step 回して正味 **−0.004722/step** しか減らない。

ラッチ構造どおりに 50 step 進めた算術（代謝と ARS のみ。OGS・Sabatier・request_co2 は無視）:

```
50 step 後  CO₂ 3.001 kg（開始 1.3）   ARS 発行 19/50 回   critical 到達せず
```

**予測**: ルール腕は CRITICAL には落ちず、**WARNING 帯に居座る**。CO₂ は単調に漂上して 50 step で約 3 kg。
サバイバル規則の CO₂ warning（2 step 滞在で `n // 4`）により、**乗員4なら1人失う**。全滅はしない。

**外れ方の候補**: 上の算術は Sabatier が回収 CO₂ を消費する効果とグレイ水経路を無視している。
また `co2_storage_kg` はキャビン CO₂（回収 CO₂ ではない）であることは確認済み。

**この予測は測定前に書いた。走らせた後に書き換えない。**
