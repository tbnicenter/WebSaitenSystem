import streamlit as st

try:
    from supabase_utils import fetch_all_pages
except ImportError:
    from views.supabase_utils import fetch_all_pages


MASTER_CACHE_TTL_SECONDS = 300


@st.cache_data(ttl=MASTER_CACHE_TTL_SECONDS, show_spinner=False)
def get_grader_master(_supabase):
    return fetch_all_pages(
        lambda start, end: _supabase.table("graders")
        .select("grader_id, grader_name, role_id, group_id")
        .order("grader_id")
        .range(start, end)
    )


@st.cache_data(ttl=MASTER_CACHE_TTL_SECONDS, show_spinner=False)
def get_question_master(_supabase):
    return fetch_all_pages(
        lambda start, end: _supabase.table("mst_questions")
        .select("response_id, question_title, correct_image_file_name")
        .order("response_id")
        .range(start, end)
    )


@st.cache_data(ttl=MASTER_CACHE_TTL_SECONDS, show_spinner=False)
def get_scoring_group_master(_supabase):
    return fetch_all_pages(
        lambda start, end: _supabase.table("scoring_groups")
        .select("group_id, group_name")
        .order("group_id")
        .range(start, end)
    )


def clear_master_cache():
    get_grader_master.clear()
    get_question_master.clear()
    get_scoring_group_master.clear()