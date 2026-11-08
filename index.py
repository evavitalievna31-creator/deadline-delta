"""Vercel API for Deadline Delta. The Clocked key never reaches the browser."""

from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit
import json
import os
import re
import unicodedata


MAX_CHARS = 4800
MAX_BODY = 44000
MAX_ROUTE = 90
FIELDS = {
    "deadline": {
        "description": (
            "Every verbatim application submission cutoff clause in this English or German notice. "
            "Preserve dates, times, time zones, applicant groups, conditions and exceptions exactly as printed. "
            "Include each conflicting cutoff, and any clause saying the deadline is pending. "
            "Exclude publication dates, dates of the event, and earlier dates expressly superseded in this notice. "
            "Do not infer a date or translate the source."
        ),
        "list": True,
        "max_items": 6,
    },
    "documents": {
        "description": (
            "Verbatim phrases identifying required application documents, including clauses "
            "explicitly saying a document is optional or no longer required. Preserve any negation. "
            "Do not invent or translate document names."
        ),
        "list": True,
        "max_items": 6,
    },
    "channel": (
        "Shortest exact phrase identifying where or how an application must be sent: "
        "the portal name, submission email address, or other specified channel. Do not translate."
    ),
    "eligibility": (
        "Verbatim clause describing who is eligible to apply. Retain any explicit exception "
        "or exclusion. Do not translate, summarize or infer eligibility."
    ),
}

NO_VALUE = {"", "null", "none", "n/a", "not found", "not applicable", "unknown"}
TEXT_KEYS = ("quote", "exact_text", "matched_text", "value", "text", "span", "excerpt", "snippet")
CHILD_KEYS = ("matches", "values", "items", "spans", "evidence", "extractions", "result", "citations", "passages")
MONTHS = {
    "january": 1, "jan": 1, "januar": 1, "february": 2, "feb": 2, "februar": 2,
    "march": 3, "mar": 3, "märz": 3, "mär": 3, "maerz": 3, "april": 4, "apr": 4,
    "may": 5, "mai": 5, "june": 6, "jun": 6, "juni": 6, "july": 7, "jul": 7,
    "juli": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "oktober": 10, "okt": 10, "november": 11,
    "nov": 11, "december": 12, "dec": 12, "dezember": 12, "dez": 12,
}
MONTH = r"(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")"
TIME_ZONES = {"utc": 0, "cet": 60, "cest": 120, "eet": 120, "eest": 180,
              "est": -300, "edt": -240, "pst": -480, "pdt": -420}
ZONE_ALIASES = {"mez": "cet", "mesz": "cest", "gmt": "utc"}
PENDING_DEADLINE = re.compile(
    r"\b(?:tba|tbd|to be (?:confirmed|announced)|will be (?:announced|published)|"
    r"not yet (?:announced|set)|wird[^.\n]{0,60}bekannt\s*gegeben|wird[^.\n]{0,55}veröffentlicht|"
    r"frist\s+folgt|noch nicht festgelegt|noch offen)\b", re.I,
)


class BadProviderResponse(Exception):
    pass


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()).strip(" .;:")


def candidates(node, source, depth=0, hint=None):
    """Yield text and a verified position hint where the extractor supplied one."""
    if depth > 6:
        return
    if isinstance(node, str):
        yield node, hint
    elif isinstance(node, list):
        for item in node[:24]:
            yield from candidates(item, source, depth + 1, hint)
    elif isinstance(node, dict):
        start = node.get("start", node.get("start_char"))
        end = node.get("end", node.get("end_char"))
        span = node.get("span")
        if isinstance(span, dict):
            start = span.get("start", start)
            end = span.get("end", end)
        start = start if type(start) is int and 0 <= start < len(source) else hint
        for key in TEXT_KEYS + CHILD_KEYS:
            if key in node:
                yield from candidates(node[key], source, depth + 1, start)
        if (
            type(start) is int and type(end) is int and 0 <= start < end <= len(source)
            and end - start <= 500 and not any(isinstance(node.get(key), str) for key in TEXT_KEYS)
        ):
            yield source[start:end], start


