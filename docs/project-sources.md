# Project source resolution

Project owner routes now resolve conversation and task references directly from
Pi's database. Task origins come from stored task associations, not request labels.
Labels, archival state, and privacy are refreshed on each read. Privacy includes
both current settings and immutable historical message privacy; turning incognito
off does not make earlier private content eligible for project context.

Missing or forgotten sessions are unavailable. Closed/forked conversations and
archived tasks are excluded from project context selection. These operations return
reference metadata; they do not copy transcripts, grant access, or dispatch models.

File references remain unavailable until the attachment service establishes real
file provenance. Runtime project instruction/content assembly is still outstanding.
