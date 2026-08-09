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
from app.market.calendar import EVENING_MIN
from app.signals.models import SignalCard

log = logging.getLogger("tradewell.notify")

_TIMEOUT_S = 8
# urlopen honours file:// (and ftp://, data://) — a typo'd webhook would quietly
# read a local file and log its contents. A push endpoint is only ever HTTP.
_ALLOWED_SCHEMES = ("http", "https")


def _header_safe(title: str) -> str:
    """A Title that http.client can actually send.

    Header VALUES are encoded latin-1 by http.client; any character above
    U+00FF raises UnicodeEncodeError inside urlopen — swallowed by _post's
    catch-all, so the push silently never left the machine. The 🌙 evening
    marker had been doing exactly that to every evening-positional push
    (review catch, 03-Aug). Common symbols map to ASCII words; anything else
    non-latin-1 is dropped. Bodies are unaffected — they travel as UTF-8
    payload bytes, not headers.
    """
    out = (title.replace("₹", "Rs ")
                .replace("🌙", "[NIGHT]")
                .replace("📍", "[LEVEL]"))
    return out.encode("latin-1", "ignore").decode("latin-1").strip()


def _is_evening_positional(card: SignalCard) -> bool:
    """A positional card born at/after 14:30 IST — the overnight-hold
    candidate the paper ledger grades. Derived from the card itself (mode +
    created_at), the same minute the gap caution keys off, so the phone
    alert, the card note, and the ledger can never disagree about which
    cards are "evening"."""
    if card.mode.value != "positional":
        return False
    return (card.created_at + 19800) % 86400 // 60 >= EVENING_MIN


def _fmt_expiry(iso: str | None) -> str | None:
    """'2026-08-04' -> '04-Aug'. The push must say WHICH series: the same
    strike trades in several expiries at once, and a push reading only
    "NIFTY 24250 CE" invites buying the wrong one in Kite. ASCII, so it is
    safe inside the latin-1 Title header. Unparseable input passes through
    verbatim — a strange expiry string is still better shown than dropped."""
    if not iso:
        return None
    try:
        from datetime import date
        d = date.fromisoformat(iso)
        return d.strftime("%d-%b")
    except Exception:
        return iso


def _headline(card: SignalCard) -> str:
    side = "BUY PE" if card.direction.value == "PE" else "BUY CE"
    exp = _fmt_expiry(card.expiry)
    tail = f" · exp {exp}" if exp else ""
    head = f"{side} {card.contract}{tail} · score {card.confidence:.0f}"
    # The distinct title is the alert: on a lock screen the marker alone says
    # "this one holds overnight" before the contract is even read.
    if _is_evening_positional(card):
        head = f"🌙 EVENING POSITIONAL · {head}"
    return head


def _body(card: SignalCard, include_sizing: bool = True) -> str:
    """Everything needed to act without opening the dashboard.

    `include_sizing=False` builds the SHARED-topic variant: the suggested lot
    count is TRADING_CAPITAL x RISK_PER_TRADE_PCT worked backwards through the
    stop distance — from a few cards a guest could reconstruct the owner's
    account size, and it doubles as the owner's actual next position size.
    Sizing derives from the owner's money, not from the signal, so it never
    leaves the primary topic.
    """
    exp = _fmt_expiry(card.expiry)
    lines = [
        f"Entry ₹{card.entry_low}–{card.entry_high}",
        f"SL ₹{card.premium_sl} · T1 ₹{card.target1} · T2 ₹{card.target2}",
    ]
    # Tape state at birth (09-Aug): the same label the card shows on screen,
    # so the phone knows a GOLDEN card without opening the dashboard. Body
    # text only — headers stay latin-1-safe via _header_safe.
    if getattr(card, "golden", False):
        lines.insert(0, "🌟 GOLDEN — developing tape, with the day (label on "
                        "trial: ledger decides at 30 fills)")
    elif getattr(card, "tape_state", None):
        align = ("with" if card.tape_aligned else "against") \
            if card.tape_aligned is not None else "?"
        pct = f"{card.tape_resolved_pct:g}% resolved, " \
            if card.tape_resolved_pct is not None else ""
        lines.append(f"Tape: {card.tape_state} ({pct}{align} the day)")
    if exp:
        # Strike is already in the contract name; the expiry is what the
        # notification was missing — the wrong-series trade is the mistake
        # this line prevents.
        lines.append(f"Strike {card.strike:g} · expiry {exp}")
    if card.suggested_lots and include_sizing:
        lines.append(f"Suggested {card.suggested_lots} lot(s)")
    if card.underlying_invalidation:
        lines.append(card.underlying_invalidation)
    if _is_evening_positional(card):
        lines.append("Overnight-hold candidate — barely trades before the close; "
                     "tomorrow's gap settles it before any stop can act.")
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


