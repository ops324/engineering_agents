# EXP-005 — 居住区容積の選定

```yaml
experiment_id: EXP-005
date:          2026-08-24
git_commit:    65d32a9
config:        SCENARIO_HABITAT (reference_limits.py)
result:        388 m³ を採用
status:        confirmed（モデリング判断。実測モジュールではない）
```

## 問題

NASA-STD-3001 [V2 6004] は **1時間平均 ppCO₂ ≤ 3 mmHg**（mmHg = 分圧）。
一方 `plant_sim` は `cabin_co2_kg`（質量）しか持たず、`config.py` に幾何情報がない。
**換算にキャビン容積が要る。** リポジトリのどこにも居住区の記述がない。

## 候補の比較（実測運用点、270 run より）

```
       容積 | 平常1.3kg  警報1.5  重大2.2  peak中央  peak最大   判定
  61.3m³   |    8.87    10.23    15.01    17.81    21.83  ✗ 平常運転が既に基準超過
 181.0m³   |    3.00     3.47     5.08     6.03     7.39  ✗ 平常運転が限界ちょうど
 260.0m³   |    2.09     2.41     3.54     4.20     5.15  ✗ 警報が基準より後
 388.0m³   |    1.40     1.62     2.37     2.81     3.45  ★ 採用
 916.0m³   |    0.59     0.68     1.00     1.19     1.46  ✗ 故障でも基準に届かない
```

## 採用理由

**388 m³ は、既存閾値が NASA基準の「下」に来る唯一の値。**

```
1.62 mmHg  co2_high      → 「動き始めろ」
2.37 mmHg  co2_critical  → 「増強しろ」
3.00 mmHg  [V2 6004]     → 「基準に違反した」
3.45 mmHg  故障時 peak最大 → 故障したときだけ帯を跨ぐ
```

運用警報が規制限界の手前で鳴る。これが正しい運用設計。

## 61.3 m³ を却下した理由

`co2_storage_critical_kg = 2.2` が 15.01 mmHg（ISS Off-Nominal）になる容積として逆算した値。

- **循環論法**。閾値から導いた容積で、その閾値を裏付けたことにはならない
- 平常運転が 8.87 mmHg で既に基準超過。警報は基準のはるか後に鳴る
- **片方しか合わない**。同容積で `co2_high 1.5 kg` は 10.23 mmHg でどの帯にも当たらない
- 15.01 であって 15.000 ではない（厳密に一致するのは 61.342 m³）
- 温度 295.15 K も未出典（293.15K なら 14.91、300K なら 15.26）

## 位置づけ

**実在モジュールの実測値ではなく、計測器の設計判断。** ISS USOS 居住容積と一致するのは補強材料で、
根拠そのものではない。`SCENARIO_HABITAT` に `source="modelling choice (2026-08-24)"` を持たせ、
**全成果物に記録される**。ppCO₂ は 1/容積 で効くので変更の影響は線形。

物理を変えないので、差し替え時は既存 run を再採点するだけでよい。

## 一次資料

- [OCHMO-TB-004 Carbon Dioxide, Rev D, 10-Mar-26](https://www.nasa.gov/wp-content/uploads/2023/12/ochmo-tb-004-carbon-dioxide.pdf) — [V2 6004] 3 mmHg、ISS Off-Nominal 15、ISS Emergency 20
- [OCHMO-TB-003 Habitable Atmosphere, Rev A](https://www.nasa.gov/wp-content/uploads/2023/12/ochmo-tb-003-habitable-atmosphere.pdf) — [V2 6006] 総圧、[V2 6002] 希釈ガス30%

**未解決**: [V2 6004] は1時間平均だが、コードは瞬時値を比較している（step 1200秒なので3step平均が正しい）。
