const fs = require('fs');
const https = require('https');

function fetchSecrets(addr, token) {
  const ca = process.env.CA_FILE ? fs.readFileSync(process.env.CA_FILE) : undefined;
  return new Promise((resolve, reject) => {
    const request = https.get(new URL('/v1/secret/data/graphql', addr), {
      headers: { 'X-Vault-Token': token }, ca, timeout: 5000,
    }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => {
        if (response.statusCode !== 200) return reject(new Error(`Vault respondió ${response.statusCode}`));
        try {
          return resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')).data.data);
        } catch (error) {
          return reject(error);
        }
      });
    });
    request.on('timeout', () => request.destroy(new Error('Vault no respondió a tiempo')));
    request.on('error', reject);
  });
}

async function loadSecrets({ retries = 30, delayMs = 2000 } = {}) {
  const addr = process.env.VAULT_ADDR || 'https://vault:8201';
  let lastError;
  for (let attempt = 0; attempt < retries; attempt += 1) {
    try {
      const token = fs.readFileSync(process.env.VAULT_TOKEN_FILE, 'utf8').trim();
      return await fetchSecrets(addr, token);
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
  throw new Error(`No se pudieron cargar los secretos de Vault: ${lastError && lastError.message}`);
}

module.exports = { loadSecrets };
