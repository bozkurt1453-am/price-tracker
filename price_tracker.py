import csv
import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests


SERPAPI_URL = "https://serpapi.com/search.json"

PRODUCTS_FILE = Path("products.json")
STATE_FILE = Path("data/state.json")
HISTORY_FILE = Path("data/history.csv")


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_text(text):
    return (text or "").lower().replace("ı", "i")


def parse_price(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value)
    text = text.replace("₺", "").replace("TL", "").replace(" ", "")
    text = re.sub(r"[^\d,.]", "", text)

    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return None


def is_relevant(item, product):
    title = normalize_text(item.get("title", ""))
    source = normalize_text(item.get("source", ""))
    blob = f"{title} {source}"

    include_terms = [normalize_text(x) for x in product.get("include_terms", [])]
    exclude_terms = [normalize_text(x) for x in product.get("exclude_terms", [])]

    if include_terms and not all(term in blob for term in include_terms):
        return False

    if any(term in blob for term in exclude_terms):
        return False

    return True


def search_google_shopping(product):
    api_key = os.environ["SERPAPI_API_KEY"]

    params = {
        "engine": "google_shopping",
        "q": product["query"],
        "api_key": api_key,
        "gl": "tr",
        "hl": "tr",
        "google_domain": "google.com.tr",
        "location": "Turkey"
    }

    response = requests.get(SERPAPI_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    raw_results = data.get("shopping_results", [])
    results = []

    for item in raw_results:
        if not is_relevant(item, product):
            continue

        price = parse_price(item.get("extracted_price") or item.get("price"))

        if price is None:
            continue

        results.append({
            "title": item.get("title"),
            "source": item.get("source"),
            "price": price,
            "price_text": item.get("price"),
            "link": item.get("link") or item.get("product_link")
        })

    return sorted(results, key=lambda x: x["price"])


def send_email(subject, body):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    email_from = os.environ.get("EMAIL_FROM", smtp_user)

    email_to_raw = os.environ["EMAIL_TO"]
    email_to_list = [email.strip() for email in email_to_raw.split(",") if email.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = ", ".join(email_to_list)
    msg.set_content(body)

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def append_history(product_id, results):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_exists = HISTORY_FILE.exists()

    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow(["checked_at", "product_id", "rank", "source", "title", "price", "link"])

        checked_at = datetime.now(timezone.utc).isoformat()

        for rank, item in enumerate(results[:10], start=1):
            writer.writerow([
                checked_at,
                product_id,
                rank,
                item["source"],
                item["title"],
                item["price"],
                item["link"]
            ])


def build_email_body(product, results, reason):
    lines = []
    lines.append(f"Ürün: {product['query']}")
    lines.append(f"Sebep: {reason}")
    lines.append("")
    lines.append("En ucuz sonuçlar:")
    lines.append("")

    for i, item in enumerate(results[:5], start=1):
        lines.append(f"{i}. {item['price']:,.0f} TL - {item['source']}")
        lines.append(f"   {item['title']}")

        if item["link"]:
            lines.append(f"   {item['link']}")

        lines.append("")

    return "\n".join(lines)


def main():
    products = load_json(PRODUCTS_FILE, [])
    state = load_json(STATE_FILE, {})

    for product in products:
        product_id = product["id"]
        target_price = float(product.get("target_price", 0))

        results = search_google_shopping(product)

        if not results:
            print(f"Sonuç bulunamadı: {product_id}")
            continue

        append_history(product_id, results)

        best = results[0]
        previous = state.get(product_id, {})
        previous_best = previous.get("best_price")
        last_alert_price = previous.get("last_alert_price")

        should_alert = False
        reason = ""

        if target_price and best["price"] <= target_price:
            if last_alert_price is None or best["price"] < float(last_alert_price):
                should_alert = True
                reason = f"Hedef fiyatın altına indi: {best['price']:,.0f} TL <= {target_price:,.0f} TL"

        elif previous_best is not None and best["price"] < float(previous_best):
            should_alert = True
            reason = f"Fiyat düştü: {previous_best:,.0f} TL -> {best['price']:,.0f} TL"

        if should_alert:
            subject = f"Fiyat alarmı: {product['query']} - {best['price']:,.0f} TL"
            body = build_email_body(product, results, reason)
            send_email(subject, body)
            state.setdefault(product_id, {})
            state[product_id]["last_alert_price"] = best["price"]

        state.setdefault(product_id, {})
        state[product_id]["best_price"] = best["price"]
        state[product_id]["best_source"] = best["source"]
        state[product_id]["best_title"] = best["title"]
        state[product_id]["best_link"] = best["link"]
        state[product_id]["last_checked_at"] = datetime.now(timezone.utc).isoformat()

        print(f"{product_id}: en ucuz fiyat = {best['price']} TL, satıcı = {best['source']}")

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
