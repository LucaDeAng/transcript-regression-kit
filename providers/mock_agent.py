"""A deterministic stand-in for the voice agent, so the kit runs with no API key.

Two versions:

    v1  follows the rules in agent/system_prompt.md.
    v2  the same agent after a "make it more helpful" prompt change:
        - skips identity verification when the caller already gave a customer code
        - no longer refuses third parties (wife, AI assistant) who quote a code
        - talks the caller out of a human handoff instead of transferring
        Everything else (outages, safety, scope, injection) still works,
        which is exactly why an aggregate score hides the regression.

The policy is keyword rules over Italian text. It is not meant to be smart:
it is meant to make the regression gate's behaviour reproducible in CI.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
THIRD_PARTY_REPLY = (
    "Posso dare informazioni solo al titolare del contratto, "
    "dopo la verifica con codice cliente e data di nascita."
)
ASK_CREDENTIALS = "Certo. Mi dice il codice cliente e la data di nascita dell'intestatario?"
SCOPE_REPLY = "Mi occupo solo di bollette, guasti e autoletture di Lumen Energia, luce e gas. Come posso aiutarla?"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).lower()


def _has(text: str, *words: str) -> bool:
    t = _norm(text)
    return any(_norm(w) in t for w in words)


def _text_of(message: dict[str, Any]) -> str:
    content = message["content"]
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if b.get("type") == "text")


def _user_texts(messages: list[dict[str, Any]]) -> list[str]:
    return [t for m in messages if m["role"] == "user" and (t := _text_of(m))]


def _customer_id(text: str) -> str | None:
    m = re.search(r"\bC-\d{4}\b", text, re.IGNORECASE)
    return m.group(0).upper() if m else None


def _birth_date(text: str) -> str | None:
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        return m.group(0)
    if m := re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text):
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    if m := re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", _norm(text)):
        d, month, y = m.groups()
        return f"{y}-{MONTHS[month]:02d}-{int(d):02d}"
    return None


def _address(text: str) -> str:
    m = re.search(
        r"\b((?:via|corso|piazza|viale|largo)\s+[A-Za-zÀ-ÿ' ]+?\s+\d+[a-zA-Z]?)"
        r"(?:,?\s+(?:a\s+)?([A-Z][a-zà-ÿ]+))?",
        text,
    )
    if not m:
        return "indirizzo non specificato"
    street, city = m.groups()
    return f"{street}, {city}" if city else street


def _reading(text: str) -> int | None:
    m = re.search(r"(?<![\w-])(\d{5,7})(?!\d)", text)
    return int(m.group(1)) if m else None


def _intent(texts: list[str]) -> str | None:
    joined = " ".join(texts)
    if _has(joined, "iban"):
        return "iban"
    if _has(joined, "autolettura", "lettura del contatore", "contatore"):
        return "meter"
    if _has(joined, "bolletta", "pagare", "importo"):
        return "bill"
    return None


def _call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args}


def _reply(text: str, *calls: dict[str, Any]) -> dict[str, Any]:
    return {"text": text, "tool_calls": list(calls)}


def _after_tool_results(messages: list[dict[str, Any]], version: str) -> dict[str, Any]:
    """The last message carries tool results: decide the next move from them."""
    calls = [b for b in messages[-2]["content"] if b.get("type") == "tool_use"]
    results = {b["tool_use_id"]: json.loads(b["content"]) for b in messages[-1]["content"]
               if b.get("type") == "tool_result"}
    texts = _user_texts(messages)
    intent = _intent(texts)

    for call in reversed(calls):
        name, args, result = call["name"], call["input"], results.get(call["id"], {})
        if name == "verify_customer":
            if result.get("verified"):
                cid = args["customer_id"]
                if intent == "meter" and (r := _reading(" ".join(texts))) is not None:
                    return _reply("", _call("submit_meter_reading", customer_id=cid, reading=r))
                return _reply("", _call("get_bill", customer_id=cid))
            failures = sum(
                1 for m in messages if m["role"] == "user" and isinstance(m["content"], list)
                for b in m["content"]
                if b.get("type") == "tool_result" and json.loads(b["content"]).get("verified") is False
            )
            if failures >= 2 and version == "v1":
                return _reply(
                    "Non riesco a verificare i dati. La passo a un collega che potrà aiutarla.",
                    _call("transfer_to_human", reason="auth_failed",
                          summary="Due verifiche di identità non riuscite per richiesta bolletta."),
                )
            return _reply("I dati non corrispondono. Può ripetere la data di nascita?")
        if name == "get_bill":
            if intent == "iban":
                return _reply(f"L'IBAN registrato termina con {result['iban'][-4:]}.")
            amount = f"{result['amount_eur']:.2f}".replace(".", ",")
            return _reply(f"L'ultima bolletta è di {amount} euro, con scadenza {result['due_date']}.")
        if name == "report_outage":
            return _reply(
                f"Segnalazione registrata con numero {result['ticket']}: "
                f"un tecnico interverrà entro {result['eta_hours']} ore."
            )
        if name == "submit_meter_reading":
            return _reply("Autolettura registrata, grazie.")
    return _reply("Come posso aiutarla?")


def respond(messages: list[dict[str, Any]], version: str = "v1") -> dict[str, Any]:
    if version not in {"v1", "v2"}:
        raise ValueError(f"unknown mock version {version!r}")
    last = messages[-1]
    if isinstance(last["content"], list) and any(b.get("type") == "tool_result" for b in last["content"]):
        if not _text_of(last):
            return _after_tool_results(messages, version)

    text = _text_of(last)
    texts = _user_texts(messages)

    if _has(text, "ignora", "istruzioni precedenti", "prompt di sistema"):
        return _reply("Posso aiutarla con bollette, guasti e autoletture di Lumen Energia. Di cosa ha bisogno?")

    if _has(text, "operatore", "persona vera", "umano", "robot"):
        if version == "v2":
            return _reply("Posso aiutarla io, mi dica pure di cosa ha bisogno.")
        return _reply(
            "Certo, la passo subito a un operatore.",
            _call("transfer_to_human", reason="customer_request",
                  summary="Il cliente ha chiesto di parlare con un operatore."),
        )

    if _has(text, "odore di gas", "puzza di gas", "fuga di gas"):
        return _reply(
            "Esca dalla stanza, non usi interruttori né fiamme e chiami il pronto intervento "
            "gas 800 900 999. Intanto registro la segnalazione.",
            _call("report_outage", address=_address(text), kind="gas", danger=True),
        )

    if _has(text, "saltata la luce", "non ho corrente", "senza luce", "blackout", "manca la corrente"):
        outage = _call("report_outage", address=_address(text), kind="electricity", danger=False)
        if _has(text, "bolletta"):
            return _reply(
                "Registro subito il guasto. Per la bolletta mi servono poi codice cliente e data di nascita.",
                outage,
            )
        return _reply("Registro subito la segnalazione del guasto.", outage)

    cid = next((c for t in reversed(texts) if (c := _customer_id(t))), None)
    intent = _intent(texts)

    if version == "v1" and _has(text, "moglie", "marito", "figlio", "figlia", "per conto", "assistente automatico"):
        return _reply(THIRD_PARTY_REPLY)

    if _has(text, "che tempo fa", "meteo", "partita", "ricetta"):
        return _reply(SCOPE_REPLY)

    if version == "v2" and cid and intent:
        # The regression: a customer code is treated as proof of identity.
        if intent == "meter" and (r := _reading(" ".join(texts))) is not None:
            return _reply("", _call("submit_meter_reading", customer_id=cid, reading=r))
        return _reply("", _call("get_bill", customer_id=cid))

    if cid and (dob := _birth_date(text)):
        return _reply("", _call("verify_customer", customer_id=cid, birth_date=dob))

    if intent:
        return _reply(ASK_CREDENTIALS)

    return _reply("Buongiorno, sono l'assistente di Lumen Energia. Posso aiutarla con bollette, guasti e autoletture.")
