// MaluDB memory spaces, called the way a customer's agent calls them (ADR-079, slice 6).
//
// Search is the official Supabase client:
//
//   const { data } = await supabase.schema('maludb').rpc('memory_search', { space, query, subject })
//
// Ingest and search by text are the platform's own routes on the same host and key,
// because supabase-js has no call for them; they are reached with fetch, as a
// customer's code would.
//
// Run by tests/test_compatibility.py in four phases:
//   before    -- no memory space yet
//   ingest    -- a space exists; queue an ingest (the Python side runs the worker)
//   after     -- the ingest has been written; search every way
//   withdrawn -- the space was deleted
// Prints one JSON object per line; the Python side decides what passing means.

import { createClient } from '@supabase/supabase-js'

const url = process.env.MALUDB_URL
const publishable = process.env.MALUDB_KEY
const secret = process.env.MALUDB_SECRET_KEY
const userJwt = process.env.MALUDB_USER_JWT
const phase = process.env.MALUDB_PHASE
if (!url || !publishable || !secret || !userJwt || !phase) {
  console.error('MALUDB_URL, MALUDB_KEY, MALUDB_SECRET_KEY, MALUDB_USER_JWT and MALUDB_PHASE are required')
  process.exit(2)
}

const options = { auth: { persistSession: false, autoRefreshToken: false } }
const service = createClient(url, secret, options)
const anon = createClient(url, publishable, options)
const signedIn = createClient(url, publishable, {
  ...options,
  global: { headers: { Authorization: `Bearer ${userJwt}` } },
})

const space = 'agent'
const results = []

async function check (name, fn) {
  try {
    const data = await fn()
    results.push({ name, ok: true, ...(data === undefined ? {} : { data }) })
  } catch (error) {
    results.push({ name, ok: false, error: String((error && error.message) || error) })
  }
}

function expect (condition, message) {
  if (!condition) throw new Error(message)
}

