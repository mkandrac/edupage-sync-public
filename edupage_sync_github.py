import os
import sys

# All execution goes through the public-safe output boundary in run.py.
if __name__ == "__main__" and os.getenv("EDUPAGE_PRIVATE_WORKER") != "1":
    raise SystemExit("Use python run.py; direct worker execution is disabled.")

from edupage_api import Edupage
from edupage_api.exceptions import BadCredentialsException
from edupage_api.login import Login

import base64
import email
import hashlib
import imaplib
import json
import os
import platform
import re
import socket
import smtplib
import subprocess
import sys
import time
import traceback
import urllib3.util.connection as urllib3_connection

from collections import Counter, defaultdict
from cryptography.fernet import Fernet, InvalidToken
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.policy import default as default_email_policy
from email.utils import make_msgid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


# ============================================================
# CONFIG
# ============================================================

from private_config import load_config
from priority import source_priority

CONFIG = load_config()
ACCOUNTS = CONFIG["accounts"]


SCHEMA_VERSION = 4

# Staršie položky nových kategórií spracuje jednorazový review. Produkcia
# po nasadení pustí iba položky vytvorené od tohto okamihu. TEST_MODE cutoff
# zámerne ignoruje, aby review videl aj existujúce kandidáty.
ACTIONABLE_CATEGORIES_START_AT = datetime.fromisoformat(
    CONFIG["capture_start_at"]
)

SESSION_CIPHER = "fernet-pbkdf2-sha256-v1"
SESSION_KDF_SALT = b"edupage-sync-gmail-state-v1"
SESSION_KDF_ITERATIONS = 480_000

TIMEZONE = CONFIG["timezone"]

OUTPUT_FILE = "edupage_output_github.json"

RUN_SLOT = os.getenv(
    "EDUPAGE_RUN_SLOT",
    "manual"
).strip().lower()

if RUN_SLOT not in {
    "morning",
    "noon",
    "early",
    "main",
    "manual"
}:
    RUN_SLOT = "manual"


# ============================================================
# TEST MODE
# ============================================================

#
# True:
# - ignoruje už spracované eventy
# - pošle ich znova
# - NEAKTUALIZUJE state
#
# False:
# - normálna produkčná prevádzka
#

TEST_MODE = os.getenv(
    "EDUPAGE_TEST_MODE",
    "0"
).lower() in {
    "1",
    "true",
    "yes"
}

# Bezpečná diagnostika prihlasovacieho HTTP toku. Nevypisuje query parametre,
# cookies, hlavičky, credentials ani telo odpovede.
HTTP_DIAGNOSTICS = os.getenv(
    "EDUPAGE_HTTP_DIAGNOSTICS",
    "0"
).lower() in {
    "1",
    "true",
    "yes"
}

FORCE_IPV4 = os.getenv(
    "EDUPAGE_FORCE_IPV4",
    "0"
).lower() in {
    "1",
    "true",
    "yes"
}

if FORCE_IPV4:
    urllib3_connection.allowed_gai_family = (
        lambda: socket.AF_INET
    )


# ============================================================
# KEYCHAIN / CLOUD SECRET CONFIG
# ============================================================

#
# Na GitHube:
#   get_secret() najprv nájde environment variable.
#
# Na Macu:
#   ak env variable neexistuje,
#   skúsi macOS Keychain.
#

KEYCHAIN_SERVICE = "edupage-sync-github"


# ============================================================
# EMAIL CONFIG
# ============================================================

GMAIL_USERNAME_SECRET = "GMAIL_USERNAME"
GMAIL_PASSWORD_SECRET = "GMAIL_APP_PASSWORD"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

STATE_SUBJECT = "[EDUPAGE-STATE]"
SYSTEM_EMAIL_LABEL = "EduPage/System"


class SessionRenewalRequired(RuntimeError):
    """Dôveryhodná EduPage session expirovala a treba ju obnoviť na Macu."""


APP_ACTION_MARKERS = (
    "žiadosti/vyhlásenia",
    "ziadosti/vyhlasenia",
    "žiadosť/vyhlásenie",
    "ziadost/vyhlasenie",
    "informovaný súhlas",
    "informovany suhlas",
    "potvrďte účasť",
    "potvrdte ucast",
    "potvrdenie účasti",
    "potvrdenie ucasti",
    "odsúhlasenie účasti",
    "odsuhlasenie ucasti",
    "vyplnenie ankety",
    "hlasovanie na edupage",
    "hlasovanie v edupage",
    "prihlasovanie prebieha pod vašim kontom",
    "prihlasovanie prebieha pod vasim kontom",
    # Kým edupage-api nemá samostatné Platby API, zachováme správy, ktoré
    # oznamujú nový predpis alebo splatnosť v module Platby.
    "v edupage v časti platby",
    "v edupage v casti platby",
    "v module platby",
    "termín splatnosti",
    "termin splatnosti"
)

APP_ACTION_VERBS = (
    "vyplniť",
    "vyplnit",
    "potvrdiť",
    "potvrdit",
    "potvrďte",
    "potvrdte",
    "hlasovať",
    "hlasovat",
    "odsúhlasiť",
    "odsuhlasit",
    "podať",
    "podat",
    "prihlásiť",
    "prihlasit",
    "prihlásenie",
    "prihlasenie",
    "odhlásiť",
    "odhlasit"
)

HOMEWORK_CHILDREN = set()

HOMEWORK_ACTION_MARKERS = (
    "domáca úloha",
    "domaca uloha",
    "d. ú.",
    "d. u.",
    "vypracovať",
    "vypracovat",
    "dopracovať",
    "dopracovat",
    "dokončiť",
    "dokoncit",
    "priniesť",
    "priniest",
    "doniesť",
    "doniest",
    "pripraviť",
    "pripravit",
    "naučiť sa",
    "naucit sa",
    "prečítať",
    "precitat",
    "napísať",
    "napisat",
    "precvičiť",
    "precvicit"
)

