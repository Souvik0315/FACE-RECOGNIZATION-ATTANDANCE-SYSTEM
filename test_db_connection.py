
from database import get_connection

required_tables = [
    "employees",
    "employee_attendance",
    "employee_registration_requests"
]

connection = get_connection()

if connection is None or not connection.is_connected():
    print("FAILED: Could not connect to MySQL.")
else:
    try:
        cursor = connection.cursor()

        cursor.execute("SHOW TABLES")
        existing_tables = {row[0] for row in cursor.fetchall()}

        print("\n--- Employee Table Check ---")

        for table in required_tables:
            if table in existing_tables:
                print(f"FOUND: {table}")
            else:
                print(f"MISSING: {table}")

        cursor.close()
    except Exception as error:
        print("DATABASE ERROR:", error)
    finally:
        connection.close()
