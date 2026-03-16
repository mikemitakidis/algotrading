export const config = { maxDuration: 60 };

export default async function handler(req, res) {
  const HETZNER = 'http://138.199.196.95:8080';
  const PASSWORD = 'AlgoTrader2024!';
  
  const log = [];
  
  try {
    // Step 1: Login
    log.push('Logging into Hetzner dashboard...');
    const loginRes = await fetch(`${HETZNER}/api/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: PASSWORD })
    });
    const loginData = await loginRes.json();
    log.push(`Login: ${JSON.stringify(loginData)}`);
    
    // Get session cookie
    const setCookie = loginRes.headers.get('set-cookie') || '';
    const cookie = setCookie.split(';')[0];
    log.push(`Cookie: ${cookie ? 'obtained' : 'missing'}`);

    // Step 2: Get current logs to see what's running
    const logsRes = await fetch(`${HETZNER}/api/logs?lines=5`, {
      headers: { 'Cookie': cookie }
    });
    const logsData = await logsRes.json();
    log.push(`Current logs: ${JSON.stringify(logsData.lines?.slice(-3))}`);

    // Step 3: Push new settings that trigger restart with correct config
    const newSettings = `# Algo Trader v2 Configuration
alpaca:
  api_key: "PKFTA4Q3ZM7YVWTMNT55MV6V5S"
  secret_key: "DbDfgYZV21JkkjeggWr3hTXKyrzxCW69V5Awt6SX5QqC"
  base_url: "https://paper-api.alpaca.markets/v2"
  feed: "iex"
bot:
  mode: "shadow"
  cycle_minutes: 15
  focus_size: 150
  rank_interval_hours: 6
  min_valid_timeframes: 3
signal_routing:
  etoro_min_tfs: 4
  ibkr_min_tfs: 3
dashboard:
  port: 8080
  password: "AlgoTrader2024!"
telegram:
  enabled: false
  token: ""
  chat_id: ""`;

    const saveRes = await fetch(`${HETZNER}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Cookie': cookie },
      body: JSON.stringify({ content: newSettings })
    });
    const saveData = await saveRes.json();
    log.push(`Settings saved: ${JSON.stringify(saveData)}`);

    // Step 4: Force restart
    const restartRes = await fetch(`${HETZNER}/api/restart`, {
      method: 'POST',
      headers: { 'Cookie': cookie }
    });
    const restartData = await restartRes.json();
    log.push(`Restart triggered: ${JSON.stringify(restartData)}`);

    res.json({ success: true, log });
  } catch (e) {
    log.push(`ERROR: ${e.message}`);
    res.json({ success: false, log });
  }
}
