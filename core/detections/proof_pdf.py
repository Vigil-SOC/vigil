"""A proof card as a PDF people can read and print: a small text-only writer.

It lays out lines of Helvetica on A4 pages and nothing else, so the report
needs no PDF library. Characters outside Windows-1252 print as "?".
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

PAGE_W, PAGE_H = 595, 842  # A4 in points
MARGIN = 48
# Helvetica's average glyph width as a share of the font size, for wrapping.
AVG_WIDTH = 0.52

# A line: (text, size, bold, indent in points).
Line = Tuple[str, float, bool, float]


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def wrap(text: str, size: float, indent: float = 0.0) -> List[str]:
    """``text`` split into lines that fit the page width at ``size``."""
    width = int((PAGE_W - 2 * MARGIN - indent) / (size * AVG_WIDTH))
    out: List[str] = []
    for para in str(text).splitlines() or [""]:
        words, line = para.split(" "), ""
        for word in words:
            while len(word) > width:  # a long hash or URL: break it
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            candidate = f"{line} {word}" if line else word
            if len(candidate) > width:
                out.append(line)
                line = word
            else:
                line = candidate
        out.append(line)
    return out


def render(lines: Iterable[Line], footer: str) -> bytes:
    """A PDF of ``lines`` top to bottom, a new page when one fills, each page
    ending with ``footer`` and its page number."""
    pages: List[List[Tuple[float, float, str, float, bool]]] = [[]]
    y = PAGE_H - MARGIN
    for text, size, bold, indent in lines:
        for part in wrap(text, size, indent):
            lead = size * 1.4
            if y - lead < MARGIN + 24:
                pages.append([])
                y = PAGE_H - MARGIN
            y -= lead
            pages[-1].append((MARGIN + indent, y, part, size, bold))
    streams = []
    total = len(pages)
    for n, page in enumerate(pages, 1):
        ops = [b"BT"]
        for x, py, text, size, bold in page:
            font = b"/F2" if bold else b"/F1"
            ops.append(
                b"%s %.1f Tf 1 0 0 1 %.1f %.1f Tm (%s) Tj"
                % (font, size, x, py, _escape(text))
            )
        ops.append(
            b"/F1 7 Tf 1 0 0 1 %d %d Tm (%s) Tj"
            % (MARGIN, MARGIN - 12, _escape(f"{footer} · page {n} of {total}"))
        )
        ops.append(b"ET")
        streams.append(b"\n".join(ops))

    # Objects: 1 catalog, 2 pages, 3 Helvetica, 4 Helvetica-Bold, then a page
    # and its content stream per page.
    objects: List[bytes] = []
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(total)).encode()
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, total))
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )
    for i, stream in enumerate(streams):
        content = 6 + 2 * i
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
            % (PAGE_W, PAGE_H, content)
        )
        objects.append(
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)
