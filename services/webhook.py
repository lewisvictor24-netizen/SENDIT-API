
import httpx

from typing import Dict, List

# ============================================
# In-memory webhook storage
# (For the lab. In production, store in DB.)
# ============================================

webhooks: Dict[str, List[str]] = {}


# ============================================
# REGISTER WEBHOOK
# ============================================

def register_webhook(
    event_type: str,
    webhook_url: str,
):
    """
    Register a webhook URL for an event.
    """

    if event_type not in webhooks:
        webhooks[event_type] = []

    if webhook_url not in webhooks[event_type]:
        webhooks[event_type].append(webhook_url)

    return {
        "message": "Webhook registered successfully",
        "event_type": event_type,
        "url": webhook_url,
    }


# ============================================
# SEND WEBHOOK
# ============================================

async def send_webhook(
    event_type: str,
    payload: dict,
):
    """
    Send payload to all registered webhook URLs.
    """

    if event_type not in webhooks:
        return

    async with httpx.AsyncClient() as client:

        for url in webhooks[event_type]:

            try:

                response = await client.post(
                    url,
                    json=payload,
                    timeout=10.0,
                )

                print(
                    f"Webhook sent to {url} "
                    f"Status: {response.status_code}"
                )

            except Exception as e:

                print(
                    f"Webhook failed for {url}: {e}"
                )