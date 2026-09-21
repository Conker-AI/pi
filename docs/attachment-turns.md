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

## Image input

Explicit message attachments can supply still PNG, JPEG and WebP bytes to vision
models through Ollama, OpenRouter, direct OpenAI and direct Anthropic adapters.
Validation checks the actual raster format, single-frame status, successful decode,
5 MiB size, 8192-pixel side limit and 4,194,304 total pixels. Original bytes are sent
as base64; Pi never fetches image URLs, executes SVG, or substitutes a text filename
for the image. Raster validation does not interpret the image or provide OCR.

Images retain their original attachment/session identity. History exclusion removes
the corresponding images from subsequent input; exact response replay reloads the
original attachments with current privacy/integrity checks. Text summaries do not
describe image contents. No base64 is copied into message text or frozen instruction
prefixes. Image sources do not gain fabricated text-passage citations. Extraction
status remains unsupported for text extraction, with a reason identifying vision
availability; that status does not mean the image was interpreted by a model.

Unknown adapters refuse image input instead of silently ignoring it. OpenRouter
additionally checks its model catalogue's image-input modality. Other providers can
reject unsupported models at their API; adapter support does not imply every model
supports vision. The normal paid-provider opt-in and fallback policy still apply.
Requests are bounded to 20 contextual images and 25 MiB original bytes (base64
equivalent). Context estimates reserve 4096 tokens per image, an explicit heuristic,
not a provider-specific token count or a billing estimate. Provider usage remains
the source of recorded actual tokens/cost. No live paid image calls were performed.

Provider wire formats follow the official [OpenAI image guide](https://developers.openai.com/api/docs/guides/images-vision),
[Anthropic vision guide](https://platform.claude.com/docs/en/build-with-claude/vision),
[OpenRouter image guide](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding)
and [Ollama vision guide](https://docs.ollama.com/capabilities/vision).
