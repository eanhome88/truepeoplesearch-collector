"""列表和导出用的筛选。姓名、电话走前缀，方便用上索引。"""

import re

PAGE_SIZE = 100
COLUMNS = ("全名", "年龄", "州", "当前电话", "电话类型", "当前地址")
SELECT_LIST = (
    "person_id, full_name, age, current_state, primary_phone, "
    "primary_phone_type, current_address"
)


def phone_prefix(raw: str) -> str:
    text = (raw or "").strip().replace("%", "").replace("_", "")
    if not text:
        return ""
    if text.startswith("(") or "-" in text:
        return text + "%"
    digits = re.sub(r"\D", "", text)
    if not digits:
        return text + "%"
    if len(digits) <= 3:
        return "(" + digits + "%"
    if len(digits) <= 6:
        return f"({digits[:3]}) {digits[3:]}%"
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:10]}%"


def build_filter(name, state, phone, wireless_only):
    clauses = [
        "person_id NOT LIKE %s",
        "person_id NOT LIKE %s",
    ]
    params = ["http%", "%resultphone%"]
    if (name or "").strip():
        clauses.append("full_name LIKE %s")
        params.append((name or "").strip().replace("%", "").replace("_", "") + "%")
    if (state or "").strip():
        clauses.append("current_state = %s")
        params.append((state or "").strip().upper())
    prefix = phone_prefix(phone or "")
    if prefix:
        clauses.append(
            "(primary_phone LIKE %s OR person_id IN "
            "(SELECT person_id FROM phone_numbers WHERE phone_number LIKE %s))"
        )
        params.extend([prefix, prefix])
    if wireless_only:
        clauses.append("primary_phone_type = %s")
        params.append("Wireless")
    return " AND ".join(clauses), params


def page_sql(where: str, after_id):
    keyset = ""
    extra = []
    if after_id:
        keyset = " AND person_id < %s"
        extra = [after_id]
    sql = (
        f"SELECT {SELECT_LIST} FROM persons WHERE {where}{keyset} "
        f"ORDER BY person_id DESC LIMIT {PAGE_SIZE + 1}"
    )
    return sql, extra
