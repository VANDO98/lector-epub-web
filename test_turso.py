import os
import libsql_client

TURSO_URL = os.environ.get("TURSO_URL", "libsql://epub-reader-vando98.aws-us-east-2.turso.io")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODM2NTM0MTcsImlkIjoiMDE5ZjRhMDctMTYwMS03NGU1LWE1MzYtMjU4ZmQ0YzEwN2UyIiwia2lkIjoiQzRfcmlwM2hMX05abldwa0pjNG5LLUtjMGRLTlNYeVUySTJma0JBbVZ2VSIsInJpZCI6Ijg4YzY5MTEyLTQyYzctNGRiYS04YWU0LWI2OTZmYTM3ZDVjNiJ9.zALbYbemd7cukeH4s0VHej5MfM85_F3p5STyuEU9lC-K5luM4aGxGXr6OlwOgmkBahXcKNBhnimkm0z8yI5jAw")

try:
    print(f"Connecting to {TURSO_URL}")
    client = libsql_client.create_client_sync(url=TURSO_URL, auth_token=TURSO_TOKEN)
    rs = client.execute("SELECT 1")
    print("Success!", rs.rows)
except Exception as e:
    print("Error:", e)
