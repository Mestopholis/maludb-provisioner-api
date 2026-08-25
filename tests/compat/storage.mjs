// Storage, with the official client, through the MaluDB gateway.
//
// Phase 10 slice 5. `AGENTS.md` requires a compatibility claim to be shown with
// the official client, and this surface is a good argument for the rule: the
// bucket and object URLs, the multipart body `upload()` builds, the shape of a
// signed link and -- the part that mattered -- *which requests carry a token
// and which do not* are all upstream's. Two of those were wrong in the gateway
// and neither was visible from the Python side, because a hand-written client
// sends whatever the person writing it assumed.
//
// What it found, both now ADR-062:
//
//   1. `supabase-js` sends the publishable key as `Authorization: Bearer <key>`.
//      MaluDB keys are opaque (ADR-028), the gateway dropped it, and
//      `storage-api` refuses an empty bearer before consulting any policy. Every
//      anonymous call answered 403 -- a whole tier of the product.
//   2. `createSignedUrl` returns a link with a `token` and no `apikey`. The
//      gateway required one, so every signed URL the platform issued was 401.
//
// Reads its projects from the environment, prints one JSON object per line, and
// exits non-zero if any case failed. The Python side asserts on that output;
// nothing here decides what "supported" means.

import { createClient } from '@supabase/supabase-js'

const url = process.env.MALUDB_URL
const key = process.env.MALUDB_KEY
const secretKey = process.env.MALUDB_SECRET_KEY
const userToken = process.env.MALUDB_USER_TOKEN
const otherUrl = process.env.MALUDB_OTHER_URL
const otherSecretKey = process.env.MALUDB_OTHER_SECRET_KEY

if (!url || !key || !secretKey || !userToken || !otherUrl || !otherSecretKey) {
  console.error(
    'MALUDB_URL, MALUDB_KEY, MALUDB_SECRET_KEY, MALUDB_USER_TOKEN, MALUDB_OTHER_URL ' +
    'and MALUDB_OTHER_SECRET_KEY are required'
  )
  process.exit(2)
}

// The bucket names are shared with the Python harness, which creates the RLS
// policy on `storage.objects` before this runs -- a customer cannot author one
// (ADR-061), so the platform does it, which is the arrangement under test.
const BUCKET = 'compat'
const GATED = 'gated'
const PUBLIC_BUCKET = 'compat-public'
// The cross-project property is a bucket and a key of the *same* name in two
// projects. Isolation here is a property of the tenant prefix and the metadata
// rather than of the object store, so identical names are what would expose a
// prefix taken from the request instead of from the resolved tenant.
const SHARED = 'shared'

const options = { auth: { persistSession: false, autoRefreshToken: false } }

// Three callers against one project, which is the whole point: the same client
// library, the same URL, and three different roles arriving at the tenant.
const anon = createClient(url, key, options)
const service = createClient(url, secretKey, options)
const asUser = createClient(url, key, {
  ...options,
  global: { headers: { Authorization: `Bearer ${userToken}` } },
})

// The second project, reached with its own key. Used to show that neither a key
// nor a signed link crosses between them.
const other = createClient(otherUrl, otherSecretKey, options)

const results = []

async function check (name, fn) {
  try {
    await fn()
    results.push({ name, ok: true })
  } catch (error) {
    results.push({ name, ok: false, error: String((error && error.message) || error) })
  }
}

function expect (condition, message) {
  if (!condition) throw new Error(message)
}

function assertNoError (result, where) {
  if (result.error) {
    throw new Error(`${where}: ${result.error.message} (${result.error.statusCode || 'no status'})`)
  }
}

// Every negative case goes through this rather than through `expect(error !==
// null)`, and the difference is not pedantry: `error !== null` is satisfied by a
// connection refused, a DNS failure or a worker that never started. Pointed at
// a dead port, five of the isolation claims below reported success -- which is
// the worst possible failure mode for an isolation test, since the run that
// proves nothing looks exactly like the run that proves everything.
//
// A server-side refusal carries a status. A transport failure does not, so it
// is failed here explicitly and says which it was.
function assertRefused (result, where) {
  const error = result.error
  if (!error) throw new Error(`${where}: served, when it should have been refused`)
  const status = error.statusCode ?? error.status
  if (status === undefined || status === null || status === '') {
    throw new Error(`${where}: refused by the transport, not by the server (${error.message})`)
  }
  return Number(status)
}

// The same rule for the two links that are fetched directly. `fetch` throws on a
// transport failure rather than returning a response, so reaching this at all
// means a server answered -- but it is written out so the next negative case
// added here copies the right thing.
function assertNotServed (response, where) {
  expect(!response.ok, `${where}: served with ${response.status}`)
}

const bytes = (text) => new Blob([text], { type: 'text/plain' })

// -- buckets --------------------------------------------------------------

