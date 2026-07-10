import sys
import re

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add libsql_client import
content = content.replace('import sqlite3', 'import libsql_client\nimport urllib.parse')

# 2. Replace DB config
db_config = '''
TURSO_URL = os.environ.get("TURSO_URL", "libsql://epub-reader-vando98.aws-us-east-2.turso.io")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODM2NTM0MTcsImlkIjoiMDE5ZjRhMDctMTYwMS03NGU1LWE1MzYtMjU4ZmQ0YzEwN2UyIiwia2lkIjoiQzRfcmlwM2hMX05abldwa0pjNG5LLUtjMGRLTlNYeVUySTJma0JBbVZ2VSIsInJpZCI6Ijg4YzY5MTEyLTQyYzctNGRiYS04YWU0LWI2OTZmYTM3ZDVjNiJ9.zALbYbemd7cukeH4s0VHej5MfM85_F3p5STyuEU9lC-K5luM4aGxGXr6OlwOgmkBahXcKNBhnimkm0z8yI5jAw")

def get_db_client():
    return libsql_client.create_client_sync(url=TURSO_URL, auth_token=TURSO_TOKEN)

def init_db():
    client = get_db_client()
    client.execute(\'\'\'
        CREATE TABLE IF NOT EXISTS books (
            id TEXT PRIMARY KEY,
            filename TEXT,
            title TEXT,
            author TEXT,
            cover BLOB,
            epub_path TEXT,
            drive_id TEXT,
            source TEXT,
            added_at TEXT
        )
    \'\'\')
    client.execute(\'\'\'
        CREATE TABLE IF NOT EXISTS progress (
            book_id TEXT PRIMARY KEY,
            cap_idx INTEGER DEFAULT 0,
            frag_idx INTEGER DEFAULT 0,
            updated_at TEXT
        )
    \'\'\')
    client.close()
'''

content = re.sub(r'def init_db\(\):.*?conn\.close\(\)', db_config, content, flags=re.DOTALL)
content = re.sub(r'def get_db\(\):.*?return conn', '', content, flags=re.DOTALL)

# 3. Fix /api/library GET
lib_get = '''
    client = get_db_client()
    rs = client.execute("SELECT b.id, b.title, b.author, b.source, b.drive_id, p.cap_idx, p.frag_idx FROM books b LEFT JOIN progress p ON b.id = p.book_id")
    client.close()
    
    books = []
    for r in rs.rows:
        books.append({
            "id": r[0],
            "title": r[1],
            "author": r[2],
            "source": r[3],
            "drive_id": r[4],
            "progress": {"cap_idx": r[5] or 0, "frag_idx": r[6] or 0}
        })
    return books
'''
content = re.sub(r'conn = get_db\(\).*?conn\.close\(\).*?return books', lib_get.strip(), content, flags=re.DOTALL, count=1)

# 4. Fix /api/library POST
lib_post = '''
    client = get_db_client()
    client.execute("INSERT OR REPLACE INTO books (id, filename, title, author, cover, epub_path, drive_id, source, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [book_id, metadata["filename"], metadata["title"], metadata["author"], cover_base64, "tmp", drive_id, "drive", datetime.now().isoformat()])
    client.close()
'''
content = re.sub(r'conn = get_db\(\).*?conn\.commit\(\).*?conn\.close\(\)', lib_post.strip(), content, flags=re.DOTALL, count=1)

# 5. Fix cover GET
cover_get = '''
    client = get_db_client()
    rs = client.execute("SELECT cover FROM books WHERE id = ?", [book_id])
    client.close()
    if not rs.rows or not rs.rows[0][0]:
        raise HTTPException(status_code=404, detail="Cover not found")
    cover_data = base64.b64decode(rs.rows[0][0])
    return StreamingResponse(io.BytesIO(cover_data), media_type="image/jpeg")
'''
content = re.sub(r'conn = get_db\(\)\n\s+row = conn\.execute.*?\n\s+conn\.close\(\)\n\s+if not row.*?\n\s+return StreamingResponse.*?media_type="image/jpeg"\)', cover_get.strip(), content, flags=re.DOTALL)

# 6. Fix progress POST
prog_post = '''
    client = get_db_client()
    client.execute("INSERT OR REPLACE INTO progress (book_id, cap_idx, frag_idx, updated_at) VALUES (?, ?, ?, ?)",
                   [book_id, cap_idx, frag_idx, datetime.now().isoformat()])
    client.close()
    return {"status": "ok"}
'''
content = re.sub(r'conn = get_db\(\)\n\s+conn\.execute\("INSERT OR REPLACE INTO progress.*?\n\s+conn\.commit\(\)\n\s+conn\.close\(\)\n\s+return \{"status": "ok"\}', prog_post.strip(), content, flags=re.DOTALL)

# 7. Fix Sync GET & POST
content = content.replace('''conn = get_db()
    progress_rows = conn.execute("SELECT book_id, cap_idx, frag_idx FROM progress").fetchall()
    conn.close()''', '''client = get_db_client()
    rs = client.execute("SELECT book_id, cap_idx, frag_idx FROM progress")
    progress_rows = [{"book_id": r[0], "cap_idx": r[1], "frag_idx": r[2]} for r in rs.rows]
    client.close()''')
    
content = content.replace('''conn = get_db()
        for book_id, p in sync_data.items():
            conn.execute("INSERT OR REPLACE INTO progress (book_id, cap_idx, frag_idx, updated_at) VALUES (?, ?, ?, ?)",
                         (book_id, p["cap_idx"], p["frag_idx"], datetime.now().isoformat()))
        conn.commit()
        conn.close()''', '''client = get_db_client()
        for book_id, p in sync_data.items():
            client.execute("INSERT OR REPLACE INTO progress (book_id, cap_idx, frag_idx, updated_at) VALUES (?, ?, ?, ?)",
                         [book_id, p["cap_idx"], p["frag_idx"], datetime.now().isoformat()])
        client.close()''')

# 8. Streaming Audio
streaming_audio = '''
@app.get("/api/audio/{book_id}/{cap_idx}/{frag_idx}")
async def get_audio(book_id: str, cap_idx: int, frag_idx: int, voz: str = "es-MX-DaliaNeural"):
    # Wait until chapters are loaded
    if book_id not in libros_sesion:
        raise HTTPException(500, "Libro no cargado en memoria")
    
    capitulos = libros_sesion[book_id]
    texto = capitulos[cap_idx]["fragmentos"][frag_idx]
    
    async def audio_stream():
        communicate = edge_tts.Communicate(texto, voz)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]
                
    return StreamingResponse(audio_stream(), media_type="audio/mpeg")
'''
content = re.sub(r'@app\.get\("/api/audio/.*?return FileResponse\(audio_path\)', streaming_audio.strip(), content, flags=re.DOTALL)

with open('api/index.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("api/index.py creado con éxito")
