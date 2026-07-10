import os
import re

with open('api/index.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Encontrar el inicio de las rutas de Auth
start_idx = content.find('@app.get("/auth/login")')
end_idx = content.find('# --- Frontend ---')

if start_idx == -1 or end_idx == -1:
    print("Could not find blocks")
    exit(1)

new_auth_block = '''
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

'''

content = content[:start_idx] + new_auth_block + content[end_idx:]

with open('api/index.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("api/index.py auth patched")
