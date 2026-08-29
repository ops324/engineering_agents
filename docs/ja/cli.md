# CLI ガイド

シミュレーション実行の推奨方法は統一 CLI です。

## 前提

- ホスト（Mac）に **Python 3.11+**（`pyproject.toml` と `ea doctor` で確認）
- pyenv 利用時: **3.11 以上**をインストール（例: `pyenv install 3.11`）。`.python-version` はマイナー系列の目安

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ea doctor
ea run
```

## ゴールデンパス

引数なしの `ea run` は次を実行します:

- シナリオ: `scrubber_degradation`
- エージェント: `labeled_rule_base`（Ollama / vLLM 不要）
- ステップ数: `scenario.yaml` の値（デフォルト 50）

`scenario.yaml` と同じ物理のみ実行にする場合は `--agents-mode none` を指定します。

## コマンド

| コマンド | 用途 |
| --- | --- |
| `ea run [SCENARIO]` | 1回シミュレーション実行 |
| `ea scenarios` | 利用可能なシナリオ一覧 |
| `ea results [RUN_ID]` | 直近 run 一覧、または `summary.json` 表示 |
| `ea doctor` | Python 3.11+・依存関係・Docker/SSOS マウント・Ollama・vLLM の確認 |
| `ea job run SPEC.json` | シリアライズ済み `RunSpec` を実行（クラスタワーカー互換） |
| `ea --version` | CLI バージョン表示 |

モジュール形式:

```bash
python3 -m tools.cli run scrubber_degradation --agents-mode none
```

## よく使うフラグ

```bash
ea run scrubber_degradation --agents-mode labeled_rule_base --steps 30
ea run ssos_eclss_loop --backend mock --actor-mode none --steps 4
ea run scrubber_degradation --agents-mode llm --llm-provider vllm
ea run scrubber_degradation --set simulation.steps=10
ea run --dry-run --write-spec /tmp/job.json
ea job run /tmp/job.json
```

英語版の詳細（フラグ一覧・exit code）: [en/cli.md](../en/cli.md)

`ssos_eclss_loop` ではシミュレーション内 actor と事後 designer が分かれる。[事後設計エージェント](memo/ssos_eclss_loop/post_run_design_agent.md)。`--agents-mode` は `--actor-mode` の非推奨エイリアス。

## 結果の確認

```bash
ea results
python3 -m streamlit run src/tools/dashboard/app.py
python3 -m tools.plant_sim_sensitivity_app  # plant_sim 3×4 の感度。ラン用ダッシュボードではない (port 8502)
```

## SSOS Docker（`ssos_eclss_loop` + ros2） { #ssos-docker-ssos_eclss_loop--ros2 }

シミュレーションは **Mac ホスト** から `ea run` で実行します。SSOS コンテナ内で `ea` を叩かないでください（`ea` はホストの `.venv` にのみインストールされます）。

**ラン間のプラント初期化**: 各 `ea run ssos_eclss_loop`（ros2）のたびに、CLI がコンテナ内で headless（solar + EPS + ECLSS）を **停止してから再起動** します。前回 run の CO₂ 貯蔵・EPS 状態などが次 run に持ち越されないようにするためです。手動で headless を起動し続ける必要はありません。

設計メモ: [memo/cli_v3_plan.md](memo/cli_v3_plan.md) · ヘルパースクリプト: [`scripts/ssos/README.md`](https://github.com/hirototamura/engineering_agents/blob/main/scripts/ssos/README.md)

### 初回：環境設定からシミュレーションまで（コマンド一式）

**マシンごとに一度。** すべて **ホスト** のターミナルで実行（コンテナに入らない）。

```bash
cd /path/to/engineering_agents

# 1. Python 3.11+ 仮想環境と CLI
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ea doctor

# 2. SSOS コンテナ作成（scripts/ssos/* を /root/ にマウント）
#    Colima / Docker 未起動なら自動起動（Apple Silicon: linux/amd64 + Rosetta）
./scripts/ssos/mac/ssos-run-detached.sh

# 3. 任意：マウント確認
docker ps --filter name=ssos
docker exec ssos test -f /root/ssos-eclss-headless.sh && echo "headless helper OK"
docker exec ssos test -d /ea/src/scenario/ssos_eclss_loop && echo "src mount OK"

