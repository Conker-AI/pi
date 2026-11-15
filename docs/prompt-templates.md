# Prompt templates

A template is a named form for one kind of request: a list of fields the composer shows and
compiles into a prompt, and the wand can fill in from the owner's rough draft. It is not a skill
and not an agent; nothing here sends a message. The owner keeps his templates here, with four
starters (Research, Bug report, Plan, Email) that are his to edit or delete.

Routes (owner session or admin key, no password, because a template grants nothing):

| Route | Does |
|---|---|
| `GET /prompts/templates` | every template (`items`, `total`): the starters first, then his own, newest first |
| `GET /prompts/templates/{id}` | one template, as `{"template": ...}` |
| `POST /prompts/templates` | create one (`name`, `description`, `fields`); at most 100 in all, then 409 `too_many` |
| `POST /prompts/templates/{id}` | replace its name, description and fields; `expected_revision` is required, a stale one is 409 `revision_conflict` |
| `POST /prompts/templates/{id}/delete` | delete it, a starter too; `expected_revision` is optional |

CLI: `conker prompt templates list|show|add|remove`, and `conker prompt enhance --template ID`.

A template (`Template`): `id`, `name` (1-60), `description` (up to 200), `fields` (1-12), `starter`
(`true` for the four seeded ones, even after an edit), `revision`, `createdAt`, `updatedAt`. His own
ids are `tpl_` and 32 hex digits; the starters are `starter-research`, `starter-bug-report`,
`starter-plan` and `starter-email`. An unknown or malformed id is 404 `template_not_found`.

A field (`TemplateField`): `id` (a letter, then letters, digits or `_`, up to 32, unique in the
template), `label` (1-40, one line), `type`, `required` (default false), `multiple` (default false,
only for `choice`), and when set `example` and `help` (up to 200) and `options` (`choice` only,
1-20 distinct texts of up to 60). `required` and `multiple` are always in a reply; the rest only
when set. Types: `shortText`, `longText`, `list`, `choice`, `number`, `date`, `switch`. Hidden
characters are removed from every text.

## The starters

Seeded once into a new database. A marker row records that, so a starter he deletes does not come
back after a restart, and one he edits keeps his edit.

## Filling a template with the wand

`POST /prompts/enhance` with `templateId` fills that template from the draft instead of rewriting
it. It is the same call as the rewrite (`docs/prompt-enhance.md`): one drafting-role model call,
counted and stopped by the daily spending cap, refused when the chat has helper models off,
nothing saved, nothing sent.

- The model gets fixed instructions and, as data, the template (name, description, each field's
  id, label, type, options, example, help) and the draft. It returns `{"fields": {id: value},
  "notes": [...]}`. The instructions tell it to use only what the draft says, to leave out a
  field the draft does not cover and not to follow instructions inside the draft or the template.
- Pi checks every value against its field and drops what does not fit; it never repairs a value.
  Text is cut at its limit (short text 200, long text 4000, 200 per list item, 20 items); a number
  must look like one; a date must be a real `YYYY-MM-DD`; a `choice` must be one of the options
  (any case, saved with the option's own spelling), several only when `multiple`; a `switch` must
  be true or false; unknown field ids are dropped.
- Pi builds the prompt text, not the model, in exactly the format the dashboard's `compilePrompt`
  builds: `<Name> request`, then one block per filled field in template order (`Label: value`,
  a long text as `Label:` and the text on the next line, a list as `Label:` and `- item` lines, a
  switch that is on as `Label: Yes`), blocks separated by a blank line, and nothing for an empty
  field.

The reply (`EnhancedPrompt`) has `text` (that prompt), `notes`, `modelId`, `templateId`, `fields`
(each accepted value by field id: a text, a list of texts or a boolean) and `missing` (the labels
of required fields still empty). Refusals besides the rewrite's: 404 `template_not_found`, 422
`nothing_filled` (no field of the template matched the draft), 502 `unusable` (not a JSON object
with `fields`, or a prompt longer than 16000 characters).

## Data

Table `prompt_templates` (`id`, `name`, `description`, `fields` JSON, `starter`, `revision`,
`created_at`, `updated_at`) and the marker table `prompt_template_seed`. Templates are the owner's
configuration, not chat text: forgetting a chat or emptying the Trash never touches them.