def _valid_url(raw: str | None) -> str | None:
    url = (raw or "").strip()
    if not url:
        return None
    if urllib.parse.urlparse(url).scheme.lower() not in _ALLOWED_SCHEMES:
        log.warning("alert webhook ignored: %s is not an http(s) URL", url)
        return None
    return url


def _webhook_url(cfg: Settings) -> str | None:
    return _valid_url(cfg.alert_webhook_url)


def _signal_urls(cfg: Settings) -> list[str]:
    """Destinations for SIGNAL-audience pushes: the primary plus, if set, the
    shared signals-only topic (ALERT_WEBHOOK_URL_2).

    The second topic exists so another person can follow the cards without
    holding the master topic — which also carries watchdog pages and position
    nags that are nobody else's business, and which can only be revoked by
    rotating the topic everyone uses. Deduped: pointing both at the same URL
    must not double-buzz every card.
    """
    urls = [_webhook_url(cfg), _valid_url(getattr(cfg, "alert_webhook_url_2", ""))]
    out: list[str] = []
    for u in urls:
        if u is not None and u not in out:
            out.append(u)
    return out


def _guest_url(cfg: Settings) -> str | None:
    """The shared signals-only topic, IF it is a *distinct* live destination.

    None when no second topic is set, when it is not http(s), or when it points
    at the same place as the primary — in that case the primary's own startup
    verification already proves the pipe, and a separate guest ping would only
    double-buzz whoever holds that one topic.
    """
    guest = _valid_url(getattr(cfg, "alert_webhook_url_2", ""))
    if guest is None or guest == _webhook_url(cfg):
        return None
    return guest


def _text_payload(title: str, body: str, cfg: Settings,
                  priority: str | None = None) -> tuple[bytes, dict[str, str]]:
    """Shape a plain title+body into the wire payload the configured format wants.

    `text` (ntfy) sends the body raw with the title in a header; `json`
    (Telegram/Slack/Discord/Home Assistant) sends title+text+message aliases.
    Shared by every plain-text sender so the two formats can never drift apart.

    `priority="min"` marks a SILENT delivery (ntfy: delivered, listed in the
    app, but no sound/banner). This is how the armed chips keep their 2xx
    delivery proof while the phone only ever SPEAKS for signal cards — the
    31-Jul request: "ntfy needs only the signals". Other webhook services
    ignore the unknown header, which degrades to the old audible behaviour.
    """
    if cfg.alert_webhook_format == "json":
        headers = {"Content-Type": "application/json"}
        if priority:
            headers["Priority"] = priority
        return (json.dumps({"title": title, "text": f"{title}\n{body}",
                            "message": f"{title}\n{body}"}).encode(), headers)
    headers = {"Content-Type": "text/plain; charset=utf-8", "Title": _header_safe(title)}
    if priority:
        headers["Priority"] = priority
    return (body.encode(), headers)


def push_text(title: str, body: str, cfg: Settings, on_result=None,
              audience: str = "private", priority: str | None = None) -> bool:
    """Fire-and-forget plain push — watchdog pages, armed confirmations, and
    anything else that must reach the phone without being a signal card.

    `audience` picks the destinations: "private" (default) goes to the primary
    topic only — position nags and infra pages describe YOUR trading and must
    never reach a shared topic. "signals" additionally posts to the shared
    signals-only topic (card lifecycle news like retirements belongs to anyone
    following the cards). `on_result` reports the PRIMARY delivery only — the
    armed chip vouches for your own phone, not a guest's.

    Same transport and same non-negotiables as push_signal: never raises,
    never blocks, silently a no-op when no webhook is configured. Returns True
    when at least one send was DISPATCHED.
    """
    urls = _signal_urls(cfg) if audience == "signals" else \
        [u for u in [_webhook_url(cfg)] if u is not None]
    if not urls:
        return False
    payload, headers = _text_payload(title, body, cfg, priority=priority)
    primary = _webhook_url(cfg)
    for url in urls:
        threading.Thread(
            target=_post,
            args=(url, payload, headers, on_result if url == primary else None),
            daemon=True, name="tradewell-alert").start()
    return True