# 4. シミュレーション
ea run ssos_eclss_loop --actor-mode labeled_rule_base

# 5. 結果確認
ea results
```

| マウント | 用途 |
| --- | --- |
| `scripts/ssos/*` → `/root/` | headless 起動スクリプト（`ssos-eclss-headless.sh` 等） |
| `src` → `/ea/src` | コード |
| `experiments/results` → `/ea/results` | 成果物（ホストに直接書き込み） |

LLM を使う場合は `--actor-mode llm` および/または `--design-mode llm` と `--llm-provider vllm`。headless 用の第 2 ターミナルは不要です。

### 2 回目以降：シミュレーションのみ（コマンド一式）

環境設定（venv・コンテナ作成）は **不要**。コンテナが動いていれば次だけで足ります。

**コンテナが Up のとき**（`docker ps --filter name=ssos` で `Up`）:

```bash
cd /path/to/engineering_agents
source .venv/bin/activate
ea run ssos_eclss_loop --actor-mode labeled_rule_base
ea results
```

**コンテナが停止しているとき**（`Exited` — 例: 対話シェルから `exit` した）:

```bash
docker start ssos
cd /path/to/engineering_agents
source .venv/bin/activate
ea run ssos_eclss_loop --actor-mode labeled_rule_base
ea results
```

マウントはコンテナ **作成時** に固定されます。ヘルパー未マウントの古いコンテナがある場合は、初回手順の `./scripts/ssos/mac/ssos-run-detached.sh` で作り直してください。

**対話デバッグ**（任意）: `./scripts/ssos/mac/ssos-run.sh` → コンテナ内 `bash /root/ssos-eclss-headless.sh`

**Windows / Linux**: 専用ランナーは未整備。`scripts/ssos/README.md` の手動マウント手順を参照。

### Mock / plant_sim（Docker 不要）

```bash
ea run ssos_eclss_loop --backend mock --actor-mode labeled_rule_base --steps 8
ea run ssos_eclss_loop --backend plant_sim --actor-mode labeled_rule_base --steps 72
ea run ssos_eclss_loop --backend mock --actor-mode llm --set agents.actor.max_actions_per_step=8
```

`plant_sim` は乗員代謝・WRS 水循環・物質収支 ledger を再現 — [Plant Sim backend 解説](memo/ssos_eclss_loop/plant_sim_backend.md)。

### 環境変数

| 変数 | デフォルト | 用途 |
| --- | --- | --- |
| `SSOS_CONTAINER` | `ssos` | コンテナ名 |
| `EA_MOUNT_SRC` | `/ea/src` | コンテナ内の src マウントパス |
| `EA_MOUNT_RESULTS` | `/ea/results` | コンテナ内の results マウントパス |
| `EA_HEADLESS_POLL_TIMEOUT_S` | `120` | headless 起動後の ros2 グラフ待ち（秒） |

`LLM_PROVIDER`、`VLLM_BASE_URL`、`VLLM_MODEL`、`VLLM_API_KEY`、`VLLM_API_TIMEOUT` はコンテナへ転送され、ros2 の llm ジョブがホスト preflight と同じバックエンドを使う。

### exit code 3（環境エラー）

`SSOS environment not ready` が出たとき:

1. `docker ps --filter name=ssos` — **Up** か？ 止まっていれば `docker start ssos`
2. `docker exec ssos test -f /root/ssos-eclss-headless.sh` — 失敗なら `./scripts/ssos/mac/ssos-run-detached.sh` で作り直す
3. `docker exec ssos test -d /ea/src/scenario/ssos_eclss_loop` — 失敗なら src マウントなし。同上で作り直す
4. `ea run` は **ホスト** から実行（コンテナ内ではない）

関連: [ssos/quickstart.md](ssos/quickstart.md)

## 並列実行（将来）

各シミュレーションは `RunSpec` JSON で表現します。将来のバッチランナーはワーカーごとに次を実行します:

```bash
ea job run /worker/jobs/job-0042.json
```

## レガシーエントリポイント

以下も同じ実行パスに委譲されます:

- `python3 src/scripts/run_mock_eclss.py`
- `python3 -m scenario.ssos_eclss_loop.scenario_run`
- `from scenario.runner import run_scenario`
