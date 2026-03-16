/** @type {import('next').NextConfig} */
const nextConfig = {
  env: {
    HETZNER_URL: process.env.HETZNER_URL || 'http://138.199.196.95:8080',
  }
}
module.exports = nextConfig
