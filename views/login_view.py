import streamlit as st
import pandas as pd
import time
import logging
from datetime import datetime, timezone
from time import perf_counter
from views.log_common import (
    classify_error,
    execute_logged_db_operation,
    get_correlation_id,
    insert_operation_log,
)

audit_logger = logging.getLogger("websaiten.audit")


def _record_login_log(
    supabase,
    grader_id: str,
    result: str,
    reason: str,
    correlation_id: str,
    duration_ms: float,
    error_details: dict = None,
    db_operations=None,
):
    """ログイン結果を標準ログと操作ログテーブルに記録する。"""
    logged_at = datetime.now(timezone.utc).isoformat()
    audit_logger.info(
        "audit.login result=%s reason=%s grader_id=%r created_at=%s "
        "correlation_id=%s duration_ms=%s",
        result,
        reason,
        grader_id,
        logged_at,
        correlation_id,
        duration_ms,
    )
    response = insert_operation_log(
        supabase=supabase,
        operator_id=grader_id or "UNKNOWN_USER",
        action_type="LOGIN",
        description=f"ログイン{result}: {reason}",
        created_at=logged_at,
        correlation_id=correlation_id,
        metadata={
            "result": "success" if result == "成功" else "failure",
            "reason": reason,
            "duration_ms": duration_ms,
            "error": error_details,
            "db_operations": db_operations or [],
        },
    )
    if response is None:
        audit_logger.error(
            "audit.login_log result=failure grader_id=%r created_at=%s correlation_id=%s",
            grader_id,
            logged_at,
            correlation_id,
        )


def _record_force_logout_log(
    supabase,
    grader_id: str,
    result: str,
    reason: str,
    correlation_id: str,
    duration_ms: float,
    error_details: dict = None,
    db_operations=None,
):
    """Record the outcome of a forced logout attempt without storing credentials."""
    insert_operation_log(
        supabase=supabase,
        operator_id=grader_id or "UNKNOWN_USER",
        action_type="FORCE_LOGOUT",
        target_id=grader_id or None,
        description=f"強制ログオフ{result}: {reason}",
        correlation_id=correlation_id,
        metadata={
            "result": "success" if result == "成功" else "failure",
            "reason": reason,
            "duration_ms": duration_ms,
            "error": error_details,
            "db_operations": db_operations or [],
        },
    )


