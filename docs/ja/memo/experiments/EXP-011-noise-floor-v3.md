# EXP-011 — LLM腕のノイズ床（新しい運用点で測り直す）

```yaml
experiment_id: EXP-011
date:          2026-08-26
git_commit:    dd42de4
branch:        experiment/eclss-evaluation-layer
config:        v3（乗員4）/ plant_sim / 50 step / inject_failures false / actor=llm / design=none
environment:   vLLM qwen3.5-9b @ 10.10.0.108:8000（max_model_len 32768）/ max_tokens 768
seed:          101（全 run 同一）
n:             temperature 0.45 で 5、temperature 0.0 で 5
status:        pre-registered（予測を先に記録。結果は下段に追記）
```

## なぜやり直すか

EXP-001 は**乗員50・survival 無し・古い閾値**の運用点で、指標は `peak_co2` だった。EXP-007〜009 で
運用点が変わり（乗員4）、判別に効く指標も変わった（person-steps / exposure）。**必要 n を出し直す。**

`design=none` とするのは、事後設計エージェント（:8001 の 27B、別モデル）を回路から外して
**actor 側のばらつきだけを測る**ため。

## 比較の土台（EXP-009、決定的な腕）

```
labeled_rule_base 故障なし  乗員 4 維持   peak 2.053 kg   3mmHg 超過 0 step   person-steps 200
none              故障なし  乗員 3        peak 3.626 kg   3mmHg 超過 20 step  person-steps 164
```

## 予測（結果を見る前に記録）

| # | 予測 | 根拠 |
|---|---|---|
| P1 | LLM 腕の peak は **2.05〜3.63 kg の間**に入る | 何もしないより良く、閾値ルールほど機械的ではない |
| P2 | 生存者は **3〜4 人**。全滅しない | 乗員4では CO2 帯滞在で一度に減るのは `4//4 = 1` 名、CRITICAL 帯は到達不能（EXP-009） |
| P3 | temperature 0.45 の SD は **0.0 の SD より大きい** | サンプリング分散が上乗せされる |
| P4 | temperature 0.0 でも **run 間で一致しない** | 共有 vLLM サーバでは決定性が得られない（EXP-002 で棄却済み）。新運用点でも成立するはず |
| P5 | person-steps は**取りうる値が少なく（5通り以下）**、ノイズ床の指標としては粗い。連続的なばらつきは exposure に出る | 乗員4 × 50 step は離散的で、1名の死亡で約36 person-steps 跳ぶ |

**P5 が当たると、「主軸（乗員）は分解能が粗く、補助指標（exposure）で n を決める」という設計になる。**

## 結果

（測定後に追記）
