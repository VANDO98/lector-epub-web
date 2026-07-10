import asyncio
import io
import os
import libsql_client
import urllib.parse
import tempfile
import uuid
import json
import base64
from datetime import datetime
from typing import Optional

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
import edge_tts
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# --- Google Drive / OAuth2 imports ---
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# Permite usar HTTP en lugar de HTTPS para el entorno de desarrollo local (localhost)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = FastAPI(title="Lector de ePubs con Voz Natural - Biblioteca & Drive")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration & Initialization ---
BOOKS_DIR = "/tmp/books"
os.makedirs(BOOKS_DIR, exist_ok=True)
DB_PATH = "library.db"
CREDENTIALS_FILE = "credentials.json"
CLIENT_SECRETS_FILE = CREDENTIALS_FILE # Assuming user downloads it as credentials.json
SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive.readonly']
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8501/auth/callback")


TURSO_URL = os.environ.get("TURSO_URL", "libsql://epub-reader-vando98.aws-us-east-2.turso.io")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODM2NTM0MTcsImlkIjoiMDE5ZjRhMDctMTYwMS03NGU1LWE1MzYtMjU4ZmQ0YzEwN2UyIiwia2lkIjoiQzRfcmlwM2hMX05abldwa0pjNG5LLUtjMGRLTlNYeVUySTJma0JBbVZ2VSIsInJpZCI6Ijg4YzY5MTEyLTQyYzctNGRiYS04YWU0LWI2OTZmYTM3ZDVjNiJ9.zALbYbemd7cukeH4s0VHej5MfM85_F3p5STyuEU9lC-K5luM4aGxGXr6OlwOgmkBahXcKNBhnimkm0z8yI5jAw")

def get_db_client():
    url = TURSO_URL.replace("libsql://", "https://")
    return libsql_client.create_client_sync(url=url, auth_token=TURSO_TOKEN)

def init_db():
    client = get_db_client()
    client.execute('''
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
    ''')
    client.execute('''
        CREATE TABLE IF NOT EXISTS progress (
            book_id TEXT PRIMARY KEY,
            cap_idx INTEGER DEFAULT 0,
            frag_idx INTEGER DEFAULT 0,
            updated_at TEXT
        )
    ''')
    
    client.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    client.close()



init_db()

# Memory store for open books (to avoid parsing EPUB every time)
libros_sesion: dict[str, list[dict]] = {}

VOCES_ESP = {
    "Dalia (México) - Femenino":    "es-MX-DaliaNeural",
    "Jorge (México) - Masculino":   "es-MX-JorgeNeural",
    "Elvira (España) - Femenino":   "es-ES-ElviraNeural",
    "Álvaro (España) - Masculino":  "es-ES-AlvaroNeural",
    "Salomé (Colombia) - Femenino": "es-CO-SalomeNeural",
    "Gonzalo (Colombia) - Masculino":"es-CO-GonzaloNeural",
    "Paloma (EE.UU.) - Femenino":   "es-US-PalomaNeural",
    "Elena (Argentina) - Femenino": "es-AR-ElenaNeural",
    "Tomás (Argentina) - Masculino":"es-AR-TomasNeural",
}

# --- Utils ---


def dividir_en_fragmentos(texto: str, max_palabras: int = 120) -> list[str]:
    parrafos = [p.strip() for p in texto.split("\n") if p.strip()]
    fragmentos, actual, cuenta = [], [], 0
    for p in parrafos:
        n = len(p.split())
        if cuenta + n > max_palabras and actual:
            fragmentos.append("\n\n".join(actual))
            actual, cuenta = [p], n
        else:
            actual.append(p)
            cuenta += n
    if actual:
        fragmentos.append("\n\n".join(actual))
    return fragmentos

def extract_epub_metadata_and_cover(epub_path: str):
    book = epub.read_epub(epub_path)
    title = book.get_metadata('DC', 'title')
    title_str = title[0][0] if title else "Título Desconocido"
    
    creator = book.get_metadata('DC', 'creator')
    author_str = creator[0][0] if creator else "Autor Desconocido"

    cover_bytes = None
    # Try to find cover
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_COVER:
            cover_bytes = item.get_content()
            break
    if not cover_bytes:
        # Fallback: look for images with 'cover' in the name
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            if 'cover' in item.get_name().lower():
                cover_bytes = item.get_content()
                break

    return title_str, author_str, cover_bytes

