"""
Fire one test alert for every Telegram alert type so you can verify
they all arrive and look right. Run with:
    python3 setup/test_alerts.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from admin_bot.alerts import (
    _send,
    alert_new_subscriber,
    alert_purchase,
    alert_tip,
    alert_whale_escalation,
    alert_custom_payment_claim,
)
from admin_bot.error_alerts import alert_bot_error, alert_bot_error_resolved


class _FakeSub:
    sub_id = "test-sub-0001"
    username = "test_fan"
    display_name = "Test Fan"
    platform = "fanvue"


async def main():
    sub = _FakeSub()

    print("1/9  New subscriber...")
    await alert_new_subscriber("fanvue", "test_fan", whale_score=30)

    await asyncio.sleep(1)
    print("2/9  Purchase alert...")
    await alert_purchase("fanvue", "test_fan", amount=27.38, tier=1)

    await asyncio.sleep(1)
    print("3/9  Tip alert...")
    await alert_tip("fanvue", "test_fan", amount=10.00)

    await asyncio.sleep(1)
    print("4/9  Whale escalation (emerging)...")
    await alert_whale_escalation(
        platform="fanvue", username="test_fan", sub_id=sub.sub_id,
        whale_score=55, total_spent=120.00, highest_purchase=77.35,
        trigger="score",
    )

    await asyncio.sleep(1)
    print("5/9  Whale escalation (mega)...")
    await alert_whale_escalation(
        platform="fanvue", username="big_spender", sub_id="test-sub-0002",
        whale_score=92, total_spent=650.00, highest_purchase=200.00,
        trigger="total_spent",
    )

    await asyncio.sleep(1)
    print("6/9  Text message dropped (network failure after retry)...")
    await _send(
        "⚠️ <b>Fanvue Message Dropped</b>\n"
        "User: <code>test-fan-00</code>\n"
        "Reason: network/timeout after retry\n"
        "Error: [TEST] ConnectTimeout('Connection timed out')\n\n"
        "Fan did not receive a reply."
    )

    await asyncio.sleep(1)
    print("7/9  Rate limit alert (429 on text message)...")
    await _send(
        "🚦 <b>Fanvue Rate Limited (429)</b>\n"
        "User: <code>test-fan-00</code>\n"
        "Retry-After: 30s\n\n"
        "Too many messages sent too fast. Bot is being throttled."
    )

    await asyncio.sleep(1)
    print("8/9  PPV send failed...")
    await _send(
        "🚨 <b>Fanvue PPV Send FAILED</b>\n"
        "User: <code>test-fan-00</code>\n"
        "Price: 2738 cents\n"
        "Status: 502\n"
        "Error: [TEST] Bad Gateway\n\n"
        "PPV was NOT delivered. Fan may be waiting."
    )

    await asyncio.sleep(1)
    print("9/9  BOT ERROR (full traceback alert)...")
    try:
        raise ValueError("Test exception — divide by zero simulation")
    except ValueError as e:
        await alert_bot_error(
            "handle_messages_received",
            e,
            sub=sub,
            platform="fanvue",
            model="Elieen Yue",
            inbound_snippet="hey can you send me something naughty",
        )

    await asyncio.sleep(1)
    print("✅  Done — check your Telegram.")


if __name__ == "__main__":
    asyncio.run(main())
