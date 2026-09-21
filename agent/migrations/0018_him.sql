-- Шаг 59: собеседник. Шаг 60: голос. Одной миграцией, потому что второе
-- без первого не имеет смысла: разговорчивость меряется откликом ЕГО.
--
-- ## Почему не объект №1
--
-- `0010_threads.sql` обещал «линии собеседника появятся с объектом №1», и
-- обещание выполнено наполовину: линии — да (`threads.side = 'user'`), а
-- объект — нет, и это решение, а не недоделка. Объекты — мир диалога:
-- экстрактор их матчит по имени, выдаёт им пары `key: value` и гасит по
-- `effective_salience`. К собеседнику не подходит ни одно из трёх. Имени у
-- него может не быть вовсе; знание о человеке — фразы («устаёт от созвонов,
-- но не жалуется»), а не пары; и гаснуть в промпте за трое суток тишины он
-- не должен — тишина с ним как раз и есть то, о чём персонаж думает.
--
-- Поэтому свои строки: факты фразами, с метками «когда узнал» и «когда
-- подтвердилось», и отдельно — взгляд целиком, одной-тремя фразами в `agent`.
-- Взгляд переписывается, факты копятся: как у человека, который помнит, что
-- ты рассказывал, а мнение о тебе меняет.

CREATE TABLE him_facts (
    id          BIGSERIAL   PRIMARY KEY,
    -- Фразой, словами персонажа. Не «occupation: бэкенд», а «пишет бэкенд и
    -- делает вид, что ему это нравится».
    text        TEXT        NOT NULL,
    noted_at    TIMESTAMPTZ NOT NULL,
    -- Когда разговор к этому возвращался. Порядок в промпте — по ней: то, о
    -- чём говорили на прошлой неделе, ближе того, что было сказано однажды.
    touched_at  TIMESTAMPTZ NOT NULL,
    hits        INTEGER     NOT NULL DEFAULT 1,
    -- Перестало быть правдой: сменил работу, передумал, оказалось не так.
    -- Строка не удаляется — «раньше он...» тоже знание.
    dropped_at  TIMESTAMPTZ,
    dropped_why TEXT,
    CONSTRAINT him_facts_text_ck CHECK (length(btrim(text)) > 0)
);
CREATE INDEX him_facts_open_idx ON him_facts (touched_at DESC) WHERE dropped_at IS NULL;

-- Взгляд на него целиком и когда пересмотрен. NULL — ещё не познакомились.
ALTER TABLE agent ADD COLUMN him_view TEXT;
ALTER TABLE agent ADD COLUMN him_at   TIMESTAMPTZ;

-- Шаг 60. Тяга говорить — черта, а не константа кода: замкнутый пишет реже.
-- Число от 0 до 1 и основание его словами; выводится из черт отдельным
-- проходом (`voice.talk_tick`) после каждого их пересмотра. NULL — черт ещё
-- нет, и голос считается от середины.
ALTER TABLE agent ADD COLUMN talk     REAL;
ALTER TABLE agent ADD COLUMN talk_why TEXT;
ALTER TABLE agent ADD COLUMN talk_at  TIMESTAMPTZ;
ALTER TABLE agent ADD CONSTRAINT agent_talk_ck CHECK (talk IS NULL OR (talk >= 0 AND talk <= 1));

-- Новое дело: разобраться в ЕГО деле. Не поручение — у персонажа нет
-- обязанностей, — а то, что делают для человека, которому не всё равно.
ALTER TABLE pursuits DROP CONSTRAINT pursuits_action_ck;
ALTER TABLE pursuits ADD CONSTRAINT pursuits_action_ck CHECK (action IN (
    'read', 'write', 'news', 'explore', 'recall', 'daydream', 'reach', 'rest',
    'tend'       -- разобраться в его деле (Шаг 59)
));

ALTER TABLE impulses DROP CONSTRAINT impulses_kind_ck;
ALTER TABLE impulses ADD CONSTRAINT impulses_kind_ck CHECK (kind IN (
    'silence', 'weather', 'curiosity', 'anniversary', 'dream', 'reading',
    'essay', 'news', 'pursuit', 'daydream', 'reach',
    'tend'       -- разобрался в его деле и есть что сказать (Шаг 59)
));
