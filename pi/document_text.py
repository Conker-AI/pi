"""Bounded DOCX body text extraction; no relationships, macros or external reads."""

import io
import zipfile
from xml.etree import ElementTree

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_XML = 4 * 1024 * 1024


def docx(raw, max_characters):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or len({entry.filename for entry in entries}) != len(entries):
            raise ValueError("Invalid document archive")
        entry = archive.getinfo("word/document.xml")
        if entry.file_size > MAX_XML or entry.flag_bits & 1:
            raise ValueError("Unsupported document body")
        with archive.open(entry) as body:
            xml = body.read(MAX_XML + 1)
        if len(xml) > MAX_XML:
            raise ValueError("Document body limit")
    text = xml.decode("utf-8-sig")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("Unsupported XML declarations")
    root = ElementTree.fromstring(text)
    if root.tag != W + "document":
        raise ValueError("Invalid document root")
    body = root.find(W + "body")
    if body is None:
        raise ValueError("Missing document body")
    paragraphs, count = [], 0
    for paragraph in body.iter(W + "p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == W + "t":
                parts.append(node.text or "")
            elif node.tag == W + "tab":
                parts.append("\t")
            elif node.tag in (W + "br", W + "cr"):
                parts.append("\n")
        value = "".join(parts)
        count += len(value) + bool(paragraphs)
        if count > max_characters:
            raise ValueError("Extracted text limit")
        paragraphs.append(value)
    return "\n".join(paragraphs)