def push_guest_verify(title: str, body: str, cfg: Settings, on_result=None) -> bool:
    """Startup verification for the shared guest topic, mirroring the primary's.

    The owner's channel is proven on every feed start by an armed ping whose 2xx
    flips `alerts_armed`; the guest topic had no equivalent, so "is the guest
    actually receiving cards?" stayed unanswerable until a live card happened to
    fire — one character off in the subscribed topic was invisible for a whole
    session. This posts ONE bare ping to the signals-only topic and reports its
    2xx/failure through on_result, arming a guest chip the same machine-verified
    way the primary is armed.

    No-op (returns False) when no DISTINCT guest topic is configured — an unset,
    bad-scheme, or primary-identical URL_2 needs no separate proof. Carries no
    sizing and names no position: a guest reads this, so it says only that the
    channel is live. Same non-negotiables as the rest of the module — never
    raises, never blocks.
    """
    url = _guest_url(cfg)
    if url is None:
        return False
    # Verification is proof, not news: silent priority — the guest's phone
    # must only ever sound for an actual card.
    payload, headers = _text_payload(title, body, cfg, priority="min")
    threading.Thread(
        target=_post, args=(url, payload, headers, on_result),
        daemon=True, name="tradewell-alert").start()
    return True


def push_retire(card: SignalCard, state: str, cfg: Settings) -> bool:
    """Card-lifecycle push (expired / cancelled), signals audience.

    Applies the SAME min-score gate as the adoption push: with ALERT_MIN_SCORE
    set, a sub-threshold card is never pushed at birth — announcing its death
    would tell the topic (including a shared guest) about a signal it was
    deliberately never shown.
    """
    if cfg.alert_min_score > 0 and (card.confidence or 0) < cfg.alert_min_score:
        return False
    score = f"score {card.confidence:.0f}" if card.confidence is not None else "unscored"
    exp = _fmt_expiry(card.expiry)
    series = f"{card.contract} (exp {exp}, {score})" if exp else f"{card.contract} ({score})"
    reason = ("thesis flipped, do not chase the old plan."
              if state == "cancelled" else "entry window closed.")
    # Lifecycle is worth a RECORD, not a buzz: retirements arrive silently
    # (min priority) so the phone only sounds when there is something to DO.
    return push_text(
        f"Tradewell: {card.mode.value} card {state}",
        f"{series} is {state} — {reason}",
        cfg, audience="signals", priority="min",
    )


def push_signal(card: SignalCard, cfg: Settings, on_result=None) -> bool:
    """Fire-and-forget push for a newly adopted card — to the primary topic
    AND the shared signals-only topic when one is configured.

    Returns True when a send was dispatched, False when it was suppressed —
    the caller uses this only for logging, never for control flow.

    `on_result` reports the PRIMARY topic's delivery outcome (2xx or not),
    exactly like push_text: with ALERT_STARTUP_PING=false there is no test
    ping, so the first real card's delivery is what arms the chip.
    """
    urls = _signal_urls(cfg)
    if not urls:
        return False
    if cfg.alert_min_score > 0 and (card.confidence or 0) < cfg.alert_min_score:
        log.debug("alert suppressed: score %.1f below %.1f",
                  card.confidence or 0, cfg.alert_min_score)
        return False

    title = _headline(card)
    primary = _webhook_url(cfg)

    def _payload(include_sizing: bool) -> tuple[bytes, dict[str, str]]:
        body = _body(card, include_sizing=include_sizing)
        if cfg.alert_webhook_format == "json":
            return json.dumps({
                "title": title,
                "text": f"{title}\n{body}",
                "message": f"{title}\n{body}",   # Telegram/Slack-friendly aliases
                "signal_id": card.id,
                "symbol": card.symbol,
                "mode": card.mode.value,
                "score": card.confidence,
                "strike": card.strike,
                "expiry": card.expiry,
                # Routing flag for json consumers (Telegram bots, HA automations)
                # so an overnight-hold candidate can ring a different bell.
                "evening_positional": _is_evening_positional(card),
                # 09-Aug: golden cards can ring their own bell too.
                "golden": bool(getattr(card, "golden", False)),
            }).encode(), {"Content-Type": "application/json"}
        return (f"{title}\n{body}".encode(),
                {"Content-Type": "text/plain; charset=utf-8", "Title": _header_safe(title)})

    for url in urls:
        # Only the owner's own topic carries sizing — see _body.
        payload, headers = _payload(include_sizing=(url == primary))
        threading.Thread(
            target=_post,
            args=(url, payload, headers, on_result if url == primary else None),
            daemon=True, name="tradewell-alert",
        ).start()
    return True
