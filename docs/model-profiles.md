# Model profiles

Model profiles provide read-only statistics about each configured model, computed entirely from what Pi has recorded during actual use. No values are estimated, stored separately, or used for model selection.

## What each number means

**Configuration revision** - The current revision number of the model role configuration. When models are added, removed, or changed, this number increments.

**Answers** - Count of completed turns where this model provided the final answer. Only turns with `status='complete'` count; failed or interrupted turns are excluded.

**Latency** - Response time measurements in milliseconds:
- `median` - Middle value of all recorded latencies for this model (nearest-rank method)
- `p90` - 90th percentile, meaning 90% of responses were this fast or faster
- `null` when no latency data exists

**Cost** - Financial cost measurements in USD:
- `total` - Sum of all recorded costs for this model
- `perAnswer` - Average cost per completed answer, or `null` when no costs recorded

**Tokens** - Token usage counts:
- `input` - Total input tokens consumed
- `output` - Total output tokens generated

**Judge scores** - Quality metrics from blind judge evaluations:
- `judged` - Number of answers that received blind judge scores
- `averageScore` - Mean of all judge scores (1-10 scale), rounded to 2 decimals
- `wins` - Times this model's answer was selected as the winner by the judge

**Owner votes** - Direct feedback from the owner:
- `up` - Count of upvotes received
- `down` - Count of downvotes received  
- `winner` - Count of times selected as the best answer

## Data sources

All statistics come from Pi's recorded activity:

- Turn data from the `turns` table (completed turns only)
- Quality scores from blind judge evaluations in `response_judgements`
- Owner votes from `response_votes`
- Message-to-turn relationships from `turn_messages`

## Exclusions

- Forgotten chats and their data never count toward profiles
- Failed or interrupted turns are excluded from all metrics
- Redacted messages are excluded from quality calculations
- Messages in excluded conversations don't affect statistics

## Read-only nature

Profiles are computed on-demand from existing data. They never:
- Store estimated or guessed values
- Influence model selection or routing
- Persist any calculations
- Modify any existing data

Models with no recorded activity show zero counts and `null` averages rather than invented values.