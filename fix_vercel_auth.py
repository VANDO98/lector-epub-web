import sys
import re

with open('api/index.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update init_db to add settings table
settings_table = '''
    client.execute(\'\'\'
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    \'\'\')
    client.close()
'''
content = content.replace('client.close()', settings_table, 1)

# 2. Add REDIRECT_URI env var support
content = content.replace('REDIRECT_URI = "http://localhost:8501/auth/callback"', 'REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8501/auth/callback")')

# 3. Replace Auth logic completely
auth_code = '''
# --- Google Drive / OAuth2 logic ---
user_credentials = {}

def get_flow():
    creds_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_env:
        client_config = json.loads(creds_env)
        return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    elif os.path.exists(CLIENT_SECRETS_FILE):
        return Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    else:
        raise HTTPException(500, "Configura GOOGLE_CREDENTIALS_JSON en Vercel o credentials.json local")

def load_user_credentials():
    global user_credentials
    client = get_db_client()
    try:
        rs = client.execute("SELECT value FROM settings WHERE key = 'google_token'")
        if rs.rows:
            creds_data = json.loads(rs.rows[0][0])
            creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
            if creds and creds.valid:
                user_credentials["default"] = creds
            elif creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request as GRequest
                creds.refresh(GRequest())
                user_credentials["default"] = creds
                save_user_credentials(creds)
    except Exception as e:
        print("Error loading token:", e)
    finally:
        client.close()

def save_user_credentials(creds):
    client = get_db_client()
    client.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('google_token', ?)", [creds.to_json()])
    client.close()

# Initialize credentials on boot
load_user_credentials()

@app.get("/auth/login")
def auth_login():
    flow = get_flow()
    auth_url, state = flow.authorization_url(prompt='consent', access_type='offline')
    return RedirectResponse(auth_url)

@app.get("/auth/callback")
async def callback(request: Request):
    flow = get_flow()
    flow.fetch_token(authorization_response=str(request.url).replace("http://", "https://"))
    creds = flow.credentials
    user_credentials["default"] = creds
    save_user_credentials(creds)
    return RedirectResponse("/")
'''

# Find where user_credentials block starts and ends
# We can replace everything from `user_credentials = {}` down to the end of `callback`
pattern = r'user_credentials = \{\}.*?return RedirectResponse\("/"\)'
content = re.sub(pattern, auth_code.strip(), content, flags=re.DOTALL)

with open('api/index.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("api/index.py auth patched")