PREPARATION_ACTION_MARKERS = (
    "kúpiť",
    "kupit",
    "zakúpiť",
    "zakupit",
    "objednať",
    "objednat",
    "uhradiť",
    "uhradit",
    "zaplatiť",
    "zaplatit",
    "priniesť",
    "priniest",
    "doniesť",
    "doniest",
    "zabezpečiť",
    "zabezpecit",
    "zaobstarať",
    "zaobstarat",
    "pripraviť",
    "pripravit"
)

HOMEWORK_EVENT_TYPES = {
    "HOMEWORK",
    "HOMEWORK_TEST"
}

TIMETABLE_CHANGE_EVENT_TYPES = {
    "CHANGE_ROOM",
    "SUBSTITUTION",
    "TT_CANCEL"
}

DIRECT_APP_ACTION_EVENT_TYPES = {
    "CONFIRMATION",
    "EXCUSED_LESSON",
    "POLL",
    "PROCESS"
}

APP_EVENT_CANDIDATE_TYPES = {
    "CLASS_TEACHER_EVENT",
    "CONTEST",
    "EVENT",
    "EXCURSION",
    "PARENTS_EVENING",
    "SCHOOL_EVENT",
    "SCHOOL_TRIP"
}

APP_ACTION_DATA_KEYS = {
    "actionid",
    "etestAnswerCards",
    "etestCards",
    "importantReply",
    "requireParentConfirm"
}


# ============================================================
# SECRET HANDLING
# ============================================================

def get_secret(secret_name):
    """
    Secret backend:

    1. Environment variable
       - GitHub Actions
       - cloud
       - Docker
       - CI/CD

    2. macOS Keychain
       - lokálny development/test
    """

    # --------------------------------------------------------
    # CLOUD / GITHUB / ENVIRONMENT
    # --------------------------------------------------------

    env_value = os.getenv(secret_name)

    if env_value:
        return env_value.strip()


    # --------------------------------------------------------
    # macOS KEYCHAIN FALLBACK
    # --------------------------------------------------------

    if platform.system() == "Darwin":

        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                secret_name,
                "-w"
            ],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            return result.stdout.strip()


    # --------------------------------------------------------
    # NOTHING FOUND
    # --------------------------------------------------------

    raise RuntimeError(
        f"Secret '{secret_name}' nebol nájdený."
    )


# ============================================================
# ENCRYPTED EDUPAGE SESSION
# ============================================================

def get_session_cipher():
    """Odvodí šifrovací kľúč z Gmail App Password, ktorý už je secret."""

    gmail_app_password = get_secret(
        GMAIL_PASSWORD_SECRET
    ).replace(
        " ",
        ""
    ).encode(
        "utf-8"
    )

    key = hashlib.pbkdf2_hmac(
        "sha256",
        gmail_app_password,
        SESSION_KDF_SALT,
        SESSION_KDF_ITERATIONS,
        dklen=32
    )

    return Fernet(
        base64.urlsafe_b64encode(
            key
        )
    )


def get_saved_session_id(state, account_key):
    session_record = (
        state
        .get(
            "edupage_sessions",
            {}
        )
        .get(
            account_key
        )
    )

    if not isinstance(session_record, dict):
        return None

    if session_record.get("cipher") != SESSION_CIPHER:
        raise RuntimeError(
            f"Session pre '{account_key}' používa neznámy formát šifrovania."
        )

    ciphertext = session_record.get(
        "php_session"
    )

    if not isinstance(ciphertext, str):
        raise RuntimeError(
            f"Session pre '{account_key}' nemá platný zašifrovaný obsah."
        )

    try:
        return (
            get_session_cipher()
            .decrypt(
                ciphertext.encode(
                    "ascii"
                )
            )
            .decode(
                "utf-8"
            )
        )

    except (InvalidToken, UnicodeError, ValueError) as error:
        raise RuntimeError(
            f"Session pre '{account_key}' sa nepodarilo dešifrovať."
        ) from error


def extract_php_session_id(edupage, subdomain):
    expected_domain = f"{subdomain}.edupage.org"

    matching_sessions = [
        cookie.value
        for cookie in edupage.session.cookies
        if (
            cookie.name == "PHPSESSID"
            and
            cookie.domain.lstrip(".") == expected_domain
        )
    ]

    if not matching_sessions:
        matching_sessions = [
            cookie.value
            for cookie in edupage.session.cookies
            if cookie.name == "PHPSESSID"
        ]

    if not matching_sessions:
        raise RuntimeError(
            f"EduPage pre '{subdomain}' neposkytol PHPSESSID cookie."
        )

    return matching_sessions[-1]


def save_session_to_state(state, account, edupage, updated_at):
    session_id = extract_php_session_id(
        edupage,
        account["subdomain"]
    )

    encrypted_session = (
        get_session_cipher()
        .encrypt(
            session_id.encode(
                "utf-8"
            )
        )
        .decode(
            "ascii"
        )
    )

    sessions = state.setdefault(
        "edupage_sessions",
        {}
    )

    sessions[
        account["key"]
    ] = {
        "cipher": SESSION_CIPHER,
        "php_session": encrypted_session,
        "updated_at": updated_at
    }


def configure_edupage_client(edupage, account):
    if not HTTP_DIAGNOSTICS:
        return

    print_dns_diagnostics(
        account["subdomain"]
    )

    edupage.session.hooks.setdefault(
        "response",
        []
    ).append(
        trace_http_response
    )


