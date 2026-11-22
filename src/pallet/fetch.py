"""The one place pallet touches the network.

Every capture goes through :func:`fetch_url`. Tests replace it; it also reads
``file://`` URLs so local files enter custody the same way remote ones do.
Bytes are kept exactly as served: no decompression is requested, no decoding
is applied.
"""

from __future__ import annotations

import email.utils
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from pallet import TOOL
from pallet.errors import FetchError

#: Response headers recorded in a receipt. Others are dropped as noise.
KEPT_HEADERS = (
    "content-type",
    "content-length",
    "content-encoding",
    "content-disposition",
    "last-modified",
    "etag",
    "date",
)

USER_AGENT = TOOL


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def _kept(headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in KEPT_HEADERS:
        value = headers.get(name)
        if value is not None:
            out[name] = str(value)
    return out


def _fetch_file(url: str) -> FetchResult:
    path = Path(url2pathname(urlparse(url).path))
    try:
        body = path.read_bytes()
        mtime = path.stat().st_mtime
    except FileNotFoundError as exc:
        raise FetchError(f"file not found: {url}", url=url) from exc
    except OSError as exc:
        raise FetchError(f"cannot read {url}: {exc}", url=url) from exc
    headers = {
        "content-length": str(len(body)),
        "last-modified": email.utils.formatdate(mtime, usegmt=True),
    }
    return FetchResult(url=url, final_url=url, status=200, body=body, headers=headers)


def fetch_url(url: str, timeout: float = 60.0) -> FetchResult:
    """Retrieve ``url`` and return its bytes, status, and identifying headers."""
    scheme = urlparse(url).scheme
    if scheme == "file":
        return _fetch_file(url)
    if scheme not in ("http", "https"):
        raise FetchError(f"unsupported URL scheme {scheme!r}: {url}", url=url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return FetchResult(
                url=url,
                final_url=response.geturl(),
                status=response.status,
                body=body,
                headers=_kept(response.headers),
            )
    except urllib.error.HTTPError as exc:
        body = exc.read() or b""
        return FetchResult(
            url=url, final_url=exc.geturl() or url, status=exc.code, body=body,
            headers=_kept(exc.headers or {}),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"could not retrieve {url}: {exc}", url=url) from exc
