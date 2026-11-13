# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""What kind of file is this? Decided by the content, never by the name or the announced type."""

ALLOWED = {"pdf": "application/pdf", "jpeg": "image/jpeg", "png": "image/png"}
HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif")


def detect(raw):
    """``'pdf'``, ``'jpeg'``, ``'png'``, ``'heic'`` (not supported) or None."""
    head = bytes(raw[:16]) if raw else b""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[4:8] == b"ftyp" and head[8:12] in HEIC_BRANDS:
        return "heic"
    return None
