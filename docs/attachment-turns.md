# Attachments in conversation turns

Turn requests accept up to five distinct `attachment_ids`. Reservation validates
that files belong to the requested session, have supported plaintext extraction,
and do not require stricter privacy than the conversation. Attached text is bounded
to 32,000 characters per submission. IDs participate in the submission identity;
replays cannot substitute another file set or repeat provider work.

Pending reservations prevent removing an upload. Binding the input message and
its immutable attachment references happens in one transaction; failed/interrupted
preparation releases reservations. History assembly adds selected messages' files
as untrusted source text, including IDs and names, and rechecks privacy/integrity.
Context exclusions therefore also exclude that message's attachments. File text is
not copied into user transcripts or automatically ingested into MemoryGate.
Message reads return bound attachment metadata in the same database snapshot as
the message. Forgotten files retain an unavailable reference without their name
or bytes, so reopening history cannot revive removed content.

This path supports UTF-8 plain text/Markdown/CSV/JSON, DOCX body text, and PDF text
layers. PDF extraction uses pypdf 6.18.1 in a short-lived isolated Python process:
10 MiB input, 200 pages, 200,000 extracted characters, 16 MiB aggregate page content
streams, a ten-second deadline, and at most two workers per Pi process. Linux workers
also apply a 512 MiB address-space ceiling and eight-second CPU limit. Windows uses
parser/deadline limits; it does not have an OS memory ceiling. This is parser isolation,
not a general code-execution sandbox. No document actions/scripts or embedded files
are executed, and the worker receives no application credentials in its environment.

PDF page labels remain in the extracted text and its immutable citation passages.
Blank/image-only pages in mixed documents are labeled explicitly. Files without a
text layer, encrypted files, malformed documents and extraction-limit failures return
clear unsupported status while remaining downloadable. Extraction does not provide
OCR, faithful visual layout, table reconstruction, or image understanding. The same
privacy, input-size, exact-source, restart and forgetting rules apply to PDF passages.
Existing unsupported uploads are not silently rewritten; upload again after upgrading.

Other uploads remain downloadable and return an explicit unsupported-input error if
submitted to a model. Automatic
forking with pending uploads is rejected: create the fork and upload to its new
session first. Browser gateway/frontend wiring remains deferred.