def parse_epub_chapters(epub_path: str) -> list[dict]:
    book = epub.read_epub(epub_path)
    capitulos = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        html = item.get_content().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")

        titulo = None
        for tag in ("h1", "h2", "h3", "title"):
            t = soup.find(tag)
            if t and t.get_text().strip():
                titulo = t.get_text().strip()
                break
        titulo = titulo or item.get_name().replace(".xhtml", "").replace(".html", "")

        texto = "\n".join(l.strip() for l in soup.get_text().splitlines() if l.strip())
        if len(texto) > 100:
            capitulos.append({
                "titulo": titulo,
                "fragmentos": dividir_en_fragmentos(texto),
            })
    return capitulos

async def stream_tts(texto: str, voz: str):
    communicate = edge_tts.Communicate(texto, voz)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            yield chunk["data"]

# --- Library Endpoints ---

@app.get("/api/library")
async def list_library():
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

@app.post("/api/library")
async def add_book(request: Request):
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded")
    
    content = await file.read()
    book_id = str(uuid.uuid4())
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        book = epub.read_epub(tmp_path)
    except:
        os.remove(tmp_path)
        raise HTTPException(status_code=400, detail="Invalid EPUB file")
        
    title = book.get_metadata('DC', 'title')
    title = title[0][0] if title else "Sin Título"
    author = book.get_metadata('DC', 'creator')
    author = author[0][0] if author else "Desconocido"

    cover_data = None
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_COVER or item.id.lower() == 'cover':
            cover_data = item.get_content()
            break
            
    cover_b64 = base64.b64encode(cover_data).decode('utf-8') if cover_data else ""

    client = get_db_client()
    client.execute("INSERT OR REPLACE INTO books (id, filename, title, author, cover, epub_path, drive_id, source, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [book_id, file.filename, title, author, cover_b64, tmp_path, "", "local", datetime.now().isoformat()])
    client.close()
    
    return {"message": "Libro añadido", "id": book_id}

@app.delete("/api/library/{book_id}")
async def delete_book(book_id: str):
    client = get_db_client()
    client.execute("DELETE FROM books WHERE id = ?", [book_id])
    client.execute("DELETE FROM progress WHERE book_id = ?", [book_id])
    client.close()
    return {"message": "Eliminado"}

@app.get("/api/library/{book_id}/open")
async def open_book(book_id: str):
    client = get_db_client()
    rs_book = client.execute("SELECT epub_path, title FROM books WHERE id = ?", [book_id])
    rs_prog = client.execute("SELECT cap_idx, frag_idx FROM progress WHERE book_id = ?", [book_id])
    client.close()
    
    if not rs_book.rows:
        raise HTTPException(404, "Libro no encontrado")
        
    row = rs_book.rows[0]
    prog = rs_prog.rows[0] if rs_prog.rows else None

    try:
        capitulos = parse_epub_chapters(row[0])
    except Exception as e:
        raise HTTPException(500, f"Error leyendo EPUB: {str(e)}")

    libros_sesion[book_id] = capitulos
    
    return {
        "id": book_id,
        "title": row[1],
        "capitulos": [{"titulo": c["titulo"], "num_fragmentos": len(c["fragmentos"])} for c in capitulos],
        "cap_idx": prog[0] if prog else 0,
        "frag_idx": prog[1] if prog else 0
    }

@app.post("/api/library/{book_id}/progress")
async def update_progress(book_id: str, cap_idx: int = Form(...), frag_idx: int = Form(...)):
    client = get_db_client()
    client.execute("INSERT OR REPLACE INTO progress (book_id, cap_idx, frag_idx, updated_at) VALUES (?, ?, ?, ?)",
                   [book_id, cap_idx, frag_idx, datetime.now().isoformat()])
    client.close()
    return {"status": "ok"}


# --- Reader Endpoints ---
@app.get("/api/audio/{book_id}/{cap_idx}/{frag_idx}")
async def get_audio(book_id: str, cap_idx: int, frag_idx: int, voz: str = "es-MX-DaliaNeural"):
    capitulos = libros_sesion.get(book_id)
    if not capitulos:
        raise HTTPException(404, "Libro no abierto en sesión. Recarga la página.")

    if cap_idx >= len(capitulos):
        raise HTTPException(404, "Capítulo no encontrado.")

    fragmentos = capitulos[cap_idx]["fragmentos"]
    if frag_idx >= len(fragmentos):
        raise HTTPException(404, "Fragmento no encontrado.")

    texto = fragmentos[frag_idx]
    if voz not in VOCES_ESP.values():
        voz = "es-MX-DaliaNeural"

    return StreamingResponse(
        stream_tts(texto, voz),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )

@app.get("/api/texto/{book_id}/{cap_idx}/{frag_idx}")
async def get_texto(book_id: str, cap_idx: int, frag_idx: int):
    capitulos = libros_sesion.get(book_id)
    if not capitulos:
        raise HTTPException(404, "Libro no abierto en sesión.")
    cap = capitulos[cap_idx]
    frag = cap["fragmentos"][frag_idx]
    return JSONResponse({"texto": frag, "titulo": cap["titulo"]})

@app.get("/api/voces")
async def get_voces():
    return JSONResponse(VOCES_ESP)

# --- Google Drive Endpoints ---

# We store auth states per session (in memory for simplicity). 
# In a real prod app, use secure cookies/sessions.
oauth_flows = {}
user_credentials = {}
TOKEN_FILE = "token.json"

# Cargar credenciales guardadas si existen
if os.path.exists(TOKEN_FILE):
    try:
        from google.oauth2.credentials import Credentials
        with open(TOKEN_FILE, "r") as f:
            creds_data = json.load(f)
            creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
            if creds and creds.valid:
                user_credentials["default"] = creds
            elif creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request as GRequest
                creds.refresh(GRequest())
                user_credentials["default"] = creds
                with open(TOKEN_FILE, "w") as f2:
                    f2.write(creds.to_json())
    except Exception as e:
        print("No se pudo cargar token.json:", e)


def get_flow():
    creds_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_env:
        client_config = json.loads(creds_env)
        return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    elif os.path.exists(CLIENT_SECRETS_FILE):
        return Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    else:
        raise HTTPException(500, "Configura GOOGLE_CREDENTIALS_JSON en Vercel o credentials.json local")

def get_drive_creds():
    client = get_db_client()
    rs = client.execute("SELECT value FROM settings WHERE key = 'google_token'")
    client.close()
    if not rs.rows:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(rs.rows[0][0]), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request as GRequest
            creds.refresh(GRequest())
            # Save refreshed token
            client = get_db_client()
            client.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('google_token', ?)", [creds.to_json()])
            client.close()
        return creds
    except:
        return None

@app.get("/auth/login")
async def login():
    flow = get_flow()
    auth_url, state = flow.authorization_url(prompt='consent', access_type='offline')
    oauth_flows[state] = flow
    return RedirectResponse(auth_url)

@app.get("/auth/callback")
async def callback(request: Request):
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        raise HTTPException(400, "Google denego el acceso: " + str(error))
    if not state:
        raise HTTPException(400, "Parametro state faltante")

    flow = oauth_flows.pop(state, None)
    if not flow:
        raise HTTPException(400, "Sesion OAuth expirada o invalida. Intenta conectar de nuevo.")

    try:
        auth_response = str(request.url)
        if auth_response.startswith("https://"):
            auth_response = "http://" + auth_response[8:]
        flow.fetch_token(authorization_response=auth_response)
        creds = flow.credentials
        
        # Save to DB
        client = get_db_client()
        client.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('google_token', ?)", [creds.to_json()])
        client.close()
        
        return RedirectResponse("/?drive=connected")
    except Exception as e:
        import traceback
        raise HTTPException(500, "Error al obtener token: " + str(e) + " | " + traceback.format_exc())

@app.get("/auth/status")
async def auth_status():
    creds = get_drive_creds()
    return {"connected": creds is not None and creds.valid}

@app.get("/api/drive/files")
async def list_drive_files():
    creds = get_drive_creds()
    if not creds:
        raise HTTPException(401, "No conectado a Drive")
    
    service = build('drive', 'v3', credentials=creds)
    results = service.files().list(
        q="name contains '.epub' and trashed=false",
        pageSize=50,
        fields="files(id, name, createdTime)"
    ).execute()
    
    items = results.get('files', [])
    return JSONResponse(items)

@app.post("/api/drive/import/{file_id}")
async def import_drive_file(file_id: str):
    creds = get_drive_creds()
    if not creds:
        raise HTTPException(401, "No conectado a Drive")
    
    service = build('drive', 'v3', credentials=creds)
    
    file_metadata = service.files().get(fileId=file_id, fields='name').execute()
    filename = file_metadata.get('name', 'drive_book.epub')
    
    book_id = str(uuid.uuid4())
    
    request = service.files().get_media(fileId=file_id)
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
        downloader = MediaIoBaseDownload(tmp, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        tmp_path = tmp.name
        
    try:
        title, author, cover = extract_epub_metadata_and_cover(tmp_path)
    except Exception as e:
        os.remove(tmp_path)
        raise HTTPException(422, f"Error parseando EPUB de Drive: {e}")

    client = get_db_client()
    client.execute("INSERT OR REPLACE INTO books (id, filename, title, author, cover, epub_path, drive_id, source, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [book_id, filename, title, author, cover, tmp_path, file_id, "drive", datetime.now().isoformat()])
    client.close()
    
    return {"message": "Libro importado de Drive", "id": book_id}

@app.post("/api/drive/sync")
async def sync_progress_to_drive():
    creds = get_drive_creds()
    if not creds:
        raise HTTPException(401, "No conectado a Drive")
        
    client = get_db_client()
    rs = client.execute("SELECT book_id, cap_idx, frag_idx FROM progress")
    progress_rows = [{"book_id": r[0], "cap_idx": r[1], "frag_idx": r[2]} for r in rs.rows]
    client.close()
    
    sync_data = {r["book_id"]: {"cap_idx": r["cap_idx"], "frag_idx": r["frag_idx"]} for r in progress_rows}
    
    service = build('drive', 'v3', credentials=creds)
    results = service.files().list(q="name='lector_epub_sync.json' and trashed=false").execute()
    items = results.get('files', [])
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".json") as f:
        json.dump(sync_data, f)
        temp_path = f.name
        
    media = MediaFileUpload(temp_path, mimetype='application/json')
    
    if items:
        file_id = items[0]['id']
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        file_metadata = {'name': 'lector_epub_sync.json'}
        service.files().create(body=file_metadata, media_body=media).execute()
        
    os.remove(temp_path)
    return {"message": "Sincronizado con éxito"}

@app.get("/api/drive/restore")
async def restore_progress_from_drive():
    creds = get_drive_creds()
    if not creds:
        raise HTTPException(401, "No conectado a Drive")
        
    service = build('drive', 'v3', credentials=creds)
    
    results = service.files().list(q="name='lector_epub_sync.json' and trashed=false").execute()
    items = results.get('files', [])
    
    if not items:
        return {"message": "No hay respaldo en Drive"}
        
    file_id = items[0]['id']
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
        
    sync_data = json.loads(fh.getvalue().decode('utf-8'))
    
    client = get_db_client()
    for book_id, p in sync_data.items():
        client.execute("INSERT OR REPLACE INTO progress (book_id, cap_idx, frag_idx, updated_at) VALUES (?, ?, ?, ?)",
                       [book_id, p["cap_idx"], p["frag_idx"], datetime.now().isoformat()])
    client.close()
    
    return {"message": "Progreso restaurado"}

# --- Frontend ---

# Servir el HTML estático principal desde static/index.html
from fastapi.responses import FileResponse
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse("static/index.html")


from fastapi.staticfiles import StaticFiles
if __name__ == '__main__':
    import uvicorn
    uvicorn.run('app:app', host='0.0.0.0', port=8501, reload=True)