def login_with_saved_session(account, username, password, state):
    session_id = get_saved_session_id(
        state,
        account["key"]
    )

    if session_id:
        edupage = Edupage()
        configure_edupage_client(
            edupage,
            account
        )

        try:
            Login(edupage).reload_data(
                account["subdomain"],
                session_id,
                username
            )

            print(
                "Login OK – obnovená zašifrovaná session z Gmail state."
            )

            return edupage

        except BadCredentialsException:
            print(
                "Uložená EduPage session expirovala – skúšam nové prihlásenie."
            )

    edupage = Edupage()
    configure_edupage_client(
        edupage,
        account
    )

    two_factor_login = edupage.login(
        username,
        password,
        account["subdomain"]
    )

    if two_factor_login is not None:
        raise SessionRenewalRequired(
            "EduPage vyžaduje potvrdenie nového zariadenia. "
            f"Na Macu spusti: python3 edupage_sync_github.py "
            f"--bootstrap-session {account['key']}"
        )

    print(
        "Login OK – nová session vytvorená heslom."
    )

    return edupage


# ============================================================
# SAFE HTTP DIAGNOSTICS
# ============================================================

def print_dns_diagnostics(subdomain):
    hostname = f"{subdomain}.edupage.org"

    try:
        addresses = sorted({
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                443,
                type=socket.SOCK_STREAM
            )
        })

        print(
            "DNS:",
            hostname,
            "->",
            ", ".join(addresses)
        )

        print(
            "HTTP address family:",
            "IPv4 only"
            if FORCE_IPV4
            else "system default"
        )

    except Exception as error:
        print(
            "DNS CHYBA:",
            hostname,
            repr(error)
        )


def trace_http_response(response, *args, **kwargs):
    parsed_url = urlsplit(response.url)
    safe_url = (
        f"{parsed_url.scheme}://"
        f"{parsed_url.netloc}"
        f"{parsed_url.path}"
    )

    response_text = response.text.lower()

    markers = [
        marker
        for marker in (
            "twofactor",
            "csrfauth",
            "captcha",
            "cloudflare"
        )
        if marker in response_text
    ]

    print(
        "HTTP TRACE:",
        response.request.method,
        response.status_code,
        safe_url,
        f"bytes={len(response.content)}",
        "markers=" + (
            ",".join(markers)
            if markers
            else "none"
        )
    )


# ============================================================
# STATE HELPERS
# ============================================================

def select_gmail_all_mail(imap, readonly=True):
    """Otvorí Gmail All Mail podľa IMAP atribútu \\All, nie podľa jazyka UI."""

    status, mailboxes = imap.list()

    if status != "OK":
        raise RuntimeError(
            "Zoznam Gmail schránok sa nepodarilo načítať."
        )

    all_mailbox = None

    for mailbox in mailboxes or []:
        if not isinstance(mailbox, bytes):
            continue

        mailbox_line = mailbox.decode(
            "ascii",
            errors="strict"
        )

        flags_end = mailbox_line.find(")")

        if flags_end == -1:
            continue

        flags = mailbox_line[:flags_end + 1].lower()

        if "\\all" not in flags:
            continue

        # Posledná položka LIST odpovede je názov mailboxu. Ponechávame
        # prípadné úvodzovky, pretože imaplib ich pri SELECT nepridáva.
        mailbox_match = re.search(
            r'("(?:[^"\\]|\\.)*"|[^ ]+)$',
            mailbox_line
        )

        if mailbox_match:
            all_mailbox = mailbox_match.group(1)
            break

    if all_mailbox is None:
        raise RuntimeError(
            "Gmail All Mail schránka s IMAP príznakom \\All nebola nájdená."
        )

    status, _ = imap.select(
        all_mailbox,
        readonly=readonly
    )

    if status != "OK":
        raise RuntimeError(
            "Gmail All Mail sa nepodarilo otvoriť."
        )


def load_state():
    """Načíta najnovší Gmail state aj po jeho archivovaní."""

    email_username, email_password = get_gmail_credentials()

    with imaplib.IMAP4_SSL(
        IMAP_SERVER,
        IMAP_PORT
    ) as imap:

        imap.login(
            email_username,
            email_password
        )

        select_gmail_all_mail(imap)

        status, search_data = imap.search(
            None,
            "FROM",
            f'"{email_username}"',
            "SUBJECT",
            f'"{STATE_SUBJECT}"'
        )

        if status != "OK":
            raise RuntimeError(
                "Vyhľadanie Gmail state emailu zlyhalo."
            )

        message_ids = search_data[0].split()

        if not message_ids:
            if CONFIG.get("require_existing_state", True):
                raise RuntimeError("Existing state required; refusing to reset history.")
            print(
                "Gmail state nenájdený – začínam s prázdnym state."
            )

            return {
                "processed_event_ids": []
            }

        latest_message_id = message_ids[-1]

        status, fetch_data = imap.fetch(
            latest_message_id,
            "(RFC822)"
        )

        if status != "OK":
            raise RuntimeError(
                "Najnovší Gmail state email sa nepodarilo načítať."
            )

        raw_message = next(
            (
                item[1]
                for item in fetch_data
                if isinstance(item, tuple)
            ),
            None
        )

        if raw_message is None:
            raise RuntimeError(
                "Gmail state email nemá čitateľné telo."
            )

        parsed_message = email.message_from_bytes(
            raw_message,
            policy=default_email_policy
        )

        body = parsed_message.get_body(
            preferencelist=("plain",)
        )

        if body is None:
            raise RuntimeError(
                "Gmail state email neobsahuje textovú časť."
            )

        state = json.loads(
            body.get_content()
        )

        processed_event_ids = state.get(
            "processed_event_ids"
        )

        if not isinstance(processed_event_ids, list):
            raise RuntimeError(
                "Gmail state má neplatný formát processed_event_ids."
            )

        if not all(
            isinstance(event_id, str)
            for event_id in processed_event_ids
        ):
            raise RuntimeError(
                "Gmail state obsahuje neplatné event ID."
            )

        print(
            "Gmail state načítaný:",
            len(processed_event_ids),
            "spracovaných eventov."
        )

        return state


