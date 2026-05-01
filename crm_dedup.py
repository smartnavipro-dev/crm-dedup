"""
CRM重複データ整理ツール v1.2
- fuzzy-only モード（デフォルト）+ Claude AI 検証（オプション）
- 複数列マッチング + ユーザー設定可能なルール
- レビュー画面で検出理由を表示
"""

import os
import io
import re
import json
import itertools

import pandas as pd
import streamlit as st
from anthropic import Anthropic
from rapidfuzz import fuzz
from dotenv import load_dotenv

load_dotenv()

MAX_ROWS = 5_000
DEFAULT_THRESHOLD = 78
MODEL = "claude-haiku-4-5-20251001"
MATCH_TYPES = ["完全一致", "電話番号一致（数字のみ比較）", "ファジーマッチ"]

# 法人格表記（比較前に除去して正規化）
CORP_WORDS = [
    '株式会社', '有限会社', '合同会社', '一般社団法人', '特定非営利活動法人',
    '社会福祉法人', '学校法人', '医療法人', '一般財団法人', '公益財団法人',
    '（株）', '（有）', '(株)', '(有)', '㈱', '㈲',
]

SAMPLE_CSV = """会社名,担当者名,メールアドレス,電話番号,会員ID
株式会社山田商事,山田太郎,yamada@yamada.co.jp,03-1234-5678,C001
山田商事株式会社,山田太郎,yamada.t@yamada.co.jp,03-1234-5678,C002
株式会社鈴木製作所,鈴木一郎,suzuki@suzuki.co.jp,06-9876-5432,C003
鈴木製作所,鈴木一郎,i.suzuki@suzuki.co.jp,,C004
田中食品,田中次郎,tanaka@tanaka.co.jp,045-111-2222,C005
田中食品株式会社,田中二郎,tanaka2@tanaka.co.jp,045-333-9999,C006
佐藤物産株式会社,佐藤花子,sato@sato.com,092-333-4444,C007
"""


# ── Union-Find ────────────────────────────────────────────────────
class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1

    def groups(self):
        from collections import defaultdict
        d = defaultdict(list)
        for i in range(len(self.parent)):
            d[self.find(i)].append(i)
        return [v for v in d.values() if len(v) >= 2]


# ── マッチングロジック ─────────────────────────────────────────────
def normalize_phone(s):
    return re.sub(r'\D', '', str(s)) if pd.notna(s) else ""


def normalize_name(s):
    """法人格表記を除去して比較用に正規化する（日本語の語順違いに対応）"""
    s = str(s).strip()
    for w in CORP_WORDS:
        s = s.replace(w, '')
    return s.strip()


def smart_ratio(a, b):
    """正規化前後の類似度のうち高い方を採用する"""
    raw = fuzz.ratio(a, b)
    norm = fuzz.ratio(normalize_name(a), normalize_name(b))
    return max(raw, norm)


def check_pair(row_i, row_j, name_col, threshold, match_rules):
    """
    ペアが重複候補かチェック。
    返り値: マッチした理由のリスト（空リストなら候補外）
    """
    reasons = []

    # 名前マッチ: 法人格を除去して正規化してから比較
    name_i = str(row_i.get(name_col, "") or "").strip()
    name_j = str(row_j.get(name_col, "") or "").strip()
    if name_i and name_j:
        score = smart_ratio(name_i, name_j)
        if score >= threshold:
            reasons.append(f"名前の類似度 {score}%")

    # 追加ルール（いずれかが一致で候補に追加）
    for rule in match_rules:
        col = rule.get("column", "")
        mtype = rule.get("match_type", "完全一致")
        val_i = str(row_i.get(col, "") or "").strip()
        val_j = str(row_j.get(col, "") or "").strip()
        if not val_i or not val_j:
            continue

        if mtype == "完全一致":
            if val_i == val_j:
                reasons.append(f"「{col}」が完全一致（{val_i}）")
        elif mtype == "電話番号一致（数字のみ比較）":
            ni, nj = normalize_phone(val_i), normalize_phone(val_j)
            if ni and nj and ni == nj:
                reasons.append(f"「{col}」の電話番号が一致（{ni}）")
        elif mtype == "ファジーマッチ":
            s = smart_ratio(val_i, val_j)
            if s >= threshold:
                reasons.append(f"「{col}」の類似度 {s}%")

    return reasons