async function platform (method, path, body, key = secret) {
  const response = await fetch(`${url}${path}`, {
    method,
    headers: { apikey: key, 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await response.text()
  let json
  try { json = JSON.parse(text) } catch { json = text }
  return { status: response.status, json }
}

async function publicSurface () {
  const response = await fetch(`${url}/rest/v1/`, { headers: { apikey: secret } })
  expect(response.ok, `the public OpenAPI description answered ${response.status}`)
  const spec = await response.json()
  return { paths: Object.keys(spec.paths || {}).sort(), definitions: Object.keys(spec.definitions || {}).sort() }
}

function expectNoExecute ({ data, error }, who) {
  expect(error, `${who} called the search wrapper: ${JSON.stringify(data)}`)
  expect(error.code === '42501' && /permission denied/.test(error.message),
    `${who} was refused, but not by the function grant: ${error.code} ${error.message}`)
}

if (phase === 'before') {
  await check('public surface', publicSurface)

  await check('search is refused by name on a project without a memory space', async () => {
    const { data, error } = await service.schema('maludb').rpc('memory_search', { space, query: [1, 0, 0], subject: 'carol' })
    expect(error, `a project without memory answered: ${JSON.stringify(data)}`)
    expect(/not enabled for this project/.test(error.message), `refused, but not by name: ${error.message}`)
  })

  await check('ingest is refused, saying how to create a space', async () => {
    const { status, json } = await platform('POST', `/memory/v1/spaces/${space}/ingest`, { items: [] })
    expect(status === 404 && /memory\/spaces/.test(json.message), `answered ${status} ${JSON.stringify(json)}`)
  })
} else if (phase === 'ingest') {
  await check('a publishable key cannot ingest', async () => {
    const { status } = await platform('POST', `/memory/v1/spaces/${space}/ingest`,
      { items: [{ subject: 'x', verb: 'owns', text: 'x', embedding: [1, 0, 0] }] }, publishable)
    expect(status === 403, `answered ${status}`)
  })

  await check('the secret key queues an ingest of embedded edges', async () => {
    const { status, json } = await platform('POST', `/memory/v1/spaces/${space}/ingest`, {
      items: [
        { subject: 'carol', verb: 'owns', text: 'carol owns the parser', embedding: [1, 0, 0] },
        { subject: 'dave', verb: 'owns', text: 'dave owns the lexer', embedding: [0, 1, 0] },
      ],
    })
    expect(status === 202 && json.state === 'pending' && json.items === 2, `answered ${status} ${JSON.stringify(json)}`)
    return { id: json.id, status_url: json.status_url }
  })
} else if (phase === 'after') {
  await check('public surface', publicSurface)

  await check('the ingest reports each item written', async () => {
    const { status, json } = await platform('GET', `/memory/v1/ingests/${process.env.MALUDB_INGEST_ID}`)
    expect(status === 200 && json.state === 'succeeded' && json.written === 2,
      `answered ${status} ${JSON.stringify(json)}`)
    expect(json.results.every((r) => r.written && r.statement_id), `a result lacks its statement: ${JSON.stringify(json.results)}`)
  })

  await check('service_role searches by subject, nearest first', async () => {
    const { data, error } = await service.schema('maludb').rpc('memory_search', { space, query: [1, 0, 0], subject: 'carol' })
    expect(!error, `search failed: ${error && error.message}`)
    expect(data.length >= 1 && data[0].content === 'carol owns the parser' && data[0].subject_name === 'carol',
      `unexpected result: ${JSON.stringify(data)}`)
    return data.map((row) => row.content)
  })

  await check('service_role searches by verb across subjects', async () => {
    const { data, error } = await service.schema('maludb').rpc('memory_search',
      { space, query: [0, 1, 0], verb: 'owns', match_count: 5 })
    expect(!error, `search failed: ${error && error.message}`)
    expect(data.length === 2 && data[0].content === 'dave owns the lexer', `unexpected order: ${JSON.stringify(data)}`)
  })

  await check('an unknown space is a 404', async () => {
    const { error } = await service.schema('maludb').rpc('memory_search', { space: 'nope', query: [1, 0, 0], subject: 'carol' })
    expect(error && error.code === 'PT404', `expected PT404, got ${error && error.code} ${error && error.message}`)
  })

  await check('a search naming neither subject nor verb is refused', async () => {
    const { error } = await service.schema('maludb').rpc('memory_search', { space, query: [1, 0, 0] })
    expect(error && error.code === 'PT400', `expected PT400, got ${error && error.code} ${error && error.message}`)
  })

  await check('anon cannot search', async () => {
    expectNoExecute(await anon.schema('maludb').rpc('memory_search', { space, query: [1, 0, 0], subject: 'carol' }), 'anon')
  })

  await check('a signed-in user cannot search', async () => {
    expectNoExecute(await signedIn.schema('maludb').rpc('memory_search', { space, query: [1, 0, 0], subject: 'carol' }),
      'an authenticated user')
  })

  await check('search by text finds what the vector search finds', async () => {
    const { status, json } = await platform('POST', `/memory/v1/spaces/${space}/search`,
      { text: 'who owns the parser', subject: 'carol', limit: 5 })
    expect(status === 200 && Array.isArray(json), `answered ${status} ${JSON.stringify(json)}`)
    expect(json.length >= 1 && json[0].content === 'carol owns the parser', `unexpected result: ${JSON.stringify(json)}`)
    return json.map((row) => row.content)
  })

  await check('search by text with a publishable key is refused', async () => {
    const { status } = await platform('POST', `/memory/v1/spaces/${space}/search`,
      { text: 'who owns the parser', subject: 'carol' }, publishable)
    expect(status === 403, `answered ${status}`)
  })

  await check('the public Data API still answers', async () => {
    const { error } = await service.from('customers').select('id').limit(1)
    expect(!error, `public broke after creating a memory space: ${error && error.message}`)
  })
} else if (phase === 'withdrawn') {
  await check('public surface', publicSurface)

  await check('deleted, search is refused by name again', async () => {
    const { data, error } = await service.schema('maludb').rpc('memory_search', { space, query: [1, 0, 0], subject: 'carol' })
    expect(error, `a project whose only space was deleted still answered: ${JSON.stringify(data)}`)
    expect(/not enabled for this project/.test(error.message), `refused, but not by name: ${error.message}`)
  })
} else {
  console.error(`unknown MALUDB_PHASE ${phase}`)
  process.exit(2)
}

for (const row of results) console.log(JSON.stringify(row))
process.exit(results.every((row) => row.ok) ? 0 : 1)