def evidence_for(node, source):
    seen = set()
    evidence = []
    for candidate, hint in candidates(node, source):
        quote = candidate.strip()
        if len(quote) < 2 or len(quote) > 500 or normalize(quote) in NO_VALUE:
            continue
        offset = hint if hint is not None and source.startswith(quote, hint) else source.find(quote)
        if offset < 0 or (offset, quote) in seen:
            continue
        seen.add((offset, quote))
        evidence.append({"text": quote, "line": source.count("\n", 0, offset) + 1, "offset": offset})
    # Providers can return both a short value and its containing evidence span.
    # Keep the containing passage if it adds context such as “not required”.
    clean = [
        item for item in evidence
        if not any(other["line"] == item["line"] and len(other["text"]) > len(item["text"])
                   and item["text"] in other["text"] for other in evidence)
    ]
    return [{"text": item["text"], "line": item["line"]}
            for item in sorted(clean, key=lambda x: x["offset"])[:6]]


def calendar_dates(s):
    matches = []
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})(?=\b|t\d{2}:)", s):
        matches.append((int(m[1]), int(m[2]), int(m[3])))
    for m in re.finditer(rf"\b(\d{{1,2}})\.?\s+({MONTH})\.?\s+(\d{{4}})\b", s):
        matches.append((int(m[3]), MONTHS[m[2]], int(m[1])))
    for m in re.finditer(rf"\b({MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s+(\d{{4}})\b", s):
        matches.append((int(m[3]), MONTHS[m[1]], int(m[2])))
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", s):
        matches.append((int(m[3]), int(m[2]), int(m[1])))
    try:
        return {date(*parts).isoformat() for parts in matches}
    except ValueError:
        return set()


def explicit_deadline(value):
    """Normalize only unambiguous full dates with explicit time/zone if present."""
    s = normalize(value)
    if PENDING_DEADLINE.search(s):
        return None
    dates = calendar_dates(s)
    if len(dates) != 1:
        return None
    iso = next(iter(dates))
    s = re.sub(r"\b(\d{4}-\d{2}-\d{2})t(?=\d{2}:)", r"\1 ", s)
    s = re.sub(r"(?<=\d)z\b", " utc", s)
    s = re.sub(r"\b(?:utc|gmt)\s*(?=[+-]\d{2}:\d{2})", "", s)
    clock_text = re.sub(r"[+-]\d{2}:\d{2}", "", s)
    times = re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(a\.?m\.?|p\.?m\.?)?", clock_text)
    if not times:
        times = [(hour, "00", marker) for hour, marker in re.findall(
            r"\b([01]?\d|2[0-3])\s*(a\.?m\.?|p\.?m\.?|uhr)\b", clock_text)]
    if len(times) > 1 or re.search(r"\b(?:midnight|mitternacht)\b", s):
        return None
    time_part = ""
    if times:
        hour, minute, meridiem = times[0]
        hour = int(hour)
        if meridiem and meridiem != "uhr":
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if meridiem.startswith("p") else 0)
        time_part = f"{hour:02d}:{minute}"
    elif re.search(r"\b(?:noon|mittag)\b", s):
        return None
    zones = re.findall(r"\b(?:utc|gmt|cet|cest|mez|mesz|est|edt|pst|pdt|eet|eest)\b|[+-]\d{2}:\d{2}", s)
    zones = {ZONE_ALIASES.get(zone, zone) for zone in zones}
    if len(zones) > 1:
        return None
    zone = next(iter(zones), "")
    if zone.startswith(("+", "-")) and (int(zone[1:3]) > 23 or int(zone[4:]) > 59):
        return None
    return iso, time_part, zone


