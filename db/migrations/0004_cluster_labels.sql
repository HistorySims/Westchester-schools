-- Add auto-generated descriptive labels to clusters

alter table clusters add column if not exists label_text text;
