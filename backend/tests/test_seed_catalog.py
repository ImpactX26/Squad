"""The seed catalog keeps the demo's prices and coverage (§8.2). No database needed."""

import copy
from collections import defaultdict
from datetime import date
from decimal import Decimal

import pytest

from tests.seed_support import seed

DATA = seed.load_data()
SERVICES = {s["code"]: s for s in DATA["services"]}
MODELS = {m["model_number"]: m for m in DATA["models"]}

# The services that fit each product category: every demo-story device has a part for each (§8.2).
SERVICES_BY_CATEGORY = {
    "laptop": ["BATTERY_REPLACE", "CMOS_REPLACE", "DISPLAY_REPLACE", "KEYBOARD_REPLACE", "FAN_REPLACE",
               "SSD_REPLACE", "SSD_UPGRADE", "RAM_UPGRADE", "CHARGER_REPLACE"],
    "desktop": ["CMOS_REPLACE", "FAN_REPLACE", "SSD_REPLACE", "SSD_UPGRADE", "RAM_UPGRADE"],
    "headphones": ["EAR_CUSHION_REPLACE"],
}


def price(service_code: str, model_number: str) -> Decimal | None:
    """Labour plus the cheapest compatible part, by SKU on a tie (§5.5); None when no part fits."""
    service = SERVICES[service_code]
    labour = Decimal(service["labour_fee"])
    if service["part_type"] is None:
        return labour
    parts = [p for p in DATA["parts"] if p["part_type"] == service["part_type"] and model_number in p["fits"]]
    if not parts:
        return None
    cheapest = min(parts, key=lambda p: (Decimal(p["unit_price"]), p["sku"]))
    return labour + Decimal(cheapest["unit_price"])


def test_the_seed_data_builds():
    rows = seed.build_rows(DATA, "hash")
    counts = {table: len(table_rows) for table, table_rows in rows.items()}
    assert counts["staff_users"] == 9
    assert counts["customers"] == 4
    assert counts["products"] == 4
    assert counts["product_models"] == 25
    assert counts["service_catalog"] == 12
    assert counts["kb_playbooks"] >= 15
    assert counts["parts"] >= 45
    categories = [m["category"] for m in DATA["models"]]
    assert (categories.count("laptop"), categories.count("desktop"), categories.count("headphones")) == (12, 6, 7)


def test_riyas_battery_replacement_costs_6_90():
    assert price("BATTERY_REPLACE", "AX14") == Decimal("6.90")


def test_every_service_with_a_compatible_part_costs_1_to_20_rupees():
    for code, service in SERVICES.items():
        assert Decimal("1.00") <= Decimal(service["labour_fee"]) <= Decimal("2.00"), code
        for model_number in MODELS:
            total = price(code, model_number)
            if total is not None:
                assert Decimal("1.00") <= total <= Decimal("20.00"), (code, model_number, total)


def test_upi_test_is_one_rupee_with_no_part_and_no_visit():
    upi = SERVICES["UPI_TEST"]
    assert (upi["labour_fee"], upi["part_type"], upi["requires_visit"]) == ("1.00", None, False)


def test_every_demo_device_has_a_part_for_every_service_of_its_category():
    for customer in DATA["customers"]:
        for device in customer["devices"]:
            category = MODELS[device["model_number"]]["category"]
            for code in SERVICES_BY_CATEGORY[category]:
                assert price(code, device["model_number"]) is not None, (device["serial_number"], code)


def test_the_demo_customers_match_the_build_plan():
    demo = {c["demo_channel"]: (c["full_name"], c["devices"][0]["serial_number"]) for c in DATA["customers"]}
    assert demo == {
        "discord": ("Riya Sharma", "AX14-7F3K92"),
        "telegram": ("Aman Verma", "VX15-Q8M2D5"),
        "email": ("Sneha Kulkarni", "PA7-3KX9TB"),
        "web": ("Rahul Nair", "LB13-W4N7PC"),
    }


def test_riyas_aurora_14_is_out_of_warranty():
    riya = next(c for c in DATA["customers"] if c["full_name"] == "Riya Sharma")
    device = riya["devices"][0]
    until = seed.add_months(date.fromisoformat(device["purchase_date"]), MODELS["AX14"]["warranty_months"])
    assert until < date.today()


def test_each_city_has_one_technician_per_visit_skill():
    visit_skills = {s["required_skill"] for s in SERVICES.values() if s["requires_visit"]}
    by_city = defaultdict(list)
    for member in DATA["staff"]:
        if member["role"] == "technician":
            by_city[member["city"]].extend(member["skills"])
    assert set(by_city) == {"Bengaluru", "Mumbai", "Delhi"}
    for city, skills in by_city.items():
        assert sorted(skills) == sorted(visit_skills), city


def test_ravi_is_the_bengaluru_battery_technician():
    battery = [m["name"] for m in DATA["staff"]
               if m["role"] == "technician" and m["city"] == "Bengaluru" and "battery" in m["skills"]]
    assert battery == ["Ravi Kumar"]


def test_ids_are_the_same_on_every_build():
    first = seed.build_rows(DATA, "hash-one")
    second = seed.build_rows(DATA, "hash-two")
    for table in first:
        assert [row[0] for row in first[table]] == [row[0] for row in second[table]], table


@pytest.mark.parametrize(
    "breakage",
    [
        lambda d: d["parts"][0].update(part_type="laser"),
        lambda d: d["parts"][0].update(fits=["ZZ99"]),
        lambda d: d["parts"][0].update(unit_price="5.4"),
        lambda d: d["customers"][0]["devices"][0].update(serial_number="AX15-7F3K92"),
        lambda d: d["playbooks"][0].update(issue_type="battery_dead"),
        lambda d: d["staff"][3].update(skills=["juggling"]),
        lambda d: d["services"].append(dict(d["services"][0])),
    ],
)
def test_bad_data_is_refused(breakage):
    broken = copy.deepcopy(DATA)
    breakage(broken)
    with pytest.raises(seed.SeedDataError):
        seed.build_rows(broken, "hash")