def compute_cluster_reasons(clusters, pair_reasons):
    """各クラスターの検出理由を集約（重複除去）"""
    result = {}
    for ci, cluster in enumerate(clusters):
        seen = set()
        unique = []
        for i, j in itertools.combinations(cluster, 2):
            key = (min(i, j), max(i, j))
            for r in pair_reasons.get(key, []):
                if r not in seen:
                    seen.add(r)
                    unique.append(r)
        result[ci] = unique
    return result


# ── Claude ────────────────────────────────────────────────────────
def get_client():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    return Anthropic(api_key=key) if key else None


def _rows_to_text(rows):
    lines = []
    for i, row in enumerate(rows):
        cols = [f"{k}: {v}" for k, v in row.items() if pd.notna(v) and str(v).strip()]
        lines.append(f"[{i}] " + " / ".join(cols))
    return "\n".join(lines)


def claude_verify_group(client, group_rows):
    prompt = f"""以下は顧客データの候補グループです。名前や属性が似ているため、同一人物・同一企業の可能性があります。

--- データ ---
{_rows_to_text(group_rows)}
--------------

以下のJSON**のみ**を返してください（説明不要）:
{{
  "is_duplicate": true または false,
  "keep_index": 残すべきレコードの番号（0始まり）,
  "reason": "判断理由を1文で"
}}

判断基準:
- 名前の表記ゆれ（漢字/ひらがな、全角/半角、株式会社の有無）は同一とみなす
- 同じ会社でも明らかに別人なら is_duplicate: false
- データが最も充実しているレコードを keep_index とする"""
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        start, end = text.find("{"), text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception as e:
        return {"is_duplicate": True, "keep_index": 0, "reason": f"(エラー: {e})"}


# ── CSV ────────────────────────────────────────────────────────────
def read_csv_robust(file_bytes):
    for enc in ["utf-8-sig", "utf-8", "shift-jis", "cp932"]:
        try:
            return pd.read_csv(io.BytesIO(file_bytes), encoding=enc, dtype=str)
        except Exception:
            continue
    return None


# ── Streamlit ─────────────────────────────────────────────────────
st.set_page_config(page_title="CRM重複データ整理", page_icon="🔍", layout="wide")

client = get_client()

with st.sidebar:
    if client:
        st.success("🤖 AI検証モード（Claude Haiku）")
    else:
        st.info("🔍 ファジーマッチモード\n\nAPIキー未設定のため、AI検証はスキップされます。ファジーマッチのみで動作します。")
    st.divider()
    st.caption(
        "**プライバシー**\n\n"
        "アップロードデータはメモリ上のみ保持し、サーバーへは保存しません。\n\n"
        "AI検証モードのみ、Anthropic社のAPIにデータが送信されます。"
    )

st.title("🔍 CRM重複データ整理ツール")

