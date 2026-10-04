"""
ASTRA - Email Notification / Mailer
===================================

Responsibilities
----------------
    - Send ASTRA advisory email notifications.
    - Generate the end-of-day financial summary report.
    - Keep SMTP credentials and infrastructure configuration in .env.

Configuration Architecture
---------------------------
Critical SMTP configuration remains in:

    .env

    SMTP_SERVER
    SMTP_PORT
    SMTP_SENDER_EMAIL
    SMTP_PASSWORD
    SMTP_RECEIVER_EMAIL

Runtime mailer scheduling/configuration such as the report mode belongs
to settings.json and is consumed by the orchestration layer (main.py).

Security
--------
SMTP credentials are never read from settings.json and must never be
exposed through Telegram /config or /set.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from logzero import logger


# ============================================================================
# ENVIRONMENT
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent

# Explicitly load the project-root .env rather than depending on the current
# working directory from which ASTRA happens to be launched.
load_dotenv(
    BASE_DIR / ".env",
    override=True,
)


# ============================================================================
# SMTP CONFIGURATION
# ============================================================================

def _get_smtp_config() -> tuple[
    str,
    int,
    str,
    str,
    str,
]:
    """
    Load SMTP configuration from the project-root .env.

    Returns
    -------
    tuple
        (
            server_host,
            port,
            sender_email,
            password,
            receiver_email,
        )

    Notes
    -----
    SMTP credentials are intentionally kept in .env because they are
    sensitive infrastructure configuration and must not be exposed through
    settings.json or Telegram runtime configuration.
    """

    server_host = os.getenv(
        "SMTP_SERVER",
        "smtp.gmail.com",
    ).strip()

    raw_port = os.getenv(
        "SMTP_PORT",
        "587",
    ).strip()

    try:
        port = int(raw_port)
    except (
        TypeError,
        ValueError,
    ):
        logger.warning(
            "Invalid SMTP_PORT=%r; falling back to 587.",
            raw_port,
        )
        port = 587

    sender_email = os.getenv(
        "SMTP_SENDER_EMAIL",
        "",
    ).strip()

    password = os.getenv(
        "SMTP_PASSWORD",
        "",
    )

    receiver_email = os.getenv(
        "SMTP_RECEIVER_EMAIL",
        "",
    ).strip()

    return (
        server_host,
        port,
        sender_email,
        password,
        receiver_email,
    )


# ============================================================================
# EMAIL ALERT
# ============================================================================

def send_email_alert(
    subject: str,
    body: str,
) -> bool:
    """
    Dispatch an ASTRA email notification.

    SMTP configuration is loaded from .env for every invocation so that
    environment-level configuration remains authoritative.

    Parameters
    ----------
    subject:
        Email subject without the ASTRA prefix.

    body:
        Plain-text email body.

    Returns
    -------
    bool
        True when the message was successfully dispatched.
        False when configuration is missing or SMTP delivery fails.
    """

    (
        server_host,
        port,
        sender_email,
        password,
        receiver_email,
    ) = _get_smtp_config()

    if not sender_email or not password or not receiver_email:
        logger.error(
            "SMTP credentials/configuration are incomplete in .env."
        )
        return False

    msg = MIMEMultipart()

    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg["Subject"] = (
        f"[A.S.T.R.A. Advisory] {subject}"
    )

    msg.attach(
        MIMEText(
            body,
            "plain",
        )
    )

    server = None

    try:
        server = smtplib.SMTP(
            server_host,
            port,
        )

        server.starttls()

        server.login(
            sender_email,
            password,
        )

        server.send_message(msg)

        logger.info(
            "Email alert sent successfully: %s",
            subject,
        )

        return True

    except Exception as exc:
        logger.exception(
            "Failed to send email alert: %s",
            exc,
        )

        return False

    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                logger.debug(
                    "SMTP connection cleanup failed.",
                    exc_info=True,
                )


# ============================================================================
# END-OF-DAY REPORT
# ============================================================================

def send_eod_email_report(
    start_cash: float,
    end_cash: float,
    transactions: list,
) -> bool:
    """
    Generate and dispatch the ASTRA end-of-day financial summary.

    Parameters
    ----------
    start_cash:
        Portfolio/cash value at the beginning of the reporting period.

    end_cash:
        Portfolio/cash value at the end of the reporting period.

    transactions:
        List of transaction dictionaries.

    Returns
    -------
    bool
        True if the report was successfully dispatched.
    """

    pnl = end_cash - start_cash

    pnl_pct = (
        (pnl / start_cash) * 100
        if start_cash > 0
        else 0.0
    )

    status = (
        "PROFIT"
        if pnl >= 0
        else "LOSS"
    )

    lines = [
        "==========================================",
        "       ASTRA DAILY EOD SUMMARY REPORT     ",
        "==========================================",
        f"Start Balance  : ₹{start_cash:,.2f}",
        f"End Balance    : ₹{end_cash:,.2f}",
        (
            f"Net P/L        : ₹{pnl:,.2f} "
            f"({pnl_pct:+.2f}%) [{status}]"
        ),
        "==========================================",
        "",
        "TRANSACTIONS EXECUTED TODAY:",
    ]

    if not transactions:
        lines.append(
            "• No trades or transactions executed today."
        )

    else:
        for tx in transactions:
            lines.append(
                (
                    f"• {tx.get('time', 'N/A')} | "
                    f"{tx.get('action', '')} "
                    f"{tx.get('ticker', '')} | "
                    f"Qty: {tx.get('qty', 0)} @ "
                    f"₹{tx.get('price', 0.0)} | "
                    f"Total: ₹{tx.get('total', 0.0)}"
                )
            )

    body = "\n".join(lines)

    return send_email_alert(
        subject=(
            "EOD Performance Summary - "
            f"Net PnL: {pnl_pct:+.2f}%"
        ),
        body=body,
    )


# ============================================================================
# PUBLIC API
# ============================================================================

__all__ = [
    "send_email_alert",
    "send_eod_email_report",
]
