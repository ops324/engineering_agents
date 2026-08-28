# EXP-024 — 誰も回していなかったループを回す（事前登録・測定前）

```yaml
experiment_id: EXP-024
date:          2026-08-29
git_commit:    c14f5f9
branch:        experiment/eclss-evaluation-layer
status:        **事前登録。測定はまだ実行していない**
費用:          決定的な腕のみ。1本 0.37秒・120本で約45秒。**GPU 0秒**
```

## なぜこれをやるか

ROADMAP R6 の判断点 **d（設計提案）は「実装済み・一度も測っていない」** と記録されていた。
調べたところ、実態はもう少し細かい:

```
ループ前半（提案を作る）  design_mode=labeled_rule_base で **120 run 実行済み**
                          120本すべてが design_proposals.json を持つ
ループ後半（提案を検証する）proposal_evaluation.json は **~/ea-runs 全体で 0件**
                          ea evaluate は一度も実データに当てられていない
LLM の designer           design_mode=llm の run は **0件**
```

**つまり「提案を出す」までは動いていて、「その提案が本当に効くか」を一度も確かめていない。**
そして確かめる側は**決定的で無料**である（対照/処置の両腕とも `labeled_rule_base`）。

**原則2（無料 → 物理 → LLM）に従えば、LLM の designer に GPU を払う前にここを埋める。**

## 動作確認（1本・実行済み）

```
ea evaluate <run>    0.37秒
  提案 5件（action_profile 2 / set_parameter 2 / service_config 1）
  note: thresholds.co2_storage_high_kg を動かす提案である旨を表示し、
        **両腕とも baseline の物差しで採点**する
  peak_kg 2.051 → 1.989（−0.0625）  terminal_margin +0.0625
  verdict: improved — 2 improved / 0 worsened
```

**物差しが凍結されていること、バーを動かす提案に注記が付くことを確認した。**

---

## 予測（測定前にコミットする。判定規則も併記）

**この枝は直近4回、予測と判定規則を後から書いて撤回した。今回は測る前に書く。**

```
P1  120本の verdict は improved が支配的（80%以上）
    判定: improved の割合を数える。80% 未満なら P1 は外れ

P2  提案の過半数が thresholds.* を含む（＝バーを動かしに行く）
    判定: changes に thresholds.* を含む run の割合。50% 未満なら P2 は外れ

P3  改善は CO₂ 系の指標に集中し、**乗員残存（crew_remaining_frozen）は
    120本のうち 10本未満でしか動かない**
    判定: crew_remaining_frozen の delta が 0 でない run を数える
```

## 解釈規則（これも先に書く）

**P1 と P3 が同時に成り立ったら、それは「良い結果」ではない。**

```
improved が支配的（P1）＋ 乗員が動かない（P3）
  ⇒ verdict の improved は「余裕が少し増えた」を意味するだけで、
     この枝の主指標（乗員残存 50点）に届いていない
  ⇒ **「設計提案ループは動くが、点になる改善は出していない」と読む**

P2 が成り立ったら、提案の主要な戦略が「警報線を下げる」である
  ⇒ 物理を良くしているのか、鳴りやすくしているだけかを分けて数える必要がある
  ⇒ その場合、次に測るべきは **thresholds を含む提案を除いた場合の verdict 分布**
```

**そして、この測定は LLM の designer の良し悪しを一切語らない。**
**語るのは「ルールの designer が作る提案の質」＝ 比較の基準線である。**
基準線が無いまま LLM を走らせれば、EXP-015 の教訓（無料の基準線を先に置く）を破ることになる。

## この測定がしないこと

- LLM designer との比較（基準線を取ってから、別の EXP で）
- 採点式の変更（1文字も変えない）
- 物理の変更（無し。既存 run の再実行のみで、世代は変わらない）

## 手戻り

**ゼロ。** 既存 run から派生する対照実験のみ。出力はスクラッチに置き、`~/ea-runs` を汚さない。
