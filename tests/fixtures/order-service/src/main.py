import requests
import os
from kafka import KafkaProducer

PAYMENTS_URL = "https://payments-service.internal"
producer = KafkaProducer(bootstrap_servers="kafka:9092")

def create_order(order):
    r = requests.post(f"{PAYMENTS_URL}/v1/charge", json=order)
    producer.send("orders.created", value=order)
    return r.json()

def cancel_order(order_id):
    url = os.getenv("PAYMENTS_URL")
    r = requests.delete(f"{url}/v1/orders/{order_id}")
    producer.send("orders.failed", value={"id": order_id})
    return r.json()
