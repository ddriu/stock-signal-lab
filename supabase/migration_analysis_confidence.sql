-- Conserva la confianza real del análisis profundo. Sin este dato, las
-- fotografías antiguas tenían que asumir 70/100 y nunca podían acreditar el
-- mínimo semanal de 75 aunque el análisis original sí lo alcanzara.

alter table public.analysis_snapshots
    add column if not exists confidence_pct integer
        check (confidence_pct between 0 and 100);
