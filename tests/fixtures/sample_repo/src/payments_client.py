import requests
import httpx
import os

PAYMENTS_URL = os.getenv("PAYMENTS_URL", "https://payments-service.internal")

def charge(amount):
    # literal URL - confidence 1.0
    r = requests.post("https://payments-service.internal/v1/charge", json={"amount": amount})
    return r.json()

def get_user(user_id):
    # env var reference - confidence 0.7
    url = os.getenv("USER_SERVICE_URL")
    r = httpx.get(f"{url}/users/{user_id}")
    return r.json()

def check_fraud(payload):
    # variable - confidence 0.4
    base = PAYMENTS_URL
    r = requests.post(f"{base}/fraud/check", json=payload)
    return r.json()