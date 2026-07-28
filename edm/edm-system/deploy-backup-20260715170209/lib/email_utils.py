"""Email body utilities for EDM auto-reply safety."""
import re

_REPLY_MARKER_SUBSTRINGS = (
    " wrote:",
    " escribió:",
    " escribio:",
    " a écrit",
    " a ecrit",
    " schrieb",
    " ha scritto:",
    " 写道",
    "发件人:",
    "发件人：",
    "-----original message-----",
)

_SPANISH_MARKERS = (
    " no tengo ",
    " no puedo ",
    " acceso",
    " permiso",
    " permisos",
    " suscripción",
    " suscripcion",
    " reembolso",
    " factura",
    " gracias",
    " por favor",
    " cuenta",
    " contraseña",
    " contrasena",
    " puedo",
    " tengo",
    " iniciar sesión",
    " iniciar sesion",
)

_GERMAN_MARKERS = (
    " hallo",
    " guten tag",
    " danke",
    " vielen dank",
    " bitte",
    " mfg",
    " mit freundlichen grüßen",
    " mit freundlichen gruessen",
    " ich habe",
    " ich kann",
    " ich konnte",
    " ich möchte",
    " ich moechte",
    " können sie",
    " koennen sie",
    " könnten sie",
    " koennten sie",
    " möglichkeit",
    " moeglichkeit",
    " bemühungen",
    " bemuhungen",
    " chatverlauf",
    " dateien",
    " gesendet",
    " bekommen",
    " natürlich",
    " natuerlich",
    " dreizehn",
    " eingestellt",
    " kopieren",
    " rechnung",
    " kündigen",
    " kuendigen",
    " rückerstattung",
    " rueckerstattung",
)


def extract_latest_reply(body: str) -> str:
    """Return only the newest human-written part of a reply thread.

    Gmail text/plain bodies often include the full quoted thread. Keyword matching on
    the full thread can match our own previous emails (for example "new features")
    instead of the customer's latest sentence (for example "No tengo acceso").

    Some Gmail replies start immediately with a localized quote marker such as
    "Sabrina <...> schrieb am ...:" and then quote the user's latest message with
    ">". In that case there is no unquoted preamble, so we recover the first quoted
    block and stop before nested iWeaver replies.
    """
    if not body:
        return ""

    text = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    latest = []
    marker_seen = False

    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()

        if _is_reply_marker(lower):
            marker_seen = True
            break
        if stripped.startswith(">"):
            continue
        if re.match(r"^-{2,}\s*(forwarded|original)\s+message\s*-{2,}$", lower):
            marker_seen = True
            break

        latest.append(line)

    cleaned = _clean_reply_text("\n".join(latest))
    if cleaned:
        return cleaned

    quoted = _extract_first_quoted_block(lines)
    if quoted:
        return quoted

    # If we saw a marker at the top but could not recover a useful quoted block,
    # return empty rather than the full thread; returning the full thread can mix
    # the customer's language with our previous Chinese/English replies.
    return "" if marker_seen else text.strip()


def _extract_first_quoted_block(lines: list[str]) -> str:
    """Extract the first quoted customer block after a reply marker."""
    marker_index = None
    for idx, line in enumerate(lines):
        if _is_reply_marker(line.strip().lower()):
            marker_index = idx
            break
    if marker_index is None:
        return ""

    collected: list[str] = []
    for line in lines[marker_index + 1:]:
        stripped = line.strip()
        if not stripped:
            if collected:
                collected.append("")
            continue
        if not stripped.startswith(">"):
            if collected:
                break
            continue

        depth = _quote_depth(stripped)
        content = _strip_quote_prefix(stripped)
        lower = content.lower()

        if depth >= 2:
            break
        if _is_reply_marker(lower):
            break
        if "iweaver@iweaver.ai" in lower or "iweaver team" in lower or "iweaver 客户" in lower:
            break

        collected.append(content)

    return _clean_reply_text("\n".join(collected))


def _quote_depth(line: str) -> int:
    count = 0
    for ch in line.lstrip():
        if ch == ">":
            count += 1
        elif ch.isspace():
            continue
        else:
            break
    return count


def _strip_quote_prefix(line: str) -> str:
    return re.sub(r"^\s*(>\s*)+", "", line).strip()


def _clean_reply_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _is_reply_marker(lower_line: str) -> bool:
    if any(marker in lower_line for marker in _REPLY_MARKER_SUBSTRINGS):
        return True
    # Common Gmail style: "On Thu, Jun 18, 2026 at 12:05 PM <x@y> wrote:"
    if lower_line.startswith("on ") and lower_line.endswith("wrote:"):
        return True
    # Common Spanish Gmail style: "El jue, ... escribió:"
    if lower_line.startswith("el ") and ("escribió:" in lower_line or "escribio:" in lower_line):
        return True
    # Common German Gmail styles:
    # "Sabrina <x@y> schrieb am Do., 2. Juli 2026, 20:14:"
    # "Am Do., 2. Juli 2026 um 20:14 Uhr schrieb Sabrina <x@y>:"
    if " schrieb am " in lower_line or lower_line.startswith("am ") and " schrieb" in lower_line:
        return True
    return False


def detect_language(text: str) -> str:
    """Small deterministic language detector for support auto-replies.

    Returns: zh, es, de, or en. The fallback is English.
    """
    if not text:
        return "en"

    compact = f" {text.strip().lower()} "
    cjk_count = sum(1 for ch in compact if "一" <= ch <= "鿿")
    alpha_count = sum(1 for ch in compact if ch.isalpha())
    if cjk_count >= 2 and cjk_count >= max(2, alpha_count * 0.2):
        return "zh"

    if any(marker in compact for marker in _SPANISH_MARKERS):
        return "es"
    if re.search(r"[áéíóúñ¿¡]", compact):
        return "es"

    if any(marker in compact for marker in _GERMAN_MARKERS):
        return "de"
    if re.search(r"[äöüß]", compact):
        return "de"

    return "en"
