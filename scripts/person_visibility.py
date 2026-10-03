"""Non-destructive read eligibility, matching the ingestion phone-number rule.

Do not use phone_count or a type label as proof that a number exists. Historical
rows may have a number only in the normalized child table. This predicate works
with MySQL 5.7 as well as MySQL 8/TiDB, without REGEXP_REPLACE.
"""

import re


WIRELESS_FIELDS = ("wireless_phone_1", "wireless_phone_2", "wireless_phone_3")
PHONE_SEPARATORS_PATTERN = r"[+() .-]*"
PHONE_DIGITS_PATTERN = (
    rf"^{PHONE_SEPARATORS_PATTERN}(1{PHONE_SEPARATORS_PATTERN})?"
    rf"[2-9]{PHONE_SEPARATORS_PATTERN}[0-9]{PHONE_SEPARATORS_PATTERN}"
    rf"[0-9]{PHONE_SEPARATORS_PATTERN}[2-9]{PHONE_SEPARATORS_PATTERN}"
    rf"([0-9]{PHONE_SEPARATORS_PATTERN}){{6}}$"
)
REPEATED_PHONE_PATTERN = (
    f"^{PHONE_SEPARATORS_PATTERN}(1{PHONE_SEPARATORS_PATTERN})?("
    + "|".join(f"({digit}{PHONE_SEPARATORS_PATTERN}){{10}}" for digit in range(2, 10)) + ")$"
)


def usable_phone_sql(column: str) -> str:
    """Only trusted SQL identifiers may be interpolated; values stay parameters."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", column):
        raise ValueError("invalid phone column identifier")
    return (
        f"(COALESCE({column}, '') REGEXP '{PHONE_DIGITS_PATTERN}' "
        f"AND COALESCE({column}, '') NOT REGEXP '{REPEATED_PHONE_PATTERN}')"
    )


def eligible_phone_type_sql(column: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", column):
        raise ValueError("invalid phone type column identifier")
    return f"LOWER(TRIM(COALESCE({column}, ''))) IN ('wireless', 'landline', 'landline/services')"


def normalized_phone_sql(column: str) -> str:
    """Canonical ten digits for a validated US number, including optional country code."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", column):
        raise ValueError("invalid phone column identifier")
    digits = f"COALESCE({column}, '')"
    for separator in ("' '", "'-'", "'.'", "'('", "')'", "'+'"):
        digits = f"REPLACE({digits}, {separator}, '')"
    return (
        f"(CASE WHEN LENGTH({digits}) = 11 AND SUBSTR({digits}, 1, 1) = '1' "
        f"THEN SUBSTR({digits}, 2) ELSE {digits} END)"
    )


def qualified_phone_count_sql(alias: str = "p", *, legacy: bool = False) -> str:
    """Count distinct qualified contacts, including person columns and child rows.

    This is a read-side projection only: historical counts and rows are untouched.
    Qualified child rows with a different display format for the same number
    contribute once, as do duplicates across person columns and child rows.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid person table alias")
    child_valid = (
        f"ph.person_id = {alias}.person_id AND {usable_phone_sql('ph.phone_number')} "
        f"AND {eligible_phone_type_sql('ph.line_type')}"
    )
    child_count = (
        f"(SELECT COUNT(DISTINCT {normalized_phone_sql('ph.phone_number')}) "
        f"FROM phone_numbers ph WHERE {child_valid})"
    )
    if legacy:
        return child_count

    own_fields = ["primary_phone", *WIRELESS_FIELDS]
    own_valid = [
        f"({usable_phone_sql(f'{alias}.{field}')}"
        + (f" AND {eligible_phone_type_sql(f'{alias}.primary_phone_type')}" if field == "primary_phone" else "")
        + ")"
        for field in own_fields
    ]
    own_normalized = [normalized_phone_sql(f"{alias}.{field}") for field in own_fields]
    terms = [child_count]
    for index, normalized in enumerate(own_normalized):
        # Earlier person fields win duplicate ties; a matching qualified child
        # is already included in child_count and must not be counted again.
        unique_before = [
            f"NOT ({own_valid[previous]} AND {normalized} = {own_normalized[previous]})"
            for previous in range(index)
        ]
        unique_child = (
            f"NOT EXISTS (SELECT 1 FROM phone_numbers ph WHERE {child_valid} "
            f"AND {normalized_phone_sql('ph.phone_number')} = {normalized})"
        )
        conditions = " AND ".join([own_valid[index], *unique_before, unique_child])
        terms.append(f"(CASE WHEN {conditions} THEN 1 ELSE 0 END)")
    return "(" + " + ".join(terms) + ")"


def person_has_phone_sql(alias: str = "p", *, legacy: bool = False) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid person table alias")
    conditions = []
    if not legacy:
        conditions.append(
            f"({usable_phone_sql(f'{alias}.primary_phone')} "
            f"AND {eligible_phone_type_sql(f'{alias}.primary_phone_type')})"
        )
        conditions.extend(usable_phone_sql(f"{alias}.{field}") for field in WIRELESS_FIELDS)
    conditions.append(
        "EXISTS (SELECT 1 FROM phone_numbers visible_phone "
        f"WHERE visible_phone.person_id = {alias}.person_id "
        f"AND {usable_phone_sql('visible_phone.phone_number')} "
        f"AND {eligible_phone_type_sql('visible_phone.line_type')})"
    )
    return "(" + " OR ".join(conditions) + ")"


def visible_person_count_sql() -> str:
    return f"SELECT COUNT(*) AS n FROM persons p WHERE {person_has_phone_sql()}"


def person_has_wireless_sql(alias: str = "p") -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid person table alias")
    conditions = [
        f"({usable_phone_sql(f'{alias}.primary_phone')} AND LOWER(TRIM(COALESCE({alias}.primary_phone_type, ''))) = 'wireless')"
    ]
    conditions.extend(usable_phone_sql(f"{alias}.{name}") for name in WIRELESS_FIELDS)
    conditions.append(
        "EXISTS (SELECT 1 FROM phone_numbers visible_phone "
        f"WHERE visible_phone.person_id = {alias}.person_id "
        f"AND {usable_phone_sql('visible_phone.phone_number')} "
        "AND LOWER(TRIM(COALESCE(visible_phone.line_type, ''))) = 'wireless')"
    )
    return "(" + " OR ".join(conditions) + ")"


def person_has_phone_type_sql(phone_type: str, alias: str = "p") -> str:
    """Match a usable visible contact, including child-only historical records.

    The UI's landline category includes Landline/Services; callers may still
    request that subtype explicitly. No request value is interpolated into SQL.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid person table alias")
    kind = str(phone_type).strip().lower()
    if kind == "wireless":
        return person_has_wireless_sql(alias)
    allowed = {
        "landline": "('landline', 'landline/services')",
        "landline/services": "('landline/services')",
    }
    if kind not in allowed:
        raise ValueError("电话类型筛选仅支持 Wireless、Landline 或 Landline/Services")
    kinds_sql = allowed[kind]
    return (
        f"(({usable_phone_sql(f'{alias}.primary_phone')} "
        f"AND LOWER(TRIM(COALESCE({alias}.primary_phone_type, ''))) IN {kinds_sql}) "
        "OR EXISTS (SELECT 1 FROM phone_numbers visible_phone "
        f"WHERE visible_phone.person_id = {alias}.person_id "
        f"AND {usable_phone_sql('visible_phone.phone_number')} "
        f"AND LOWER(TRIM(COALESCE(visible_phone.line_type, ''))) IN {kinds_sql}))"
    )


