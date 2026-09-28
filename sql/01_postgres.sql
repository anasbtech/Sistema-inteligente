-- ============================================================
-- SEGURANÇA — extras do PostgreSQL (opcional)
--
-- O sistema roda sem este arquivo, em SQLite. Aplique-o quando migrar
-- para PostgreSQL, DEPOIS de `python run.py` ter criado as tabelas:
--
--     psql $DATABASE_URL -f sql/01_postgres.sql
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ------------------------------------------------------------
-- Índice HNSW para busca 1:N por distância cosseno.
-- m=32 / ef_construction=128 equilibra recall e tempo de construção.
-- Para consultar com mais recall: SET hnsw.ef_search = 100;
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_pessoas_embedding_hnsw
    ON pessoas USING hnsw (embedding vector_cosine_ops)
    WITH (m = 32, ef_construction = 128);

CREATE INDEX IF NOT EXISTS ix_amostras_embedding_hnsw
    ON amostras_faciais USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS ix_pessoas_nome_trgm
    ON pessoas USING gin (nome gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ix_alertas_abertos
    ON alertas (severidade DESC, disparado_em DESC)
    WHERE status <> 'RESOLVIDO';

-- ------------------------------------------------------------
-- Trilha de auditoria: append-only de verdade.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION auditoria_somente_insercao()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'A trilha de auditoria é somente-inserção. Operação % bloqueada na linha %.',
        TG_OP, COALESCE(OLD.id, -1);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_auditoria_imutavel ON auditoria;
CREATE TRIGGER trg_auditoria_imutavel
    BEFORE UPDATE OR DELETE ON auditoria
    FOR EACH ROW EXECUTE FUNCTION auditoria_somente_insercao();

-- Em produção, tire também o privilégio do papel da aplicação:
--   REVOKE UPDATE, DELETE, TRUNCATE ON auditoria FROM seguranca_app;

-- ------------------------------------------------------------
-- O formato do identificador, exigido pelo banco.
-- ------------------------------------------------------------
ALTER TABLE pessoas DROP CONSTRAINT IF EXISTS ck_formato_id_biometrico;
ALTER TABLE pessoas ADD CONSTRAINT ck_formato_id_biometrico
    CHECK (id_biometrico ~ '^BIO-[MFN]-[0-9]{6}$');

-- ------------------------------------------------------------
-- O identificador nunca muda depois de emitido — nem por SQL direto.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION id_biometrico_imutavel()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id_biometrico IS DISTINCT FROM OLD.id_biometrico THEN
        RAISE EXCEPTION
            'O identificador biométrico é permanente (% -> %). Corrigir o sexo do cadastro não o reemite.',
            OLD.id_biometrico, NEW.id_biometrico;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_id_biometrico_imutavel ON pessoas;
CREATE TRIGGER trg_id_biometrico_imutavel
    BEFORE UPDATE ON pessoas
    FOR EACH ROW EXECUTE FUNCTION id_biometrico_imutavel();

-- ------------------------------------------------------------
-- Visão de apoio: registros e próximo ID por prefixo
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW vw_esquema_identificadores AS
SELECT
    s.sexo,
    CASE s.sexo WHEN 'M' THEN 'BIO-M' WHEN 'F' THEN 'BIO-F' ELSE 'BIO-N' END AS prefixo,
    CASE s.sexo WHEN 'M' THEN 'Masculino' WHEN 'F' THEN 'Feminino'
                ELSE 'Não informado' END AS rotulo,
    COUNT(p.id) AS registros,
    CASE s.sexo WHEN 'M' THEN 'BIO-M-' WHEN 'F' THEN 'BIO-F-' ELSE 'BIO-N-' END
        || LPAD(s.proximo::text, 6, '0') AS proximo_id
FROM sequencia_biometrica s
LEFT JOIN pessoas p ON p.sexo = s.sexo AND p.ativo
GROUP BY s.sexo, s.proximo;