def save_output(output):

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            output,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# EMAIL
# ============================================================

def get_gmail_credentials():
    email_from = get_secret(
        GMAIL_USERNAME_SECRET
    )

    email_password = get_secret(
        GMAIL_PASSWORD_SECRET
    )

    # Google App Password môže byť skopírovaný
    # s medzerami medzi štvoricami znakov
    email_password = email_password.replace(
        " ",
        ""
    )

    return email_from, email_password


def send_email(subject, body):
    email_from, email_password = get_gmail_credentials()

    msg = EmailMessage()

    msg["From"] = email_from
    msg["To"] = email_from
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(
        domain=email_from.rsplit("@", 1)[-1]
    )

    msg.set_content(
        body
    )

    with smtplib.SMTP_SSL(
        SMTP_SERVER,
        SMTP_PORT
    ) as smtp:

        smtp.login(
            email_from,
            email_password
        )

        smtp.send_message(
            msg
        )

    if subject.startswith((
        STATE_SUBJECT,
        "[EDUPAGE-RAW]"
    )):
        try:
            file_system_email(
                msg["Message-ID"],
                email_from,
                email_password
            )
        except Exception as error:
            # Doručenie RAW/state je dôležitejšie než kozmetické upratanie.
            # Pri chybe preto beh nezduplikujeme; správa iba zostane v Inboxe.
            print(
                "UPOZORNENIE: systémový Gmail sa nepodarilo upratať:",
                repr(error)
            )


def file_system_email(message_id, email_username, email_password):
    """Označí vlastný RAW/state email štítkom, prečíta ho a archivuje."""

    with imaplib.IMAP4_SSL(
        IMAP_SERVER,
        IMAP_PORT
    ) as imap:
        imap.login(
            email_username,
            email_password
        )

        select_gmail_all_mail(
            imap,
            readonly=False
        )

        matching_ids = []

        # SMTP prijatie a sprístupnenie cez IMAP nemusia byť úplne súčasné.
        for attempt in range(5):
            status, search_data = imap.search(
                None,
                "HEADER",
                "Message-ID",
                f'"{message_id}"'
            )

            if status != "OK":
                raise RuntimeError(
                    "Vyhľadanie odoslaného systémového emailu zlyhalo."
                )

            matching_ids = search_data[0].split()

            if matching_ids:
                break

            if attempt < 4:
                time.sleep(1)

        if not matching_ids:
            raise RuntimeError(
                "Odoslaný systémový email sa cez IMAP nenašiel."
            )

        for gmail_message_id in matching_ids:
            operations = (
                (
                    "+X-GM-LABELS",
                    f'("{SYSTEM_EMAIL_LABEL}")'
                ),
                (
                    "-X-GM-LABELS",
                    "(\\Inbox)"
                ),
                (
                    "+FLAGS",
                    "(\\Seen)"
                )
            )

            for command, value in operations:
                status, _ = imap.store(
                    gmail_message_id,
                    command,
                    value
                )

                if status != "OK":
                    raise RuntimeError(
                        f"Gmail IMAP operácia {command} zlyhala."
                    )


def send_raw_email(output):

    generated_at = output[
        "generated_at"
    ]

    subject_date = (
        generated_at[:16]
        .replace(
            "T",
            " "
        )
    )

    if TEST_MODE:
        subject_prefix = "[EDUPAGE-CATEGORY-REVIEW]"
    else:
        subject_prefix = (
            f"[EDUPAGE-RAW][{RUN_SLOT.upper()}]"
        )

    subject = f"{subject_prefix} {subject_date}"

    body = json.dumps(
        output,
        ensure_ascii=False,
        indent=2
    )

    send_email(
        subject,
        body
    )


def send_state_email(state):
    body = json.dumps(
        state,
        ensure_ascii=False,
        indent=2
    )

    send_email(
        STATE_SUBJECT,
        body
    )


def bootstrap_session(account_key):
    """Na dôveryhodnom Macu vytvorí a do Gmail state uloží novú session."""

    account = next(
        (
            item
            for item in ACCOUNTS
            if item["key"] == account_key
        ),
        None
    )

    if account is None:
        valid_keys = ", ".join(
            item["key"]
            for item in ACCOUNTS
        )

        raise RuntimeError(
            f"Neznámy účet '{account_key}'. Platné hodnoty: {valid_keys}."
        )

    state = load_state()

    username = get_secret(
        account["username_secret"]
    )

    password = get_secret(
        account["password_secret"]
    )

    edupage = Edupage()
    configure_edupage_client(
        edupage,
        account
    )

    two_factor_login = edupage.login(
        username,
        password,
        account["subdomain"]
    )

    if two_factor_login is not None:
        raise SessionRenewalRequired(
            "Aj lokálne prihlásenie žiada potvrdenie nového zariadenia. "
            "Potvrď ho v EduPage a potom bootstrap spusti znova."
        )

    now = datetime.now(
        ZoneInfo(
            TIMEZONE
        )
    )

    state["schema_version"] = SCHEMA_VERSION

    save_session_to_state(
        state,
        account,
        edupage,
        now.isoformat(
            timespec="seconds"
        )
    )

    state["updated_at"] = now.isoformat(
        timespec="seconds"
    )

    send_state_email(
        state
    )

    print(
        f"Session pre {account['name']} bola zašifrovaná a uložená "
        "do nového Gmail [EDUPAGE-STATE]."
    )


