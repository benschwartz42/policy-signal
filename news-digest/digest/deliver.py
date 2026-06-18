"""Email delivery. Resend HTTP API is primary; SMTP is the fallback.

Transport is chosen at runtime from the environment:
  - RESEND_API_KEY set        -> Resend
  - else SMTP_HOST set         -> SMTP (e.g. Google Workspace)
  - else                       -> raise (caller decides; --dry-run never delivers)

Both paths share one signature so a third transport is a drop-in addition.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from .config import Delivery

log = logging.getLogger("digest.deliver")


class DeliveryError(RuntimeError):
    pass


def _send_resend(delivery: Delivery, subject: str, html_body: str, text_body: str, env: dict) -> None:
    key = env["RESEND_API_KEY"]
    if not delivery.sender:
        raise DeliveryError("delivery.sender is required for Resend")
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "from": delivery.sender,
            "to": delivery.recipients,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        raise DeliveryError(f"Resend API error {resp.status_code}: {resp.text[:300]}")
    log.info("Resend delivered to %d recipient(s)", len(delivery.recipients))


def _send_smtp(delivery: Delivery, subject: str, html_body: str, text_body: str, env: dict) -> None:
    host = env["SMTP_HOST"]
    port = int(env.get("SMTP_PORT", 587))
    user = env.get("SMTP_USER")
    password = env.get("SMTP_PASSWORD")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = delivery.sender or (user or "")
    msg["To"] = ", ".join(delivery.recipients)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        if port in (587,):
            server.starttls()
            server.ehlo()
        if user and password:
            server.login(user, password)
        server.sendmail(msg["From"], delivery.recipients, msg.as_string())
    log.info("SMTP delivered to %d recipient(s)", len(delivery.recipients))


def deliver(
    delivery: Delivery,
    subject: str,
    html_body: str,
    text_body: str,
    env: dict | None = None,
) -> str:
    """Send the digest. Returns the transport name used. Raises DeliveryError."""
    env = env or dict(os.environ)
    if not delivery.recipients:
        raise DeliveryError("no recipients configured")

    if env.get("RESEND_API_KEY"):
        _send_resend(delivery, subject, html_body, text_body, env)
        return "resend"
    if env.get("SMTP_HOST"):
        _send_smtp(delivery, subject, html_body, text_body, env)
        return "smtp"
    raise DeliveryError("no transport configured (set RESEND_API_KEY or SMTP_HOST)")
