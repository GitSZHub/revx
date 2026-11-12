import base64, os, time
import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization

load_dotenv()
API_KEY = os.environ["REVX_API_KEY"]
BASE = "https://revx.revolut.com"

with open("keys/private.pem", "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None)

def signed_get(path: str):
    ts = str(int(time.time() * 1000))
    message = ts + "GET" + path  # timestamp + method + path (+ body, empty for GET)
    sig = base64.b64encode(private_key.sign(message.encode())).decode()
    headers = {
        "X-Revx-API-Key": API_KEY,
        "X-Revx-Timestamp": ts,
        "X-Revx-Signature": sig,
    }
    return requests.get(BASE + path, headers=headers, timeout=10)

r = signed_get("/api/1.0/balances")
print(r.status_code)
for b in r.json() if r.ok else [r.text]:
    print(b)