def date_identity(parts):
    """Compare absolute times only when both time and a supported offset are explicit."""
    iso, clock, zone = parts
    minutes = TIME_ZONES.get(zone)
    if zone.startswith(("+", "-")):
        minutes = (int(zone[1:3]) * 60 + int(zone[4:])) * (-1 if zone[0] == "-" else 1)
    if not clock or minutes is None:
        return parts
    try:
        instant = datetime.fromisoformat(f"{iso}T{clock}").replace(tzinfo=timezone(timedelta(minutes=minutes)))
        return "instant", instant.astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError):
        return parts


def single_deadline(quotes):
    if not quotes:
        return None, "deadline_missing"
    texts = [normalize(item["text"]) for item in quotes]
    if any(PENDING_DEADLINE.search(s) for s in texts):
        return None, "pending"
    parts = [explicit_deadline(s) for s in texts]
    keys = {date_identity(part) for part in parts if part}
    dates_in_one_quote = any(len(calendar_dates(s)) > 1 for s in texts)
    if len(keys) > 1 or dates_in_one_quote:
        return None, "multiple_deadlines"
    if not all(parts):
        return None, "deadline_incomplete"
    return parts[0], "date_stated"


def date_record(parts):
    if parts is None:
        return None
    return {"date": parts[0], "time": parts[1], "zone": parts[2].upper()}


def deadline_relation(old, new):
    if old is None:
        return "date_stated"
    if date_identity(old) == date_identity(new):
        return "same_date"
    if old[0] == new[0] and (not old[1] or not new[1] or not old[2] or not new[2]):
        return "deadline_precision"
    return "new_date"


def field_record(key, before, after):
    left = {normalize(e["text"]) for e in before}
    right = {normalize(e["text"]) for e in after}
    if not before and not after:
        status, note = "absent", "neither"
    elif key == "deadline":
        old, _ = single_deadline(before)
        new, reason = single_deadline(after)
        if new is None:
            status, note = "review", reason
        else:
            status, note = {
                "same_date": ("same", "matching_deadline"),
                "new_date": ("changed", "deadline_differs"),
                "date_stated": ("added", "new_evidence"),
                "deadline_precision": ("review", "deadline_precision"),
            }[deadline_relation(old, new)]
    elif not after:
        status, note = "review", "not_verified_in_revision"
    elif not before:
        status, note = "added", "new_evidence"
    elif left == right:
        status, note = "same", "matching"
    else:
        status, note = "changed", "wording_differs"
    return {"key": key, "status": status, "note": note, "before": before, "after": after}


def compare_signal(raw):
    verdict = raw.get("verdict")
    if not isinstance(verdict, str) or not verdict.strip():
        raise BadProviderResponse("Compare verdict missing")
    v = normalize(verdict).replace("_", " ").replace("-", " ")
    docs = raw.get("documents") or []
    if isinstance(docs, dict):
        docs = list(docs.values())
    if not isinstance(docs, (list, tuple)):
        docs = []
    has_conflict = bool(raw.get("conflicts")) or any(isinstance(doc, dict) and bool(doc.get("conflicts")) for doc in docs)
    has_duplicate = bool(raw.get("duplicates")) or any(isinstance(doc, dict) and bool(doc.get("duplicates")) for doc in docs)
    if v in {"conflict", "conflicts", "conflicting", "conflicts found", "has conflicts", "contradiction", "contradictory"}:
        has_conflict = True
    if v in {"duplicate", "duplicates", "duplicates found", "has duplicates"}:
        has_duplicate = True
    return {"verdict": verdict.strip()[:64], "conflicts": has_conflict, "duplicates": has_duplicate}


def add_cutoff_context(quotes, source):
    """Restore an applicant label when Extract returns just the date on its line."""
    lines = source.split("\n")
    enriched = []
    for quote in quotes:
        line = lines[quote["line"] - 1].strip()
        if len(line) <= 500 and quote["text"] in line and len(calendar_dates(normalize(line))) <= 1:
            quote = {"text": line, "line": quote["line"]}
        if quote not in enriched:
            enriched.append(quote)
    return enriched


