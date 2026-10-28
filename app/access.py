
"""
Who may see and change an investigation.

Each investigation has one owner, recorded with the case and never changed
(sql/e2e/m24_e2e_investigation_ownership.sql). Its owner may read and
change it; others see it only once it is published, and then only as its
published report, never its working records. Every write path calls
require_write() on the same connection, inside the same transaction,
before writing, so ownership is enforced where the data is changed, not
only in what the pages show.

Identity:
- Signed in through Streamlit's authentication (st.login), a user is
  "oidc:" + a hash of the provider and subject; no e-mail is stored.
- CRIE_LOCAL_USER names a local single user for an installation on one's
  own machine. It is ignored in hosted mode (CRIE_HOSTED=1).
- With the sandbox on (CRIE_SANDBOX=1; on by default when hosted), a visitor
  who chooses to try CRIE becomes a guest: "guest:" + a hash of a random
  token carried in the page's link (?sandbox=...). The link is the key to
  that sandbox: whoever has it can continue the work, nobody else can see
  it. Guests cannot publish, and may open at most GUEST_CASE_LIMIT cases.

Without the ownership schema (the working database today) CRIE keeps its
single-user behaviour: every case is the local investigator's.
"""

import hashlib
import os
import re
import secrets
from dataclasses import dataclass

import streamlit as st

HOSTED = "CRIE_HOSTED"
LOCAL_USER = "CRIE_LOCAL_USER"
SANDBOX = "CRIE_SANDBOX"
GUEST_PARAM = "sandbox"                   # query parameter carrying a guest's token
GUEST_TOKEN = "crie_guest_token"
GUEST_CASE_LIMIT = 5
DEMO_DATABASE = "corporate_intelligence_e2e"
DEMO_PREFIX = "SYNTHDEMO"                 # LEIs of the synthetic demonstration
SYSTEM_PREFIX = "crie:"                   # owners no user can sign in as
DEMO_OWNER = "crie:demonstration"
CAN_WRITE = "crie_can_write"              # set while an owned case is shown

NOT_YOURS = "Only this investigation's owner can change it."


class AccessDenied(PermissionError):
    pass


class HostingConfigurationError(RuntimeError):
    """Hosted mode is misconfigured; CRIE stops rather than serve it."""


@dataclass(frozen=True)
class User:
    subject: str
    name: str


def hosted():
    return os.getenv(HOSTED) == "1"


def sandbox():
    """Guests may try CRIE where cases have owners and the sandbox is on."""

    return (os.getenv(SANDBOX, "1" if hosted() else "0") == "1"
            and st.session_state.get(OWNERSHIP, False))


def _guest_token():
    token = st.session_state.get(GUEST_TOKEN)
    if token is None:
        try:
            token = st.query_params.get(GUEST_PARAM)
        except Exception:  # noqa: BLE001
            token = None
    return token if token and re.fullmatch(r"[0-9a-f]{32}", token) else None


def start_guest():
    """Give this visitor a private sandbox, kept in the page's link."""

    st.session_state[GUEST_TOKEN] = secrets.token_hex(16)
    keep_guest_link()


def keep_guest_link():
    """Keep the guest's token in the link, so the sandbox survives reloads and page changes."""

    token = _guest_token()
    if token and sandbox():
        st.session_state[GUEST_TOKEN] = token
        if st.query_params.get(GUEST_PARAM) != token:
            st.query_params[GUEST_PARAM] = token


def is_guest(user):
    return user is not None and user.subject.startswith("guest:")

def require_hosted_target():
    """A hosted CRIE serves only the demonstration database, never the working one."""

    if hosted() and os.getenv("MYSQL_DATABASE") != DEMO_DATABASE:
        raise HostingConfigurationError(
            f"A hosted CRIE serves only the {DEMO_DATABASE} database.")


def sign_in_available():
    try:
        return "auth" in st.secrets
    except Exception:  # noqa: BLE001 - no secrets file
        return False


def current_user():
    """The signed-in user, the local user (not hosted), or None."""

    try:
        if st.user.is_logged_in:
            issuer, subject = st.user.get("iss", ""), st.user.get("sub", "")
            digest = hashlib.sha256(f"{issuer}|{subject}".encode("utf-8")).hexdigest()[:40]
            return User(f"oidc:{digest}", str(st.user.get("name") or "Signed-in user"))
    except Exception:  # noqa: BLE001 - authentication not configured
        pass
    local = os.getenv(LOCAL_USER, "").strip()
    if local and not hosted():
        return User(f"local:{local}", local)
    token = _guest_token()
    if token and sandbox():
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()[:40]
        return User(f"guest:{digest}", "Guest")
    return None


def ownership_installed(connection):
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = "
                       "DATABASE() AND TABLE_NAME = 'investigation_cases' "
                       "AND COLUMN_NAME = 'owner_subject'")
        return cursor.fetchone()[0] == 1
    finally:
        cursor.close()


def may_write(owner, user):
    """Whether `user` may change a case owned by `owner` ('' for a legacy case)."""

    if user is None:
        return False
    if owner == "":
        return user.subject.startswith("local:")       # legacy cases: the local investigator
    return owner == user.subject


def require_write(connection, case_id, user=None):
    """
    Raise AccessDenied unless the current user may change the case. Runs on
    the caller's connection; the owner never changes, so the check holds for
    the caller's transaction.
    """

    if not ownership_installed(connection):
        if hosted():
            raise AccessDenied("This database has no ownership records.")
        return                                           # single-user installation
    user = user or current_user()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT owner_subject FROM investigation_cases WHERE case_id = %s",
                       (case_id,))
        row = cursor.fetchone()
    finally:
        cursor.close()
    if row is None or not may_write(row[0], user):
        raise AccessDenied(NOT_YOURS)


OWNERSHIP = "crie_ownership"             # whether this database records owners


def can_start():
    """Opening an investigation needs a user wherever cases have owners."""

    return not st.session_state.get(OWNERSHIP, False) or current_user() is not None


def start_options():
    return {} if can_start() else {"disabled": True,
                                   "help": "Sign in to start an investigation."}


def submit_options():
    """Disable a recording control unless the shown case may be changed."""

    if st.session_state.get(CAN_WRITE, True):
        return {}
    return {"disabled": True, "help": NOT_YOURS}


def notice():
    if not st.session_state.get(CAN_WRITE, True):
        st.caption("You are reading this investigation; only its owner can record changes.")
