
"""
Conservative identity of registered source documents.

Pure functions: no database access. Used to decide when several
registered sources are really one document, or share a publisher,
so that agreement among them is not mistaken for independent
corroboration.

Document identity errs towards "same document": treating two
registrations as one document can only withhold corroboration,
never create it. It never assumes that dropping a query parameter
preserves identity, except for known tracking parameters.
"""

import re
import unicodedata
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit

# Query parameters that only track referrals (D5).
TRACKING_PARAMETERS = {"fbclid", "gclid", "msclkid", "mc_eid"}

DEFAULT_PORTS = {"http": 80, "https": 443}


def is_tracking_parameter(name):
    key = unquote_plus(name).lower()
    return key.startswith("utm_") or key in TRACKING_PARAMETERS


def url_key(url):
    """
    Canonical form of a document URL, or None if there is none.

    Scheme and host are case-insensitive and http/https are treated
    alike; default ports and the fragment (a position within the
    document) are dropped, as are tracking parameters. The path and
    every other query parameter, with its value, are kept: they can
    identify a different document. Remaining parameters are sorted.
    """

    if not url or not str(url).strip():
        return None

    parts = urlsplit(str(url).strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()

    if not host:
        return str(url).strip()

    port = parts.port
    if port is not None and port != DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"

    if scheme in ("http", "https"):
        scheme = "http(s)"

    query = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking_parameter(name)
    )

    return f"{scheme}://{host}{parts.path or '/'}" + (
        f"?{urlencode(query)}" if query else ""
    )


def publisher_key(publisher):
    """
    Publisher compared without case, accents, spacing or punctuation
    ("SPP Infrastructure, a.s." equals "SPP infrastructure a. s."),
    or None if unknown. Different keys do not mean independence.
    """

    if publisher is None:
        return None

    text = unicodedata.normalize("NFKD", str(publisher))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[\W_]+", "", text.casefold())
    return text or None


def document_keys(sources):
    """
    Map source_id to a document key. Registrations with the same URL
    key, or with the same title, publisher and publication date (all
    recorded), are one document, transitively. Sources without either
    keep a key of their own.
    """

    parent = {}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(a, b):
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    ids = []
    for source in sources:
        source_id = int(source["source_id"])
        ids.append(source_id)
        node = ("source", source_id)
        parent.setdefault(node, node)

        url = url_key(source.get("source_url"))
        if url:
            union(node, ("url", url))

        publisher = publisher_key(source.get("publisher"))
        title = source.get("document_title")
        if title and publisher and source.get("publication_date"):
            union(node, ("signature", str(title).strip().casefold(),
                         publisher, str(source["publication_date"])))

    # Name each document by its lowest source ID.
    members = {}
    for source_id in ids:
        members.setdefault(find(("source", source_id)), []).append(source_id)

    return {
        source_id: f"document:{min(group)}"
        for group in members.values()
        for source_id in group
    }