def select_route_clauses(client, quotes, route):
    """Use only 'use' decisions for known source passages, never the highest score alone."""
    passages = {f"cutoff-{i}": quote for i, quote in enumerate(quotes, start=1)}
    try:
        raw = client.rerank(
            query=("Which passages explicitly state an application deadline that applies to the applicant "
                   f"group or submission route named {json.dumps(route, ensure_ascii=False)}? Include every "
                   "applicable cutoff. A cutoff for another group or method does not apply."),
            context={"application_route": route},
            documents=[{"id": key, "text": quote["text"]} for key, quote in passages.items()],
        )
        results = raw.get("results")
        if not isinstance(results, list):
            return [], "route_unavailable"
        usable, seen = set(), set()
        for item in results:
            if not isinstance(item, dict) or item.get("action") not in {"use", "expand", "skip"}:
                return [], "route_unavailable"
            identifier = item.get("id")
            if not isinstance(identifier, str) or identifier not in passages or identifier in seen:
                return [], "route_unavailable"
            seen.add(identifier)
            if item["action"] == "use":
                usable.add(identifier)
        if seen != set(passages):
            return [], "route_unavailable"
        selected = [quote for key, quote in passages.items() if key in usable]
        return selected, None if selected else "route_not_found"
    except Exception:
        # An unavailable route search must never make an unscoped date applicable.
        return [], "route_unavailable"


def verify_deadline(client, revised, parts, route="", proof=None):
    iso, clock, zone = parts
    stamp = " ".join(piece for piece in (iso, clock, zone.upper()) if piece)
    subject = (f"the applicant group or submission route {json.dumps(route, ensure_ascii=False)}"
               if route else "all applicants")
    claim = f"The revised notice establishes one applicable submission deadline for {subject}: {stamp}."
    if proof:
        claim += " The wording that applies to this group or route is " + json.dumps(proof["text"], ensure_ascii=False) + "."
    try:
        raw = client.grounding(
            documents=[{"id": "revised-notice", "text": revised}],
            question=(f"What single submission cutoff does the revised notice explicitly give for {subject}? "
                      "Check whether an exception, a pending date, or another cutoff also applies to that scope."),
            answer=claim,
            evidence="all",
        )
        verdict = raw.get("verdict")
        answers = raw.get("answers_question")
    except Exception:
        # Comparison and source excerpts remain useful if Verify is temporarily unavailable.
        return "verify_unavailable"
    if not isinstance(verdict, str) or not verdict.strip():
        return "verify_unavailable"
    verdict = normalize(verdict).replace("_", " ").replace("-", " ")
    if isinstance(answers, dict):
        answers = answers.get("value", answers.get("answer"))
    answers_yes = answers is True or isinstance(answers, str) and normalize(answers) in {"true", "yes"}
    if verdict in {"supported", "fully supported", "grounded", "fully grounded"} and answers_yes:
        return "supported"
    return "verify_unsupported"


def deadline_decision(client, revised, row, route=""):
    old, _ = single_deadline(row["before"])
    scoped_quotes = add_cutoff_context(row["after"], revised) if route else row["after"]
    check = {"state": "review", "route": route, "before": date_record(old), "after": None,
             "proof": [], "reranked": False}
    if route and len(scoped_quotes) > 1:
        scoped_quotes, error = select_route_clauses(client, scoped_quotes, route)
        if error:
            row.update(status="review", note=error)
            return {**check, "reason": error}
        check["reranked"] = True
    new, reason = single_deadline(scoped_quotes)
    if new is None:
        if route and row["after"]:
            row.update(status="review", note=reason)
        return {**check, "reason": reason}
    proof = scoped_quotes[0] if route else None
    verification = verify_deadline(client, revised, new, route, proof)
    if verification != "supported":
        if route and verification == "verify_unsupported":
            verification = "route_unverified"
        row.update(status="review", note=verification)
        return {**check, "reason": verification}
    if route:
        row.update(status="scoped", note="route_verified")
        return {**check, "state": "stated", "reason": "route_verified", "after": date_record(new),
                "proof": [proof]}
    return {**check, "state": "stated", "reason": deadline_relation(old, new), "after": date_record(new)}


