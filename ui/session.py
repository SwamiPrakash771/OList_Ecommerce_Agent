"""Who is logged in, and the server-side role check every page runs first."""

import streamlit as st

from repository import AdminRepository, CustomerRepository


def current_user() -> dict | None:
    return st.session_state.get("user")


def require_role(role: str) -> dict:
    """Stop rendering the page unless the logged-in user has this role."""
    user = current_user()
    if not user or user["role"] != role:
        st.error("You don't have access to this page.")
        st.stop()
    return user


def customer_repo() -> CustomerRepository:
    user = require_role("customer")
    return CustomerRepository(user["customer_unique_id"])


def admin_repo() -> AdminRepository:
    require_role("admin")
    return AdminRepository()


def logout() -> None:
    st.session_state.clear()
    st.rerun()
