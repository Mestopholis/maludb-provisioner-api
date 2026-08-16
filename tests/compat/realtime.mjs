// Postgres Changes, with the official client, through the MaluDB gateway.
//
// Deliberately not a Python WebSocket client. `AGENTS.md` requires
// compatibility to be shown with the official client, and this surface is the
// reason why: the URL, the `apikey` in the query string, the Phoenix
// serialiser version and the channel protocol are all upstream's, and a
// reimplementation would test our reading of them rather than the things
// themselves. Slice 4 found the `/socket` path mapping exactly this way.
//
// Speaks the same JSON-lines protocol as suite.mjs, plus one line the harness
// waits for: `{"name":"subscribed"}` is the signal to write a row, because the
// insert has to happen *after* the subscription or there is nothing to deliver.

import { createClient } from '@supabase/supabase-js'

const url = process.env.MALUDB_URL
const key = process.env.MALUDB_KEY
const table = process.env.MALUDB_TABLE ?? 'notes'

const emit = (row) => console.log(JSON.stringify(row))

const client = createClient(url, key, {
  realtime: { params: { eventsPerSecond: 10 } },
})

const timeout = setTimeout(() => {
  emit({ name: 'postgres_changes', ok: false, error: 'no event before the deadline' })
  process.exit(1)
}, Number(process.env.MALUDB_WS_DEADLINE ?? 90_000))

const channel = client
  .channel('compat-notes')
  .on(
    'postgres_changes',
    { event: 'INSERT', schema: 'public', table },
    (payload) => {
      clearTimeout(timeout)
      emit({
        name: 'postgres_changes',
        ok: true,
        type: payload.eventType,
        table: payload.table,
        body: payload.new?.body ?? null,
      })
      // The client is what the customer runs, so the exit path is the one they
      // would take: unsubscribe, then leave.
      channel.unsubscribe().then(() => process.exit(0))
    },
  )
  .subscribe((status, error) => {
    if (status === 'SUBSCRIBED') {
      emit({ name: 'subscribed', ok: true })
    } else if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT') {
      // Not fatal, and this is the interesting half: a project whose Realtime
      // instance is asleep is closed with 1013 while the platform wakes it, and
      // phoenix.js reconnects on its own backoff. Exiting here would turn the
      // wake -- which the client is built to ride out -- into a failure.
      emit({ name: 'retrying', ok: false, status, error: String(error ?? '') })
    }
  })