await check('storage create bucket', async () => {
  for (const [name, isPublic] of [[BUCKET, false], [GATED, false], [PUBLIC_BUCKET, true]]) {
    const created = await service.storage.createBucket(name, { public: isPublic })
    assertNoError(created, `createBucket ${name}`)
  }
  const shared = await service.storage.createBucket(SHARED, { public: false })
  assertNoError(shared, `createBucket ${SHARED}`)
})

await check('storage list buckets', async () => {
  const listed = await service.storage.listBuckets()
  assertNoError(listed, 'listBuckets')
  const names = listed.data.map((bucket) => bucket.name)
  for (const expected of [BUCKET, GATED, PUBLIC_BUCKET, SHARED]) {
    expect(names.includes(expected), `listBuckets did not return ${expected}: ${names}`)
  }
})

await check('storage get bucket', async () => {
  const got = await service.storage.getBucket(PUBLIC_BUCKET)
  assertNoError(got, 'getBucket')
  expect(got.data.public === true, 'a bucket created public did not read back public')
})

// -- objects --------------------------------------------------------------

await check('storage upload', async () => {
  const uploaded = await service.storage.from(BUCKET).upload('notes/hello.txt', bytes('hello'))
  assertNoError(uploaded, 'upload')
  expect(uploaded.data.path === 'notes/hello.txt', `upload returned ${uploaded.data.path}`)
})

await check('storage upload rejects a duplicate without upsert', async () => {
  // Upstream's behaviour, and one a migrating application depends on: the
  // second write is a 409 rather than a silent overwrite.
  const again = await service.storage.from(BUCKET).upload('notes/hello.txt', bytes('again'))
  const status = assertRefused(again, 'a duplicate upload')
  expect(status === 409, `a duplicate upload answered ${status}, expected 409`)
})

await check('storage upsert', async () => {
  const upserted = await service.storage
    .from(BUCKET)
    .upload('notes/hello.txt', bytes('hello again'), { upsert: true })
  assertNoError(upserted, 'upsert')
})

await check('storage download', async () => {
  const downloaded = await service.storage.from(BUCKET).download('notes/hello.txt')
  assertNoError(downloaded, 'download')
  const text = await downloaded.data.text()
  expect(text === 'hello again', `download returned ${JSON.stringify(text)}`)
})

await check('storage list objects', async () => {
  const listed = await service.storage.from(BUCKET).list('notes')
  assertNoError(listed, 'list')
  const names = listed.data.map((entry) => entry.name)
  expect(names.includes('hello.txt'), `list did not return hello.txt: ${names}`)
})

await check('storage remove', async () => {
  await service.storage.from(BUCKET).upload('notes/doomed.txt', bytes('temporary'))
  const removed = await service.storage.from(BUCKET).remove(['notes/doomed.txt'])
  assertNoError(removed, 'remove')

  const after = await service.storage.from(BUCKET).download('notes/doomed.txt')
  assertRefused(after, 'downloading a removed object')
})

// -- the two URLs that carry no API key (ADR-062) --------------------------

await check('storage public url', async () => {
  await service.storage.from(PUBLIC_BUCKET).upload('logo.txt', bytes('public bytes'), {
    upsert: true,
  })
  const { data } = service.storage.from(PUBLIC_BUCKET).getPublicUrl('logo.txt')

  // Plain `fetch`, deliberately: a browser following this link sends an
  // `apikey` header for nobody, and going through the client here would hide
  // exactly the thing being claimed.
  const response = await fetch(data.publicUrl)
  expect(response.ok, `a public URL answered ${response.status}`)
  expect((await response.text()) === 'public bytes', 'a public URL served the wrong bytes')
})

await check('storage signed url', async () => {
  await service.storage.from(BUCKET).upload('notes/private.txt', bytes('signed bytes'), {
    upsert: true,
  })
  const signed = await service.storage.from(BUCKET).createSignedUrl('notes/private.txt', 60)
  assertNoError(signed, 'createSignedUrl')
  expect(signed.data.signedUrl.includes('token='), 'a signed URL carried no token')

  // Again with `fetch` and no key. This is the case the gateway used to refuse:
  // the object is in a **private** bucket and the link is the only permission.
  const response = await fetch(signed.data.signedUrl)
  expect(response.ok, `a signed URL answered ${response.status}`)
  expect((await response.text()) === 'signed bytes', 'a signed URL served the wrong bytes')
})

await check('storage a signed url expires', async () => {
  const signed = await service.storage.from(BUCKET).createSignedUrl('notes/private.txt', 1)
  assertNoError(signed, 'createSignedUrl for expiry')
  await new Promise((resolve) => setTimeout(resolve, 2500))
  const response = await fetch(signed.data.signedUrl)
  assertNotServed(response, 'an expired signed URL')
})