def keepalive_sessions():
    """Obnoví uložené session bez loginu heslom a bez čítania správ."""

    state = load_state()
    keepalive_errors = []

    now = datetime.now(
        ZoneInfo(
            TIMEZONE
        )
    )

    refreshed_at = now.isoformat(
        timespec="seconds"
    )

    for account in ACCOUNTS:
        print(
            f"Keepalive: {account['name']}"
        )

        try:
            username = get_secret(
                account["username_secret"]
            )

            session_id = get_saved_session_id(
                state,
                account["key"]
            )

            if not session_id:
                raise SessionRenewalRequired(
                    f"Pre {account['name']} nie je uložená session."
                )

            edupage = Edupage()
            configure_edupage_client(
                edupage,
                account
            )

            try:
                Login(edupage).reload_data(
                    account["subdomain"],
                    session_id,
                    username
                )

            except BadCredentialsException as error:
                raise SessionRenewalRequired(
                    f"Session pre {account['name']} expirovala. "
                    f"Na Macu spusti: python3 edupage_sync_github.py "
                    f"--bootstrap-session {account['key']}"
                ) from error

            save_session_to_state(
                state,
                account,
                edupage,
                refreshed_at
            )

            print(
                "Keepalive OK – session obnovená."
            )

        except Exception as error:
            keepalive_errors.append(
                f"{account['name']}: {error}"
            )

            print(
                "KEEPALIVE CHYBA:",
                str(error)
            )

    if keepalive_errors:
        print(
            "Gmail state sa nemení, pretože keepalive neobnovil všetky session."
        )

        raise RuntimeError(
            "; ".join(
                keepalive_errors
            )
        )

    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = refreshed_at
    state["last_keepalive_at"] = refreshed_at

    send_state_email(
        state
    )

    print(
        "Keepalive hotový – obe session obnovené a Gmail state aktualizovaný."
    )


def event_type_name(notification):
    """Vráti stabilný názov EventType bez závislosti od jeho __str__ formátu."""

    event_type = getattr(
        notification,
        "event_type",
        None
    )

    name = getattr(
        event_type,
        "name",
        None
    )

    if name:
        return name

    return str(event_type).removeprefix(
        "EventType."
    )


def contains_app_action_marker(notification):
    text = (
        getattr(
            notification,
            "text",
            ""
        )
        or ""
    ).casefold()

    if any(
        marker in text
        for marker in APP_ACTION_MARKERS
    ):
        return True

    mentions_edupage = (
        "edupage" in text
        or "edu page" in text
    )

    return mentions_edupage and any(
        verb in text
        for verb in APP_ACTION_VERBS
    )


def notification_data(notification):
    data = getattr(
        notification,
        "additional_data",
        {}
    )

    return data if isinstance(data, dict) else {}


def action_value_is_set(value):
    if isinstance(value, str):
        return value.strip().casefold() not in {
            "",
            "0",
            "false",
            "no",
            "none",
            "null"
        }

    return bool(value)


def has_action_metadata(notification):
    data = notification_data(
        notification
    )

    return any(
        action_value_is_set(
            data.get(key)
        )
        for key in APP_ACTION_DATA_KEYS
    )


def parse_date_value(value):
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        return datetime.fromisoformat(
            value.strip()
        ).date()

    except ValueError:
        try:
            return date.fromisoformat(
                value.strip()[:10]
            )

        except ValueError:
            return None


def homework_due_date(notification):
    data = notification_data(
        notification
    )

    old_values = data.get(
        "oldVals"
    )

    candidates = [
        data.get("date")
    ]

    if isinstance(old_values, dict):
        candidates.extend([
            old_values.get("date"),
            old_values.get("dueDate")
        ])

    for candidate in candidates:
        parsed = parse_date_value(
            candidate
        )

        if parsed:
            return parsed

    return None


def contains_explicit_homework_action(notification):
    data = notification_data(
        notification
    )

    old_values = data.get(
        "oldVals"
    )

    if not isinstance(old_values, dict):
        old_values = {}

    text = " ".join(
        str(value)
        for value in (
            getattr(notification, "text", ""),
            data.get("nazov", ""),
            data.get("popis", ""),
            old_values.get("title", ""),
            old_values.get("nazov", ""),
            old_values.get("popis", "")
        )
        if value
    ).casefold()

    return any(
        marker in text
        for marker in HOMEWORK_ACTION_MARKERS
    )


def contains_preparation_action(notification):
    text = (
        getattr(
            notification,
            "text",
            ""
        )
        or ""
    ).casefold()

    return any(
        marker in text
        for marker in PREPARATION_ACTION_MARKERS
    )


def affected_dates(notification):
    data = notification_data(
        notification
    )

    values = [
        data.get("date"),
        data.get("datefrom"),
        data.get("dateto")
    ]

    values.extend(
        key
        for key in data
        if isinstance(key, str)
    )

    parsed_dates = {
        parsed
        for value in values
        if (
            parsed := parse_date_value(
                value
            )
        )
    }

    return sorted(
        parsed_dates
    )


def classify_notification(notification, children, today):
    """Transport classification only; relevance is decided by the parent brief.

    Never gate messages on keywords, recipient matching, completion or expiry.
    Unknown event types are retained so new EduPage categories cannot vanish.
    H_* events are technical timeline bookkeeping, not parent-facing content.
    """
    category = event_type_name(notification)
    if category.startswith("H_"):
        return None
    if category in HOMEWORK_EVENT_TYPES:
        return "homework"
    if category in TIMETABLE_CHANGE_EVENT_TYPES:
        return "timetable_change"
    if category in DIRECT_APP_ACTION_EVENT_TYPES:
        return "app_action"
    if category in {"MESSAGE", "None"}:
        return "message"
    return "event"


