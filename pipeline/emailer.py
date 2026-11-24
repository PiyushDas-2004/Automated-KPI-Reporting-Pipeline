"""Email the HTML summary over SMTP (Gmail: smtp.gmail.com:587 with STARTTLS).

Configuration comes ONLY from environment variables (GitHub Actions secrets):
    SMTP_USER   - the sending Gmail address
    SMTP_PASS   - a Gmail *app password* (not the account password)
    REPORT_TO   - comma-separated recipient list
Optional: SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587).

If any required variable is missing the email is skipped with a clear log
line; the pipeline does not fail. Credentials and addresses are never logged.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .logging_utils import log

logger = logging.getLogger("pipeline.email")

REQUIRED_VARS = ("SMTP_USER", "SMTP_PASS", "REPORT_TO")


def smtp_settings() -> dict | None:
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v, "").strip()]
    if missing:
        log(logger, logging.WARNING,
            "email skipped: SMTP settings not configured (set the SMTP_USER, SMTP_PASS and REPORT_TO secrets)",
            missing_variables=missing)
        return None
    return {
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ["SMTP_USER"].strip(),
        "password": os.environ["SMTP_PASS"],
        "to": [a.strip() for a in os.environ["REPORT_TO"].split(",") if a.strip()],
    }


def build_message(subject: str, html: str, text: str, sender: str, recipients: list[str],
                  images: list[tuple[str, Path]]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    html_part = msg.get_payload()[1]
    for cid, path in images:  # inline images referenced as <img src="cid:...">
        html_part.add_related(path.read_bytes(), maintype="image", subtype="png", cid=f"<{cid}>",
                              filename=path.name)
    return msg


def send_report(subject: str, html: str, text: str, images: list[tuple[str, Path]]) -> bool:
    settings = smtp_settings()
    if settings is None:
        return False
    msg = build_message(subject, html, text, settings["user"], settings["to"], images)
    with smtplib.SMTP(settings["host"], settings["port"], timeout=60) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(settings["user"], settings["password"])
        smtp.send_message(msg)
    log(logger, logging.INFO, "email sent", recipients=len(settings["to"]), host=settings["host"])
    return True
