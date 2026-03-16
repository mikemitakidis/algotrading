// Vercel API proxy → Hetzner server
// All dashboard calls go through here so credentials stay server-side

const HETZNER = process.env.HETZNER_URL || 'http://138.199.196.95:8080'
const PASSWORD = process.env.DASHBOARD_PASSWORD || 'AlgoTrader2024!'

export async function GET(request, { params }) {
  const path = params.path.join('/')
  const { searchParams } = new URL(request.url)
  const query = searchParams.toString()
  const url = `${HETZNER}/api/${path}${query ? '?' + query : ''}`

  try {
    const res = await fetch(url, {
      headers: { 'Cookie': `session=authenticated`, 'X-Internal-Auth': PASSWORD },
      cache: 'no-store'
    })
    const data = await res.json()
    return Response.json(data)
  } catch (e) {
    return Response.json({ error: 'Cannot reach trading server', detail: e.message }, { status: 503 })
  }
}

export async function POST(request, { params }) {
  const path = params.path.join('/')
  const url = `${HETZNER}/api/${path}`
  let body = null
  try { body = await request.json() } catch {}

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Cookie': `session=authenticated`,
        'X-Internal-Auth': PASSWORD
      },
      body: body ? JSON.stringify(body) : undefined,
      cache: 'no-store'
    })
    const data = await res.json()
    return Response.json(data)
  } catch (e) {
    return Response.json({ error: 'Cannot reach trading server', detail: e.message }, { status: 503 })
  }
}