def serialize_relevant_details(notification, category):
    data = notification_data(
        notification
    )

    details = {}

    if category == "homework":
        old_values = data.get(
            "oldVals"
        )

        if not isinstance(old_values, dict):
            old_values = {}

        due_date = homework_due_date(
            notification
        )

        details = {
            "title": (
                old_values.get("title")
                or old_values.get("nazov")
                or data.get("nazov")
                or ""
            ),
            "description": (
                old_values.get("popis")
                or data.get("popis")
                or ""
            ),
            "due_date": (
                due_date.isoformat()
                if due_date
                else None
            ),
            "subject_id": data.get(
                "predmetid"
            )
        }

    elif category == "timetable_change":
        details = {
            "affected_dates": [
                item.isoformat()
                for item in affected_dates(
                    notification
                )
            ]
        }

    elif category == "app_action":
        scalar_keys = (
            "confirmTime",
            "date",
            "datefrom",
            "dateto",
            "name",
            "requireParentConfirm",
            "time_from",
            "time_to",
            "typeName"
        )

        details = {
            key: data.get(key)
            for key in scalar_keys
            if isinstance(
                data.get(key),
                (str, int, float, bool)
            )
        }

    return {
        key: value
        for key, value in details.items()
        if value not in (None, "", [], {})
    }


def collect_dictionary_keys(value, prefix="", depth=0):
    """Získa iba názvy polí; hodnoty sa do diagnostického logu nedostanú."""

    if depth > 2 or not isinstance(value, dict):
        return set()

    keys = set()

    for key, child_value in value.items():
        safe_key = str(key)

        if (
            "/" in safe_key
            or "?" in safe_key
            or "%" in safe_key
        ):
            continue

        key_path = (
            f"{prefix}.{safe_key}"
            if prefix
            else safe_key
        )

        keys.add(
            key_path
        )

        keys.update(
            collect_dictionary_keys(
                child_value,
                key_path,
                depth + 1
            )
        )

    return keys


def probe_categories():
    """Read-only prehľad reálne dostupných timeline kategórií oboch škôl."""

    state = load_state()
    probe_errors = []
    processed_ids = set(
        state.get(
            "processed_event_ids",
            []
        )
    )

    for account in ACCOUNTS:
        print()
        print(
            f"CATEGORY PROBE: {account['name']}"
        )

        try:
            username = get_secret(
                account["username_secret"]
            )

            session_id = get_saved_session_id(
                state,
                account["key"]
            )

            if not session_id:
                raise SessionRenewalRequired(
                    f"Pre {account['name']} nie je uložená session."
                )

            edupage = Edupage()
            configure_edupage_client(
                edupage,
                account
            )

            Login(edupage).reload_data(
                account["subdomain"],
                session_id,
                username
            )

            notifications = edupage.get_notifications()

            counts = Counter()
            unfinished_counts = Counter()
            app_marker_counts = Counter()
            selected_counts = Counter()
            new_selected_counts = Counter()
            keys_by_type = defaultdict(set)

            probe_children = list(HOMEWORK_CHILDREN)

            today = datetime.now(
                ZoneInfo(
                    TIMEZONE
                )
            ).date()

            for notification in notifications:
                category = event_type_name(
                    notification
                )

                counts[category] += 1

                if not getattr(
                    notification,
                    "is_done",
                    False
                ):
                    unfinished_counts[category] += 1

                if contains_app_action_marker(
                    notification
                ):
                    app_marker_counts[category] += 1

                selected_type = classify_notification(
                    notification,
                    probe_children,
                    today
                )

                if selected_type:
                    selected_counts[selected_type] += 1

                    event_key = (
                        f'{account["key"]}:'
                        f'{notification.event_id}'
                    )

                    if event_key not in processed_ids:
                        new_selected_counts[selected_type] += 1

                keys_by_type[category].update(
                    collect_dictionary_keys(
                        getattr(
                            notification,
                            "additional_data",
                            {}
                        )
                    )
                )

            print(
                "Timeline položiek:",
                len(notifications)
            )

            for category in sorted(counts):
                safe_keys = ",".join(
                    sorted(
                        keys_by_type[category]
                    )[:30]
                ) or "none"

                print(
                    "CATEGORY",
                    category,
                    f"total={counts[category]}",
                    f"open={unfinished_counts[category]}",
                    f"app_markers={app_marker_counts[category]}",
                    f"data_keys={safe_keys}"
                )

            print(
                "SELECTED",
                " ".join(
                    f"{key}={selected_counts[key]}"
                    for key in (
                        "app_action",
                        "homework",
                        "timetable_change"
                    )
                )
            )

            print(
                "SELECTED_NEW",
                " ".join(
                    f"{key}={new_selected_counts[key]}"
                    for key in (
                        "app_action",
                        "homework",
                        "timetable_change"
                    )
                )
            )

        except Exception as error:
            probe_errors.append(
                f"{account['name']}: {error}"
            )

            print(
                "CATEGORY PROBE CHYBA:",
                str(error)
            )

    if probe_errors:
        raise RuntimeError(
            "; ".join(
                probe_errors
            )
        )

    print()
    print(
        "Category probe hotový – Gmail state ani RAW sa nemenili."
    )


if len(sys.argv) > 1:
    if (
        len(sys.argv) == 3
        and
        sys.argv[1] == "--bootstrap-session"
    ):
        bootstrap_session(
            sys.argv[2]
        )
        raise SystemExit(0)

    if (
        len(sys.argv) == 2
        and
        sys.argv[1] == "--keepalive"
    ):
        keepalive_sessions()
        raise SystemExit(0)

    if (
        len(sys.argv) == 2
        and
        sys.argv[1] == "--probe-categories"
    ):
        probe_categories()
        raise SystemExit(0)

    raise SystemExit(
        "Použitie: python3 edupage_sync_github.py "
        "[--bootstrap-session ACCOUNT_KEY | --keepalive | --probe-categories]"
    )


