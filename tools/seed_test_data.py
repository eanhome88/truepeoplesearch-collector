# -*- coding: utf-8 -*-
"""
快速注入真实风格测试数据，供本地 Web 面板全面验证与客户功能展示。
"""
import os
import sys
import time
import mysql.connector

DB_CONFIG = {
    "host": os.environ.get("TPS_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("TPS_DB_PORT", 3306)),
    "user": os.environ.get("TPS_DB_USER", "root"),
    "password": os.environ.get("TPS_DB_PASSWORD", ""),
    "database": os.environ.get("TPS_DB_NAME", "people_search"),
    "autocommit": True,
}

SAMPLE_PERSONS = [
    {
        "person_id": "px82l44nur68u2l2l8n60",
        "full_name": "Jeffrey Edwards",
        "first_name": "Jeffrey",
        "middle_name": "",
        "last_name": "Edwards",
        "gender": "男",
        "age": 54,
        "birth_month": 4,
        "birth_year": 1972,
        "primary_phone": "(201) 888-2222",
        "primary_phone_type": "Wireless",
        "current_address": "154 Summit Ave, Jersey City, NJ 07304",
        "address_duration": "(Jan 2018 - Present)",
        "all_phones": "(201) 888-2222 (Wireless), (201) 333-1111 (Landline), (201) 777-6666 (Wireless)",
        "wireless_phone_1": "(201) 888-2222",
        "wireless_phone_2": "(201) 777-6666",
        "wireless_phone_3": "",
        "current_city": "Jersey City",
        "current_state": "NJ",
        "current_zip": "07304",
        "phone_count": 3,
        "email_count": 2,
        "prev_addr_count": 4,
    },
    {
        "person_id": "px66k99abc12d3e4f5g67",
        "full_name": "Jamie Perez",
        "first_name": "Jamie",
        "middle_name": "Maria",
        "last_name": "Perez",
        "gender": "女",
        "age": 42,
        "birth_month": 8,
        "birth_year": 1984,
        "primary_phone": "(305) 555-1001",
        "primary_phone_type": "Wireless",
        "current_address": "742 Ocean Dr, Miami, FL 33139",
        "address_duration": "(Feb 2016 - Present)",
        "all_phones": "(305) 555-1001 (Wireless), (305) 555-1002 (Wireless), (305) 555-1003 (Wireless)",
        "wireless_phone_1": "(305) 555-1001",
        "wireless_phone_2": "(305) 555-1002",
        "wireless_phone_3": "(305) 555-1003",
        "current_city": "Miami",
        "current_state": "FL",
        "current_zip": "33139",
        "phone_count": 5,
        "email_count": 3,
        "prev_addr_count": 2,
    },
    {
        "person_id": "px11a22b33c44d55e66f7",
        "full_name": "Robert Smith",
        "first_name": "Robert",
        "middle_name": "Alan",
        "last_name": "Smith",
        "gender": "男",
        "age": 68,
        "birth_month": 11,
        "birth_year": 1958,
        "primary_phone": "(214) 444-3333",
        "primary_phone_type": "Landline",
        "current_address": "450 Elm St, Dallas, TX 75201",
        "address_duration": "(Oct 2005 - Present)",
        "all_phones": "(214) 444-3333 (Landline)",
        "wireless_phone_1": "",
        "wireless_phone_2": "",
        "wireless_phone_3": "",
        "current_city": "Dallas",
        "current_state": "TX",
        "current_zip": "75201",
        "phone_count": 1,
        "email_count": 1,
        "prev_addr_count": 5,
    },
    {
        "person_id": "px99z88y77x66w55v44u3",
        "full_name": "Sarah Johnson",
        "first_name": "Sarah",
        "middle_name": "Elizabeth",
        "last_name": "Johnson",
        "gender": "女",
        "age": 31,
        "birth_month": 3,
        "birth_year": 1995,
        "primary_phone": "(212) 777-8888",
        "primary_phone_type": "Wireless",
        "current_address": "120 Broadway Ave, New York, NY 10005",
        "address_duration": "(May 2021 - Present)",
        "all_phones": "(212) 777-8888 (Wireless), (917) 666-5555 (Wireless)",
        "wireless_phone_1": "(212) 777-8888",
        "wireless_phone_2": "(917) 666-5555",
        "wireless_phone_3": "",
        "current_city": "New York",
        "current_state": "NY",
        "current_zip": "10005",
        "phone_count": 2,
        "email_count": 4,
        "prev_addr_count": 1,
    },
    {
        "person_id": "px77m88n99p00q11r22s3",
        "full_name": "Michael Brown",
        "first_name": "Michael",
        "middle_name": "David",
        "last_name": "Brown",
        "gender": "男",
        "age": 50,
        "birth_month": 1,
        "birth_year": 1976,
        "primary_phone": "(213) 999-0001",
        "primary_phone_type": "Wireless",
        "current_address": "888 Figueroa St, Los Angeles, CA 90017",
        "address_duration": "(Sep 2014 - Present)",
        "all_phones": "(213) 999-0001 (Wireless)",
        "wireless_phone_1": "(213) 999-0001",
        "wireless_phone_2": "",
        "wireless_phone_3": "",
        "current_city": "Los Angeles",
        "current_state": "CA",
        "current_zip": "90017",
        "phone_count": 2,
        "email_count": 1,
        "prev_addr_count": 3,
    },
    {
        "person_id": "px33t44u55v66w77x88y9",
        "full_name": "Emily Davis",
        "first_name": "Emily",
        "middle_name": "Rose",
        "last_name": "Davis",
        "gender": "女",
        "age": 28,
        "birth_month": 6,
        "birth_year": 1998,
        "primary_phone": "(312) 444-8899",
        "primary_phone_type": "Wireless",
        "current_address": "300 N Michigan Ave, Chicago, IL 60601",
        "address_duration": "(Aug 2022 - Present)",
        "all_phones": "(312) 444-8899 (Wireless)",
        "wireless_phone_1": "(312) 444-8899",
        "wireless_phone_2": "",
        "wireless_phone_3": "",
        "current_city": "Chicago",
        "current_state": "IL",
        "current_zip": "60601",
        "phone_count": 1,
        "email_count": 2,
        "prev_addr_count": 1,
    }
]

def seed_data():
    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()
    print(f"[*] 正在向数据库写入 {len(SAMPLE_PERSONS)} 条全字段测试人物档案...")

    sql = """
        INSERT INTO persons (
            person_id, full_name, first_name, middle_name, last_name, gender, age, birth_month, birth_year,
            primary_phone, primary_phone_type, current_address, address_duration, all_phones,
            wireless_phone_1, wireless_phone_2, wireless_phone_3, current_city, current_state, current_zip,
            phone_count, email_count, prev_addr_count
        ) VALUES (
            %(person_id)s, %(full_name)s, %(first_name)s, %(middle_name)s, %(last_name)s, %(gender)s, %(age)s, %(birth_month)s, %(birth_year)s,
            %(primary_phone)s, %(primary_phone_type)s, %(current_address)s, %(address_duration)s, %(all_phones)s,
            %(wireless_phone_1)s, %(wireless_phone_2)s, %(wireless_phone_3)s, %(current_city)s, %(current_state)s, %(current_zip)s,
            %(phone_count)s, %(email_count)s, %(prev_addr_count)s
        )
        ON DUPLICATE KEY UPDATE
            full_name = VALUES(full_name),
            primary_phone = VALUES(primary_phone),
            primary_phone_type = VALUES(primary_phone_type),
            wireless_phone_1 = VALUES(wireless_phone_1),
            all_phones = VALUES(all_phones),
            current_address = VALUES(current_address);
    """

    for p in SAMPLE_PERSONS:
        cur.execute(sql, p)
        # Insert phone numbers into phone_numbers table
        cur.execute(
            """
            INSERT INTO phone_numbers (person_id, phone_number, line_type, carrier, is_primary)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE line_type=VALUES(line_type)
            """,
            (p["person_id"], p["primary_phone"], p["primary_phone_type"], "Verizon Wireless", 1)
        )
        if p["wireless_phone_2"]:
            cur.execute(
                """
                INSERT INTO phone_numbers (person_id, phone_number, line_type, carrier, is_primary)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE line_type=VALUES(line_type)
                """,
                (p["person_id"], p["wireless_phone_2"], "Wireless", "T-Mobile USA", 0)
            )

    conn.close()
    print("[*] 数据库测试数据写入完成！")

    # Seed Redis metrics
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        r.set("tps:metrics:counter:attempt", 8560)
        r.set("tps:metrics:counter:success", 8542)
        r.set("tps:metrics:counter:dedup_hit", 1820)
        r.set("tps:metrics:lat:count:scrape_ms", 8542)
        r.set("tps:metrics:lat:sum:scrape_ms", 367306.0) # ~43ms avg
        print("[*] Redis 性能与执行指标注入完成: 8,560 次执行, 99.8% 成功率, 43ms 响应时间！")
    except Exception as e:
        print(f"[*] Redis 注入提示: {e}")

if __name__ == "__main__":
    seed_data()
