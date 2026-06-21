from kafka import KafkaProducer

producer = KafkaProducer(bootstrap_servers="kafka:9092")

def publish_order(order):
    producer.send("orders.created", value=order)
    producer.send("orders.failed", value=order)