await check('storage a signed url is refused by another project', async () => {
  const signed = await service.storage.from(BUCKET).createSignedUrl('notes/private.txt', 60)
  assertNoError(signed, 'createSignedUrl for the cross-project check')

  // The same link, pointed at the other project's hostname. The token is signed
  // with this project's secret and the hostname is what names the tenant, so it
  // must not resolve there -- a signed URL that travelled between projects
  // would make every private object on the platform reachable from any one of
  // them.
  const moved = signed.data.signedUrl.replace(url, otherUrl)
  expect(moved !== signed.data.signedUrl, 'the signed URL was not repointed; the test is vacuous')
  assertNotServed(await fetch(moved), 'a signed URL for one project, at another')
})

// -- RLS on storage.objects, which is the authorization mechanism ---------

await check('storage rls hides an object from an anonymous caller', async () => {
  await service.storage.from(GATED).upload('secret.txt', bytes('gated bytes'), { upsert: true })

  // The publishable key reaches the tenant as `anon` (ADR-062), and no policy
  // admits `anon` to this bucket. Before ADR-062 this call answered 403 for
  // want of a token, which is the same colour of failure for a different
  // reason -- and would have made this assertion pass while proving nothing
  // about RLS at all.
  const denied = await anon.storage.from(GATED).download('secret.txt')
  assertRefused(denied, 'an anonymous download from a private bucket')
})

await check('storage rls admits a signed-in user', async () => {
  // Same object, same URL, same client library. What differs is the role in the
  // token, and a policy on `storage.objects` is what decides -- so this is the
  // half that shows the mechanism *works* rather than only that it refuses.
  const allowed = await asUser.storage.from(GATED).download('secret.txt')
  assertNoError(allowed, 'authenticated download from the gated bucket')
  expect((await allowed.data.text()) === 'gated bytes', 'the gated object served wrong bytes')
})

await check('storage rls does not admit a signed-in user elsewhere', async () => {
  // The policy names one bucket. A policy that admitted `authenticated` to
  // everything would pass the case above and be a project with no isolation
  // between its own buckets, so the negative half is asserted too.
  const denied = await asUser.storage.from(BUCKET).download('notes/private.txt')
  assertRefused(denied, 'a signed-in read of a bucket the policy does not name')
})

await check('storage rls hides objects from an anonymous list', async () => {
  // `list()` is the other way an object's existence leaks: a caller that cannot
  // download it should not be told it is there. Either answer is correct --
  // upstream may refuse the call or return nothing -- but a third outcome,
  // neither data nor a server refusal, is a run that proved nothing and is
  // failed rather than counted as a hidden object.
  const listed = await anon.storage.from(GATED).list()
  if (listed.error) {
    assertRefused(listed, 'an anonymous list')
    return
  }
  expect(Array.isArray(listed.data), 'list returned neither data nor an error')
  const names = listed.data.map((entry) => entry.name)
  expect(!names.includes('secret.txt'), `an anonymous list revealed ${names}`)
})

// -- one project cannot reach another's objects ---------------------------

await check("storage a project cannot reach another project's objects", async () => {
  const mine = await service.storage.from(SHARED).upload('secret.txt', bytes('first project'), {
    upsert: true,
  })
  assertNoError(mine, 'upload to the shared-name bucket')

  const theirs = await other.storage.createBucket(SHARED, { public: false })
  if (theirs.error && !String(theirs.error.message).toLowerCase().includes('exist')) {
    throw new Error(`the other project could not create its bucket: ${theirs.error.message}`)
  }
  const theirUpload = await other.storage
    .from(SHARED)
    .upload('secret.txt', bytes('second project'), { upsert: true })
  assertNoError(theirUpload, 'upload to the other project')

  // Same bucket name, same key, two projects, each holding its own service key.
  // If the object prefix were ever taken from the request rather than from the
  // tenant the gateway resolved, one of these reads returns the other's bytes.
  const first = await service.storage.from(SHARED).download('secret.txt')
  assertNoError(first, 'download from the first project')
  const second = await other.storage.from(SHARED).download('secret.txt')
  assertNoError(second, 'download from the second project')

  expect((await first.data.text()) === 'first project', 'the first project read the wrong bytes')
  expect((await second.data.text()) === 'second project', 'the second project read the wrong bytes')
})

await check("storage a key for another project reaches nothing", async () => {
  // ADR-008 through the official client: the hostname is the routing key and
  // the key is validated against the project it names, so a client built from
  // one project's key and another's URL is refused rather than served.
  const crossed = createClient(otherUrl, secretKey, options)
  const denied = await crossed.storage.from(SHARED).download('secret.txt')
  const status = assertRefused(denied, "one project's key against another's URL")
  expect(status === 401, `a mismatched key answered ${status}, expected 401`)
})

for (const row of results) console.log(JSON.stringify(row))
process.exit(results.every((row) => row.ok) ? 0 : 1)