# ── Session state ─────────────────────────────────────────────────
defaults = {
    "step": 1,
    "df": None,
    "name_col": None,
    "name_threshold": DEFAULT_THRESHOLD,
    "match_rules": [],
    "rule_counter": 0,
    "clusters": [],
    "claude_results": {},
    "cluster_reasons": {},
    "decisions": {},
    "review_idx": 0,
    "file_name": "",
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── Step indicator ─────────────────────────────────────────────────
step_labels = ["① アップロード", "② ルール設定", "③ 解析中", "④ レビュー", "⑤ エクスポート"]
cols_step = st.columns(5)
for i, label in enumerate(step_labels):
    is_active = (i + 1 == st.session_state.step)
    style = "font-weight:bold;color:#007AFF;" if is_active else "color:#999;"
    cols_step[i].markdown(f'<p style="{style}">{label}</p>', unsafe_allow_html=True)
st.divider()


# ═══════════════════════════════════════════════
# STEP 1: Upload
# ═══════════════════════════════════════════════
if st.session_state.step == 1:
    st.subheader("① CSVファイルをアップロード")
    st.caption(f"対応文字コード: UTF-8 / Shift-JIS / CP932　最大 {MAX_ROWS:,} 行")

    with st.expander("📋 サンプルデータで動作確認する"):
        st.code(SAMPLE_CSV, language="text")
        if st.button("このサンプルデータを使う"):
            df = pd.read_csv(io.StringIO(SAMPLE_CSV), dtype=str)
            st.session_state.df = df
            st.session_state.file_name = "sample.csv"
            st.session_state.step = 2
            st.rerun()

    uploaded = st.file_uploader("CSVファイルを選択", type=["csv"])
    if uploaded:
        raw = uploaded.read()
        df = read_csv_robust(raw)
        if df is None:
            st.error("文字コードを認識できませんでした。UTF-8またはShift-JISで保存し直してください。")
        elif len(df) > MAX_ROWS:
            st.error(f"行数が上限（{MAX_ROWS:,}行）を超えています。分割してアップロードしてください。")
        elif len(df) < 2:
            st.error("データが1行以下です。")
        else:
            st.success(f"✅ {len(df):,} 行 × {len(df.columns)} 列 を読み込みました")
            st.dataframe(df.head(10), use_container_width=True)
            if st.button("次へ → ルールを設定する", type="primary"):
                st.session_state.df = df
                st.session_state.file_name = uploaded.name
                st.session_state.step = 2
                st.rerun()


# ═══════════════════════════════════════════════
# STEP 2: Rule configuration
# ═══════════════════════════════════════════════
elif st.session_state.step == 2:
    df = st.session_state.df
    cols = df.columns.tolist()

    st.subheader("② 重複検出ルールを設定")

    # ── 基本設定 ──────────────────────────────────────────────────
    st.markdown("#### 基本設定")
    name_col = st.selectbox(
        "【チェック項目】重複を調べる列",
        cols,
        index=0,
        help="この列の値が似ているレコードを重複候補として検出します。会社名・担当者名など主となる列を選んでください。",
    )

    threshold = st.slider(
        "どこまで似ていれば重複候補とするか",
        min_value=60,
        max_value=100,
        value=st.session_state.name_threshold,
        step=1,
        format="%d%%",
        key="threshold_slider",
    )
    if threshold >= 90:
        st.caption(f"🔵 現在 {threshold}%：ほぼ完全一致のみ検出。「山田商事」と「山田商事株式会社」は検出、「山田商事」と「山田電機」はスルー。見逃しが増えます。")
    elif threshold >= 78:
        st.caption(f"🟢 現在 {threshold}%（推奨）：「株式会社山田商事」と「山田商事株式会社」のような語順違い・法人格の有無を検出します。")
    else:
        st.caption(f"🟡 現在 {threshold}%：広めに検出。関係のない会社が候補に混じる可能性があります。レビューで確認しながら進めてください。")

    # ── しきい値の可視化（実データのペアをリアルタイム表示） ──────────
    name_values = df[name_col].fillna("").astype(str).tolist()
    n_sample = min(len(name_values), 50)
    sample_pairs = []
    for i, j in itertools.combinations(range(n_sample), 2):
        a, b = name_values[i], name_values[j]
        if not a.strip() or not b.strip():
            continue
        score = smart_ratio(a, b)
        if score >= 40:
            sample_pairs.append((a, b, int(score)))
    sample_pairs.sort(key=lambda x: -x[2])

    detected_pairs   = [(a, b, s) for a, b, s in sample_pairs if s >= threshold]
    borderline_pairs = [(a, b, s) for a, b, s in sample_pairs if threshold - 15 <= s < threshold]

    col_det, col_brd = st.columns(2)
    with col_det:
        st.markdown(
            f'<div style="background:#F0FFF4;border:1px solid #00C851;border-radius:6px;'
            f'padding:0.6rem;text-align:center">'
            f'✅ <b>検出される候補</b><br>'
            f'<span style="font-size:1.6rem;font-weight:bold;color:#00713A">{len(detected_pairs)}件</span>'
            f'</div>', unsafe_allow_html=True
        )
    with col_brd:
        st.markdown(
            f'<div style="background:#FFFBEB;border:1px solid #F59E0B;border-radius:6px;'
            f'padding:0.6rem;text-align:center">'
            f'⚠️ <b>ギリギリ外れているペア</b><br>'
            f'<span style="font-size:1.6rem;font-weight:bold;color:#B45309">{len(borderline_pairs)}件</span>'
            f'</div>', unsafe_allow_html=True
        )

    if sample_pairs:
        with st.expander("実際のデータで確認（スライダーを動かすと変わります）"):
            for a, b, s in detected_pairs[:5]:
                st.markdown(f"✅ `{a}` × `{b}` → **{s}%**")
            if borderline_pairs:
                st.caption("── 以下は現在スルーされているが近いペア（感度を下げると検出される） ──")
                for a, b, s in borderline_pairs[:3]:
                    st.markdown(f"⚠️ `{a}` × `{b}` → **{s}%**")
            if not detected_pairs and not borderline_pairs:
                st.caption("この感度では似ているペアが見つかりませんでした。感度を下げてみてください。")

    st.divider()

    # ── 追加マッチングルール ───────────────────────────────────────
    st.markdown("#### 追加マッチングルール（任意）")
    st.caption(
        "名前が全然違っていても、以下の列がいずれか一致した場合も重複候補として検出します。"
        "メールアドレスや電話番号を追加すると検出精度が上がります。"
    )

    # ヘッダー行
    if st.session_state.match_rules:
        h1, h2, h3 = st.columns([4, 4, 1])
        h1.caption("列")
        h2.caption("マッチ方式")
        h3.caption("")

    # ルール一覧
    delete_id = None
    for rule in st.session_state.match_rules:
        rid = rule["id"]
        c1, c2, c3 = st.columns([4, 4, 1])
        with c1:
            col_idx = cols.index(rule["column"]) if rule["column"] in cols else 0
            rule["column"] = st.selectbox(
                "列", cols,
                index=col_idx,
                key=f"rule_col_{rid}",
                label_visibility="collapsed",
            )
        with c2:
            type_idx = MATCH_TYPES.index(rule["match_type"]) if rule["match_type"] in MATCH_TYPES else 0
            rule["match_type"] = st.selectbox(
                "マッチ方式", MATCH_TYPES,
                index=type_idx,
                key=f"rule_type_{rid}",
                label_visibility="collapsed",
            )
        with c3:
            if st.button("✕", key=f"rule_del_{rid}", help="このルールを削除"):
                delete_id = rid

    if delete_id is not None:
        st.session_state.match_rules = [r for r in st.session_state.match_rules if r["id"] != delete_id]
        st.rerun()

    if st.button("＋ ルールを追加"):
        new_id = st.session_state.rule_counter
        st.session_state.rule_counter += 1
        st.session_state.match_rules.append({
            "id": new_id,
            "column": cols[0],
            "match_type": MATCH_TYPES[0],
        })
        st.rerun()

    # ── 現在の検出条件サマリー ────────────────────────────────────
    st.divider()
    st.markdown("#### 現在の検出条件（以下のいずれかに該当すると重複候補）")
    st.markdown(f"- 「**{name_col}**」の類似度 ≥ **{threshold}%**")
    for rule in st.session_state.match_rules:
        mtype_label = {
            "完全一致": "が完全一致",
            "電話番号一致（数字のみ比較）": "の電話番号が一致（ハイフン等を無視して比較）",
            "ファジーマッチ": f"の類似度 ≥ {threshold}%",
        }.get(rule["match_type"], rule["match_type"])
        st.markdown(f"- 「**{rule['column']}**」{mtype_label}")

    st.divider()

    # プレビュー
    st.markdown("**プレビュー（先頭5件）**")
    st.dataframe(df.head(5), use_container_width=True)

    c1, c2 = st.columns([1, 4])
    with c1:
        if st.button("← 戻る"):
            st.session_state.step = 1
            st.rerun()
    with c2:
        if st.button("解析開始 →", type="primary"):
            st.session_state.name_col = name_col
            st.session_state.name_threshold = threshold
            st.session_state.step = 3
            st.rerun()


# ═══════════════════════════════════════════════
# STEP 3: Analyze
# ═══════════════════════════════════════════════
elif st.session_state.step == 3:
    df = st.session_state.df
    name_col = st.session_state.name_col
    threshold = st.session_state.name_threshold
    match_rules = st.session_state.match_rules

    st.subheader("③ 重複解析中…")

    n = len(df)
    uf = UnionFind(n)
    pair_reasons = {}   # (i, j) -> list[str]

    # ── 検出ルール確認 ────────────────────────────────────────────
    with st.expander("適用中の検出ルール"):
        st.markdown(f"- 「{name_col}」の類似度 ≥ {threshold}%")
        for rule in match_rules:
            st.markdown(f"- 「{rule['column']}」が {rule['match_type']}")

    # ── Step 1: マッチング ────────────────────────────────────────
    st.markdown("**Step 1: 重複候補の検出**")
    bar1 = st.progress(0.0)
    total_pairs = n * (n - 1) // 2
    checked = 0
    tick = max(1, total_pairs // 200)

    # NaN を空文字に変換してから dict 化（NaN同士の誤検知防止）
    rows_dicts = [df.fillna("").iloc[i].to_dict() for i in range(n)]

    for i, j in itertools.combinations(range(n), 2):
        reasons = check_pair(rows_dicts[i], rows_dicts[j], name_col, threshold, match_rules)
        if reasons:
            uf.union(i, j)
            pair_reasons[(min(i, j), max(i, j))] = reasons
        checked += 1
        if checked % tick == 0:
            bar1.progress(min(checked / total_pairs, 1.0))
    bar1.progress(1.0)

    raw_clusters = uf.groups()

    if not raw_clusters:
        st.success("✅ 重複候補が見つかりませんでした。データはきれいです！")
        if st.button("← 最初に戻る"):
            for k, v in defaults.items():
                st.session_state[k] = v
            st.rerun()
        st.stop()

    st.success(f"✅ {len(raw_clusters)} グループの候補を検出")

    # ── Step 2: Claude verification（任意） ───────────────────────
    verified_clusters = []
    claude_results = {}

    if client:
        st.markdown("**Step 2: AIによる重複確認**")
        bar2 = st.progress(0.0)
        status2 = st.empty()
        for ci, cluster in enumerate(raw_clusters):
            rows = [rows_dicts[idx] for idx in cluster]
            result = claude_verify_group(client, rows)
            if result.get("is_duplicate", True):
                vi = len(verified_clusters)
                claude_results[vi] = result
                verified_clusters.append(cluster)
            bar2.progress((ci + 1) / len(raw_clusters))
            status2.markdown(f"{ci + 1}/{len(raw_clusters)} グループ確認済み")
        bar2.progress(1.0)
        dismissed = len(raw_clusters) - len(verified_clusters)
        st.success(
            f"✅ AI検証完了　{len(verified_clusters)} グループを重複と判定"
            + (f"　（{dismissed} グループを除外）" if dismissed else "")
        )
    else:
        verified_clusters = raw_clusters
        st.info(f"ℹ️ ファジーマッチのみ　{len(verified_clusters)} グループをレビューに進みます")

    # クラスターごとの検出理由を集約
    cluster_reasons = compute_cluster_reasons(verified_clusters, pair_reasons)

    st.session_state.clusters = verified_clusters
    st.session_state.claude_results = claude_results
    st.session_state.cluster_reasons = cluster_reasons
    st.session_state.review_idx = 0
    st.session_state.decisions = {}
    st.session_state.step = 4
    st.rerun()


# ═══════════════════════════════════════════════
# STEP 4: Review
# ═══════════════════════════════════════════════
elif st.session_state.step == 4:
    df = st.session_state.df
    clusters = st.session_state.clusters
    claude_results = st.session_state.claude_results
    cluster_reasons = st.session_state.cluster_reasons
    decisions = st.session_state.decisions
    idx = st.session_state.review_idx
    total = len(clusters)
    reviewed = len(decisions)

    st.subheader(f"④ レビュー　{reviewed}/{total} 完了")
    st.progress(reviewed / total if total else 1.0)

    if reviewed >= total:
        st.success(f"✅ 全 {total} グループのレビューが完了しました")
        if st.button("エクスポートへ →", type="primary"):
            st.session_state.step = 5
            st.rerun()
        st.stop()

    cluster = clusters[idx]
    ai_rec = claude_results.get(idx, {})
    keep_hint = ai_rec.get("keep_index", None)
    ai_reason = ai_rec.get("reason", "")
    has_ai = keep_hint is not None

    # ── グループ情報 ──────────────────────────────────────────────
    st.markdown(f"**グループ {idx + 1} / {total}**　｜　{len(cluster)} 件の候補")

    # 検出理由バナー
    detect_reasons = cluster_reasons.get(idx, [])
    if detect_reasons:
        reasons_text = "　/　".join(detect_reasons)
        st.markdown(
            f'<div style="background:#EFF6FF;border-left:4px solid #3B82F6;'
            f'padding:0.5rem 0.8rem;border-radius:4px;margin-bottom:0.5rem">'
            f'🔍 <b>検出理由</b>: {reasons_text}</div>',
            unsafe_allow_html=True,
        )

    # AIコメント
    if has_ai and ai_reason:
        st.info(f"🤖 AIコメント: {ai_reason}")
    elif not has_ai:
        st.caption("ファジーマッチで検出されたグループです。内容を確認して判断してください。")

    # ── サイドバイサイド表示 ──────────────────────────────────────
    cols_disp = st.columns(len(cluster))
    for col_ui, (local_i, row_idx) in zip(cols_disp, enumerate(cluster)):
        row = df.iloc[row_idx]
        is_recommended = has_ai and (local_i == keep_hint)
        border = "2px solid #007AFF" if is_recommended else "1px solid #ddd"
        badge = "&nbsp;🤖 <b>AIおすすめ</b>" if is_recommended else ""
        with col_ui:
            st.markdown(
                f'<div style="border:{border};border-radius:8px;padding:0.8rem">'
                f'<b>レコード {row_idx + 1}</b>{badge}'
                f'<hr style="margin:0.4rem 0">',
                unsafe_allow_html=True,
            )
            for col_name in df.columns:
                val = row[col_name]
                if pd.notna(val) and str(val).strip():
                    st.markdown(f"**{col_name}**: {val}")
            st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("**このグループをどう処理しますか？**")

    row_options = [f"レコード {r + 1}" for r in cluster]
    default_radio = min(keep_hint if keep_hint is not None else 0, len(cluster) - 1)
    selected_label = st.radio(
        "残すレコードを選択",
        row_options,
        index=default_radio,
        horizontal=True,
        key=f"radio_{idx}",
    )
    selected_row_idx = cluster[row_options.index(selected_label)]

    btn1, btn2, btn3 = st.columns(3)
    with btn1:
        if st.button("✅ 選択を残して他を削除", type="primary", key=f"keep_{idx}"):
            decisions[idx] = {"action": "keep", "keep_row": selected_row_idx, "cluster": cluster}
            st.session_state.decisions = decisions
            st.session_state.review_idx = idx + 1
            st.rerun()
    with btn2:
        if st.button("❌ 別人・別会社（全員残す）", key=f"sep_{idx}"):
            decisions[idx] = {"action": "separate", "cluster": cluster}
            st.session_state.decisions = decisions
            st.session_state.review_idx = idx + 1
            st.rerun()
    with btn3:
        if st.button("⏭ スキップ", key=f"skip_{idx}"):
            decisions[idx] = {"action": "skip", "cluster": cluster}
            st.session_state.decisions = decisions
            st.session_state.review_idx = idx + 1
            st.rerun()

    if idx > 0:
        if st.button("← 前のグループ", key=f"back_{idx}"):
            st.session_state.review_idx = idx - 1
            st.rerun()


# ═══════════════════════════════════════════════
# STEP 5: Export
# ═══════════════════════════════════════════════
elif st.session_state.step == 5:
    df = st.session_state.df
    decisions = st.session_state.decisions

    st.subheader("⑤ エクスポート")

    delete_row_indices = set()
    for dec in decisions.values():
        if dec["action"] == "keep":
            for row_idx in dec["cluster"]:
                if row_idx != dec["keep_row"]:
                    delete_row_indices.add(row_idx)

    keep_mask = ~df.index.isin(delete_row_indices)
    df_cleaned = df[keep_mask].reset_index(drop=True)
    df_deleted = df[~keep_mask].reset_index(drop=True)

    st.markdown(f"""
| | 件数 |
|---|---|
| 元データ | {len(df):,} 件 |
| 削除対象 | {len(df_deleted):,} 件 |
| 残すデータ | {len(df_cleaned):,} 件 |
""")

    cleaned_csv_bytes = df_cleaned.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label="📥 cleaned.csv をダウンロード（重複除去済み・元と同じ列構成）",
        data=cleaned_csv_bytes,
        file_name="cleaned.csv",
        mime="text/csv",
    )

    if len(df_deleted) > 0:
        deleted_csv_bytes = df_deleted.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label=f"📥 deleted_records.csv をダウンロード（削除対象 {len(df_deleted)}件・全列）",
            data=deleted_csv_bytes,
            file_name="deleted_records.csv",
            mime="text/csv",
        )
        st.caption("deleted_records.csv には削除対象の全データが入っています。CRMで照合しながら削除する際にご利用ください。")

    if len(df_deleted) > 0:
        with st.expander("削除対象レコードのプレビュー"):
            st.dataframe(df_deleted, use_container_width=True)

    st.divider()
    if st.button("🔁 最初からやり直す"):
        current_counter = st.session_state.rule_counter  # widgetキー重複防止のため保持
        for k, v in defaults.items():
            st.session_state[k] = v
        st.session_state.rule_counter = current_counter
        st.rerun()