def visible_phone_fields_sql(alias: str = "p") -> dict:
    """Project only qualified contact fields; never expose legacy all_phones text."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid person table alias")
    primary_ok = f"{usable_phone_sql(f'{alias}.primary_phone')} AND {eligible_phone_type_sql(f'{alias}.primary_phone_type')}"
    child_ok = (
        f"visible_phone.person_id = {alias}.person_id "
        f"AND {usable_phone_sql('visible_phone.phone_number')} "
        f"AND {eligible_phone_type_sql('visible_phone.line_type')}"
    )
    numbers = [f"CASE WHEN {primary_ok} THEN {alias}.primary_phone END"]
    types = [f"CASE WHEN {primary_ok} THEN {alias}.primary_phone_type END"]
    descriptions = [
        f"CASE WHEN {primary_ok} THEN CONCAT({alias}.primary_phone, ' (', {alias}.primary_phone_type, ')') END"
    ]
    fields = {}
    for name in WIRELESS_FIELDS:
        condition = usable_phone_sql(f"{alias}.{name}")
        fields[name] = f"CASE WHEN {condition} THEN {alias}.{name} END"
        numbers.append(fields[name])
        types.append(f"CASE WHEN {condition} THEN 'Wireless' END")
        descriptions.append(f"CASE WHEN {condition} THEN CONCAT({alias}.{name}, ' (Wireless)') END")
    child_order = "ORDER BY visible_phone.is_primary DESC, visible_phone.phone_number LIMIT 1"
    numbers.append(f"(SELECT visible_phone.phone_number FROM phone_numbers visible_phone WHERE {child_ok} {child_order})")
    types.append(f"(SELECT visible_phone.line_type FROM phone_numbers visible_phone WHERE {child_ok} {child_order})")
    descriptions.append(
        "(SELECT GROUP_CONCAT(DISTINCT CONCAT(visible_phone.phone_number, ' (', visible_phone.line_type, ')')) "
        f"FROM phone_numbers visible_phone WHERE {child_ok})"
    )
    fields["primary_phone"] = "COALESCE(" + ", ".join(numbers) + ")"
    fields["primary_phone_type"] = "COALESCE(" + ", ".join(types) + ")"
    fields["all_phones"] = "CONCAT_WS(', ', " + ", ".join(descriptions) + ")"
    return fields


def person_export_view_sql() -> str:
    phones = visible_phone_fields_sql()
    return f"""CREATE OR REPLACE VIEW 人物主表 AS
SELECT
    p.person_id AS `人物ID`,
    p.full_name AS `全名`,
    p.gender AS `性别`,
    p.age AS `年龄`,
    {phones['primary_phone']} AS `当前电话`,
    {phones['primary_phone_type']} AS `当前电话类型`,
    p.current_address AS `当前地址`,
    p.address_duration AS `当前地址时长`,
    p.last_name AS `姓`,
    p.first_name AS `名`,
    p.middle_name AS `中间名`,
    {phones['all_phones']} AS `电话列表`,
    {phones['wireless_phone_1']} AS `移动号码1`,
    {phones['wireless_phone_2']} AS `移动号码2`,
    {phones['wireless_phone_3']} AS `移动号码3`
FROM persons p
WHERE {person_has_phone_sql()};"""
