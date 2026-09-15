-- ADR-079 memory slice 5b: raw text, extracted and embedded with the customer's
-- own provider keys.
--
-- **A space names its models.** Extraction (anthropic or openai) and embedding
-- (openai or voyage) are chosen separately (decision 5); the names are the
-- provider's model identifiers, free-form text that only travels in a request
-- body to a fixed host. There is no endpoint column, and there must never be one:
-- a customer-supplied URL would aim platform infrastructure at an address of the
-- customer's choosing.
ALTER TABLE memory_spaces ADD COLUMN IF NOT EXISTS extraction_provider VARCHAR(20);
ALTER TABLE memory_spaces ADD COLUMN IF NOT EXISTS extraction_model    VARCHAR(100);
ALTER TABLE memory_spaces ADD COLUMN IF NOT EXISTS embedding_provider  VARCHAR(20);
ALTER TABLE memory_spaces ADD COLUMN IF NOT EXISTS embedding_model     VARCHAR(100);

ALTER TABLE memory_spaces DROP CONSTRAINT IF EXISTS memory_spaces_extraction_provider_check;
ALTER TABLE memory_spaces ADD CONSTRAINT memory_spaces_extraction_provider_check
    CHECK (extraction_provider IS NULL OR extraction_provider IN ('anthropic', 'openai'));
ALTER TABLE memory_spaces DROP CONSTRAINT IF EXISTS memory_spaces_embedding_provider_check;
ALTER TABLE memory_spaces ADD CONSTRAINT memory_spaces_embedding_provider_check
    CHECK (embedding_provider IS NULL OR embedding_provider IN ('openai', 'voyage'));
ALTER TABLE memory_spaces DROP CONSTRAINT IF EXISTS memory_spaces_models_named_together;
ALTER TABLE memory_spaces ADD CONSTRAINT memory_spaces_models_named_together
    CHECK ((extraction_provider IS NULL) = (extraction_model IS NULL)
       AND (embedding_provider IS NULL) = (embedding_model IS NULL));

-- **An ingest is edges or text.** `edges` is slice 5a's: the customer's own
-- embeddings. `text` is extracted and embedded by the worker.
ALTER TABLE memory_ingests ADD COLUMN IF NOT EXISTS kind VARCHAR(10) NOT NULL DEFAULT 'edges';
ALTER TABLE memory_ingests DROP CONSTRAINT IF EXISTS memory_ingests_kind_check;
ALTER TABLE memory_ingests ADD CONSTRAINT memory_ingests_kind_check CHECK (kind IN ('edges', 'text'));

-- A text ingest spends seconds per item at a provider, so "running for too long"
-- is measured from the last item the worker finished rather than from when it
-- started: a live worker is never mistaken for a dead one.
ALTER TABLE memory_ingests ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
