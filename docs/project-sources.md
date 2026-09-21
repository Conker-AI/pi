# Project source resolution

Project owner routes now resolve conversation and task references directly from
Pi's database. Task origins come from stored task associations, not request labels.
Labels, archival state, and privacy are refreshed on each read. Privacy includes
both current settings and immutable historical message privacy; turning incognito
off does not make earlier private content eligible for project context.

Missing or forgotten sessions are unavailable. Closed/forked conversations and
archived tasks are excluded from project context selection. These operations return
reference metadata; they do not copy transcripts, grant access, or dispatch models.

Session settings accept an optional `projectId`. Active-project validation happens
at selection and submission; the selected instructions and project revision are
frozen in the submission/turn snapshot. History assembly includes these owner
instructions between agent and session instructions. Later project edits cannot
change a running turn. Selection does not import linked transcripts, memory or
grants, including when privacy modes are enabled.

File references remain unavailable until the attachment service establishes real
file provenance. Runtime linked-content selection remains outstanding.
