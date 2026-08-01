"""
Vygeneruje appčinu denní "proof kartu" (JPG) pro organický Instagram post —
stáhne živá data z veřejného /public/transparency a vyrenderuje je přes
`social_content.render_proof_card`. Skutečná render logika je ve sdíleném
modulu `social_content.py` (stejný vzor jako `telegram_ticket.py` vs.
`ticket_telegram.py`).

Použití:
    python3 scripts/render_proof_card.py
    python3 scripts/render_proof_card.py --base-url https://apexsignal-backend.onrender.com --out dnes.jpg
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from social_content import fetch_public_stats, render_proof_card

DEFAULT_BASE_URL = "https://apexsignal-backend.onrender.com"


def main():
    parser = argparse.ArgumentParser(description="Vyrenderuje proof kartu z živých appčiných dat.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Backend appky (default: produkce)")
    parser.add_argument("--out", default="proof_card.jpg", help="Kam uložit výsledný JPG")
    args = parser.parse_args()

    data = fetch_public_stats(args.base_url)
    img = render_proof_card(data)
    img.save(args.out, "JPEG", quality=92)
    print(f"Uloženo: {args.out}  (zdroj: {args.base_url}/public/transparency)")


if __name__ == "__main__":
    main()
