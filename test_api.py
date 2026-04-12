"""
test_api.py — Test de conexion a la API de football data.
Usa la variable de entorno RAPIDAPI_KEY (no hardcodear).
"""
import os
import requests

url = "https://free-api-live-football-data.p.rapidapi.com/football-current-live"

api_key = os.environ.get("RAPIDAPI_KEY", "")
if not api_key:
    print("⚠️  RAPIDAPI_KEY no configurada en .env")
    exit(1)

headers = {
    "X-RapidAPI-Key": api_key,
    "X-RapidAPI-Host": "free-api-live-football-data.p.rapidapi.com"
}

response = requests.get(url, headers=headers)

print("STATUS:", response.status_code)
print("TEXT:", response.text)
