import streamlit as st
from supabase import create_client, Client


def get_supabase_client() -> Client:
    supabase_url = st.secrets["SUPABASE_URL"]
    supabase_key = st.secrets["SUPABASE_SERVICE_ROLE_KEY"]

    return create_client(supabase_url, supabase_key)