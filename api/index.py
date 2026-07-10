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
BOOKS_DIR = "books"
os.makedirs(BOOKS_DIR, exist_ok=True)
DB_PATH = "library.db"
CREDENTIALS_FILE = "credentials.json"
CLIENT_SECRETS_FILE = CREDENTIALS_FILE # Assuming user downloads it as credentials.json
SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive.readonly']
REDIRECT_URI = "http://localhost:8501/auth/callback"


TURSO_URL = os.environ.get("TURSO_URL", "libsql://epub-reader-vando98.aws-us-east-2.turso.io")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODM2NTM0MTcsImlkIjoiMDE5ZjRhMDctMTYwMS03NGU1LWE1MzYtMjU4ZmQ0YzEwN2UyIiwia2lkIjoiQzRfcmlwM2hMX05abldwa0pjNG5LLUtjMGRLTlNYeVUySTJma0JBbVZ2VSIsInJpZCI6Ijg4YzY5MTEyLTQyYzctNGRiYS04YWU0LWI2OTZmYTM3ZDVjNiJ9.zALbYbemd7cukeH4s0VHej5MfM85_F3p5STyuEU9lC-K5luM4aGxGXr6OlwOgmkBahXcKNBhnimkm0z8yI5jAw")

def get_db_client():
    return libsql_client.create_client_sync(url=TURSO_URL, auth_token=TURSO_TOKEN)

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
    client.execute("INSERT OR REPLACE INTO books (id, filename, title, author, cover, epub_path, drive_id, source, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [book_id, metadata["filename"], metadata["title"], metadata["author"], cover_base64, "tmp", drive_id, "drive", datetime.now().isoformat()])
    client.close()
    
    return {"message": "Libro añadido", "id": book_id}

@app.delete("/api/library/{book_id}")
async def delete_book(book_id: str):
    conn = get_db()
    row = conn.execute("SELECT epub_path FROM books WHERE id = ?", (book_id,)).fetchone()
    if row:
        try:
            os.remove(row["epub_path"])
        except OSError:
            pass
    conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    conn.execute("DELETE FROM progress WHERE book_id = ?", (book_id,))
    conn.commit()
    conn.close()
    return {"message": "Eliminado"}

@app.get("/api/library/{book_id}/open")
async def open_book(book_id: str):
    conn = get_db()
    row = conn.execute("SELECT epub_path, title FROM books WHERE id = ?", (book_id,)).fetchone()
    prog = conn.execute("SELECT cap_idx, frag_idx FROM progress WHERE book_id = ?", (book_id,)).fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(404, "Libro no encontrado")

    try:
        capitulos = parse_epub_chapters(row["epub_path"])
    except Exception as e:
        raise HTTPException(500, f"Error leyendo EPUB: {str(e)}")

    libros_sesion[book_id] = capitulos
    
    return {
        "id": book_id,
        "title": row["title"],
        "capitulos": [{"titulo": c["titulo"], "num_fragmentos": len(c["fragmentos"])} for c in capitulos],
        "cap_idx": prog["cap_idx"] if prog else 0,
        "frag_idx": prog["frag_idx"] if prog else 0
    }

@app.post("/api/library/{book_id}/progress")
async def update_progress(book_id: str, cap_idx: int = Form(...), frag_idx: int = Form(...)):
    conn = get_db()
    conn.execute('''
        INSERT INTO progress (book_id, cap_idx, frag_idx, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(book_id) DO UPDATE SET
        cap_idx=excluded.cap_idx, frag_idx=excluded.frag_idx, updated_at=excluded.updated_at
    ''', (book_id, cap_idx, frag_idx, datetime.now().isoformat()))
    conn.commit()
    conn.close()
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

@app.get("/auth/login")
async def login():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise HTTPException(500, "credentials.json no encontrado. Configura OAuth en Google Cloud.")
    
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=REDIRECT_URI
    )
    auth_url, state = flow.authorization_url(prompt='consent', access_type='offline')
    # Guardamos el Flow COMPLETO (incluye code_verifier de PKCE)
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

    # Recuperar el Flow original (que tiene el code_verifier de PKCE)
    flow = oauth_flows.pop(state, None)
    if not flow:
        raise HTTPException(400, "Sesion OAuth expirada o invalida. Intenta conectar de nuevo.")

    try:
        auth_response = str(request.url)
        # Asegurarse de que sea http (OAuthlib lo requiere para localhost)
        if auth_response.startswith("https://"):
            auth_response = "http://" + auth_response[8:]
        flow.fetch_token(authorization_response=auth_response)
        creds = flow.credentials
        user_credentials["default"] = creds
        with open(TOKEN_FILE, "w") as f_token:
            f_token.write(creds.to_json())
        return RedirectResponse("/?drive=connected")
    except Exception as e:
        import traceback
        raise HTTPException(500, "Error al obtener token: " + str(e) + " | " + traceback.format_exc())

@app.get("/auth/status")
async def auth_status():
    is_connected = "default" in user_credentials and user_credentials["default"].valid
    return {"connected": is_connected}

@app.get("/api/drive/files")
async def list_drive_files():
    if "default" not in user_credentials:
        raise HTTPException(401, "No conectado a Drive")
    
    creds = user_credentials["default"]
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
    if "default" not in user_credentials:
        raise HTTPException(401, "No conectado a Drive")
    
    creds = user_credentials["default"]
    service = build('drive', 'v3', credentials=creds)
    
    file_metadata = service.files().get(fileId=file_id, fields='name').execute()
    filename = file_metadata.get('name', 'drive_book.epub')
    
    book_id = str(uuid.uuid4())
    epub_path = os.path.join(BOOKS_DIR, f"{book_id}.epub")
    
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(epub_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
        
    try:
        title, author, cover = extract_epub_metadata_and_cover(epub_path)
    except Exception as e:
        os.remove(epub_path)
        raise HTTPException(422, f"Error parseando EPUB de Drive: {e}")

    conn = get_db()
    conn.execute('''
        INSERT INTO books (id, filename, title, author, cover, epub_path, drive_id, source, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (book_id, filename, title, author, cover, epub_path, file_id, "drive", datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    return {"message": "Libro importado de Drive", "id": book_id}

@app.post("/api/drive/sync")
async def sync_progress_to_drive():
    if "default" not in user_credentials:
        raise HTTPException(401, "No conectado a Drive")
        
    client = get_db_client()
    rs = client.execute("SELECT book_id, cap_idx, frag_idx FROM progress")
    progress_rows = [{"book_id": r[0], "cap_idx": r[1], "frag_idx": r[2]} for r in rs.rows]
    client.close()
    
    sync_data = {r["book_id"]: {"cap_idx": r["cap_idx"], "frag_idx": r["frag_idx"]} for r in progress_rows}
    
    creds = user_credentials["default"]
    service = build('drive', 'v3', credentials=creds)
    
    # Check if sync file exists
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
    if "default" not in user_credentials:
        raise HTTPException(401, "No conectado a Drive")
        
    creds = user_credentials["default"]
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
    
    conn = get_db()
    for book_id, p in sync_data.items():
        conn.execute('''
            INSERT INTO progress (book_id, cap_idx, frag_idx, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(book_id) DO UPDATE SET
            cap_idx=excluded.cap_idx, frag_idx=excluded.frag_idx, updated_at=excluded.updated_at
        ''', (book_id, p["cap_idx"], p["frag_idx"], datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
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

