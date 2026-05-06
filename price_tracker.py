import csv
import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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
    text = text.replace("₺", "").replace("TL", "").replace("TRY", "").replace(" ", "")
    text = re.sub(r"[^\d,.]", "", text)

    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return None


def is_relevant_text(title, source, product):
    title = normalize_text(title)
    source = normalize_text(source)
    blob = f"{title} {source}"

    include_terms = [normalize_text(x) for x in product.get("include_terms", [])]
    exclude_terms = [normalize_text(x) for x in product.get("exclude_terms", [])]

    if include_terms and not all(term in blob for term in include_terms):
        return False

    if any(term in blob for term in exclude_terms):
        return False

    return True


def add_query_params(url, params):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    for key, value in params.items():
        query[key] = [value]

    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def search_google_shopping(product):
    api_key = os.environ["SERPAPI_API_KEY"]

    params = {
        "engine": "google_shopping",
        "q": product["query"],
        "api_key": api_key,
        "gl": "tr",
        "hl": "tr",
        "google_domain": "google.com.tr",
        "location": "Turkey",
        "sort_by": "1"
    }

    response = requests.get(SERPAPI_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    raw_results = data.get("shopping_results", [])
    shopping_results = []

    for item in raw_results:
        title = item.get("title", "")
        source = item.get("source", "")

        if not is_relevant_text(title, source, product):
            continue

        price = parse_price(item.get("extracted_price") or item.get("price"))

        if price is None:
            continue

        shopping_results.append({
            "title": title,
            "source": source,
            "price": price,
            "price_text": item.get("price"),
            "link": item.get("link") or item.get("product_link"),
            "serpapi_immersive_product_api": item.get("serpapi_immersive_product_api"),
            "multiple_sources": item.get("multiple_sources")
        })

    return sorted(shopping_results, key=lambda x: x["price"])


def get_store_offers_from_immersive_product(serpapi_immersive_url):
    if not serpapi_immersive_url:
        return []

    api_key = os.environ["SERPAPI_API_KEY"]

    url = add_query_params(
        serpapi_immersive_url,
        {
            "api_key": api_key,
            "more_stores": "true"
        }
    )

    response = requests.get(url, timeout=30)
    response.raise_for_status()
    data = response.json()

    stores = data.get("product_results", {}).get("stores", [])
    offers = []

    for store in stores:
        price = parse_price(store.get("extracted_price") or store.get("price"))

        if price is None:
            continue

        offers.append({
            "title": store.get("title") or data.get("product_results", {}).get("title", ""),
            "source": store.get("name", "Bilinmeyen mağaza"),
            "price": price,
            "price_text": store.get("price"),
            "shipping": store.get("shipping"),
            "total": store.get("total"),
            "link": store.get("link"),
            "rating": store.get("rating"),
            "reviews": store.get("reviews"),
            "tag": store.get("tag")
        })

    return sorted(offers, key=lambda x: x["price"])


def get_best_offers(product):
    shopping_results = search_google_shopping(product)

    if not shopping_results:
        return []

    # İlk olarak Google'ın ürün detayındaki mağaza tekliflerini almaya çalışıyoruz.
    # Böylece Amazon, Beymen, MediaMarkt vb. ayrı ayrı listelenebiliyor.
    for result in shopping_results[:3]:
        offers = get_store_offers_from_immersive_product(
            result.get("serpapi_immersive_product_api")
        )

        relevant_offers = []
        for offer in offers:
            if is_relevant_text(offer.get("title", ""), offer.get("source", ""), product):
                relevant_offers.append(offer)

        if len(relevant_offers) >= 2:
            return sorted(relevant_offers, key=lambda x: x["price"])

    # Eğer ürün detay mağazaları gelmezse, normal Google Shopping sonuçlarına düşüyoruz.
    fallback = []

    for item in shopping_results:
        fallback.append({
            "title": item["title"],
            "source": item["source"],
            "price": item["price"],
            "price_text": item.get("price_text"),
            "shipping": None,
            "total": None,
            "link": item.get("link"),
            "rating": None,
            "reviews": None,
            "tag": None
        })

    return sorted(fallback, key=lambda x: x["price"])


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


def append_history(product_id, offers):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_exists = HISTORY_FILE.exists()

    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "checked_at",
                "product_id",
                "rank",
                "source",
                "title",
                "price",
                "shipping",
                "total",
                "link"
            ])

        checked_at = datetime.now(timezone.utc).isoformat()

        for rank, item in enumerate(offers[:10], start=1):
            writer.writerow([
                checked_at,
                product_id,
                rank,
                item.get("source"),
                item.get("title"),
                item.get("price"),
                item.get("shipping"),
                item.get("total"),
                item.get("link")
            ])


