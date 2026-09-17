"""IST only — the app must be right on any host clock (17-Sep, a week in UTC+4).

Kite's historical API reads a naive from/to as IST, kiteconnect's WebSocket
hands back naive HOST-local datetimes, and Python's default log clock is the
host zone. On an IST Mac all three coincide and nothing shows; in Dubai every
tick was stamped 90 minutes early, every sync ended 90 minutes short, and
every log line read 90 minutes behind the market it described.

This is a STATIC guard: it walks the AST of every file under app/ and fails
if naive host-local time reappears. The fix is always the same —
datetime.now(IST), datetime.fromtimestamp(ts, IST), never the bare form.

Run:  python backend/tests/test_ist_only.py
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))

_DT_NAMES = {"datetime", "date", "_dt", "_datetime", "_date"}
_TIME_NAMES = {"time", "_time", "_t"}


def _recv(node) -> str:
    """The receiver's trailing name: `datetime` in datetime.now(), also in
    datetime.datetime.now()."""
    v = node.func.value
    if isinstance(v, ast.Name):
        return v.id
    if isinstance(v, ast.Attribute):
        return v.attr
    return ""


def offenders_in(src: str) -> list:
    """(lineno, why) for every host-local time call. AST, so comments,
    docstrings and nested arguments cannot fool it."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr, recv = node.func.attr, _recv(node)
        nargs = len(node.args) + len(node.keywords)
        has_tz = len(node.args) >= 2 or any(k.arg == "tz" for k in node.keywords)
        if attr == "now" and recv in _DT_NAMES and nargs == 0:
            out.append((node.lineno, "datetime.now() is host-local — use datetime.now(IST)"))
        elif attr == "today" and recv in _DT_NAMES:
            out.append((node.lineno, "today() is host-local — use datetime.now(IST).date()"))
        elif attr == "utcnow":
            out.append((node.lineno, "utcnow() is naive — use datetime.now(timezone.utc) or IST"))
        elif attr == "localtime" and recv in _TIME_NAMES:
            out.append((node.lineno, "time.localtime is host-local — use gmtime(ts + 19800)"))
        elif attr == "astimezone" and nargs == 0:
            out.append((node.lineno, "astimezone() with no zone converts to the HOST zone"))
        elif attr == "fromtimestamp" and recv in _DT_NAMES and not has_tz:
            out.append((node.lineno, "fromtimestamp(ts) is host-local — fromtimestamp(ts, IST)"))
    return sorted(out)


# kiteconnect's naive datetimes are handled in exactly one place (kite/ticker.py
# _epoch, via .timestamp()), so nothing needs an exemption today.
ALLOW = set()


def test_no_host_local_time_anywhere_in_app():
    found = []
    for root, _dirs, files in os.walk(APP):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, APP)
            with open(path) as fh:
                for lineno, why in offenders_in(fh.read()):
                    if (rel, lineno) not in ALLOW:
                        found.append("app/%s:%d  %s" % (rel, lineno, why))
    assert not found, "host-local time in app/:\n  " + "\n  ".join(found)
    print("  IST    -> no naive now()/today()/fromtimestamp()/localtime in app/")


def test_the_scanner_actually_catches_the_bug_shapes():
    bad = (
        "now = datetime.now()\n"
        "d = date.today()\n"
        "r = datetime.fromtimestamp(last).replace(hour=0)\n"
        "q = datetime.fromtimestamp(int(max(a, b)))\n"      # nested args: regex-proof
        "e = date.fromtimestamp(now_ts + 19800)\n"          # the assistant.py shape
        "z = dt.astimezone()\n"
        "u = datetime.datetime.utcnow()\n"
        "l = time.localtime(ts)\n"
    )
    good = (
        "now = datetime.now(IST)\n"
        "r = datetime.fromtimestamp(last, IST)\n"
        "q = datetime.fromtimestamp(int(x), tz=IST)\n"
        "z = dt.astimezone(IST)\n"
        "g = time.gmtime(ts + 19800)\n"
        "m = mcal.now_ist()\n"
        "# datetime.now() in a comment is fine\n"
        "s = 'datetime.now() in a string is fine'\n"
    )
    assert len(offenders_in(bad)) == 8, offenders_in(bad)
    assert offenders_in(good) == [], offenders_in(good)
    print("  IST    -> scanner: 8/8 bug shapes caught, 0 false positives")


def test_log_clock_is_ist_on_any_host():
    import logging
    import time

    import app.main  # noqa: F401 — installs the converter

    saved = os.environ.get("TZ")
    try:
        for tz in ("Asia/Dubai", "UTC", "America/New_York"):
            os.environ["TZ"] = tz
            time.tzset()
            st = logging.Formatter.converter(0)          # epoch 0 = 05:30 IST
            assert (st.tm_hour, st.tm_min) == (5, 30), (tz, st.tm_hour, st.tm_min)
            # Through an INSTANCE, the way logging really calls it. A bare
            # lambda on the class is bound as a method here and raises on
            # every line — the first version of this fix did exactly that,
            # and the class-level call above still passed.
            rec = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
            rec.created = 0.0
            assert logging.Formatter("%(asctime)s", "%H:%M:%S").format(rec) == "05:30:00", tz
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()
    print("  IST    -> log timestamps read IST under Dubai / UTC / New York hosts")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