def analyze(client, earlier, revised, route=""):
    """Compare notices, extract quotes, optionally rerank by route, then verify the date."""
    signal = compare_signal(client.compare(documents=[earlier, revised]))
    old = client.extract(text=earlier, fields=FIELDS).get("fields")
    new = client.extract(text=revised, fields=FIELDS).get("fields")
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise BadProviderResponse("Extract fields missing")
    rows = [field_record(key, evidence_for(old.get(key), earlier), evidence_for(new.get(key), revised))
            for key in FIELDS]
    decision = deadline_decision(client, revised, rows[0], route)
    return {"mode": "live", "signal": signal, "rows": rows, "decision": decision}


def read_notices(payload):
    if not isinstance(payload, dict):
        return None
    old, new = payload.get("earlier"), payload.get("revised")
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    old, new = old.strip(), new.strip()
    if not (12 <= len(old) <= MAX_CHARS and 12 <= len(new) <= MAX_CHARS):
        return None
    return old, new


def read_route(payload):
    raw = payload.get("route", "") if isinstance(payload, dict) else None
    if not isinstance(raw, str):
        return None
    route = " ".join(raw.split())
    return route if len(route) <= MAX_ROUTE else None


_quota = defaultdict(deque)
_quota_lock = Lock()


def allow_request(address):
    """Small per-instance guard against accidental repeat runs, not a billing limit."""
    now = monotonic()
    with _quota_lock:
        for key in list(_quota):
            while _quota[key] and now - _quota[key][0] >= 60:
                _quota[key].popleft()
            if not _quota[key]:
                del _quota[key]
        bucket = _quota[address[:80]]
        total = _quota["*"]
        if len(bucket) >= 5 or len(total) >= 14:
            return False
        bucket.append(now)
        total.append(now)
        return True


class handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_json(200, {"configured": bool(os.environ.get("CLOCKED_API_KEY", "").strip())})

    def do_POST(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "").casefold()
        if self.headers.get("Sec-Fetch-Site") == "cross-site" or (
            origin and urlsplit(origin).netloc.casefold() != host
        ):
            self.send_json(403, {"error": "wrong_origin"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.send_json(415, {"error": "bad_input"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            self.send_json(413, {"error": "bad_input"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            notices, route = read_notices(payload), read_route(payload)
        except (ValueError, UnicodeError, RecursionError):
            notices, route = None, None
        if notices is None or route is None:
            self.send_json(400, {"error": "bad_input"})
            return
        key = os.environ.get("CLOCKED_API_KEY", "").strip()
        if not key:
            self.send_json(503, {"error": "not_configured"})
            return
        ip = self.headers.get("x-vercel-forwarded-for", "").split(",")[0].strip() or self.client_address[0]
        if not allow_request(ip):
            self.send_json(429, {"error": "rate_limit"})
            return
        try:
            from clocked_sdk import Clocked

            with Clocked(api_key=key, timeout=10, max_attempts=1) as client:
                result = analyze(client, *notices, route=route)
        except BadProviderResponse:
            self.send_json(502, {"error": "unexpected_result"})
            return
        except Exception as exc:
            # Do not expose provider responses, tokens, or submitted text in browser errors.
            status = getattr(exc, "status", getattr(exc, "status_code", None))
            if status in (401, 403):
                self.send_json(502, {"error": "provider_auth"})
            elif status in (402, 429):
                self.send_json(429, {"error": "provider_limit"})
            else:
                self.send_json(502, {"error": "provider_error"})
            return
        self.send_json(200, result)
