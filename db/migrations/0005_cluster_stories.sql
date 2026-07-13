-- Cache "what's the story" answers per cluster

alter table clusters
  add column if not exists story_text text,
  add column if not exists story_citations jsonb,
  add column if not exists story_generated_at timestamptz;
