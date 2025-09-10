# 🛡️ CSP Reporting Server

A production-ready, dockerized solution for receiving, processing, and storing Content Security Policy (CSP) violation reports.

## 🏗️ Architecture

```
CSP Reports → Caddy (Auto-SSL/TLS) → Flask App → RabbitMQ Cluster → Worker → Elasticsearch
```

### Components:
- **Caddy**: Reverse proxy with rate limiting and automatic SSL termination via Let's Encrypt.
- **Flask App**: CSP report receiver and validator
- **RabbitMQ Cluster**: Message queue (3-node cluster for HA)
- **Worker**: Processes and forwards reports to storage
- **Elasticsearch**: Storage and indexing for CSP violation data
- **Kibana**: Data visualization dashboard

## 🚀 Quick Start

### 1. Setup Environment
```bash
# Copy and configure environment variables
cp example.env .env

# Edit .env, making sure to set your domain name
# DOMAIN_NAME=csp-reports.yourcompany.com
```

### 2. Start Services
```bash
# Start all services
docker-compose up -d

# Check status
docker-compose ps
```

Caddy will automatically provision a production-ready SSL certificate from Let's Encrypt on startup. There are no manual steps required.

### 3. Test CSP Endpoint
```bash
# Send test report (use -k if testing with a self-signed cert for localhost)
curl -k -X POST https://${DOMAIN_NAME:-localhost}/csp-report \
  -H "Content-Type: application/csp-report" \
  -d @test_payloads/csp_report_payload.json
```


## 🏢 External Elasticsearch Integration

To send CSP reports to an external Elasticsearch cluster instead of the local one:

```bash
# Update .env file
ELASTICSEARCH_HOSTS=https://your-external-es-cluster.com:9200

# If authentication is required:
ELASTICSEARCH_USERNAME=your_username
ELASTICSEARCH_PASSWORD=your_password
# or
ELASTICSEARCH_API_KEY=your_api_key

# For SSL/TLS with custom certificates:
ELASTICSEARCH_CA_CERTS=/path/to/ca-certificates.pem
```


## 📊 Monitoring & Analytics

### Access Points:
- **CSP Endpoint**: `https://your-domain/csp-report`
- **Health Check**: `https://your-domain/health`
- **Kibana Dashboard**: `http://localhost:5601`
- **RabbitMQ Management**: `http://localhost:15672`
- **Elasticsearch API**: `http://localhost:9200`

### Sample Queries:
```bash
# View all violations
curl "http://localhost:9200/csp-violations-*/_search?pretty"

# Filter by environment
curl "http://localhost:9200/csp-violations-*/_search" -d '{
  "query": {
    "wildcard": {
      "document-uri": "*staging.company.com*"
    }
  }
}'
```

## 📁 Project Structure

```
csp-reporting-server/
├── caddy/
│   └── Caddyfile               # Caddy configuration
├── docker-compose.yml          # Main orchestration
├── example.env                 # Configuration template
├── .gitignore                  # Git ignore rules
├── csp-receiver-app/           # Flask CSP receiver
├── csp-worker-app/             # Message processor
├── rabbitmq/                   # RabbitMQ configuration
├── test_payloads/              # Sample CSP reports
└── elastic_template.json       # Elasticsearch mapping
```

## 🔧 Configuration Files

### `example.env`
Complete environment variable template with:
- RabbitMQ cluster settings
- Elasticsearch configuration
- SSL/TLS settings
- Performance tuning parameters

### `elastic_template.json`
Elasticsearch index template for optimal CSP data storage and search performance.

## 🚨 Security Features

- **Automatic HTTPS**: Caddy automatically provisions and renews SSL/TLS certificates from Let's Encrypt.
- **Rate Limiting**: 5 req/sec per IP, burst up to 10, handled by Caddy.
- **SSL/TLS Encryption**: Modern cipher suites and protocols (TLS 1.2+) managed by Caddy.
- **Security Headers**: HSTS, XSS protection, content sniffing prevention.
- **Input Validation**: CSP report format validation.
- **Request Size Limits**: Enforced by the reverse proxy.

