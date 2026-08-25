"""Pure-Python HTML -> PDF fallback for exports.

The primary exporter is WeasyPrint, which reproduces the on-screen HTML/CSS
with high fidelity.  WeasyPrint, however, links against system libraries
(pango/cairo/gdk-pixbuf) that are frequently unavailable on shared hosts such
as PythonAnywhere, and on those hosts every download used to silently degrade
to an ``.html`` attachment - the user asked for a PDF and received a web page.

This module removes that failure mode.  It renders a genuine PDF with
ReportLab (already a declared dependency, pure Python, no system libraries) by
walking the same rendered HTML and laying out its text and tables.

Accuracy contract:
* Every number is copied **verbatim** from the rendered HTML.  Nothing here
  recomputes, re-rounds or reformats a value, so a PDF produced by this path
  always shows the same figures as the screen and as the WeasyPrint path.
* Tables are emitted as real PDF tables so ledger columns (debit / credit /
  running balance) stay aligned and complete.
* If even this path fails, the caller decides the response; this module never
  pretends success.
"""
from __future__ import annotations

import io
import logging
import os
import re
from html import unescape
from html.parser import HTMLParser

log = logging.getLogger(__name__)

# Same geometry the WeasyPrint path forces: 14.8cm x 21cm with 1cm margins.
PAGE_WIDTH_CM = 14.8
PAGE_HEIGHT_CM = 21.0
MARGIN_CM = 1.0

# Block-level tags that terminate the current inline run.
_BLOCK_TAGS = {
    'p', 'div', 'section', 'article', 'header', 'footer', 'li', 'ul', 'ol',
    'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre',
    'tr', 'table', 'thead', 'tbody', 'tfoot', 'dl', 'dt', 'dd', 'figcaption',
}
_HEADING_TAGS = {'h1': 13, 'h2': 12, 'h3': 11, 'h4': 10.5, 'h5': 10, 'h6': 9.5}
_SKIP_CONTENT_TAGS = {'script', 'style', 'noscript', 'svg', 'canvas', 'head'}
_INLINE_SEP_TAGS = {'br', 'hr'}

# Application chrome that must never reach an exported document.  These mirror
# the selectors the app already hides under ``@media print`` in layout.html, so
# the ReportLab path and the WeasyPrint print-media path agree.
_SKIP_CLASS_TOKENS = {
    'sidebar', 'sidebar-overlay', 'mobile-header', 'modal', 'modal-backdrop',
    'loading-overlay', 'd-print-none', 'toast-container', 'offcanvas',
}
_SKIP_ID_TOKENS = {
    'sidebar', 'sidebarOverlay', 'loadingOverlay',
}

# Elements with no end tag.  They must not deepen the skip nesting counter, or
# the counter could never return to zero and the rest of the document would be
# discarded.
_VOID_TAGS = {
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
    'meta', 'param', 'source', 'track', 'wbr',
}


def _is_chrome(attrs):
    """True when this element and its subtree should be excluded from the PDF."""
    for key, value in attrs:
        if key == 'class':
            tokens = set((value or '').split())
            if tokens & _SKIP_CLASS_TOKENS:
                return True
        elif key == 'id' and value in _SKIP_ID_TOKENS:
            return True
    return False

_WS = re.compile(r'\s+')


def _clean(text):
    return _WS.sub(' ', unescape(text or '')).strip()


