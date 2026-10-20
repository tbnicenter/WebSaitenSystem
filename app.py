import streamlit as st
import pandas as pd
import time
import os
import importlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from supabase import create_client
from streamlit.errors import StreamlitSecretNotFoundError
import toml
from views.log_common import (
    execute_logged_db_operation,
    get_correlation_id,
    insert_operation_log,
)

audit_logger = logging.getLogger("websaiten.audit")
if not audit_logger.handlers:
    audit_handler = logging.StreamHandler()
    audit_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    audit_logger.addHandler(audit_handler)
audit_logger.setLevel(logging.INFO)
audit_logger.propagate = False


# 🎯 【画面幅の拡張】ワイド解像度（1920x1200）をフル活用
st.set_page_config(
    page_title="試験データ管理システム", 
    page_icon="📊", 
    layout="wide"
)


# ⚙️ settingsモジュールのインポート
try:
    import settings
    # Streamlitの再実行で古いsettingsモジュールが再利用されないよう、常に最新ファイルを反映します。
    settings = importlib.reload(settings)
except ImportError:
    st.error("❌ settings.py が見つかりません。設定ファイルを作成してください。")
    st.stop()

# 🛡️ 設定ファイルからタブ権限を動的に取得・パースするロジック
def _parse_role_list(val):
    if isinstance(val, list):
        return [int(x) for x in val if str(x).isdigit()]
    if isinstance(val, int):
        return [val]
    if isinstance(val, str):
        return [int(x.strip()) for x in val.split(",") if x.strip().isdigit()]
    return []

if hasattr(settings, "TAB_ROLE_CONFIG") and settings.TAB_ROLE_CONFIG:
    TAB_ACCESS = {k: _parse_role_list(v) for k, v in settings.TAB_ROLE_CONFIG.items()}
else:
    TAB_ACCESS = {"tab1": [], "tab2": [], "tab3": [], "tab4": [], "tab5": [], "tab6": [], "tab7": []}


