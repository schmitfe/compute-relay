from django.test import Client

def worker_client():
    return Client(HTTP_AUTHORIZATION="Bearer synthetic-worker-token")
