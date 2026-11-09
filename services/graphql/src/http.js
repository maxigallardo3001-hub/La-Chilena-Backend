const fs = require('fs');
const https = require('https');

const ca = process.env.CA_FILE ? fs.readFileSync(process.env.CA_FILE) : undefined;

class UpstreamError extends Error {
  constructor(kind, status) {
    super(kind);
    this.kind = kind;
    this.status = status;
  }
}

function createClient({ baseUrl, credential, timeoutMs = 2000 }) {
  return {
    get(path, { identity, correlationId } = {}) {
      return new Promise((resolve, reject) => {
        const headers = { 'X-Service-Credential': credential, 'X-Correlation-Id': correlationId || '-' };
        if (identity) {
          headers['X-Authenticated-User'] = identity.userId;
          headers['X-Authenticated-Roles'] = identity.roles.join(',');
        }
        const request = https.request(new URL(path, baseUrl), { method: 'GET', headers, ca, timeout: timeoutMs }, (response) => {
          const chunks = [];
          response.on('data', (chunk) => chunks.push(chunk));
          response.on('end', () => {
            let body = null;
            try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch (_) {}
            resolve({ status: response.statusCode, body });
          });
        });
        request.on('timeout', () => request.destroy(new UpstreamError('timeout')));
        request.on('error', (error) => reject(error instanceof UpstreamError ? error : new UpstreamError('unavailable')));
        request.end();
      });
    },
  };
}

module.exports = { createClient, UpstreamError };
