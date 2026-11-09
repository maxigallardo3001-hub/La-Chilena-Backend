const fs = require('fs');
const https = require('https');

const request = https.get(`https://localhost:${process.env.PORT || 8700}/health`,
  { ca: fs.readFileSync(process.env.CA_FILE), timeout: 3000 },
  (response) => process.exit(response.statusCode === 200 ? 0 : 1));
request.on('error', () => process.exit(1));
request.on('timeout', () => process.exit(1));