class _HtmlToFlowables(HTMLParser):
    """Collects headings, paragraphs and tables from rendered HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []          # list of ('text', str, style) | ('table', rows)
        self._skipping = False    # inside a subtree excluded from the PDF
        self._skip_depth = 0      # nesting depth remaining inside that subtree
        self._buf = []            # current inline text buffer
        self._style = 'body'
        self._in_table = False
        self._rows = []
        self._row = None
        self._cell = None
        self._cell_colspan = 1
        self._cell_header = False

    # -- buffer handling -------------------------------------------------
    def _flush(self, style=None):
        text = _clean(' '.join(self._buf))
        self._buf = []
        if text:
            self.blocks.append(('text', text, style or self._style))

    # -- parser events ---------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skipping:
            # Inside a skipped subtree: only count elements that will emit an
            # end tag, otherwise the counter can never return to zero.
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in _SKIP_CONTENT_TAGS or _is_chrome(attrs):
            self._skipping = True
            self._skip_depth = 1
            return

        ad = dict(attrs)
        if tag in _HEADING_TAGS:
            self._flush()
            self._style = tag
        elif tag == 'table':
            self._flush()
            self._in_table = True
            self._rows = []
        elif tag == 'tr':
            self._row = []
        elif tag in ('td', 'th'):
            self._cell = []
            self._cell_header = (tag == 'th')
            try:
                self._cell_colspan = max(1, int(ad.get('colspan') or 1))
            except (TypeError, ValueError):
                self._cell_colspan = 1
        elif tag in _INLINE_SEP_TAGS:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        # Self-closing form (<img/>, <br/>): no separate end tag will arrive.
        tag = tag.lower()
        if self._skipping:
            return
        if tag in _SKIP_CONTENT_TAGS or _is_chrome(attrs):
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skipping:
            self._skip_depth -= 1
            if self._skip_depth <= 0:
                self._skipping = False
                self._skip_depth = 0
            return

        if tag in ('td', 'th') and self._row is not None:
            text = _clean(' '.join(self._cell or []))
            self._cell = None
            self._row.append((text, self._cell_colspan, self._cell_header))
        elif tag == 'tr' and self._row is not None:
            if any(c[0] for c in self._row):
                self._rows.append(self._row)
            self._row = None
        elif tag == 'table':
            self._in_table = False
            if self._rows:
                self.blocks.append(('table', self._rows))
            self._rows = []
        elif tag in _HEADING_TAGS:
            self._flush(style=tag)
            self._style = 'body'
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._skipping:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self._buf.append(data)

    def close(self):
        self._flush()
        super().close()


def _parse_html(html):
    parser = _HtmlToFlowables()
    try:
        parser.feed(html or '')
        parser.close()
    except Exception:
        log.exception('HTML parsing for PDF fallback was interrupted')
    return parser.blocks


def _build_story(blocks, body_font, bold_font, small_size):
    """Translate parsed blocks into ReportLab flowables."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    normal = ParagraphStyle(
        'pdfBody', fontName=body_font, fontSize=8.2, leading=10.4,
        spaceAfter=3, wordWrap='LTR',
    )
    small = ParagraphStyle(
        'pdfSmall', parent=normal, fontSize=small_size, leading=small_size + 1.6,
        textColor=colors.HexColor('#444444'),
    )
    heading_styles = {
        tag: ParagraphStyle(
            f'pdf{tag}', fontName=bold_font, fontSize=size, leading=size + 2.4,
            spaceBefore=6, spaceAfter=3, textColor=colors.HexColor('#111111'),
        )
        for tag, size in _HEADING_TAGS.items()
    }
    cell = ParagraphStyle(
        'pdfCell', fontName=body_font, fontSize=7.4, leading=9.0, wordWrap='LTR',
    )
    cell_bold = ParagraphStyle('pdfCellBold', parent=cell, fontName=bold_font)

    story = []
    for block in blocks:
        if block[0] == 'text':
            _kind, payload, style = block
            if style in heading_styles:
                story.append(Paragraph(payload, heading_styles[style]))
            elif style == 'small':
                story.append(Paragraph(payload, small))
            else:
                story.append(Paragraph(payload, normal))
        else:
            rows = block[1]
            width = max((sum(c[1] for c in r) for r in rows), default=1) or 1
            data = []
            for r in rows:
                out = []
                for text, colspan, is_header in r:
                    out.append(Paragraph(text, cell_bold if is_header else cell))
                    for _ in range(colspan - 1):
                        out.append('')
                while len(out) < width:
                    out.append('')
                data.append(out[:width])
            if not data:
                continue
            avail = (PAGE_WIDTH_CM - 2 * MARGIN_CM) * cm
            try:
                col_w = [avail / width] * width
                tbl = Table(data, colWidths=col_w, repeatRows=1 if _looks_tabled(rows) else 0)
            except Exception:
                tbl = Table(data, repeatRows=0)
            tbl.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#9aa0a6')),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eceff1')),
                ('LEFTPADDING', (0, 0), (-1, -1), 3),
                ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                ('TOPPADDING', (0, 0), (-1, -1), 2),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ]))
            story.append(Spacer(1, 3))
            story.append(tbl)
            story.append(Spacer(1, 3))
    if not story:
        story.append(Paragraph('No printable content.', normal))
    return story


def _looks_tabled(rows):
    return bool(rows) and any(c[2] for c in rows[0])


def render_html_to_pdf(html, download_name='document.pdf', title='AMS Report'):
    """Render *html* into a real PDF.  Returns bytes, or ``None`` on failure.

    Raises nothing: callers fall back to their own behaviour when this returns
    ``None``, but the intent is that this path succeeds wherever ReportLab is
    importable, so a PDF is always delivered.
    """
    try:
        from reportlab.lib.units import cm
        from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate
    except Exception:
        log.warning('ReportLab unavailable; cannot build fallback PDF.')
        return None

    try:
        blocks = _parse_html(html)
        # Prefer Helvetica (always present in ReportLab); DejaVu is optional.
        body_font, bold_font, small_size = 'Helvetica', 'Helvetica-Bold', 7.4
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            candidates = [
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            ]
            if all(os.path.exists(p) for p in candidates):
                pdfmetrics.registerFont(TTFont('DejaVu', candidates[0]))
                pdfmetrics.registerFont(TTFont('DejaVu-Bold', candidates[1]))
                body_font, bold_font = 'DejaVu', 'DejaVu-Bold'
        except Exception:
            pass

        story = _build_story(blocks, body_font, bold_font, small_size)

        buf = io.BytesIO()
        pagesize = (PAGE_WIDTH_CM * cm, PAGE_HEIGHT_CM * cm)
        doc = BaseDocTemplate(
            buf, pagesize=pagesize, title=title,
            leftMargin=MARGIN_CM * cm, rightMargin=MARGIN_CM * cm,
            topMargin=MARGIN_CM * cm, bottomMargin=MARGIN_CM * cm,
        )
        frame = Frame(
            MARGIN_CM * cm, MARGIN_CM * cm,
            (PAGE_WIDTH_CM - 2 * MARGIN_CM) * cm,
            (PAGE_HEIGHT_CM - 2 * MARGIN_CM) * cm,
            id='main',
        )

        def _decorate(canv, _doc):
            canv.saveState()
            canv.setFont(body_font, 6.5)
            canv.setFillColorRGB(0.35, 0.35, 0.35)
            canv.drawString(MARGIN_CM * cm, 0.55 * cm, title)
            canv.drawRightString(
                (PAGE_WIDTH_CM - MARGIN_CM) * cm, 0.55 * cm,
                f'Page {_doc.page}',
            )
            canv.restoreState()

        doc.addPageTemplates([PageTemplate(id='main', frames=[frame], onPage=_decorate)])
        doc.build(story)
        data = buf.getvalue()
        if not data.startswith(b'%PDF-'):
            log.error('ReportLab produced a non-PDF payload; refusing to serve it.')
            return None
        return data
    except Exception:
        log.exception('ReportLab fallback PDF generation failed.')
        return None