# 🌐 1. Supabaseの接続および画像ストレージURLの環境変数・シークレット一括ロード
@st.cache_resource
def init_connection():
    """
    🌐 Supabase接続設定（Render環境変数およびシークレットを完全パース）
    """
    setting_names = {
        "SUPABASE_URL": ("SUPABASE_URL",),
        "SUPABASE_KEY": ("SUPABASE_KEY",),
        "SUPABASE_STORAGE_URL": ("SUPABASE_STORAGE_URL", "STORAGE_BASE_URL"),
    }
    config = {name: "" for name in setting_names}

    for name, aliases in setting_names.items():
        for alias in aliases:
            value = os.environ.get(alias, "").strip()
            if value:
                config[name] = value
                break

    try:
        streamlit_secrets = st.secrets
        for name, aliases in setting_names.items():
            if config[name]:
                continue
            for alias in aliases:
                value = streamlit_secrets.get(alias, "")
                if isinstance(value, str) and value.strip():
                    config[name] = value.strip()
                    break
    except StreamlitSecretNotFoundError:
        pass

    if any(not value for value in config.values()):
        local_secrets_path = Path(__file__).resolve().parent / "supabase" / "secrets.toml"
        try:
            local_secrets = toml.loads(local_secrets_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            local_secrets = {}

        for name, aliases in setting_names.items():
            if config[name]:
                continue
            for alias in aliases:
                value = local_secrets.get(alias, "")
                if isinstance(value, str) and value.strip():
                    config[name] = value.strip()
                    break

    missing_settings = [name for name, value in config.items() if not value]
    if missing_settings:
        missing = "、".join(missing_settings)
        raise RuntimeError(
            f"Supabase設定が不足しています（{missing}）。"
            "ローカルでは supabase/secrets.toml、Renderでは Environment Variables に設定してください。"
        )

    url = config["SUPABASE_URL"]
    key = config["SUPABASE_KEY"]
    storage_url = config["SUPABASE_STORAGE_URL"]

    # 💡 【今回の最重要マージ】取得した最新のストレージURLを、システム全体が参照する settings クラスへ動的注入！
    # これにより、各採点子画面（question_list.py や hold_management.py）内の `settings.STORAGE_BASE_URL` が
    # 自動的にこの環境変数の値へと寸分の狂いもなくリアルタイムに同期されます。
    if storage_url:
        # 💡 万が一設定値の末尾にスラッシュがなくても、自動で補完してURLの破損を完全ガード！
        if not storage_url.endswith("/"):
            storage_url += "/"
        settings.STORAGE_BASE_URL = storage_url

    return create_client(url, key)


# 🌐 Supabase クライアント初期化実行
if not hasattr(st.session_state, "supabase_client"):
    try:
        supabase = init_connection()
        st.session_state["supabase_client"] = supabase
    except Exception as e:
        st.error(f"❌ Supabaseの初期化に失敗しました: {e}")
        st.stop()
else:
    supabase = st.session_state["supabase_client"]

# 🎨 共通コンポーネント：個別更新・一括更新用のポップアップ確認パネル関数
def display_confirm_panel(message, pending_key, container=st):
    """確認ダイアログを表示し、はい/いいえの選択結果を返します"""
    with container.container(border=True):
        st.warning(message)
        c1, c2 = st.columns(2)
        with c1:
            if st.button(settings.LABELS["confirm_yes"], key=f"confirm_yes_{pending_key}", use_container_width=True):
                return True
        with c2:
            if st.button(settings.LABELS["confirm_no"], key=f"confirm_no_{pending_key}", use_container_width=True):
                return False
    return None

# 🔐 1. 未ログイン時の制御
if not st.session_state.get("logged_in", False):
    from views.login_view import show_login_page
    show_login_page(supabase, settings)

# 📄 2. ログイン成功後の全共通ガード＆ルーティング処理
else:
    current_user_id = st.session_state.get("user_id")
    current_user_name = st.session_state.get("user_name")
    current_role_id = st.session_state.get("role_id", None)
    correlation_id = get_correlation_id(st.session_state)

    # ⏱️ 共通自動ログアウトシステム
    current_time = time.time()
    last_activity = st.session_state.get("last_activity_time")
    
    if last_activity is not None:
        elapsed_time = current_time - last_activity
        if elapsed_time > settings.LOGIN_TIMEOUT_SECONDS:
            logout_started_at = time.perf_counter()
            audit_logger.info(
                "audit.logout reason=timeout grader_id=%r correlation_id=%s",
                current_user_id,
                correlation_id,
            )
            logout_operations = []
            if current_user_id:
                timeout_operations = (
                    (
                        "grader_login_status_reset",
                        lambda: supabase.table("graders")
                        .update({"login_status": False})
                        .eq("grader_id", current_user_id)
                        .execute(),
                    ),
                    (
                        "grader_scoring_locks_release",
                        lambda: supabase.table("tbl_scoring_question_management")
                        .update({"is_locked": False, "locked_by_webid": None, "locked_at": None})
                        .eq("locked_by_webid", current_user_id)
                        .execute(),
                    ),
                )
                for operation_name, execute in timeout_operations:
                    try:
                        execute_logged_db_operation(
                            operator_id=current_user_id,
                            action_type="AUTO_LOGOUT",
                            operation_name=operation_name,
                            execute=execute,
                            correlation_id=correlation_id,
                            operation_events=logout_operations,
                        )
                    except Exception:
                        pass

            insert_operation_log(
                supabase=supabase,
                operator_id=current_user_id or "UNKNOWN_USER",
                action_type="AUTO_LOGOUT",
                description="操作タイムアウトによる自動ログオフ",
                correlation_id=correlation_id,
                metadata={
                    "result": (
                        "failure"
                        if any(operation["result"] == "failure" for operation in logout_operations)
                        else "success"
                    ),
                    "duration_ms": round((time.perf_counter() - logout_started_at) * 1000, 2),
                    "db_operations": logout_operations,
                },
            )
            
            # 🚨 【自動リセット対策】放置時も古い記憶（ステップ位置など）を完全に破壊して初期化
            for key in list(st.session_state.keys()):
                del st.session_state[key]
                
            st.error(settings.LABELS["login_timeout"])
            st.rerun()

            
    st.session_state["last_activity_time"] = current_time
    # ─── 📊 共通ヘッダーエリア ───
    col_title, col_logout = st.columns([4, 1])
    with col_title:
        st.markdown(settings.LABELS["app_title"])
        st.caption(settings.LABELS["logged_in_user"].format(name=current_user_name, user_id=current_user_id, role=current_role_id))
    with col_logout:
        st.write("") 
        if st.button(settings.LABELS["logout"], use_container_width=True, key="main_logout_btn"):
            logout_started_at = time.perf_counter()
            audit_logger.info(
                "audit.logout reason=user_requested grader_id=%r correlation_id=%s",
                current_user_id,
                correlation_id,
            )
            logout_db_operations = []
            if current_user_id:
                logout_operations = (
                    (
                        "grader_login_status_reset",
                        lambda: supabase.table("graders")
                        .update({"login_status": False})
                        .eq("grader_id", current_user_id)
                        .execute(),
                    ),
                    (
                        "grader_scoring_locks_release",
                        lambda: supabase.table("tbl_scoring_question_management")
                        .update({"is_locked": False, "locked_by_webid": None, "locked_at": None})
                        .eq("locked_by_webid", current_user_id)
                        .execute(),
                    ),
                )
                for operation_name, execute in logout_operations:
                    try:
                        execute_logged_db_operation(
                            operator_id=current_user_id,
                            action_type="LOGOUT",
                            operation_name=operation_name,
                            execute=execute,
                            correlation_id=correlation_id,
                            operation_events=logout_db_operations,
                        )
                    except Exception:
                        pass

            insert_operation_log(
                supabase=supabase,
                operator_id=current_user_id or "UNKNOWN_USER",
                action_type="LOGOUT",
                description="採点者によるログオフ",
                correlation_id=correlation_id,
                metadata={
                    "result": (
                        "failure"
                        if any(operation["result"] == "failure" for operation in logout_db_operations)
                        else "success"
                    ),
                    "duration_ms": round((time.perf_counter() - logout_started_at) * 1000, 2),
                    "db_operations": logout_db_operations,
                },
            )

            # 🚨 【今回の最重要対策】Streamlitのセッションに記憶されている全変数を完全に消去！
            for key in list(st.session_state.keys()):
                del st.session_state[key]
                
            # 💡 ログアウト完了後に真っ新な状態で強制再起動
            st.rerun()


    # ─── 🛡️ タブ表示権限の動的コントロール ───
    def role_allowed(tab_key, role):
        if role is None:
            return False
        allowed = TAB_ACCESS.get(tab_key, [])
        return role in allowed

    show_tab1 = role_allowed("tab1", current_role_id)
    show_tab2 = role_allowed("tab2", current_role_id)
    show_tab3 = role_allowed("tab3", current_role_id)
    show_tab4 = role_allowed("tab4", current_role_id)
    show_tab5 = role_allowed("tab5", current_role_id)  
    show_tab6 = role_allowed("tab6", current_role_id)  
    show_tab7 = role_allowed("tab7", current_role_id)  

    # 🧪【新設】テストモード用のセッション状態を初期化（デフォルトは本番モード=False）
    if "is_test_mode_active" not in st.session_state:
        st.session_state["is_test_mode_active"] = False

    # 💡 管理者（role_id == 4）の場合のみ、テストモードへ切り替える隠しタブ権限を有効化
    show_tab8 = (current_role_id == 4)

    visible_tab_labels = []
    visible_tab_keys = []
    
    if show_tab1:
        visible_tab_labels.append(settings.LABELS["tab1_label"])
        visible_tab_keys.append("tab1")
    if show_tab2:
        visible_tab_labels.append(settings.LABELS["tab2_group_label"])
        visible_tab_keys.append("tab2")
    if show_tab3:
        visible_tab_labels.append(settings.LABELS.get("tab3_header", "採点情報付きCSV取込"))
        visible_tab_keys.append("tab3")
    if show_tab4:
        visible_tab_labels.append(settings.LABELS['tab3_title'])
        visible_tab_keys.append("tab4")
    if show_tab5:
        visible_tab_labels.append("📥 AI採点用データ管理")
        visible_tab_keys.append("tab5")
    if show_tab6:
        visible_tab_labels.append("📤 採点完了データ出力") 
        visible_tab_keys.append("tab6")                         
    if show_tab7:
        visible_tab_labels.append("🟣 採点保留")
        visible_tab_keys.append("tab7")
    
    # 👑【新設】8番目のテスト管理タブを動的にマウント
    if show_tab8:
        visible_tab_labels.append("🛠️ テスト用デバッグ")
        visible_tab_keys.append("tab8")

    # ─── 🔄 タブの生成と画面呼び出し ───
    if visible_tab_labels:
        # st.tabs() は全タブの本体を毎回実行するため、非表示画面のDB取得まで発生します。
        # 選択中の画面だけを実行し、再描画時のDBアクセスとUI生成を抑えます。
        st.markdown(
            """
            <style>
            div[data-testid="stRadio"] div[role="radiogroup"] > label {
                background: transparent !important;
            }
            div[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) [data-testid="stMarkdownContainer"] {
                background: #dbeafe;
                border-radius: 4px;
                padding: 4px 10px;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        selected_tab = st.radio(
            "画面",
            options=visible_tab_keys,
            format_func=lambda key: visible_tab_labels[visible_tab_keys.index(key)],
            horizontal=True,
            key="active_tab_key",
            label_visibility="collapsed",
        )
        previous_tab = st.session_state.get("_previous_logged_tab_key")
        if previous_tab is not None and previous_tab != selected_tab:
            tab_labels = dict(zip(visible_tab_keys, visible_tab_labels))
            transition_description = (
                f"画面遷移: {tab_labels.get(previous_tab, previous_tab)}"
                f" -> {tab_labels.get(selected_tab, selected_tab)}"
            )
            audit_logger.info(
                "audit.page_transition grader_id=%r from=%r to=%r correlation_id=%s",
                current_user_id,
                tab_labels.get(previous_tab, previous_tab),
                tab_labels.get(selected_tab, selected_tab),
                correlation_id,
            )
            insert_operation_log(
                supabase=supabase,
                operator_id=current_user_id or "UNKNOWN_USER",
                action_type="PAGE_TRANSITION",
                description=transition_description,
                correlation_id=correlation_id,
                metadata={
                    "result": "success",
                    "from": tab_labels.get(previous_tab, previous_tab),
                    "to": tab_labels.get(selected_tab, selected_tab),
                },
            )
        st.session_state["_previous_logged_tab_key"] = selected_tab
        tab_map = {selected_tab: st.container()}
        
        if selected_tab == "tab1":
            with tab_map[selected_tab]:
                from views.question_list import show_question_list
                show_question_list(supabase, settings, current_user_id, current_role_id)
                
        if selected_tab == "tab2":
            with tab_map[selected_tab]:
                from views.progress_mgmt import show_progress_management
                show_progress_management(supabase, settings, display_confirm_panel)
                
        if selected_tab == "tab3":
            with tab_map[selected_tab]:
                from views.data_io_mgmt import show_csv_import
                show_csv_import(supabase, settings)
                
        if selected_tab == "tab4":
            with tab_map[selected_tab]:
                from views.data_io_mgmt import show_file_io_sample
                show_file_io_sample(settings)
                
        if selected_tab == "tab5":
            with tab_map[selected_tab]:
                from views.data_io_mgmt import show_data_io_management
                show_data_io_management(supabase, settings)
                
        if selected_tab == "tab6":
            with tab_map[selected_tab]:
                from views.data_io_mgmt import show_graded_output
                show_graded_output(supabase, settings)
                
        if selected_tab == "tab7":
            with tab_map[selected_tab]:
                from views.hold_management import show_hold_management_page
                show_hold_management_page(supabase, settings, display_confirm_panel, current_user_id)

        # 👑【新設】8番目のタブ：テスト用デバッグ画面の実装
        if selected_tab == "tab8":
            with tab_map[selected_tab]:
                st.markdown("### 🛠️ テスト用デバッグ管理")
                st.markdown(settings.LABELS["debug_tab_description"])
                
                # 💡 トグルスイッチでテストモードのON/OFFを完全コントロール！
                # 他の画面や settings.py を一切書き換えることなく、この状態がシステム全体に安全連動します。
                is_test = st.toggle(
                    "🧪 テストモードを有効化する（デバッグツールを露出）", 
                    value=st.session_state["is_test_mode_active"],
                    key="toggle_test_mode_switch"
                )
                st.session_state["is_test_mode_active"] = is_test

                if is_test:
                    st.success(settings.LABELS["test_mode_enabled"])
                    
                    # 💡 先ほど作成した「問題テーブル全削除ツール」をここにマウント！
                    # テストモードがONの時だけ、この下に物理削除ボタンが綺麗に出現します。
                    try:
                        from views.progress_mgmt import show_danger_zone_test_tools
                        show_danger_zone_test_tools(supabase, settings)
                    except ImportError:
                        # まだ views/progress_mgmt.py 内に関数を定義していない場合のセーフティフォールバック
                        st.caption(settings.LABELS["test_tool_missing"])
                else:
                    st.info(settings.LABELS["test_mode_disabled"])
    else:
        st.warning(settings.LABELS["no_visible_tabs"].format(role_id=current_role_id))
        tab_map = {}

        
