"""Send an approved email over SMTP (Gmail: smtp.gmail.com with an app password).

Env: SMTP_USER, SMTP_PASSWORD, optional SMTP_HOST (smtp.gmail.com), SMTP_PORT (587), SMTP_FROM.
"""
import os
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid


class MailerNotConfigured(Exception):
    pass


class SmtpMailer:
    def __init__(self):
        self.host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.sender = os.environ.get("SMTP_FROM", self.user)

    def send(self, to: str, subject: str, body: str, in_reply_to: str | None = None) -> str:
        """Returns the Message-ID, which the inbox sync uses to match replies."""
        if not (self.user and self.password):
            raise MailerNotConfigured("Email isn't set up: add SMTP_USER and SMTP_PASSWORD to .env")
        msg = EmailMessage()
        msg["From"], msg["To"] = self.sender, to
        msg["Subject"] = f"Re: {subject}" if in_reply_to and not subject.lower().startswith("re:") else subject
        msg["Message-ID"] = make_msgid(domain=self.sender.rsplit("@", 1)[-1] or None)
        if in_reply_to:
            msg["In-Reply-To"] = msg["References"] = in_reply_to
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.user, self.password)
            smtp.send_message(msg)
        return msg["Message-ID"]
