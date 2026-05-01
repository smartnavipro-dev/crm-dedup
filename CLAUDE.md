# CRM重複データ整理ツール — プロジェクトコンテキスト

## 概要
b2b-pain-hunterで検出した「CRMの重複データ問題」を解決するAIツール。
Pattern A戦略（無料で配布 → 信頼 → 有償依頼）の第一弾。
Streamlit製。公開済み。

**デプロイ先**: https://crm-dedup.streamlit.app/
**GitHubリポジトリ**: https://github.com/smartnavipro-dev/crm-dedup

## ファイル構成
```
C:\Users\chanc\crm_dedup\
  crm_dedup.py        ← メインアプリ（v1.2）
  requirements.txt    ← 依存パッケージ
  .env.example        ← APIキーのサンプル（実際の.envはgit管理外）
  .gitignore          ← .env・CSVを除外
  CLAUDE.md           ← このファイル
```

## 起動コマンド
```
cd C:\Users\chanc\crm_dedup
streamlit run crm_dedup.py
```

## スタック
- Streamlit（UI）
- pandas（CSV処理）
- rapidfuzz（ファジーマッチング）
- anthropic / Claude Haiku（AI重複検証・オプション）
- python-dotenv

## アーキテクチャ：5ステップウィザード

| ステップ | 内容 |
|---|---|
| Step 1 アップロード | CSV投入（UTF-8/Shift-JIS/CP932自動判定、最大5,000行）。サンプルデータボタンあり |
| Step 2 ルール設定 | 名前列・ID列・類似度しきい値スライダー・追加マッチングルール（動的追加/削除） |
| Step 3 解析 | fuzzy検出 → Claude検証（オプション） → クラスター理由を集約 |
| Step 4 レビュー | サイドバイサイド表示・検出理由バナー・残す/別人/スキップ・前グループに戻る |
| Step 5 エクスポート | cleaned.csv（元と同列構成）+ delete_ids.csv（削除対象IDリスト）|

## 公開方針（C案）
- APIキーなし → fuzzy-onlyモードで全機能動作（コストゼロ）
- APIキーあり → Claude Haikuがグループを検証・AIおすすめを表示
- Streamlit Cloudにデプロイ予定（saas_diagnosisと同じ手順）
- サイドバーにモード表示・プライバシー免責文あり

---

## 重要な実装ノート（2026-04-30）

### ① 日本語名前マッチング（最重要）
`fuzz.token_sort_ratio`は英語用（スペースで単語分割）。日本語では全文字列が1トークンになるため機能しない。

**実装: `smart_ratio(a, b)`**
```python
CORP_WORDS = ['株式会社', '有限会社', '合同会社', '一般社団法人', ...]

def normalize_name(s):
    for w in CORP_WORDS: s = s.replace(w, '')
    return s.strip()

def smart_ratio(a, b):
    return max(fuzz.ratio(a, b), fuzz.ratio(normalize_name(a), normalize_name(b)))
```
- 法人格（株式会社等）を除去してから`fuzz.ratio`で比較
- 正規化前後の高い方を採用
- 「株式会社山田商事」×「山田商事株式会社」→ 100%（修正前は50%で未検出）

### ② NaN誤検知防止
`pd.read_csv(dtype=str)`でも空セルはfloat NaNになる。
`str(NaN) or ""` = `"nan"`（非空）になり完全一致で誤検知が発生する。

**修正: fillna("")してからdict変換**
```python
rows_dicts = [df.fillna("").iloc[i].to_dict() for i in range(n)]
```

### ③ rule_counter リセット禁止
やり直し時にrule_counterを0に戻すと、Streamlitのwidgetキー（`rule_col_0`等）が前回セッションの値を引き継いでルール設定が化ける。

**修正: rule_counterを保持**
```python
current_counter = st.session_state.rule_counter
for k, v in defaults.items():
    st.session_state[k] = v
st.session_state.rule_counter = current_counter
```

### ④ 追加マッチングルールのロジック
各ルールは OR 条件（いずれか一致で重複候補）。

| マッチ方式 | 処理 |
|---|---|
| 完全一致 | `val_i == val_j` |
| 電話番号一致 | `re.sub(r'\D', '', s)`で数字のみ抽出して比較 |
| ファジーマッチ | `smart_ratio(val_i, val_j) >= threshold` |

### ⑤ Union-Find でクラスタリング
ペアごとのマッチ判定 → Union-Findで同一グループに集約 → `groups()`で2件以上のグループのみ返す。

### ⑥ cluster_reasons の集約
```python
pair_reasons = {}  # key: (min_i, max_j) → list[str]
cluster_reasons = {}  # key: verified_cluster_index → list[str]
```
Claudeがグループを除外した後でも、pair_reasonsは元行インデックスベースなので正しくマッピングできる。

---

## 次のアクション
1. ✅ Streamlit Cloudへのデプロイ（GitHub連携）→ https://crm-dedup.streamlit.app/
2. note / Xでの告知記事（ドラフト完成 → note_article.md、投稿はまだ）
3. #3 商談メモ→CRM変換ツールの着手
