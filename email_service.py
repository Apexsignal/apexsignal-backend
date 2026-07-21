"""
Odesílání e-mailů (uvítací při registraci, obnova hesla) přes obyčejné
SMTP — appka tím nezávisí na konkrétní třetí straně (Gmail, SendGrid,
Mailgun, Resend...), stačí zadat SMTP údaje dané služby jako proměnné
prostředí:

    SMTP_HOST, SMTP_PORT (výchozí 587), SMTP_USER, SMTP_PASSWORD,
    SMTP_FROM_EMAIL (výchozí SMTP_USER), SMTP_FROM_NAME (výchozí "ApexSignal")

Appka bez nastaveného SMTP e-maily jen vypíše do logu, nezhroutí se kvůli
nim — registrace/reset hesla appce funguje i bez e-mailu, jen se
uživatel o tom nedozví hned.
"""
import os
import smtplib
from email.mime.text import MIMEText


def _smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    if not _smtp_configured():
        print(f"[email_service] SMTP není nastavené, e-mail se neposílá (adresát: {to_email}, předmět: {subject})")
        return False

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ.get("SMTP_FROM_EMAIL", user)
    from_name = os.environ.get("SMTP_FROM_NAME", "ApexSignal")

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(from_email, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email_service] Odeslání e-mailu selhalo ({to_email}): {e}")
        return False


def _wrap_html(inner: str) -> str:
    return f"""
    <div style="background:#0B0E14;padding:32px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      <div style="max-width:480px;margin:0 auto;background:#141925;border:1px solid #262E3F;border-radius:16px;padding:28px 24px;">
        <div style="color:#19E0C4;font-size:20px;font-weight:700;margin-bottom:20px;">ApexSignal</div>
        {inner}
      </div>
    </div>
    """


def send_welcome_email(to_email: str) -> bool:
    inner = """
    <p style="color:#E7EBF2;font-size:15px;line-height:1.6;">Vítej v ApexSignal!</p>
    <p style="color:#8A93A8;font-size:14px;line-height:1.6;">
      Tvůj účet je založený a připravený k použití. Appka ti bude generovat
      sázkové tikety na základě statistického modelu — krátký, střední i BOOST.
    </p>
    <p style="color:#8A93A8;font-size:13px;line-height:1.6;">
      Připomínka: appka je analytický nástroj, ne záruka výhry. Sázej zodpovědně, 18+.
    </p>
    """
    return send_email(to_email, "Vítej v ApexSignal", _wrap_html(inner))


def send_password_reset_email(to_email: str, reset_link: str) -> bool:
    inner = f"""
    <p style="color:#E7EBF2;font-size:15px;line-height:1.6;">Někdo (doufejme, že ty) požádal o obnovení hesla k účtu ApexSignal.</p>
    <p style="margin:24px 0;">
      <a href="{reset_link}" style="background:#19E0C4;color:#0B0E14;text-decoration:none;padding:12px 20px;border-radius:10px;font-weight:700;font-size:14px;display:inline-block;">Nastavit nové heslo</a>
    </p>
    <p style="color:#8A93A8;font-size:13px;line-height:1.6;">
      Odkaz je platný 1 hodinu. Pokud sis o obnovení hesla nežádal, tenhle e-mail jen ignoruj — tvůj účet zůstane beze změny.
    </p>
    """
    return send_email(to_email, "Obnovení hesla — ApexSignal", _wrap_html(inner))