def show_login_page(supabase, settings):
    """
    🔐 画面制御1: ログイン画面（二重ログイン防止＆強制解除対応版）
    """
    st.markdown(settings.LABELS["login_title"])

    with st.form("login_form"):
        input_id = st.text_input(settings.LABELS["login_user_id"], placeholder=settings.LABELS["input_id_placeholder"])
        input_pass = st.text_input(settings.LABELS["login_password"], type="password", placeholder=settings.LABELS["input_pass_placeholder"])
        
        # フォーム内のボタン等幅対称配置
        col_login, col_unlock = st.columns(2)
        with col_login:
            submit_button = st.form_submit_button(settings.LABELS["login_button"], use_container_width=True)
        with col_unlock:
            unlock_button = st.form_submit_button(settings.LABELS["force_unlock"], use_container_width=True)
        
        # --- ① 通常ログイン処理 ---
        if submit_button:
            login_started_at = perf_counter()
            correlation_id = get_correlation_id(st.session_state)
            db_operations = []
            clean_id = str(input_id or "").strip()
            if not input_id or not input_pass:
                _record_login_log(
                    supabase,
                    clean_id,
                    "失敗",
                    "採点者IDまたはパスワード未入力",
                    correlation_id,
                    round((perf_counter() - login_started_at) * 1000, 2),
                )
                st.error(settings.LABELS["login_required_fields"])
            else:
                try:
                    response = execute_logged_db_operation(
                        operator_id=clean_id,
                        action_type="LOGIN",
                        operation_name="grader_credential_lookup",
                        correlation_id=correlation_id,
                        operation_events=db_operations,
                        execute=lambda: supabase.table("graders")
                        .select("grader_id, grader_name, role_id, group_id, login_status, last_activity_at")
                        .eq("grader_id", clean_id)
                        .eq("password", input_pass)
                        .limit(1)
                        .execute(),
                    )

                    if response.data:
                        user_data = response.data[0]
                        current_status = user_data.get("login_status", False)
                        raw_last_activity = user_data.get("last_activity_at")

                        is_timeout = False
                        if raw_last_activity:
                            try:
                                raw_str = str(raw_last_activity).strip()
                                if " " in raw_str:
                                    date_part, time_part = raw_str.split(" ", 1)
                                    time_part = time_part.split("+")[0].split("Z")[0]
                                    clean_ts_str = f"{date_part} {time_part}"
                                else:
                                    clean_ts_str = raw_str.split("+")[0].split("Z")[0]

                                db_datetime = datetime.fromisoformat(clean_ts_str)
                                db_epoch = db_datetime.replace(tzinfo=timezone.utc).timestamp()
                                now_epoch = datetime.now(timezone.utc).timestamp()
                                timeout_limit = getattr(
                                    settings, "TIMEOUT_SECONDS", settings.LOGIN_TIMEOUT_SECONDS
                                )
                                elapsed_seconds = now_epoch - db_epoch
                                st.info(settings.LABELS["login_debug_time"].format(
                                    elapsed=int(elapsed_seconds),
                                    limit=timeout_limit,
                                    db_value=raw_last_activity
                                ))
                                if elapsed_seconds > timeout_limit or elapsed_seconds < 0:
                                    is_timeout = True
                            except Exception as e:
                                st.error(settings.LABELS["login_time_error"].format(
                                    error=e, db_value=raw_last_activity
                                ))

                        if current_status is True and not is_timeout:
                            _record_login_log(
                                supabase,
                                clean_id,
                                "失敗",
                                "他の端末でログイン中",
                                correlation_id,
                                round((perf_counter() - login_started_at) * 1000, 2),
                                db_operations=db_operations,
                            )
                            st.error(settings.LABELS["login_already_active"])
                        else:
                            now_str = datetime.now(timezone.utc).isoformat()
                            execute_logged_db_operation(
                                operator_id=clean_id,
                                action_type="LOGIN",
                                operation_name="grader_login_status_update",
                                correlation_id=correlation_id,
                                operation_events=db_operations,
                                execute=lambda: supabase.table("graders")
                                .update({
                                    "login_status": True,
                                    "last_activity_at": now_str
                                })
                                .eq("grader_id", clean_id)
                                .execute(),
                            )

                            raw_role = user_data.get("role_id")
                            parsed_role = None
                            if raw_role is not None:
                                try:
                                    parsed_role = int(raw_role)
                                except (TypeError, ValueError):
                                    try:
                                        parsed_role = int(str(raw_role).strip())
                                    except (TypeError, ValueError):
                                        parsed_role = None

                            st.session_state["logged_in"] = True
                            st.session_state["user_id"] = clean_id
                            st.session_state["user_name"] = user_data.get("grader_name", "未設定")
                            st.session_state["group_id"] = user_data.get("group_id", None)
                            st.session_state["role_id"] = parsed_role
                            st.session_state["last_activity_time"] = time.time()

                            _record_login_log(
                                supabase,
                                clean_id,
                                "成功",
                                "認証成功",
                                correlation_id,
                                round((perf_counter() - login_started_at) * 1000, 2),
                                db_operations=db_operations,
                            )
                            st.success(settings.LABELS["login_success"])
                            st.rerun()
                    else:
                        _record_login_log(
                            supabase,
                            clean_id,
                            "失敗",
                            "採点者IDまたはパスワード不一致",
                            correlation_id,
                            round((perf_counter() - login_started_at) * 1000, 2),
                            db_operations=db_operations,
                        )
                        st.error(settings.LABELS["login_invalid_credentials"])
                except Exception as e:
                    _record_login_log(
                        supabase,
                        clean_id,
                        "失敗",
                        "認証処理エラー",
                        correlation_id,
                        round((perf_counter() - login_started_at) * 1000, 2),
                        classify_error(e),
                        db_operations=db_operations,
                    )
                    st.error(settings.LABELS["login_error"].format(error=e))

        # --- ② ログイン詰まり強制解除処理 ---
        if unlock_button:
            unlock_started_at = perf_counter()
            correlation_id = get_correlation_id(st.session_state)
            db_operations = []
            clean_id = str(input_id or "").strip()
            if not input_id or not input_pass:
                _record_force_logout_log(
                    supabase,
                    clean_id,
                    "失敗",
                    "採点者IDまたはパスワード未入力",
                    correlation_id,
                    round((perf_counter() - unlock_started_at) * 1000, 2),
                    db_operations=db_operations,
                )
                audit_logger.warning(
                    "audit.force_logout result=failure reason=missing_credentials "
                    "grader_id=%r correlation_id=%s",
                    clean_id,
                    correlation_id,
                )
                st.error(settings.LABELS["unlock_required_fields"])
            else:
                try:
                    # IDとパスワードの整合性を確認
                    response = execute_logged_db_operation(
                        operator_id=clean_id,
                        action_type="FORCE_LOGOUT",
                        operation_name="grader_credential_lookup",
                        correlation_id=correlation_id,
                        operation_events=db_operations,
                        execute=lambda: supabase.table("graders")
                        .select("grader_id, login_status")
                        .eq("grader_id", clean_id)
                        .eq("password", input_pass)
                        .limit(1)
                        .execute(),
                    )

                    if response.data and len(response.data) > 0:
                        # 整合性が取れたら、DBのログイン状態を強制的に False へリセット
                        execute_logged_db_operation(
                            operator_id=clean_id,
                            action_type="FORCE_LOGOUT",
                            operation_name="grader_login_status_reset",
                            correlation_id=correlation_id,
                            operation_events=db_operations,
                            execute=lambda: supabase.table("graders")
                            .update({"login_status": False})
                            .eq("grader_id", clean_id)
                            .execute(),
                        )
                        
                        # 💡【新規追加】その採点者が掴みっぱなしにしていた問題のレコードロックも100%連動して全自動解放！
                        execute_logged_db_operation(
                            operator_id=clean_id,
                            action_type="FORCE_LOGOUT",
                            operation_name="grader_scoring_locks_release",
                            correlation_id=correlation_id,
                            operation_events=db_operations,
                            execute=lambda: supabase.table("tbl_scoring_question_management")
                            .update({
                                "is_locked": False, 
                                "locked_by_webid": None, 
                                "locked_at": None
                            })
                            .eq("locked_by_webid", clean_id)
                            .execute(),
                        )

                        _record_force_logout_log(
                            supabase,
                            clean_id,
                            "成功",
                            "ログイン状態と問題ロックを解除",
                            correlation_id,
                            round((perf_counter() - unlock_started_at) * 1000, 2),
                            db_operations=db_operations,
                        )
                            
                        audit_logger.info(
                            "audit.force_logout result=success grader_id=%r",
                            clean_id,
                        )
                        st.success(settings.LABELS["unlock_success"])
                    else:
                        _record_force_logout_log(
                            supabase,
                            clean_id,
                            "失敗",
                            "採点者IDまたはパスワード不一致",
                            correlation_id,
                            round((perf_counter() - unlock_started_at) * 1000, 2),
                            db_operations=db_operations,
                        )
                        audit_logger.warning(
                            "audit.force_logout result=failure reason=invalid_credentials "
                            "grader_id=%r correlation_id=%s",
                            clean_id,
                            correlation_id,
                        )
                        st.error(settings.LABELS["unlock_invalid_credentials"])
                except Exception as e:
                    _record_force_logout_log(
                        supabase,
                        clean_id,
                        "失敗",
                        "処理エラー",
                        correlation_id,
                        round((perf_counter() - unlock_started_at) * 1000, 2),
                        classify_error(e),
                        db_operations=db_operations,
                    )
                    audit_logger.error(
                        "audit.force_logout result=failure reason=processing_error "
                        "grader_id=%r correlation_id=%s error=%s",
                        clean_id,
                        correlation_id,
                        classify_error(e),
                    )
                    st.error(settings.LABELS["unlock_error"].format(error=e))
