# Design References

These are upstream references used to validate the starter architecture. They are references, not copied specifications; implementation should verify current upstream behavior before relying on details.

## OpenAI Codex

- OpenAI Codex — Custom instructions with AGENTS.md
  - https://developers.openai.com/codex/agent-configuration/agents-md
- OpenAI Codex — Best practices
  - https://developers.openai.com/codex/learn/best-practices

## Claude Code

- Claude Code — Project memory / CLAUDE.md
  - https://docs.anthropic.com/en/docs/claude-code/memory
- Claude Code release notes — CLAUDE.md imports
  - https://docs.anthropic.com/en/release-notes/claude-code

## Supabase

- Supabase architecture
  - https://supabase.com/docs/guides/getting-started/architecture
- Supabase Data REST API / PostgREST
  - https://supabase.com/docs/guides/api
- Supabase API keys
  - https://supabase.com/docs/guides/getting-started/api-keys
- Supabase Auth architecture
  - https://supabase.com/docs/guides/auth/architecture
- Supabase JWTs
  - https://supabase.com/docs/guides/auth/jwts
- Supabase self-hosted API keys
  - https://supabase.com/docs/guides/self-hosting/self-hosted-auth-keys
- Supabase self-hosted Envoy gateway
  - https://supabase.com/docs/guides/self-hosting/self-hosted-envoy
- Supabase Realtime
  - https://supabase.com/docs/guides/realtime

## Maintenance rule

Because upstream projects change, do not treat this file as a frozen protocol specification. When implementing a compatibility feature, confirm behavior against current upstream documentation and black-box tests.
