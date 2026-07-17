import requests


EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "Content-Type": "application/json",
}


def send_expo_push(token: str, title: str, body: str, data: dict | None = None) -> bool:
    """Invia una push notification via Expo Push API (supporta FCM/Android e APNs/iOS)."""
    if not token or not str(token).startswith("ExponentPushToken"):
        return False
    payload = {
        "to": token,
        "title": title,
        "body": body,
        "sound": "default",
        "priority": "high",
        "channelId": "default",
    }
    if data:
        payload["data"] = data
    try:
        resp = requests.post(EXPO_PUSH_URL, json=payload, headers=_HEADERS, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def notify_user_push(user, title: str, body: str, data: dict | None = None) -> int:
    """
    Invia una push a tutti i device attivi dell'utente.
    Rispetta la preferenza user.notify_push. Non solleva mai eccezioni.
    """
    try:
        if user is None or not getattr(user, "notify_push", True):
            return 0
        from .models import PushDevice  # import locale per evitare cicli
        tokens = list(
            PushDevice.objects.filter(utente=user, is_active=True)
            .values_list("token", flat=True)
        )
        if not tokens:
            return 0
        return send_expo_push_bulk(tokens=tokens, title=title, body=body, data=data)
    except Exception:
        return 0


def send_expo_push_bulk(tokens: list[str], title: str, body: str, data: dict | None = None) -> int:
    """Invia push a più device, restituisce il numero di invii riusciti."""
    valid = [t for t in tokens if t and str(t).startswith("ExponentPushToken")]
    if not valid:
        return 0

    messages = [
        {
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
            "priority": "high",
            "channelId": "default",
            **({"data": data} if data else {}),
        }
        for token in valid
    ]
    try:
        resp = requests.post(EXPO_PUSH_URL, json=messages, headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            return 0
        results = resp.json().get("data", [])
        return sum(1 for r in results if r.get("status") == "ok")
    except Exception:
        return 0
