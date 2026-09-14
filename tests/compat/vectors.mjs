// MaluDB vector compartments, called through the official Supabase client.
//
// ADR-077, compartments slice 3. What a customer's server-side code does:
//
//   await supabase.schema('maludb').rpc('vector_insert', { namespace, subject, verb, content, embedding })
//   const { data } = await supabase.schema('maludb').rpc('vector_search', { namespace, subject, verb, query })
//
// Run by tests/test_compatibility.py in three phases -- MALUDB_PHASE=before (not
// enabled), after (enabled through the platform's routes and worker) and
// withdrawn (turned off again). Prints one JSON object per line; the Python side
// decides what passing means.

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

const where = { namespace: 'docs', subject: 'page', verb: 'about' }
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

async function publicSurface () {
  const response = await fetch(`${url}/rest/v1/`, { headers: { apikey: secret } })
  expect(response.ok, `the public OpenAPI description answered ${response.status}`)
  const spec = await response.json()
  return {
    paths: Object.keys(spec.paths || {}).sort(),
    definitions: Object.keys(spec.definitions || {}).sort(),
  }
}

// A refusal has to be *the* refusal: PostgreSQL's grant on the wrapper, not a
// gateway or PostgREST that happens to be broken.
function expectNoExecute ({ data, error }, who) {
  expect(error, `${who} called a wrapper: ${JSON.stringify(data)}`)
  expect(error.code === '42501' && /permission denied/.test(error.message),
    `${who} was refused, but not by the function grant: ${error.code} ${error.message}`)
}

if (phase === 'before') {
  await check('public surface', publicSurface)

  await check('the wrappers are refused by name on a project without any MaluDB feature', async () => {
    const { data, error } = await service.schema('maludb').rpc('vector_compartments')
    expect(error, `a project without vectors answered: ${JSON.stringify(data)}`)
    expect(/not enabled for this project/.test(error.message), `refused, but not by name: ${error.message}`)
  })
} else if (phase === 'after') {
  await check('public surface', publicSurface)

  await check('service_role creates a compartment', async () => {
    const { data, error } = await service.schema('maludb').rpc('vector_compartment_create', { ...where, dimensions: 3 })
    expect(!error, `create failed: ${error && error.message}`)
    expect(typeof data === 'number', `create returned ${JSON.stringify(data)}`)
  })

  await check('service_role inserts embeddings as number arrays', async () => {
    const one = await service.schema('maludb').rpc('vector_insert',
      { ...where, content: 'alpha', embedding: [1, 0, 0], metadata: { lang: 'en' } })
    expect(!one.error, `insert failed: ${one.error && one.error.message}`)
    const many = await service.schema('maludb').rpc('vector_insert_many',
      { ...where, items: [{ content: 'beta', embedding: [0, 1, 0], metadata: { lang: 'fr' } }] })
    expect(!many.error && many.data === 1, `insert_many failed: ${many.error && many.error.message}`)
  })

  await check('service_role searches, nearest first, with metadata', async () => {
    const { data, error } = await service.schema('maludb').rpc('vector_search',
      { ...where, query: [1, 0.1, 0], match_count: 2 })
    expect(!error, `search failed: ${error && error.message}`)
    expect(data.length === 2 && data[0].content === 'alpha', `unexpected order: ${JSON.stringify(data)}`)
    expect(data[0].metadata.lang === 'en', `metadata missing: ${JSON.stringify(data[0])}`)
    return data.map((row) => row.content)
  })

  await check('a metadata filter narrows the search', async () => {
    const { data, error } = await service.schema('maludb').rpc('vector_search',
      { ...where, query: [1, 0.1, 0], filter: { lang: 'fr' } })
    expect(!error, `filtered search failed: ${error && error.message}`)
    expect(data.length === 1 && data[0].content === 'beta', `filter did not narrow: ${JSON.stringify(data)}`)
  })

  await check('a plan limit is refused with its name', async () => {
    // The fixture's plan allows two vectors; both are stored.
    const { data, error } = await service.schema('maludb').rpc('vector_insert',
      { ...where, content: 'gamma', embedding: [0, 0, 1] })
    expect(error, `an insert past the limit succeeded: ${JSON.stringify(data)}`)
    expect(error.code === 'PT403' && error.hint === 'vector_max_count',
      `refused, but not as the limit: ${error.code} ${error.hint} ${error.message}`)
  })

  await check('an unknown compartment is a 404', async () => {
    const { error } = await service.schema('maludb').rpc('vector_search',
      { namespace: 'no', subject: 'such', verb: 'thing', query: [1, 0, 0] })
    expect(error && error.code === 'PT404', `expected PT404, got ${error && error.code}`)
  })

  await check('anon cannot call a wrapper', async () => {
    expectNoExecute(await anon.schema('maludb').rpc('vector_search', { ...where, query: [1, 0, 0] }), 'anon')
  })

  await check('a signed-in user cannot call a wrapper', async () => {
    expectNoExecute(await signedIn.schema('maludb').rpc('vector_search', { ...where, query: [1, 0, 0] }),
      'an authenticated user')
  })

  await check('service_role deletes, and the deleted chunk is not found', async () => {
    const search = await service.schema('maludb').rpc('vector_search', { ...where, query: [1, 0, 0] })
    const alpha = search.data.find((row) => row.content === 'alpha')
    const { data, error } = await service.schema('maludb').rpc('vector_delete', { ...where, ids: [alpha.id] })
    expect(!error && data === 1, `delete failed: ${error && error.message}`)
    const again = await service.schema('maludb').rpc('vector_search', { ...where, query: [1, 0, 0] })
    expect(!again.data.some((row) => row.content === 'alpha'), 'the deleted chunk is still returned')
  })

  await check('the public Data API still answers', async () => {
    const { error } = await service.from('customers').select('id').limit(1)
    expect(!error, `public broke after enabling vectors: ${error && error.message}`)
  })
} else if (phase === 'withdrawn') {
  await check('public surface', publicSurface)

  await check('turned off, the wrappers are refused by name again', async () => {
    const { data, error } = await service.schema('maludb').rpc('vector_compartments')
    expect(error, `a disabled project still answered: ${JSON.stringify(data)}`)
    expect(/not enabled for this project/.test(error.message), `refused, but not by name: ${error.message}`)
  })
} else {
  console.error(`unknown MALUDB_PHASE ${phase}`)
  process.exit(2)
}

for (const row of results) console.log(JSON.stringify(row))
process.exit(results.every((row) => row.ok) ? 0 : 1)
