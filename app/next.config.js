/** @type {import('next').NextConfig} */
const nextConfig = {
  // These can be overridden in Vercel Environment Variables
  env: {
    HETZNER_URL: process.env.HETZNER_URL || 'http://138.199.196.95:8080',
    DASHBOARD_PASSWORD: process.env.DASHBOARD_PASSWORD || 'AlgoTrader2024!',
  }
}
module.exports = nextConfig
