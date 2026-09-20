-- Эссе: второе дело с результатом вне базы (после чтения).
--
-- Пишется только если персонаж захотел. Темы с экрана нет. Почты в этом
-- шаге нет: готовый текст лежит файлом в outbox/, импульс 'essay' может
-- потом рассказать об этом в разговоре.
--
-- Текст эссе в Postgres не хранится целиком. В базе — заголовок, причина,
-- путь и порции-конспекты (короткие), чтобы следующий вечер помнил, что
-- уже сказано. Полный текст — файл, как книга на полке.

ALTER TABLE agent ADD COLUMN essay_at TIMESTAMPTZ;

CREATE TABLE essays (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    why         TEXT NOT NULL,
    file_path   TEXT NOT NULL UNIQUE,
    opened_at   TIMESTAMPTZ NOT NULL,
    closed_at   TIMESTAMPTZ,
    closed_why  TEXT,
    CONSTRAINT essays_title_ck CHECK (length(btrim(title)) > 0),
    CONSTRAINT essays_why_ck   CHECK (length(btrim(why)) > 0),
    CONSTRAINT essays_closed_ck CHECK ((closed_at IS NULL) = (closed_why IS NULL))
);

CREATE UNIQUE INDEX essays_one_open_uq
    ON essays ((closed_at IS NULL))
    WHERE closed_at IS NULL;

CREATE TABLE essay_passages (
    id         BIGSERIAL PRIMARY KEY,
    essay_id   BIGINT NOT NULL REFERENCES essays(id) ON DELETE CASCADE,
    at         TIMESTAMPTZ NOT NULL,
    chars      INT NOT NULL,
    conspectus TEXT,
    CONSTRAINT essay_passages_chars_ck CHECK (chars > 0)
);
CREATE INDEX essay_passages_essay_idx ON essay_passages (essay_id, at);

ALTER TABLE impulses DROP CONSTRAINT impulses_kind_ck;
ALTER TABLE impulses ADD CONSTRAINT impulses_kind_ck CHECK (kind IN (
    'silence',
    'weather',
    'curiosity',
    'anniversary',
    'dream',
    'reading',
    'essay'
));
