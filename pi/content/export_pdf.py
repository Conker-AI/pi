"""PDF documents drawn from markdown blocks with ReportLab; fonts are local, nothing is fetched.

DejaVu (Debian's fonts-dejavu-core, installed in the image) covers Latin, Cyrillic and Greek;
right-to-left lines are not reordered. Without it ReportLab's own Vera fonts are used, which
cover Latin only; a letter a font lacks draws as an empty box, never as an error.
"""

from __future__ import annotations

import io
from pathlib import Path
from xml.sax.saxutils import escape

from pi.content.markdown_blocks import Block, ExportLimit, Span

MAX_PAGES = 500
ROWS_PER_TABLE = 40  # Long tables are drawn as stacked parts, which keeps layout linear.
DEPTH = 8
STEP = 16  # Points per list or quote level.
DEJAVU = Path("/usr/share/fonts/truetype/dejavu")
FACES = {
    "ConkerSans": ("DejaVuSans.ttf", "Vera.ttf"),
    "ConkerSans-Bold": ("DejaVuSans-Bold.ttf", "VeraBd.ttf"),
    "ConkerSans-Italic": ("DejaVuSans-Oblique.ttf", "VeraIt.ttf"),
    "ConkerSans-BoldItalic": ("DejaVuSans-BoldOblique.ttf", "VeraBI.ttf"),
    "ConkerMono": ("DejaVuSansMono.ttf", "Vera.ttf"),
    "ConkerMono-Bold": ("DejaVuSansMono-Bold.ttf", "VeraBd.ttf"),
}


def _fonts():
    import reportlab
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if "ConkerSans" in pdfmetrics.getRegisteredFontNames():
        return
    bundled = Path(reportlab.__file__).with_name("fonts")
    # One family throughout: DejaVu when its regular face exists, so a missing italic falls
    # back to DejaVu's upright face rather than to Vera's smaller character set.
    family = (DEJAVU / FACES["ConkerSans"][0]).is_file()
    regular = {"ConkerSans-Italic": "ConkerSans", "ConkerSans-BoldItalic": "ConkerSans-Bold"}
    for name, (dejavu, vera) in FACES.items():
        path = DEJAVU / dejavu
        if family and not path.is_file():
            path = DEJAVU / FACES[regular.get(name, "ConkerSans")][0]
        pdfmetrics.registerFont(TTFont(name, str(path if family else bundled / vera)))
    for face in ("ConkerSans", "ConkerMono"):
        bold = face + "-Bold"
        italic = face + "-Italic" if face == "ConkerSans" else face
        both = face + "-BoldItalic" if face == "ConkerSans" else bold
        for weight, slant, name in ((0, 0, face), (1, 0, bold), (0, 1, italic), (1, 1, both)):
            addMapping(face, weight, slant, name)


def _markup(spans: list[Span]) -> str:
    parts = []
    for span in spans:
        text = escape(span.text).replace("\n", "<br/>")
        if span.code:
            text = f'<font face="ConkerMono">{text}</font>'
        if span.bold:
            text = f"<b>{text}</b>"
        if span.italic:
            text = f"<i>{text}</i>"
        if span.strike:
            text = f"<strike>{text}</strike>"
        if span.href:
            href = escape(span.href, {'"': "&quot;"})
            text = f'<a href="{href}" color="#0563C1"><u>{text}</u></a>'
        parts.append(text)
    return "".join(parts)


