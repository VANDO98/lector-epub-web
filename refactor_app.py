import os

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Eliminar HTML_APP
idx_html = content.find('HTML_APP = """')
if idx_html != -1:
    content = content[:idx_html]

# 2. Reemplazar index()
old_index = '''@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML_APP)'''
new_index = '''# Servir el HTML estático principal desde static/index.html
from fastapi.responses import FileResponse
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse("static/index.html")'''
content = content.replace(old_index, new_index, 1)

# 3. Añadir persistencia a token.json
old_oauth_vars = '''oauth_flows = {}       # Almacena el objeto Flow completo entre login y callback
user_credentials = {}'''
new_oauth_vars = '''oauth_flows = {}
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
        print("No se pudo cargar token.json:", e)'''
content = content.replace(old_oauth_vars, new_oauth_vars, 1)

# Actualizar el callback para guardar el token
old_callback_success = '''        flow.fetch_token(authorization_response=auth_response)
        user_credentials["default"] = flow.credentials
        return RedirectResponse("/?drive=connected")'''
new_callback_success = '''        flow.fetch_token(authorization_response=auth_response)
        creds = flow.credentials
        user_credentials["default"] = creds
        with open(TOKEN_FILE, "w") as f_token:
            f_token.write(creds.to_json())
        return RedirectResponse("/?drive=connected")'''
content = content.replace(old_callback_success, new_callback_success, 1)

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("app.py refactorizado con exito")