# ============================================================
# LOAD STATE
# ============================================================

state = load_state()

processed_ids = set(
    state.get(
        "processed_event_ids",
        []
    )
)

new_event_ids = []

all_children = []
all_messages = []
account_results = []
run_errors = []


# ============================================================
# PROCESS ACCOUNTS
# ============================================================

for account in ACCOUNTS:

    print()
    print(
        "=" * 60
    )

    print(
        "Spracovávam:",
        account["name"]
    )

    print(
        "=" * 60
    )

    try:

        # ----------------------------------------------------
        # LOAD SECRETS
        # ----------------------------------------------------

        username = get_secret(
            account[
                "username_secret"
            ]
        )

        password = get_secret(
            account[
                "password_secret"
            ]
        )


        # ----------------------------------------------------
        # LOGIN
        # ----------------------------------------------------

        edupage = login_with_saved_session(
            account,
            username,
            password,
            state
        )

        save_session_to_state(
            state,
            account,
            edupage,
            datetime.now(
                ZoneInfo(
                    TIMEZONE
                )
            ).isoformat(
                timespec="seconds"
            )
        )


        # ----------------------------------------------------
        # CHILDREN
        # ----------------------------------------------------

        student_ids = (
            edupage
            .data
            .get(
                "parentStudentids",
                []
            )
        )

        students = (
            edupage
            .data[
                "dbi"
            ][
                "students"
            ]
        )

        classes = (
            edupage
            .data[
                "dbi"
            ][
                "classes"
            ]
        )

        children = []


        for student_id in student_ids:

            student = students.get(
                str(
                    student_id
                )
            )

            if not student:
                continue


            class_info = classes.get(
                str(
                    student.get(
                        "classid",
                        ""
                    )
                ),
                {}
            )


            child = {

                "school":
                    account[
                        "name"
                    ],

                "school_key":
                    account[
                        "key"
                    ],

                "id":
                    str(
                        student.get(
                            "id",
                            ""
                        )
                    ),

                "firstname":
                    student.get(
                        "firstname",
                        ""
                    ),

                "lastname":
                    student.get(
                        "lastname",
                        ""
                    ),

                "class_id":
                    str(
                        student.get(
                            "classid",
                            ""
                        )
                    ),

                "class_name":
                    class_info.get(
                        "name",
                        ""
                    ),

                "class_short":
                    class_info.get(
                        "short",
                        ""
                    )
            }


            children.append(
                child
            )

            all_children.append(
                child
            )


        print(
            "Deti:"
        )


        for child in children:

            print(
                "-",
                child[
                    "firstname"
                ],
                child[
                    "lastname"
                ],
                "|",
                child[
                    "class_name"
                ]
            )


        # ----------------------------------------------------
        # CHILD MATCHING
        # ----------------------------------------------------

        def identify_children(
            notification
        ):

            recipient = (
                notification
                .recipient
                or ""
            )

            matches = []


            for child in children:

                firstname = (
                    child[
                        "firstname"
                    ]
                )

                full_name = " ".join(
                    part
                    for part in (
                        firstname,
                        child[
                            "lastname"
                        ]
                    )
                    if part
                )

                if (
                    firstname
                    and
                    (
                        full_name in recipient
                        or firstname in recipient
                    )
                ):

                    matches.append(
                        firstname
                    )

                    continue

                class_name = (
                    child[
                        "class_name"
                    ]
                )

                class_short = (
                    child[
                        "class_short"
                    ]
                )


                if (
                    class_name
                    and
                    class_name
                    in recipient
                ):

                    matches.append(
                        child[
                            "firstname"
                        ]
                    )

                    continue


                if (
                    class_short
                    and
                    class_short
                    in recipient
                ):

                    matches.append(
                        child[
                            "firstname"
                        ]
                    )


            if matches:

                return list(
                    dict.fromkeys(
                        matches
                    )
                )


            return [
                "SPOLOČNÉ / NEURČENÉ"
            ]


        # ----------------------------------------------------
        # RELEVANT ITEM SERIALIZER
        # ----------------------------------------------------

        source_items = {
            str(item.get("timelineid")): item
            for item in (edupage.data.get("items") or [])
            if isinstance(item, dict)
        }

        def serialize_item(
            notification,
            item_type,
            matched_children
        ):

            created_at = (
                notification
                .created_at
            )


            return {

                "priority": source_priority(notification, source_items),

                "source":
                    "edupage",

                "school":
                    account[
                        "name"
                    ],

                "school_key":
                    account[
                        "key"
                    ],

                "event_id":
                    notification
                    .event_id,

                "event_type":
                    event_type_name(
                        notification
                    ),

                "item_type":
                    item_type,

                "created_at":
                    (
                        created_at
                        .isoformat()

                        if created_at
                        else None
                    ),

                "school_date":
                    (
                        created_at
                        .date()
                        .isoformat()

                        if created_at
                        else None
                    ),

                "author":
                    notification
                    .author
                    or "",

                "recipient":
                    notification
                    .recipient
                    or "",

                "children":
                    matched_children,

                "is_done":
                    bool(
                        getattr(
                            notification,
                            "is_done",
                            False
                        )
                    ),

                "done_at":
                    (
                        notification.done_at.isoformat()
                        if getattr(
                            notification,
                            "done_at",
                            None
                        )
                        else None
                    ),

                "text":
                    (
                        notification
                        .text
                        .strip()

                        if notification.text
                        else ""
                    ),

                "details":
                    serialize_relevant_details(
                        notification,
                        item_type
                    ),

                # Preserve source event fields for downstream interpretation.
                # Only notification data, never client/session/login objects.
                "additional_data":
                    json.loads(json.dumps(
                        notification_data(notification),
                        ensure_ascii=False,
                        default=str
                    ))
            }


        # ----------------------------------------------------
        # GET NOTIFICATIONS
        # ----------------------------------------------------

        notifications = (
            edupage
            .get_notifications()
        )

        account_messages = []


        for notification in notifications:

            matched_children = identify_children(
                notification
            )

            item_type = classify_notification(
                notification,
                matched_children,
                datetime.now(
                    ZoneInfo(
                        TIMEZONE
                    )
                ).date()
            )

            if item_type is None:
                continue


            # ------------------------------------------------
            # GLOBAL UNIQUE KEY
            # ------------------------------------------------

            event_key = (
                f'{account["key"]}:'
                f'{notification.event_id}'
            )


            # ------------------------------------------------
            # DEDUPLICATION
            # ------------------------------------------------

            if (
                not TEST_MODE
                and
                event_key
                in processed_ids
            ):

                continue

            # Historické položky nových kategórií patria iba do jednorazového
            # review. Produkcia od nasadenia prijíma iba čerstvé položky.
            if (
                not TEST_MODE
                and
                (
                    not notification.created_at
                    or notification.created_at.replace(
                        tzinfo=None
                    ) < ACTIONABLE_CATEGORIES_START_AT
                )
            ):
                continue


            message = (
                serialize_item(
                    notification,
                    item_type,
                    matched_children
                )
            )


            # Ignoruj položku bez textu aj bez použiteľných detailov.
            if (
                not message["text"]
                and
                not message["details"]
                and
                not message["additional_data"]
            ):

                continue


            account_messages.append(
                message
            )

            all_messages.append(
                message
            )

            new_event_ids.append(
                event_key
            )




        print(
            "Nových relevantných položiek:",
            len(
                account_messages
            )
        )


        account_results.append({

            "school":
                account[
                    "name"
                ],

            "school_key":
                account[
                    "key"
                ],

            "status":
                "ok",

            "new_message_count":
                len(
                    account_messages
                ),

            "new_item_count":
                len(
                    account_messages
                )
        })


    except Exception as error:

        run_errors.append(
            f'{account["name"]}: {repr(error)}'
        )

        print(
            "CHYBA:",
            repr(
                error
            )
        )

        if HTTP_DIAGNOSTICS:
            print()
            print(
                "TRACEBACK:"
            )
            traceback.print_exc()


        account_results.append({

            "school":
                account[
                    "name"
                ],

            "school_key":
                account[
                    "key"
                ],

            "status":
                "error",

            "error":
                repr(
                    error
                )
        })


