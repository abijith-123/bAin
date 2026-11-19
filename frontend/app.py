"""Minimal Streamlit scaffold for the bAin operations dashboard."""

import os

import httpx
import streamlit as st


API_BASE_URL = os.getenv("BAIN_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

st.set_page_config(page_title="bAin | Hospital Operations", page_icon="🏥", layout="wide")
st.title("bAin")
st.caption("Hospital discharge orchestration · Synthetic demonstration scaffold")
st.info(
    "The dashboard shell is ready. Patient workflows and role-aware views will be added "
    "against the finalized shared API contract."
)

if st.button("Check API connection"):
    try:
        response = httpx.get(f"{API_BASE_URL}/api/health", timeout=3.0)
        response.raise_for_status()
        st.success(f"API responded: {response.json().get('status', 'unknown')}")
    except httpx.HTTPError as exc:
        st.error(f"Could not reach the API at {API_BASE_URL}: {exc}")
