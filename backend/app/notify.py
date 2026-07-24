"""Off-desk signal delivery: push a card to a webhook the moment it is adopted.

WHY THIS EXISTS: the browser alert is the only thing that ever told you a signal
fired, and it needs the dashboard tab open and focused-enough for the audio
context to unlock. On 22-Jul the best card the engine has produced (92.9) was
issued at 13:15 while nothing was watching, expired 8 minutes later, and cost
more than the whole day made. A signal nobody sees is a signal that did not
happen.

DESIGN CONSTRAINTS, all learned from the loop this runs inside:
  * NEVER raise. This is called from the signal reconcile path; an exception
    here must not stop the engine from issuing signals.
  * NEVER block. The HTTP call runs on a daemon thread with a short timeout, so
    a hung webhook cannot stall the evaluation cycle.
  * NO new dependency. urllib is stdlib and works on the 3.9 interpreter this
    machine has.

Transport is a plain JSON POST, which is what ntfy.sh, Telegram bot sendMessage,
Slack/Discord webhooks and Home Assistant all accept. `ALERT_WEBHOOK_FORMAT`
picks the body shape; `text` (default) suits ntfy, `json` suits the rest.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

from app.config import Settings
from app.signals.models import SignalCard

log = logging.getLogger("tradewell.notify")

_TIMEOUT_S = 8
# urlopen honours file:// (and ftp://, data://) — a typo'd webhook would quietly
# read a local file and log its contents. A push endpoint is only ever HTTP.
_ALLOWED_SCHEMES = ("http", "https")


def _headline(card: SignalCard) -> str:
    side = "BUY PE" if card.direction.value == "PE" else "BUY CE"
    return f"{side} {card.contract} · score {card.confidence:.0f}"


def _body(card: SignalCard) -> str:
    """Everything needed to act without opening the dashboard."""
    lines = [
        f"Entry ₹{card.entry_low}–{card.entry_high}",
        f"SL ₹{card.premium_sl} · T1 ₹{card.target1} · T2 ₹{card.target2}",
    ]
    if card.suggested_lots:
        lines.append(f"Suggested {card.suggested_lots} lot(s)")
    if card.underlying_invalidation:
        lines.append(card.underlying_invalidation)
    # The card's own validity, so a push read late is self-evidently stale.
    lines.append(f"Valid for {max(0, card.valid_until - card.created_at) // 60} min")
    return "\n".join(lines)


def _post(url: str, data: bytes, headers: dict[str, str], on_result=None) -> None:
    """POST from the daemon thread; report the DELIVERY outcome to on_result.

    on_result(True) means the endpoint answered 2xx — the push actually landed
    at the webhook. Anything else (non-2xx, DNS, refused, timeout) is False.
    The distinction exists because "dispatched" once masqueraded as "armed"
    while a typo'd URL swallowed every alert.
    """
    ok = False
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            ok = resp.status < 300
            if not ok:
                log.warning("alert webhook returned %s", resp.status)
    except urllib.error.HTTPError as exc:
        log.warning("alert webhook HTTP %s: %s", exc.code, exc.reason)
    except Exception as exc:  # network down, DNS, timeout — never propagate
        log.warning("alert webhook failed: %s", exc)
    if on_result is not None:
        try:
            on_result(ok)
        except Exception:  # pragma: no cover - a callback bug must not kill the thread
            log.debug("alert on_result callback failed", exc_info=True)


def _webhook_url(cfg: Settings) -> str | None:
    url = (cfg.alert_webhook_url or "").strip()
    if not url:
        return None
    if urllib.parse.urlparse(url).scheme.lower() not in _ALLOWED_SCHEMES:
        log.warning("alert webhook ignored: %s is not an http(s) URL", url)
        return None
    return url


def push_text(title: str, body: str, cfg: Settings, on_result=None) -> bool:
    """Fire-and-forget plain push — watchdog pages, armed confirmations, and
    anything else that must reach the phone without being a signal card.

    Same transport and same non-negotiables as push_signal: never raises,
    never blocks, silently a no-op when no webhook is configured. Returns True
    when a send was DISPATCHED; pass `on_result` to learn (from the posting
    thread) whether the endpoint actually answered 2xx.
    """
    url = _webhook_url(cfg)
    if url is None:
        return False
    if cfg.alert_webhook_format == "json":
        payload = json.dumps({"title": title, "text": f"{title}\n{body}",
                              "message": f"{title}\n{body}"}).encode()
        headers = {"Content-Type": "application/json"}
    else:
        payload = body.encode()
        headers = {"Content-Type": "text/plain; charset=utf-8", "Title": title}
    threading.Thread(target=_post, args=(url, payload, headers, on_result),
                     daemon=True, name="tradewell-alert").start()
    return True


def push_signal(card: SignalCard, cfg: Settings) -> bool:
    """Fire-and-forget push for a newly adopted card.

    Returns True when a send was dispatched, False when it was suppressed —
    the caller uses this only for logging, never for control flow.
    """
    url = _webhook_url(cfg)
    if url is None:
        return False
    if cfg.alert_min_score > 0 and (card.confidence or 0) < cfg.alert_min_score:
        log.debug("alert suppressed: score %.1f below %.1f",
                  card.confidence or 0, cfg.alert_min_score)
        return False

    title, body = _headline(card), _body(card)
    if cfg.alert_webhook_format == "json":
        payload = json.dumps({
            "title": title,
            "text": f"{title}\n{body}",
            "message": f"{title}\n{body}",   # Telegram/Slack-friendly aliases
            "signal_id": card.id,
            "symbol": card.symbol,
            "mode": card.mode.value,
            "score": card.confidence,
        }).encode()
        headers = {"Content-Type": "application/json"}
    else:
        payload = f"{title}\n{body}".encode()
        headers = {"Content-Type": "text/plain; charset=utf-8", "Title": title}

    threading.Thread(
        target=_post, args=(url, payload, headers), daemon=True,
        name="tradewell-alert",
    ).start()
    return True
