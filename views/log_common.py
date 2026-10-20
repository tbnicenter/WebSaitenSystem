import json
import logging
import os
import traceback
import streamlit as st
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

logger = logging.getLogger("websaiten.audit")


def get_correlation_id(session_state) -> str:
    """Return a stable, non-secret identifier for the current Streamlit session."""
    correlation_id = session_state.get("correlation_id")
    if not correlation_id:
        correlation_id = str(uuid4())
        session_state["correlation_id"] = correlation_id
    return correlation_id


def get_app_version() -> str:
    """Return the deployed revision or an explicitly configured application version."""
    return (
        os.environ.get("APP_VERSION", "").strip()
        or os.environ.get("RENDER_GIT_COMMIT", "").strip()
        or os.environ.get("GIT_COMMIT", "").strip()
        or "unknown"
    )


def classify_error(error: Exception) -> dict:
    """Extract safe, structured error identifiers without recording its message."""
    details = {"type": type(error).__name__}
    code = getattr(error, "code", None)
    if isinstance(code, (str, int)):
        details["code"] = str(code)[:64]
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        details["status_code"] = status_code
    if error.__traceback__:
        details["stack"] = [
            {
                "file": frame.filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
                "function": frame.name,
                "line": frame.lineno,
            }
            for frame in traceback.extract_tb(error.__traceback__)[-8:]
        ]
    return details


def insert_operation_log(
    supabase,
    operator_id: str,
    action_type: str,
    description: str,
    target_id: str = None,
    created_at: str = None,
    metadata: dict = None,
    correlation_id: str = None,
):
    """
    tbl_operation_logs テーブルに操作ログを1件正しく刻印する（DBフィールド完全適合版）
    """
    log_timestamp = created_at or datetime.now(timezone.utc).isoformat()
    log_metadata = dict(metadata or {})
    log_metadata["app_version"] = get_app_version()
    if correlation_id:
        log_metadata["correlation_id"] = correlation_id
    log_description = (
        f"{description} | metadata={json.dumps(log_metadata, ensure_ascii=False, separators=(',', ':'))}"
    )
    try:
        response = supabase.table("tbl_operation_logs").insert({
            "operator_id": operator_id,       # 採点者・管理者のID
            "action_type": action_type,       # 例: "CSV_EXPORT", "LOGIN"
            "target_id": target_id,           # 対象の問題IDや日付など（任意・初期値None）
            "description": log_description,   # 詳細な文言と任意の診断メタデータ
            "created_at": log_timestamp       # UTCの操作日時
        }).execute()
        return response
    except Exception as log_err:
        logger.error(
            "audit.operation_log result=failure action_type=%s operator_id=%r error_type=%s",
            action_type,
            operator_id,
            type(log_err).__name__,
        )
        st.warning(
            "⚠️ 操作ログの記録に失敗しました。詳細はアプリケーションログを確認してください。"
        )
        return None


def execute_logged_db_operation(
    operator_id: str,
    action_type: str,
    operation_name: str,
    execute,
    correlation_id: str,
    operation_events=None,
):
    """Execute one DB operation and collect diagnostics without extra database writes."""
    started_at = perf_counter()
    try:
        response = execute()
    except Exception as error:
        duration_ms = round((perf_counter() - started_at) * 1000, 2)
        error_details = classify_error(error)
        event = {
            "operation": operation_name,
            "result": "failure",
            "duration_ms": duration_ms,
            "error": error_details,
        }
        if operation_events is not None:
            operation_events.append(event)
        logger.error(
            "audit.db_operation result=failure action=%s operation=%s "
            "operator_id=%r correlation_id=%s duration_ms=%s error=%s",
            action_type,
            operation_name,
            operator_id,
            correlation_id,
            duration_ms,
            json.dumps(error_details, separators=(",", ":")),
        )
        raise

    duration_ms = round((perf_counter() - started_at) * 1000, 2)
    rows = getattr(response, "data", None)
    affected_rows = len(rows) if isinstance(rows, list) else None
    event = {
        "operation": operation_name,
        "result": "success",
        "duration_ms": duration_ms,
        "affected_rows": affected_rows,
    }
    if operation_events is not None:
        operation_events.append(event)
    logger.info(
        "audit.db_operation result=success action=%s operation=%s "
        "operator_id=%r correlation_id=%s duration_ms=%s affected_rows=%s",
        action_type,
        operation_name,
        operator_id,
        correlation_id,
        duration_ms,
        affected_rows,
    )
    return response


def release_all_user_locks_common(supabase, user_id: str):
    """
    🔒 【バグ修正用】対象ユーザーが掴みっぱなしにしている全レコードのロックを強制一括解除する
    """
    try:
        supabase.table("tbl_scoring_question_management").update({
            "is_locked": False,
            "locked_by_webid": None,
            "locked_at": None
        }).eq("locked_by_webid", user_id).execute()
        return True
    except Exception:
        # 他の処理を巻き込んで落ちないようログ等に留める
        return False