# ============================================================
# CREATE OUTPUT
# ============================================================

now = datetime.now(
    ZoneInfo(
        TIMEZONE
    )
)


output = {

    "schema_version":
        SCHEMA_VERSION,

    "generated_at":
        now.isoformat(
            timespec="seconds"
        ),

    "run_slot":
        RUN_SLOT,

    "accounts":
        account_results,

    "children":
        all_children,

    "new_message_count":
        len(
            all_messages
        ),

    "new_item_count":
        len(
            all_messages
        ),

    "messages":
        all_messages,

    "items":
        all_messages
}


# ============================================================
# SAVE OUTPUT
# ============================================================

save_output(
    output
)


print()
print(
    "JSON uložený:",
    OUTPUT_FILE
)


# ============================================================
# SEND RAW EMAIL
# ============================================================

email_sent = False


if (
    len(
        all_messages
    ) > 0
    or RUN_SLOT in {
        "morning",
        "noon",
        "early",
        "main"
    }
):

    try:

        send_raw_email(
            output
        )

        email_sent = True

        print(
            "RAW email odoslaný."
        )


    except Exception as error:

        run_errors.append(
            f"RAW email: {repr(error)}"
        )

        print()
        print(
            "CHYBA PRI ODOSIELANÍ EMAILU:"
        )

        print(
            repr(
                error
            )
        )


else:

    print(
        "Žiadne nové správy."
    )

    print(
        "Email sa neposiela."
    )


# ============================================================
# UPDATE STATE
# ============================================================

#
# State aktualizujeme iba po úplne úspešnom produkčnom behu.
# Ak boli nové správy, RAW musí byť odoslaný skôr než state.
# State posielame aj pri nule správ, aby sa zachovala obnovená session.
#

if (
    not TEST_MODE
    and
    not run_errors
    and
    (
        not all_messages
        or
        email_sent
    )
):

    all_processed_ids = sorted(
        processed_ids.union(
            new_event_ids
        )
    )


    state[
        "processed_event_ids"
    ] = all_processed_ids

    state[
        "schema_version"
    ] = SCHEMA_VERSION

    state[
        "updated_at"
    ] = now.isoformat(
        timespec="seconds"
    )

    try:

        send_state_email(
            state
        )

        print(
            "Gmail state aktualizovaný."
        )

    except Exception as error:

        run_errors.append(
            f"STATE email: {repr(error)}"
        )

        print(
            "CHYBA PRI UKLADANÍ GMAIL STATE:"
        )

        print(
            repr(error)
        )


elif TEST_MODE:

    print(
        "TEST MODE – state sa nemení."
    )


elif run_errors:

    print(
        "State sa nemení, pretože beh nebol úplne úspešný."
    )


# ============================================================
# FINISH
# ============================================================

print()
print(
    "=" * 60
)

print(
    "EduPage sync hotový."
)

print(
    "=" * 60
)

print(
    "Celkom nových relevantných položiek:",
    len(
        all_messages
    )
)


if run_errors:

    print()
    print(
        "Beh skončil s chybami:"
    )

    for run_error in run_errors:
        print(
            "-",
            run_error
        )

    raise SystemExit(1)
