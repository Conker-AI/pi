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

This path supports UTF-8 plaintext only. Other uploads remain downloadable and
return an explicit unsupported-input error if submitted to a model. Automatic
forking with pending uploads is rejected: create the fork and upload to its new
session first. Browser gateway/frontend wiring remains deferred.
