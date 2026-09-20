-- Новости: глянул в мир, в чат — только если отозвалось.
-- Тексты ленты в базу не кладём. Метка news_at — заслонка захода.
-- Род impulses.kind = 'news': предмет — зацепка, не заголовок.

ALTER TABLE agent ADD COLUMN news_at TIMESTAMPTZ;

ALTER TABLE impulses DROP CONSTRAINT impulses_kind_ck;
ALTER TABLE impulses ADD CONSTRAINT impulses_kind_ck CHECK (kind IN (
    'silence',
    'weather',
    'curiosity',
    'anniversary',
    'dream',
    'reading',
    'essay',
    'news'
));
