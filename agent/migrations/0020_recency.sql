-- Шаг 64: недавнее ближе к поверхности, чем давнее.
--
-- `surface_score` (0019) считал давность от ЗАПИСИ, а не от события. Сцена
-- из детства, которую сон вспомнил этой ночью, и вчерашний прожитый день
-- стояли в отборе вровень: обе записаны недавно. Биографию пишут четыре
-- писателя, и три из них смотрят назад — значит, в «вспоминается» чаще
-- ехало прошлое, чем неделя, которую он прожил.
--
-- У живого человека иначе: что было на этой неделе, на уме само, без повода.
-- Множитель по давности СОБЫТИЯ: ×2 для сегодняшнего, ×1.5 через неделю,
-- ×1 для всего, что старше пары месяцев. Не по источнику — вчерашнее,
-- рассказанное в разговоре, всплывает так же, как вчерашнее прожитое.
-- Исключение одно, сон: у него своя быстрая память (0019), и удваивать её
-- значило бы вернуть сон на язык, откуда его только что сняли.
CREATE OR REPLACE FUNCTION surface_score(weight REAL, last_recalled TIMESTAMPTZ,
                                         created_at TIMESTAMPTZ, surfaced_at TIMESTAMPTZ,
                                         source TEXT, at TIMESTAMPTZ,
                                         happened_at TIMESTAMPTZ)
RETURNS DOUBLE PRECISION LANGUAGE SQL IMMUTABLE AS $$
    SELECT surface_score(weight, last_recalled, created_at, surfaced_at, source, at)
         * CASE
             WHEN source = 'dream' OR happened_at > at THEN 1.0
             ELSE 1.0 + pow(0.5, EXTRACT(EPOCH FROM (at - happened_at)) / 86400.0 / 7.0)
           END
$$;
