import { cookies } from 'next/headers'

const PASSWORD = process.env.DASHBOARD_PASSWORD || 'AlgoTrader2024!'

export async function POST(request) {
  const { password } = await request.json()
  if (password === PASSWORD) {
    const cookieStore = cookies()
    cookieStore.set('algo_auth', 'true', {
      httpOnly: true,
      secure: true,
      maxAge: 60 * 60 * 24 * 7 // 7 days
    })
    return Response.json({ ok: true })
  }
  return Response.json({ ok: false }, { status: 401 })
}
