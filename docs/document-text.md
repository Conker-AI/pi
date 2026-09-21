# Document text model input

Extracted sources are split into stable 1200-character passages with attachment-bound
IDs, Unicode-codepoint offsets and excerpt SHA-256 hashes. The model receives these
IDs alongside the exact text. Owner-authenticated GET
`/sessions/{session_id}/attachments/{identity}/passages/{index}` resolves a passage
through current integrity, privacy and availability checks, with no-store responses.
Removal makes references unavailable. Offsets describe extracted text, not DOCX pages.
This provides inspectable source references; it does not yet validate free-form model
citation syntax or prove the model used a supplied source.

Attachment extraction supports UTF-8 plain text, Markdown, CSV and JSON as inert
source text. JSON is not executed and CSV formulas are not evaluated. Declared
DOCX files support bounded `word/document.xml` body text: paragraphs, table-cell
paragraphs, tabs and line breaks. This is text extraction, not visual rendering.
Images, headers, footnotes, pagination and document layout are not extracted.

DOCX uses no external relationships, macros, filesystem extraction or network
requests. Duplicate archive entries, encrypted body entries, over 1000 members,
body XML over 4 MiB, XML entity/DOCTYPE declarations, invalid UTF-8/XML and text
over the existing 200000-character bound are unsupported. Existing submission
limits still require at most 32000 extracted characters across five attachments.

Bytes retain existing immutable session/message binding, privacy checks and
removal/forgetting behavior. Old unsupported attachment records are not silently
rewritten; re-upload to process using the new extractor. PDF/OCR and multimodal
image input remain unsupported. No document parser claims complete DOCX fidelity.

38 focused document/attachment/submission tests passed using generated archives,
temporary SQLite and recorder providers. No owner document or paid model was used.
