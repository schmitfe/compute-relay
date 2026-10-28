import os

def environment():
    result = {key: value for key, value in os.environ.items()
        if not key.startswith("BROKER_") and key not in {"DJANGO_SETTINGS_MODULE", "PYTHONPATH"}}
    result["PYTHONUNBUFFERED"] = "1"
    return result
