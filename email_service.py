"""
Odesílání e-mailů (uvítací při registraci, obnova hesla) přes Brevo HTTP
API (https://api.brevo.com) — appka běží na Renderu, který na free plánu
blokuje veškerý odchozí provoz na SMTP porty (25/465/587), takže obyčejné
SMTP tam nejde použít. Brevo API jede přes běžné HTTPS (port 443), to
blokované není.

Proměnné prostředí:
    BREVO_API_KEY, BREVO_FROM_EMAIL (musí být ověřená adresa v Brevo),
    BREVO_FROM_NAME (výchozí "ApexSignal"), BREVO_REPLY_TO (výchozí = odesílatel)

DŮLEŽITÉ k doručitelnosti: BREVO_FROM_EMAIL musí být na VLASTNÍ doméně
(tikety@apexsignal.cz), ne na svobodné schránce typu @seznam.cz nebo
@gmail.com. Seznam i Google mají v DMARC nastavené, že jejich jménem smí
posílat jen jejich vlastní servery — Brevo mezi ně nepatří, takže takový
e-mail projde rovnou do spamu, ať je jeho obsah jakýkoli. K vlastní doméně
musí být v DNS zapsané záznamy, které Brevo vydá při ověření domény.

Appka bez nastaveného API klíče e-maily jen vypíše do logu, nezhroutí se
kvůli nim — registrace/reset hesla appce funguje i bez e-mailu, jen se
uživatel o tom nedozví hned.
"""
import os
import re
from html import unescape

import requests

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def _brevo_configured() -> bool:
    return bool(os.environ.get("BREVO_API_KEY") and os.environ.get("BREVO_FROM_EMAIL"))


def _html_to_text(html: str) -> str:
    """
    Hrubý převod HTML na čitelný prostý text. Appka nechce kvůli tomuhle
    tahat další knihovnu — stačí odstranit značky a poskládat řádky.
    """
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6])>", "\n", text)
    # Odkaz appka přepíše na "text (adresa)", ať v textové verzi nezmizí.
    text = re.sub(r'(?is)<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for i, line in enumerate(lines) if line or (i and lines[i - 1])).strip()


def send_email(to_email: str, subject: str, html_body: str, text_body: str | None = None) -> bool:
    if not _brevo_configured():
        print(f"[email_service] Brevo API není nastavené, e-mail se neposílá (adresát: {to_email}, předmět: {subject})")
        return False

    api_key = os.environ["BREVO_API_KEY"]
    from_email = os.environ["BREVO_FROM_EMAIL"]
    from_name = os.environ.get("BREVO_FROM_NAME", "ApexSignal")
    reply_to = os.environ.get("BREVO_REPLY_TO", from_email)

    payload = {
        "sender": {"email": from_email, "name": from_name},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_body,
        # Textová verze appce chyběla úplně. E-mail bez ní je pro
        # spamové filtry (Seznam i Gmail) podezřelý sám o sobě —
        # legitimní odesílatelé posílají obě verze.
        "textContent": text_body or _html_to_text(html_body),
        # Odpověď na e-mail musí někam dorazit; adresa, na kterou nikdo
        # nečte, je další drobný mínus u filtrů.
        "replyTo": {"email": reply_to, "name": from_name},
    }

    try:
        resp = requests.post(
            BREVO_API_URL,
            headers={"api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
            timeout=15,
        )
        if resp.status_code >= 300:
            print(f"[email_service] Odeslání e-mailu selhalo ({to_email}): {resp.status_code} {resp.text}")
            return False
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
      sázkové tikety na základě statistického modelu — krátký i střední.
    </p>
    <p style="color:#8A93A8;font-size:13px;line-height:1.6;">
      Připomínka: appka je analytický nástroj, ne záruka výhry. Sázej zodpovědně, 18+.
    </p>
    """
    return send_email(to_email, "Vítej v ApexSignal", _wrap_html(inner))


def send_channel_welcome_email(to_email: str, telegram_deep_link: str) -> bool:
    """
    Appka tohle posílá HNED po úspěšném zaplacení kanálu (viz webhook
    v backend_api.py) — zákazník appce nezakládá účet, takže tenhle
    e-mail je JEDINÝ způsob, jak se k němu párovací odkaz na Telegram
    vůbec dostane.
    """
    inner = f"""
    <p style="color:#E7EBF2;font-size:15px;line-height:1.6;">Díky za platbu! Kanál je aktivní.</p>
    <p style="color:#8A93A8;font-size:14px;line-height:1.6;">
      Zbývá poslední krok — propojit Telegram, kam appka bude posílat tikety.
      Klikni na tlačítko níž (na telefonu, kde máš Telegram nainstalovaný):
    </p>
    <p style="margin:24px 0;">
      <a href="{telegram_deep_link}" style="background:#19E0C4;color:#0B0E14;text-decoration:none;padding:12px 20px;border-radius:10px;font-weight:700;font-size:14px;display:inline-block;">Propojit Telegram</a>
    </p>
    <p style="color:#8A93A8;font-size:13px;line-height:1.6;">
      Odkaz je platný hodinu a jde použít jen jednou. Pokud vyprší, napiš appce
      tenhle e-mail znovu (přes formulář na apexsignal.cz/transparentni-ucet)
      a pošle ti nový.
    </p>
    <p style="color:#8A93A8;font-size:13px;line-height:1.6;">
      Od zítřejšího rána ti bude appka denně posílat 1 tiket — nejdřív zkusí sestavit
      ten hodnotnější, střední, a jen když pro něj trh zrovna nenabízí dost kvalitních
      zápasů, pošle krátký místo něj. Sázku si vždycky klikáš sám, kde chceš.
    </p>
    <p style="color:#8A93A8;font-size:12px;line-height:1.6;">
      18+. Hazardní hraní může být návykové. Sázej jen to, co si můžeš dovolit prohrát.
    </p>
    """
    return send_email(to_email, "Kanál je aktivní — propoj si Telegram", _wrap_html(inner))


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
