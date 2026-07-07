with base as (
    select date(created_at) as day, label, confidence
    from {{ ref('stg_classifications') }}
)
select
    day,
    label,
    count(*)                                   as message_count,
    round(avg(confidence), 3)                  as avg_confidence,
    countif(label = 'hate_speech')             as hate_count,
    round(safe_divide(countif(label = 'hate_speech'), count(*)), 3) as hate_rate
from base
group by day, label
order by day desc