class _Styles:
    def __init__(self):
        from reportlab.lib.colors import HexColor
        from reportlab.lib.styles import ParagraphStyle

        self.make, self.cache = ParagraphStyle, {}
        self.body = ParagraphStyle(
            "Body", fontName="ConkerSans", fontSize=10.5, leading=14.5, spaceAfter=6
        )
        self.quote_color = HexColor("#555555")
        sizes = {1: 20, 2: 16, 3: 13.5, 4: 12, 5: 11, 6: 10.5}
        self.headings = {
            level: ParagraphStyle(
                f"H{level}",
                parent=self.body,
                fontName="ConkerSans-Bold",
                fontSize=size,
                leading=size * 1.3,
                spaceBefore=10 if level <= 2 else 6,
                spaceAfter=6,
            )
            for level, size in sizes.items()
        }
        self.code = ParagraphStyle(
            "Code",
            parent=self.body,
            fontName="ConkerMono",
            fontSize=8.5,
            leading=11,
            backColor=HexColor("#F2F2F2"),
            borderPadding=4,
            spaceBefore=4,
            spaceAfter=8,
        )

    def paragraph(self, block: Block, base=None):
        depth = min(block.indent + block.quote, DEPTH)
        key = (id(base), depth, bool(block.marker), bool(block.quote))
        if key not in self.cache:
            look = {"textColor": self.quote_color} if block.quote else {}
            self.cache[key] = self.make(
                f"P{len(self.cache)}",
                parent=base or self.body,
                leftIndent=STEP * depth,
                bulletIndent=STEP * (depth - 1) if block.marker else 0,
                bulletFontName="ConkerSans",
                **look,
            )
        return self.cache[key]

    def cell(self, columns: int, align: str | None, header: bool):
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

        key = ("cell", columns > 8, align, header)
        if key not in self.cache:
            size = 7 if columns > 8 else 9
            self.cache[key] = self.make(
                f"C{len(self.cache)}",
                parent=self.body,
                fontName="ConkerSans-Bold" if header else "ConkerSans",
                fontSize=size,
                leading=size * 1.25,
                spaceAfter=0,
                alignment={"center": TA_CENTER, "right": TA_RIGHT}.get(align, TA_LEFT),
            )
        return self.cache[key]


def _table(block: Block, styles: _Styles, width: float) -> list:
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    columns = len(block.header)

    def row(cells, header):
        return [
            Paragraph(
                _markup(cells[i] if i < len(cells) else []),
                styles.cell(columns, block.align[i] if i < len(block.align) else None, header),
            )
            for i in range(columns)
        ]

    head = row(block.header, True)
    look = TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#A6A6A6")),
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#E8ECEA")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )
    parts, body = [], block.rows or [[]]
    for start in range(0, len(body), ROWS_PER_TABLE):
        rows = [row(cells, False) for cells in body[start : start + ROWS_PER_TABLE] if cells]
        table = Table([head, *rows], colWidths=[width / columns] * columns, repeatRows=1)
        table.setStyle(look)
        parts.append(table)
    parts.append(Spacer(1, 8))
    return parts


def pdf(blocks: list[Block], title: str) -> bytes:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import HRFlowable, Paragraph, Preformatted, SimpleDocTemplate
    from reportlab.platypus.doctemplate import LayoutError

    _fonts()
    styles = _Styles()
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=title,
        author="Conker",
        creator="Conker",
    )
    width = document.width
    story = []
    for block in blocks:
        if block.kind == "heading":
            story.append(Paragraph(_markup(block.spans), styles.headings[block.level]))
        elif block.kind == "paragraph":
            style = styles.paragraph(block)
            story.append(Paragraph(_markup(block.spans), style, bulletText=block.marker))
        elif block.kind == "code":
            style = styles.paragraph(block, styles.code)
            fits = int((width - style.leftIndent - 8) / (0.61 * style.fontSize))
            story.append(Preformatted(block.text.expandtabs(4), style, maxLineLength=fits))
        elif block.kind == "rule":
            story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#A6A6A6")))
        elif block.kind == "table":
            story.extend(_table(block, styles, width))

    over = []

    def page(canvas, doc):
        if doc.page > MAX_PAGES:
            over.append(doc.page)
            raise ExportLimit("pages")
        canvas.saveState()
        canvas.setFont("ConkerSans", 8)
        canvas.setFillColor(HexColor("#808080"))
        canvas.drawCentredString(A4[0] / 2, 1.2 * cm, str(doc.page))
        canvas.restoreState()

    try:
        document.build(story or [Paragraph("", styles.body)], onFirstPage=page, onLaterPages=page)
    except LayoutError:
        raise ExportLimit("layout") from None
    except Exception:
        if over:  # ReportLab rewrites the message of an error raised in a page callback.
            raise ExportLimit("pages") from None
        raise
    return output.getvalue()
