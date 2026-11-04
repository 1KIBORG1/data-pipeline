import requests
import json
from typing import Any
from parsel import Selector


def extract_links(url):
    response = requests.get(url)
    sel = Selector(text=response.text, type="xml")
    links = sel.xpath('//*[local-name()="url"]/*[local-name()="loc"]/text()').getall()
    if links:
        return links
    else:
        raise 'Ошибка'


def extract_card_preview(html: str) -> dict[str, str | None]:
    sel = Selector(text=html)
    result = dict()
    result['title'] = sel.xpath('//h2/text()').get()
    result['url'] = sel.xpath('//a/@href').get()
    result['price'] = sel.xpath('//span[@class="price"]/text()').get()
    return result


def find_ad_script(html: str) -> str | None:
    sel = Selector(text=html)
    ad_script = sel.xpath(
        """//script[contains(text(), 'self.__next_f.push(') and contains(text(), '\\"advertisement\\":{\\"id\\"')]/text()""").get()
    return ad_script.strip() if ad_script else None


def extract_ad_dict(html: str) -> dict[str, Any] | None:
    text = find_ad_script(html)
    if text is None:
        return None

    try:
        payload = json.loads(text[19:-1])
        line = next(
            line for line in payload[1].splitlines()
            if '{"advertisement":{"id"' in line
        )
        data = json.loads(line[line.index(":") + 1:])
        return data[3]["advertisement"]

    except (json.JSONDecodeError, IndexError, ValueError, TypeError, StopIteration):
        return None


def build_attrs_map(attrs_list: list[dict[str, Any]] | None) -> dict[str, str]:
    result = {}

    for item in attrs_list or []:
        label = item.get("label")
        value = item.get("value")

        if not label or value is None or value in ("", "$undefined"):
            continue

        result[str(label).strip()] = str(value).strip()

    return result


def parse_apartment_html(html: str, url: str = "") -> dict[str, Any] | None:
    ad = extract_ad_dict(html)
    if not ad or not ad.get("id"):
        return None
    loc = ad.get("location") or {}
    params = ad.get("parameters") or {}
    category = params.get("category") or {}
    price_obj = params.get("price") or {}
    attrs = build_attrs_map(params.get("attributes"))

    if category.get("mainValue") != "APARTMENTS":
        return None
    if loc.get("country") and loc.get("country") != "Slovensko":
        return None
    vybavenie = attrs.get("Vybavenie", "")
    has_elevator = bool(params.get("hasElevator")) or ("Výťah" in vybavenie)
    balcony_cnt = attrs.get("Počet balkónov") or attrs.get("Počet loggií")
    has_balcony = (
            any(w in vybavenie for w in ("Balkón", "Loggia", "Terasa"))
            or (balcony_cnt is not None and balcony_cnt not in ("0", "Nie", "nie"))
    )
    floor_raw = params.get("floor")
    if floor_raw is None:
        floor_raw = attrs.get("Poschodie") or attrs.get("Umiestnenie")
    return {
        "id": ad["id"],
        "url": url or (
            f"https://www.nehnutelnosti.sk/detail/"
            f"{ad['id']}/{ad.get('sefName', '')}"
        ),
        "title": ad.get("title"),
        "transaction_type": params.get("transaction"),
        "category_sub": category.get("subValue"),
        "rooms_count": attrs.get("Počet izieb / miestností"),
        "price_eur": price_obj.get("priceNum"),
        "price_per_sqm_eur": price_obj.get("unitPrice"),
        "area_sqm": params.get("area"),
        "floor": floor_raw,
        "total_floors": params.get("numberOfFloors"),
        "property_condition": params.get("realEstateState"),
        "has_elevator": has_elevator,
        "has_balcony": has_balcony,
        "city": loc.get("city"),
        "district": loc.get("district"),
        "updated_at": ad.get("updatedAt"),
    }
