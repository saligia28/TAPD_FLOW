from __future__ import annotations

import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import List

from core.config import Config

from .models import MailJob, MailResult


def send_mail(job: MailJob, cfg: Config) -> MailResult:
    dry_run = cfg.mail_dry_run or not cfg.smtp_host
    if dry_run:
        _print_dry_run(job)
        return MailResult(contact=job.contact, sent=False, dry_run=True, message="dry-run")
    use_ssl = cfg.smtp_use_ssl
    use_tls = cfg.smtp_use_tls and not use_ssl
    try:
        if use_ssl:
            smtp = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port)
        else:
            smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        with smtp as client:
            if use_tls:
                client.starttls()
            if cfg.smtp_user and cfg.smtp_password:
                client.login(cfg.smtp_user, cfg.smtp_password)
            message = _build_message(job, cfg)
            client.send_message(message)
        return MailResult(contact=job.contact, sent=True, dry_run=False, message="sent")
    except Exception as exc:  # pragma: no cover - network failure
        return MailResult(contact=job.contact, sent=False, dry_run=False, message=str(exc))


def _print_dry_run(job: MailJob) -> None:
    print("[testflow][mail] dry-run ->", job.contact.email)
    for attachment in job.attachments:
        print(f"  attachment: {attachment}")


def _build_message(job: MailJob, cfg: Config) -> EmailMessage:
    msg = EmailMessage()
    sender = cfg.mail_sender or (cfg.smtp_user or "testflow@example.com")
    msg["From"] = sender
    msg["To"] = job.contact.email
    msg["Subject"] = job.subject
    msg.set_content(job.body)
    for attachment in job.attachments:
        path = Path(attachment)
        try:
            data = path.read_bytes()
        except OSError as exc:
            print(f"[testflow][mail] failed to read attachment {path}: {exc}")
            continue
        msg.add_attachment(
            data,
            maintype="application",
            subtype="octet-stream",
            filename=path.name,
        )
    return msg
