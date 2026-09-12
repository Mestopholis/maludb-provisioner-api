// The MaluDB data-model graph, read through the official Supabase client.
//
// Phase 12 slice 5 (ADR-074). What a customer's code actually does:
//
//   const { data } = await supabase.schema('maludb').from('datamodel_relations').select()
//
// Run twice by tests/test_compatibility.py -- MALUDB_PHASE=before, on a project
// that has not enabled the graph, and MALUDB_PHASE=after, once it has been
// enabled and refreshed through the platform's own routes. Prints one JSON
// object per line; the Python side decides what passing means.
//
// Three callers, because the copy's whole security claim is who may read it:
// a secret key (service_role), the publishable key (anon), and the publishable
// key carrying a signed-in user's token (authenticated).

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

// The public schema's OpenAPI description, as the Data API serves it. Compared
// before and after enabling by the Python side: enabling must extend the
// surface, never alter what `public` already published.
async function publicSurface () {
  const response = await fetch(`${url}/rest/v1/`, { headers: { apikey: secret } })
  expect(response.ok, `the public OpenAPI description answered ${response.status}`)
  const spec = await response.json()
  return {
    paths: Object.keys(spec.paths || {}).sort(),
    definitions: Object.keys(spec.definitions || {}).sort(),
  }
}

if (phase === 'before') {
  await check('public surface', publicSurface)

  await check('the maludb schema is refused on a project that has not enabled it', async () => {
    const { data, error } = await service.schema('maludb').from('datamodel_relations').select('*')
    expect(error, `a project without the graph returned data: ${JSON.stringify(data)}`)
    expect(
      /not enabled for this project/.test(error.message),
      `refused, but not by name: ${error.message}`
    )
  })
} else if (phase === 'after') {
  await check('public surface', publicSurface)

  await check('service_role reads the copy', async () => {
    const { data, error } = await service
      .schema('maludb').from('datamodel_relations').select('relation_name, kind')
    expect(!error, `service_role was refused: ${error && error.message}`)
    const names = data.map((row) => row.relation_name).sort()
    for (const expected of ['customers', 'notes', 'secrets']) {
      expect(names.includes(expected), `${expected} is missing from the copy: ${names}`)
    }
    return names
  })

  await check('the copy carries a description of each relation', async () => {
    const { data, error } = await service
      .schema('maludb').from('datamodel_relations').select('description')
      .eq('relation_name', 'customers').single()
    expect(!error, `could not read customers' description: ${error && error.message}`)
    const columns = (data.description.columns || []).map((column) => column.name)
    expect(columns.includes('id'), `customers' description has no id column: ${columns}`)
  })

  await check('the graph edges are readable and joined to their nodes', async () => {
    const { data, error } = await service
      .schema('maludb').from('datamodel_edges')
      .select('relationship, source:datamodel_nodes!source_node_id(name), target:datamodel_nodes!target_node_id(name)')
    expect(!error, `could not read edges: ${error && error.message}`)
    expect(data.length > 0, 'the copy has no edges')
  })

  // A refusal has to be *the* refusal. Any error at all would also be produced
  // by a broken gateway, and a test that passes when everything is down is not
  // testing who may read the copy -- which is how this was first written.
  function expectPermissionDenied (result, who) {
    const { data, error } = result
    expect(error, `${who} read the copy: ${JSON.stringify(data)}`)
    expect(
      error.code === '42501' && /permission denied for schema maludb/.test(error.message),
      `${who} was refused, but not by PostgreSQL's grants: ${error.code} ${error.message}`
    )
  }

  await check('anon is refused the copy', async () => {
    expectPermissionDenied(
      await anon.schema('maludb').from('datamodel_relations').select('*'), 'anon')
  })

  await check('a signed-in user is refused the copy', async () => {
    expectPermissionDenied(
      await signedIn.schema('maludb').from('datamodel_relations').select('*'), 'an authenticated user')
  })

  await check('the public Data API still answers', async () => {
    const { error } = await service.from('customers').select('id').limit(1)
    expect(!error, `public broke after enabling: ${error && error.message}`)
  })
} else {
  console.error(`unknown MALUDB_PHASE ${phase}`)
  process.exit(2)
}

for (const row of results) console.log(JSON.stringify(row))
process.exit(results.every((row) => row.ok) ? 0 : 1)