def build_email_body(product, top_5, target_price, reason):
    lines = []

    lines.append(f"Ürün: {product['query']}")
    lines.append(f"Hedef fiyat: {target_price:,.0f} TL")
    lines.append(f"Sebep: {reason}")
    lines.append("")
    lines.append("En ucuz 5 sonuç:")
    lines.append("")

    for i, item in enumerate(top_5, start=1):
        marker = " ✅ HEDEF ALTI" if item["price"] <= target_price else ""

        lines.append(f"{i}. {item['price']:,.0f} TL - {item['source']}{marker}")
        lines.append(f"   Ürün: {item['title']}")

        if item.get("shipping"):
            lines.append(f"   Kargo: {item['shipping']}")

        if item.get("total"):
            lines.append(f"   Toplam: {item['total']}")

        if item.get("rating"):
            reviews = item.get("reviews")
            if reviews:
                lines.append(f"   Puan: {item['rating']} / Yorum: {reviews}")
            else:
                lines.append(f"   Puan: {item['rating']}")

        if item.get("link"):
            lines.append(f"   Link: {item['link']}")
        else:
            lines.append("   Link: Bulunamadı")

        lines.append("")

    return "\n".join(lines)


def main():
    products = load_json(PRODUCTS_FILE, [])
    state = load_json(STATE_FILE, {})

    for product in products:
        product_id = product["id"]
        target_price = float(product.get("target_price", 0))

        offers = get_best_offers(product)

        if not offers:
            print(f"Sonuç bulunamadı: {product_id}")
            continue

        append_history(product_id, offers)

        top_5 = offers[:5]
        best = top_5[0]

        previous = state.get(product_id, {})
        previous_best = previous.get("best_price")
        last_alert_price = previous.get("last_alert_price")

        has_target_price_in_top_5 = any(item["price"] <= target_price for item in top_5)

        should_alert = False
        reason = ""

        # Yeni kural:
        # Mail sadece ilk 5 sonuç içinde en az bir ürün hedef fiyatın altındaysa gitsin.
        if has_target_price_in_top_5:
            cheapest_under_target = min(
                [item for item in top_5 if item["price"] <= target_price],
                key=lambda x: x["price"]
            )

            if last_alert_price is None or cheapest_under_target["price"] < float(last_alert_price):
                should_alert = True
                reason = (
                    f"İlk 5 sonuç içinde hedef fiyat altı ürün var: "
                    f"{cheapest_under_target['price']:,.0f} TL <= {target_price:,.0f} TL"
                )

        # Ek kural:
        # Hedef fiyat altına inmemişse mail atma.
        # Yani sadece fiyat düştü diye 18.000 TL üstündeyken mail gitmeyecek.

        if should_alert:
            subject = f"Fiyat alarmı: {product['query']} - {best['price']:,.0f} TL"
            body = build_email_body(product, top_5, target_price, reason)
            send_email(subject, body)

            state.setdefault(product_id, {})
            state[product_id]["last_alert_price"] = min(
                item["price"] for item in top_5 if item["price"] <= target_price
            )

        state.setdefault(product_id, {})
        state[product_id]["best_price"] = best["price"]
        state[product_id]["best_source"] = best["source"]
        state[product_id]["best_title"] = best["title"]
        state[product_id]["best_link"] = best["link"]
        state[product_id]["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        state[product_id]["top_5"] = top_5

        print(f"{product_id}: en ucuz fiyat = {best['price']} TL, satıcı = {best['source']}")

        if has_target_price_in_top_5:
            print("İlk 5 içinde hedef fiyat altı ürün var.")
        else:
            print("İlk 5 içinde hedef fiyat altı ürün yok, mail atılmadı.")

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
