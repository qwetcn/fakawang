"""SMTP 发邮件(仅标准库)。返回 (True, "") 或 (False, 错误原因),从不抛异常。"""

import smtplib
import ssl
from email.message import EmailMessage


def send_mail(host, port, security, username, password, sender, to,
              subject, text, html=None, timeout=15):
    try:
        msg = EmailMessage()
        msg["From"] = sender or username
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        try:
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if username:
                server.login(username, password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True, ""
    except Exception as e:
        return False, str(e) or e.__class__.__name__
