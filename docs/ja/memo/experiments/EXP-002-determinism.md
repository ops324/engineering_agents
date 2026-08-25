# EXP-002 — 直列化による決定性（棄却）

```yaml
experiment_id: EXP-002
date:          2026-08-24
git_commit:    6f01b424
branch:        feat/separate-design-agents
config:        noise_t00_conditions.yaml + agents.llm.max_concurrency=1
environment:   plant_sim / vLLM qwen3.5-9b @ 10.10.0.108:8000 / temperature 0.0
seed:          101（2本とも同一）
status:        rejected
```

## 仮説

EXP-001 の非決定性は vLLM の並列バッチ由来。`core/llm/base.py` の
`BoundedSemaphore(max_concurrency)` を 1 にして全 LLM 呼び出しを直列化すれば、
バッチ構成が一定になり決定的になる。**コード変更不要**で試せる。

決定的になれば必要 n が 68/arm から 1 に落ち、ルール腕と同じ土俵で比較できる。

## 結果

**棄却。**

```
det_a  2490.8 秒   det_b  2628.7 秒   （並列版は 565-660 秒 = 約4.3倍の減速）
設定は有効（agents_config.yaml に max_concurrency: 1 が記録されている）

telemetry.jsonl : 不一致（171行が異なる）
messages.jsonl  : 600件中一致は35件のみ
初回分岐        : index 19, step 1, eclss_operator_8
                  message と memory は一致、reasoning が異なる
outcome         : peak 3.471 対 3.706 / コマンド 82 対 111
```

分岐が step 1 から step 3 に遅れただけで、消えていない。

## なぜ効かなかったか

`max_concurrency` が制御するのは**こちら側のリクエスト数のみ**。
`10.10.0.108:8000` は研究室の共有サーバで、他テナントのリクエストが同一バッチに混ざりうる。
加えて vLLM の連続バッチング・プレフィックスキャッシュは KV キャッシュ状態に依存する。

**共有推論サーバからは決定性を得られない。**

（注: 「バッチが常に1になる」という機構の説明は**未実証**。減速4.3倍は直列化が起きた証拠だが、
サーバ側バッチが1になったことの証拠ではない。）

## 実務上の結論

**`max_concurrency=1` は使わない。** 4.3倍遅くなるだけで決定性は得られない。
残る道は「1 run から多数の観測が取れる量を測る」方向（規則適合度など）。

まだ試していない可能性: GPU を占有制御し、プレフィックスキャッシュ無効・eager モードで
vLLM を立て直す。ただし排他利用が前提で、保証もない。

データ: `~/ea-runs/2026-08-24-evidence/noise/det_a/`, `det_b/`（telemetry・messages 全文）
