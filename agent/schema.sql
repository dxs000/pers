-- Схема состояния. Поднимается ПУСТОЙ: миграции нет, старт чистый
-- (ROADMAP, «Чистый старт»). Проверка — не «таблицы создались», а промпты,
-- собранные на фикстуре: `golden.py --check`.
--
-- Схема писалась не наугад: контракт `snapshot.Turn` (Шаг 17) уже сказал,
-- какие запросы понадобятся и в каком виде данные обязаны выйти наружу.
-- Каждое решение ниже отвечает какому-нибудь читателю, а не «полноте модели».

-- =============================================================================
-- Объекты. «Я» — объект №0: обещание Фазы 0 становится строкой
-- =============================================================================
CREATE TABLE objects (
    id          BIGINT PRIMARY KEY,
    type        TEXT        NOT NULL DEFAULT 'other',
    label       TEXT        NOT NULL DEFAULT '',
    -- Каноничная форма для матчинга, посчитанная в Python
    -- (`snapshot.norm_name`). НЕ `lower(label)`: SQL-функция `lower()`
    -- зависит от локали базы и под `C` не трогает кириллицу вовсе.
    -- База сравнивает готовые строки и регистр не складывает.
    label_norm  TEXT        NOT NULL DEFAULT '',
    salience    REAL        NOT NULL DEFAULT 1.0,
    last_seen   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- `next_id` уходит в последовательность: счётчик в состоянии был ровно тем,
-- чем счётчики бывают при двух писателях. Начинается с 1, потому что 0 занят
-- под self и выдаётся не последовательностью, а руками, ровно один раз.
CREATE SEQUENCE objects_id_seq START 1 OWNED BY objects.id;
ALTER TABLE objects ALTER COLUMN id SET DEFAULT nextval('objects_id_seq');

-- Затухание считается в SQL и становится `ORDER BY`, как обещал ROADMAP.
-- С Шага 20 по Шаг 26 формула жила в ДВУХ местах — здесь и в
-- `store.effective_salience`, — и это был тот самый соблазн размазать
-- логику между Python и SQL, о котором предупреждает роадмап. Копия ушла
-- вместе с JSON-движком: место осталось одно, и правило «правишь одну —
-- правишь обе» больше не нужно соблюдать. Сбруя по-прежнему сверяет
-- ВЫДАЧУ, поэтому правка формулы видна эталоном, а не рассуждением.
CREATE FUNCTION effective_salience(salience REAL, last_seen TIMESTAMPTZ, at TIMESTAMPTZ)
RETURNS DOUBLE PRECISION LANGUAGE SQL IMMUTABLE AS $$
    SELECT CASE
        WHEN last_seen IS NULL THEN salience::DOUBLE PRECISION
        ELSE salience * pow(
            0.5,
            greatest(EXTRACT(EPOCH FROM (at - last_seen)) / 3600.0, 0.0) / 72.0
        )
    END
$$;

CREATE INDEX objects_last_seen_idx ON objects (last_seen DESC);

-- УНИКАЛЬНЫЙ, а не обычный: `_match_object` ищет объект SELECT-ом и, не найдя,
-- вставляет. Это check-then-insert, и при двух писателях (3c) оба не находят
-- «Аню» и оба вставляют. `SELECT ... FOR UPDATE` тут бесполезен — запирать
-- нечего, строки ещё нет; фантом закрывается только индексом.
--
-- Частичный по двум причинам, и обе — про валидные состояния, а не про
-- оптимизацию:
--   `label_norm <> ''` — кандидат без имени законен, и вторая безымянная
--     строка не должна падать на конфликте с первой;
--   `id <> 0`          — объект №0 держит 'self'. Без исключения объект,
--     которого собеседник назвал бы «self», сматчился бы с самим персонажем,
--     и чужие факты уехали бы в его self-блок.
--
-- Индекс фиксирует допущение, которое код уже делает: `_match_object` берёт
-- `ORDER BY o.id LIMIT 1`, то есть двух разных «Ань» в памяти сегодня и так
-- быть не может. Если Фаза 5 захочет различать тёзок — снимать придётся
-- вместе с матчингом, а не отдельно.
CREATE UNIQUE INDEX objects_label_norm_uq
    ON objects (label_norm) WHERE label_norm <> '' AND id <> 0;

-- =============================================================================
-- Псевдонимы
-- =============================================================================
CREATE TABLE aliases (
    -- Суррогатный ключ здесь не для уникальности (её держит индекс ниже), а
    -- ради ПОРЯДКА. В JSON псевдонимы лежали списком, и порядок был порядком
    -- появления: «Аня» сказали раньше «Анечки». Экстрактор видит этот список
    -- при матчинге, значит порядок доезжает до промпта. `ORDER BY alias` дал
    -- бы алфавитный — сбруя поймала это на первом же прогоне паритета.
    id         BIGSERIAL PRIMARY KEY,
    object_id  BIGINT NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
    alias      TEXT   NOT NULL,
    alias_norm TEXT   NOT NULL
);
CREATE UNIQUE INDEX aliases_norm_idx ON aliases (object_id, alias_norm);
CREATE INDEX aliases_lookup_idx ON aliases (alias_norm);

-- =============================================================================
-- Ассершены. ОДНА таблица на объекты и на self — `merge_self_assertions`
-- перестаёт быть отдельной функцией, потому что self это object_id = 0
-- =============================================================================
CREATE TABLE assertions (
    id         BIGSERIAL PRIMARY KEY,
    object_id  BIGINT      NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
    key        TEXT        NOT NULL,
    value      TEXT        NOT NULL,
    confidence TEXT        NOT NULL DEFAULT 'low',
    hits       INTEGER     NOT NULL DEFAULT 1,
    ts         TIMESTAMPTZ,
    -- `dream` придёт Фазой 7; колонка названа под него заранее.
    source     TEXT        NOT NULL DEFAULT 'user',
    -- Промоушен по смене источника (Шаг 15). Отдельное поле, а не сдвиг
    -- схлопнутого `confidence`.
    confirmed  BOOLEAN     NOT NULL DEFAULT FALSE,
    CONSTRAINT assertions_confidence_ck CHECK (confidence IN ('low', 'med', 'high')),
    CONSTRAINT assertions_source_ck     CHECK (source IN ('user', 'self', 'web', 'dream'))
);

-- Пара (key, value) — то, по чему `_merge_assertions` решает «тот же факт».
-- Противоречия живут рядом: разные value при одном key НЕ конфликтуют, и
-- уникальность стоит по паре, а не по key. Механика Фазы 2 сохранена буквой.
CREATE UNIQUE INDEX assertions_pair_idx ON assertions (object_id, key, value);
CREATE INDEX assertions_object_idx ON assertions (object_id);

-- =============================================================================
-- Эпизоды
-- =============================================================================
CREATE TABLE episodes (
    id         BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ,
    ended_at   TIMESTAMPTZ,
    exchanges  INTEGER NOT NULL DEFAULT 0,
    -- NULL — выжимка не собралась. Мёртвый груз, который читатели
    -- пропускают, а Curator (Фаза 5) однажды уберёт.
    summary    TEXT,
    -- Под Фазу 7: приснившийся эпизод надо будет отличать от бывшего.
    source     TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX episodes_ended_idx ON episodes (ended_at DESC);

-- =============================================================================
-- Сессия и реплики. Буфер перестаёт быть полем состояния
-- =============================================================================
CREATE TABLE sessions (
    id         BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at   TIMESTAMPTZ,
    closed_at  TIMESTAMPTZ,
    -- «Отрезанная голова»: сколько обменов не доехало. В JSON копилось при
    -- обрезке буфера; здесь физически хранится всё, и `dropped` доживает свой
    -- век как разница между «сколько было» и «сколько взяли в промпт выжимки».
    dropped    INTEGER NOT NULL DEFAULT 0,
    episode_id BIGINT REFERENCES episodes(id) ON DELETE SET NULL
);

-- Открытая сессия ровно одна. Инвариант код УЖЕ предполагает в четырёх
-- местах (`_open_session`, `session_stale`, `summary_buffer`, `close_session`
-- — все читают `WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1`) и нигде
-- не проверяет. Второй фантом того же рода, что у объектов: два писателя
-- видят «открытой нет» и открывают две, после чего первая молча осиротеет
-- вместе с половиной `messages`.
--
-- Выражение — `(closed_at IS NULL)`, а не константа: для всех строк,
-- попавших под предикат, оно даёт TRUE, и индекс из одного значения даёт
-- ровно одну строку. Индексировать сам `closed_at` было бы бесполезно —
-- NULL-ы в уникальном индексе друг с другом не конфликтуют.
CREATE UNIQUE INDEX sessions_one_open_uq
    ON sessions ((closed_at IS NULL)) WHERE closed_at IS NULL;

CREATE TABLE messages (
    id         BIGSERIAL PRIMARY KEY,
    session_id BIGINT      NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts         TIMESTAMPTZ NOT NULL,
    role       TEXT        NOT NULL,
    text       TEXT        NOT NULL,
    CONSTRAINT messages_role_ck CHECK (role IN ('user', 'assistant'))
);
CREATE INDEX messages_session_idx ON messages (session_id, ts);

-- =============================================================================
-- Агент: то, что было полями `self` мимо ассершенов
-- =============================================================================
CREATE TABLE agent (
    id               SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    name             TEXT NOT NULL DEFAULT 'Некто',
    traits           TEXT[] NOT NULL DEFAULT '{}',
    mood             TEXT NOT NULL DEFAULT 'нейтральное',
    place_label      TEXT,
    place_lat        DOUBLE PRECISION,
    place_lon        DOUBLE PRECISION,
    place_source     TEXT,
    place_asked      TEXT,
    place_resolved_at TIMESTAMPTZ,
    -- Латч среды: не память, а «что было за окном в прошлый раз». Перезаписы-
    -- вается целиком, в objects ничего не кладёт. JSONB, потому что форма
    -- принадлежит рендеру (`_outside_changes`), а не хранилищу.
    outside_latch    JSONB,
    last_exchange_ts TIMESTAMPTZ,
    last_search_ts   TIMESTAMPTZ
);
INSERT INTO agent (id) VALUES (1);

-- =============================================================================
-- Очередь входящих от GUI (Фаза 4). Заводится сейчас, потому что схема
-- поднимается один раз и `ALTER` задним числом дороже пустой таблицы
-- =============================================================================
CREATE TABLE inbox (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    text       TEXT NOT NULL,
    handled_at TIMESTAMPTZ,
    -- Терминальное состояние для клиента (Шаг 28). `handled_at` говорит
    -- «прочитано», но не говорит, где ответ, — а клиенту, отправившему
    -- реплику, нужно знать именно это, иначе он ждёт строку, которой не
    -- будет. Отсюда три состояния вместо двух:
    --
    --   handled_at IS NULL              — ещё не прочитано;
    --   handled_at + reply_id           — ответ вот эта строка `messages`;
    --                                     у склеенных реплик он ОБЩИЙ, и это
    --                                     верно: их слова ответ получили;
    --   handled_at + reply_id IS NULL   — прочитано поздно, своего ответа не
    --                                     будет (реплика по ту сторону
    --                                     разрыва сессии; текст выброшен,
    --                                     счёт ушёл в sessions.dropped).
    --
    -- ON DELETE SET NULL, а не CASCADE: строка очереди — свидетельство, что
    -- человек эти слова написал, и она не должна исчезать вместе с уборкой
    -- сообщений (Curator, Фаза 5).
    reply_id   BIGINT REFERENCES messages(id) ON DELETE SET NULL
);
-- Частичный индекс: потребитель спрашивает только необработанное, и полный
-- индекс по мере роста таблицы стал бы платить за то, что никто не читает.
CREATE INDEX inbox_pending_idx ON inbox (id) WHERE handled_at IS NULL;

-- Состояний у записи ДВА, и третьего («взято в работу») здесь сознательно
-- нет. Колонка `claimed_at` рассматривалась и отклонена: она сделала бы
-- пометку отдельным фактом, который может разойтись с записью обмена.
-- Вместо неё `handled_at` ставится ТЕМ ЖЕ коммитом, что и строки `messages`
-- (транзакция T1 хода), и потому является следствием записи, а не спутником.
-- Исполнено на Шаге 28 (`store_pg.mark_handled`), пачкой, а не строкой:
--
--   UPDATE inbox SET handled_at = %s, reply_id = %s
--    WHERE id = ANY(%s) AND handled_at IS NULL RETURNING id
--
-- Вернулось меньше, чем просили, — «кто-то опередил»: T1 роняется целиком,
-- обмен откатывается, ответ выбрасывается. Всё или ничего, потому что
-- половина помеченной пачки означала бы разговор, разорванный между двумя
-- отвечающими.
--
-- **`FOR UPDATE SKIP LOCKED` на выборке отменён, и это правка Шага 28.**
-- Решение Шага 22 предполагало, что лок хоть что-то держит; он не держит:
-- транзакция не переживает вызов LLM (то же правило, по которому отклонили
-- `claimed_at`), значит замок снимается до того, как ответ начнёт
-- считаться. Защита, снимающаяся раньше защищаемого, — молчаливый no-op,
-- ровно того рода, из-за которого Шаг 23 вычищал `commit()`, а Шаг 25 завёл
-- `_require_unit`. Настоящая развязка — условный UPDATE выше, и она
-- атомарна сама по себе.
--
-- Цена решения названа: упади процесс между T1 и T2 — обмен записан, факты
-- нет. Дыра терпима именно здесь, потому что `messages` хранит разговор
-- целиком: факты выводимы из записи, запись из фактов — нет. Восстановитель-
-- ный проход и водяной знак `agent.digested_through` отложены в 3c вместе.

CREATE TABLE followups (
    id         BIGSERIAL PRIMARY KEY,
    reply_id   BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    findings   JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at    TIMESTAMPTZ
);
CREATE INDEX followups_pending_idx ON followups (id) WHERE done_at IS NULL;
