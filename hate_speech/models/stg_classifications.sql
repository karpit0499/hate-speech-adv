select
    message_id,
    input_text,
    label,
    confidence,
    rationale,
    model_version,
    created_at
from {{ source('moderation', 'classifications_raw') }}
where label is not null
