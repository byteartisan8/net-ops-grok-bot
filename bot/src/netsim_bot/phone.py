"""The operator's phone: signing a phone in to the approvals page, and knowing it again afterwards.

The approvals page for the phone (:mod:`netsim_bot.api.phone`) has a listener of its own, which only this machine
and the user's tailnet reach (``tailscale serve``, never Funnel). Being on the tailnet is not enough: a phone must
be signed in.

* **Sign-in link.** The operator asks for one at the NOC room (Approvals -> Sign in a phone) or with
  ``botctl phone pair``: a one-time code, valid for 10 minutes, shown as a QR code. The phone opens
  ``/m/pair/<code>`` and taps Sign in; the code is used up and the phone gets a random token of its own in a cookie
  (HttpOnly, SameSite=Lax, Secure over https). The office keeps the token's SHA-256 only.
* **Signed in** for ``BOT_PHONE_SESSION_DAYS`` (30) from the sign-in, until it signs out, or is signed out from the
  NOC room or with ``botctl phone sign-out``. A phone that signs out or runs out is forgotten: the tickets it decided
  keep its name (``decided_by``), the activity log its sign-in and sign-out.
* **Forms** carry a value derived from the phone's own token (against cross-site requests): no other page can make
  one that the office accepts.

Codes and tokens never reach the roles. A Bot sees the links to the phone's pages (``review_ticket``'s result), and a
link approves nothing.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

from netsim_bot.models import Phone, now
from netsim_bot.settings import BotSettings
from netsim_bot.store import Store

PAIR_TTL_S = 600.0  # how long a sign-in link works
MAX_OPEN_PAIRINGS = 5  # sign-in links not yet used: a new one pushes out the oldest
SEEN_EVERY_S = 60.0  # a phone's last_seen is written at most this often


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def form_token(token: str) -> str:
    """What a signed-in phone's forms carry: only the holder of the phone's token can make it."""
    return hashlib.sha256(b"netsim-phone-form:" + token.encode()).hexdigest()[:32]


def clean_label(label: str | None) -> str:
    """A phone's name as it shows in the audit log (``approved-by:phone:<label>``): letters, digits and dashes."""
    return re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")[:24].strip("-")


def device_label(user_agent: str | None) -> str:
    """The name of a phone signed in without one: what its browser says it is."""
    ua = user_agent or ""
    for mark, label in (("iPhone", "iphone"), ("iPad", "ipad"), ("Android", "android")):
        if mark in ua:
            return label
    return "browser"


class Phones:
    """Sign-in links (in memory: a restart of grok-bot voids them) and the signed-in phones (in the store)."""

    def __init__(self, store: Store, settings: Callable[[], BotSettings]) -> None:
        self.store = store
        self._settings = settings
        self._pairings: dict[str, dict[str, Any]] = {}  # sha256(code) -> {label, expires}

    # ------------------------------------------------------------------ sign-in links
    def pair(self, label: str | None = None) -> dict[str, Any]:
        """A new one-time sign-in code (shown once: only its hash is kept)."""
        t = time.time()
        self._expire(t)
        while len(self._pairings) >= MAX_OPEN_PAIRINGS:
            del self._pairings[min(self._pairings, key=lambda k: self._pairings[k]["expires"])]
        code = secrets.token_urlsafe(24)
        expires = round(t + PAIR_TTL_S, 3)
        self._pairings[token_hash(code)] = {"label": clean_label(label), "expires": expires}
        return {"code": code, "label": clean_label(label) or None, "expires": expires, "ttl_s": PAIR_TTL_S}

    def pairing(self, code: str) -> dict[str, Any] | None:
        """The sign-in offer behind a code, while it is valid."""
        self._expire(time.time())
        return self._pairings.get(token_hash(code))

    def open_pairings(self) -> int:
        self._expire(time.time())
        return len(self._pairings)

    def redeem(
        self, code: str, user_agent: str | None = None, tailnet_user: str | None = None
    ) -> tuple[Phone, str] | None:
        """Use a code up: the new phone and its token (for the cookie), or None for an unknown, used or old code."""
        offer = self.pairing(code)
        if offer is None:
            return None
        del self._pairings[token_hash(code)]
        token = secrets.token_urlsafe(32)
        phone = Phone(
            id=self.store.next_id("PHONE", width=2),
            label=offer["label"] or device_label(user_agent),
            token_hash=token_hash(token),
            last_seen=now(),
            user_agent=(user_agent or "")[:160] or None,
            tailnet_user=(tailnet_user or "")[:120] or None,
        )
        self.store.save_phone(phone)
        return phone, token

    def _expire(self, t: float) -> None:
        for key in [k for k, v in self._pairings.items() if v["expires"] <= t]:
            del self._pairings[key]

    # ------------------------------------------------------------------ signed-in phones
    def expires(self, phone: Phone) -> float:
        return phone.created + self._settings().phone_session_days * 86_400

    def signed_in(self, token: str | None) -> Phone | None:
        """The phone a cookie's token belongs to, if it is still signed in."""
        if not token:
            return None
        phone = self.store.phone_by_token_hash(token_hash(token))
        if phone is None:
            return None
        if time.time() >= self.expires(phone):
            self.store.delete_phone(phone.id)
            return None
        if phone.last_seen is None or time.time() - phone.last_seen > SEEN_EVERY_S:
            phone.last_seen = now()
            self.store.save_phone(phone)
        return phone

    def decided(self, phone: Phone) -> None:
        """Count an approval or rejection made with the phone."""
        fresh = self.store.phone(phone.id) or phone
        fresh.decisions += 1
        fresh.last_seen = now()
        self.store.save_phone(fresh)

    def sign_out(self, phone_id: str) -> Phone:
        """Forget a phone: its cookie stops working at once. Returns it as it was."""
        phone = self.store.phone(phone_id)
        if phone is None:
            raise KeyError(phone_id)
        self.store.delete_phone(phone_id)
        return phone

    def listed(self) -> list[dict[str, Any]]:
        """The phones signed in, newest first, without token hashes (the ones whose time ran out are forgotten)."""
        t = time.time()
        out = []
        for p in self.store.phones():
            if t >= self.expires(p):
                self.store.delete_phone(p.id)
            else:
                out.append({**p.public(), "expires": round(self.expires(p), 3)})
        return out
