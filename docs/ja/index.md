# クイックスタート

数分で最初のシミュレーションを実行できます。Engineering Agents は、**ECLSS 異常の検知 → 運用中の対応 → 事後の恒久設計提案** までをエージェントチームで再現します。

!!! tip "必要なもの"
    - 全プラットフォームで **Python 3.11+** と **Git**
    - `ssos_eclss_loop` の `--backend ros2`（実 SSOS プラント）のみ **Docker**
    - `--agents-mode llm` のみ **Ollama** または研究室 **vLLM**

---

## インストール

=== "macOS / Linux"

    ```bash
    git clone https://github.com/hirototamura/engineering_agents.git
    cd engineering_agents

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -U pip
    pip install -e ".[dev]"

    ea doctor
    ```

=== "Windows（PowerShell）"

    #### 1. Python 環境

    ```powershell
    git clone https://github.com/hirototamura/engineering_agents.git
    cd engineering_agents

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -U pip
    python -m pip install -e ".[dev]"

    python -m tools.cli doctor
    python -m pytest -q
    ```

    #### 2. Docker Desktop（実 SSOS のみ）

    [Docker Desktop](https://www.docker.com/products/docker-desktop/) を **WSL 2** バックエンドでインストール。**Windows Containers** は無効のまま。

    ```powershell
    wsl --status
    docker info --format "OSType={{.OSType}} OperatingSystem={{.OperatingSystem}}"
    docker run --rm hello-world
    ```

    | インストーラ項目 | 推奨 |
    | --- | --- |
    | Use WSL 2 instead of Hyper-V | 有効 |
    | Allow Windows Containers | 無効 |

    `wsl --status` で WSL 未導入の場合: 管理者 PowerShell で `wsl --install` → 再起動 → Docker Desktop 起動。

    #### 3. SSOS スクリプトの実行

    `scripts/*.sh` は **Git Bash** から実行。例:

    ```powershell
    & "C:\Program Files\Git\bin\bash.exe" -lc "ea run ssos_eclss_loop --backend mock --actor-mode labeled_rule_base --steps 8"
    ```

    !!! tip "Windows で詰まりやすい点"
        - `python` が Microsoft Store を開く → App Execution Alias を無効化するか、インストール済み Python のフルパスを使う。
        - `Activate.ps1` がブロックされる → `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`、または `.\.venv\Scripts\python.exe` を直接呼ぶ。
        - 詳細手順: [概要 §2B](overview.md#2b-windows-powershell--docker-desktop)。

!!! note "Cloud / CI 環境"
    更新スクリプトが `pip install -e ".[dev]"` でシステム Python に入れる場合は、`ea` の代わりに `python3 -m tools.cli` を使ってください。

---

## CLI でシミュレーションを実行

統合 CLI **`ea`** が推奨エントリポイントです。

### ゴールデンパス（Docker 不要・Ollama 不要）

```bash
ea run scrubber_degradation --agents-mode labeled_rule_base --steps 20
ea results
```

| コマンド | 用途 |
| --- | --- |
| `ea run [SCENARIO]` | 1 回シミュレーション |
| `ea scenarios` | シナリオ一覧 |
| `ea results [RUN_ID]` | 直近 run または `summary.json` 表示 |
| `ea doctor` | Python・依存・Docker/SSOS・Ollama・vLLM の確認 |

モジュール形式（同等）:

```bash
python3 -m tools.cli run scrubber_degradation --agents-mode labeled_rule_base --steps 20
```

!!! tip "設計 → 検証ループ"
    Run N で `design_proposals.json` が発行されます。Run N+1 を `--apply-proposals` 付きで実行すると設定がマージされ再シミュレーション — [ssos_eclss_loop シナリオ](scenario-ssos-eclss-loop.md#実行方法) を参照。

### Mock SSOS シナリオ（Docker 不要）

```bash
ea run ssos_eclss_loop --backend mock --actor-mode labeled_rule_base --steps 8
ea results
```

### 実 SSOS（Docker + ros2）

macOS 初回セットアップ — **ホスト** のみで実行:

```bash
./scripts/ssos/mac/ssos-run-detached.sh
ea run ssos_eclss_loop --actor-mode labeled_rule_base
ea results
```

詳細: [SSOS Docker セットアップ](ssos/quickstart.md) · CLI 全文: [CLI ガイド](cli.md)

---

## シナリオ概要

| シナリオ | 内容 | バックエンド | 典型コマンド |
| --- | --- | --- | --- |
| [scrubber_degradation](scenario-scrubber-degradation.md) | Python モック上の CO₂ スクラバー異常 | `StationSimulator` | `ea run scrubber_degradation --agents-mode labeled_rule_base` |
| [ssos_eclss_loop](scenario-ssos-eclss-loop.md) | SSOS ECLSS（ARS/OGS/WRS）のエージェント運用 | `mock` / `ros2` | `ea run ssos_eclss_loop --backend mock --actor-mode labeled_rule_base` |

両シナリオでエージェントモードは共通: **`none`** / **`labeled_rule_base`**（再現性の高い回帰）/ **`llm`**（Ollama または研究室 vLLM）。

---

## 結果ファイルの保存場所

デフォルトの出力ルート:

```text
src/experiments/results/<run_id>/
```

| ファイル | 内容 |
| --- | --- |
| `telemetry.jsonl` | ステップごとのプラント指標 |
| `messages.jsonl` | エージェントメッセージ・推論 |
| `events.jsonl` | 異常注入・コマンド・設計イベント |
| `design_proposals.json` | **ラン終了後**の恒久設計提案 |
| `summary.json` | run メタデータ（`scenario`, `agents_mode` 等） |
| `health_metrics.jsonl` | safe / warning / critical（ssos_eclss_loop） |

`--run-id` / `--output-dir` / `EA_RESULTS_ROOT` で上書き可能。

```bash
ea results                          # 直近 run 一覧
ea results scrubber_degradation_labeled_rule_base
python3 -m streamlit run src/tools/dashboard/app.py   # 任意: ダッシュボード
```

---

## 次に読むページ

| トピック | ページ |
| --- | --- |
| プロジェクト目的とダッシュボード | [概要](overview.md) |
| レイヤ設計とエージェントフロー | [アーキテクチャ](architecture.md) |
| JSONL と API 契約 | [API 契約](api-contracts.md) |
| コーディングエージェント向け規律 | [エージェントガイド](AGENTS.md) |
| SSOS Docker + ROS 2 接合 | [SSOS 接合](ssos/index.md) |
| ローカルでドキュメント閲覧 | `pip install -e ".[dev]" && mkdocs serve` → [http://127.0.0.1:8000](http://127.0.0.1:8000) |