## Securing the Report Endpoint (Recommended)

To prevent unauthorized parties from submitting fake or malicious CSP reports to your endpoint, it is highly recommended to secure it using a proxy-based, shared-secret authentication method.

The flow is as follows:
1.  The browser sends the CSP report to a proxy endpoint on your main application's server (e.g., `/csp-report-proxy`).
2.  Your main application's web server receives the report, adds a secret HTTP header (`X-Report-Token`), and forwards it to the actual CSP reporting server.
3.  The CSP receiver application validates this token before accepting the report.

This method ensures the secret token is never exposed to the public.

### Step 1: Set the Secret Token

In your `.env` file, set a strong, random value for the `REPORTING_SECRET_TOKEN` variable. If this value is not set, authentication is disabled.

```bash
# in .env
REPORTING_SECRET_TOKEN=YOUR_RANDOMLY_GENERATED_SECRET_TOKEN
```

### Step 2: Update Your CSP Header

In your web application, change the `report-uri` directive to point to a local path on your domain.

**From:**
`report-uri https://csp-reports.your-domain.com/csp-report;`

**To:**
`report-uri /csp-report-proxy;`

### Step 3: Configure Your Web Server Proxy

Add the following configuration to your main application's web server.

#### Caddy Example
```caddy
# On your main application server (e.g., www.your-app-domain.com)

@csp_proxy path /csp-report-proxy
reverse_proxy @csp_proxy https://csp-reports.your-domain.com/csp-report {
    # Add the secret token header to match the one in your .env file
    header_up X-Report-Token "YOUR_RANDOMLY_GENERATED_SECRET_TOKEN"
    # Pass along the original client's IP address
    header_up X-Forwarded-For {remote_ip}
}
```

#### Nginx Example
```nginx
# On your main application server (e.g., www.your-app-domain.com)

location /csp-report-proxy {
    # Add the secret token header to match the one in your .env file
    proxy_set_header X-Report-Token "YOUR_RANDOMLY_GENERATED_SECRET_TOKEN";

    # Forward the request to the CSP reporting server
    proxy_pass https://csp-reports.your-domain.com/csp-report;

    # Pass along the original client's IP address
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

#### Apache Example
*(Requires `mod_proxy` and `mod_headers`)*
```apache
# On your main application server (e.g., www.your-app-domain.com)

<Location /csp-report-proxy>
    # Add the secret token header to match the one in your .env file
    RequestHeader set X-Report-Token "YOUR_RANDOMLY_GENERATED_SECRET_TOKEN"

    # Forward the request to the CSP reporting server
    ProxyPass "https://csp-reports.your-domain.com/csp-report"
    ProxyPassReverse "https://csp-reports.your-domain.com/csp-report"
</Location>
```

## 🎯 Use Cases

### Multi-Environment Support
All environments can send to the same endpoint - filter by `document-uri` in Elasticsearch:
```javascript
// Production CSP policy
<meta http-equiv="Content-Security-Policy" 
      content="default-src 'self'; report-uri https://csp-reports.company.com/csp-report">

// Staging CSP policy  
<meta http-equiv="Content-Security-Policy" 
      content="default-src 'self'; report-uri https://csp-reports.company.com/csp-report">
```

### Data Flow
1. **Browser** sends CSP violation to `/csp-report`
2. **Caddy** applies rate limiting, provides SSL, and forwards to Flask
3. **Flask app** validates and queues report in RabbitMQ
4. **Worker** processes queue and sends to Elasticsearch
5. **Data** available for analysis in Kibana

## 🤝 Contributing

1. Copy `example.env` to `.env`
2. Modify settings as needed
3. Test locally with `docker-compose up -d`
4. Submit PRs for improvements

## 📜 License

MIT License

Copyright (c) 2025 bifr0est

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.